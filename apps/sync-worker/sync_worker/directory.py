"""Who is behind a SharePoint site group or a Microsoft 365 group's owners, as ACL principals.

Graph lists a file's site groups (Owners/Members/Visitors) but not their members, so those are read from
SharePoint's own REST API (`/_api/web/sitegroups/getbyid(<id>)/users`, which needs the certificate and the
SharePoint Sites.Selected permission). Members are Entra users, Entra or Microsoft 365 groups, the owners of a
Microsoft 365 group, or the everyone claims; site groups can't contain other site groups.

Answers are cached for CACHE_SECONDS, so a crawl asks once per group rather than once per file. Membership
changes don't touch the files, so delta never reports them: the daily ACL sweep re-reads every file's
permissions, which picks them up (the same staleness bound as any other permission-only change).
"""
import itertools
import logging
import time

from sharepoint_rag import acl

from . import permissions
from .graph import AppGraph, GraphError, seg
from .permissions import Lookups

log = logging.getLogger(__name__)

CACHE_SECONDS = 600
_MAX_ENTRIES = 50_000
# What SharePoint or Graph answer for a group or user the app can't see, or that no longer exists.
_UNREADABLE = (401, 403, 404)

Principals = tuple[str, ...] | None  # None: couldn't be resolved, which makes the file unresolved


class Directory:
    def __init__(self, graph: AppGraph) -> None:
        self._graph = graph
        self._cache: dict[tuple[str, ...], tuple[float, Principals]] = {}

    async def lookups(self, site_url: str, perms: list[dict]) -> Lookups:
        """Everything `permissions.to_acl` needs to resolve these permissions of a file in `site_url`."""
        site_groups, owners, upns = permissions.needs(perms)
        return Lookups(
            site_groups={g: await self.site_group(site_url, g) for g in site_groups},
            owners={g: await self.group_owners(g) for g in owners},
            users={u.lower(): await self.user(u) for u in upns},
        )

    async def site_group(self, site_url: str, group_id: str) -> Principals:
        return await self._cached(("site_group", site_url, group_id), lambda: self._site_group(site_url, group_id))

    async def group_owners(self, group_id: str) -> Principals:
        return await self._cached(("owners", group_id), lambda: self._group_owners(group_id))

    async def _cached(self, key: tuple[str, ...], fetch) -> Principals:
        hit = self._cache.get(key)
        if hit and hit[0] > time.monotonic():
            return hit[1]
        value = await fetch()  # transient errors raise and aren't cached: the job is retried
        self._cache.pop(key, None)  # re-inserted below, so insertion order stays oldest-first
        if len(self._cache) >= _MAX_ENTRIES:
            now = time.monotonic()
            self._cache = {k: v for k, v in self._cache.items() if v[0] > now}
            # Still full of fresh entries: drop the oldest down to 90%, so this runs once per many inserts.
            for k in list(itertools.islice(self._cache, max(0, len(self._cache) - _MAX_ENTRIES * 9 // 10))):
                del self._cache[k]
        self._cache[key] = (time.monotonic() + CACHE_SECONDS, value)
        return value

    async def _site_group(self, site_url: str, group_id: str) -> Principals:
        if not group_id.isdigit():
            return None
        url = f"{site_url.rstrip('/')}/_api/web/sitegroups/getbyid({group_id})/users?$select=LoginName"
        try:
            members = await self._graph.sharepoint_get_all(url)
        except GraphError as e:
            if e.status not in _UNREADABLE:
                raise
            # Usually the SharePoint Sites.Selected permission or the site grant is missing, or the group only
            # lets its own members see who is in it (then the site grant needs `fullcontrol`).
            log.warning("site group %s of %s: members unreadable (SharePoint %d)", group_id, site_url, e.status)
            return None
        principals: set[str] = set()
        for member in members:
            login = member.get("LoginName") or ""
            if group := permissions.owners_group(login):
                resolved = await self.group_owners(group)
            elif upn := permissions.user_upn(login):
                resolved = await self.user(upn)
            else:
                p = permissions.claim_principal(login)
                resolved = None if p is None else ((p,) if p else ())
            if resolved is None:
                log.info("site group %s of %s: member %r can't be resolved", group_id, site_url, login)
                return None
            principals.update(resolved)
        return tuple(sorted(principals))

    async def _group_owners(self, group_id: str) -> Principals:
        # Owners are users or service principals; only users can sign in and search.
        try:
            owners = await self._graph.get_all(f"/groups/{seg(group_id)}/owners/microsoft.graph.user?$select=id")
        except GraphError as e:
            if e.status not in _UNREADABLE:
                raise
            log.warning("owners of group %s unreadable (Graph %d): GroupMember.Read.All missing?", group_id, e.status)
            return None
        return tuple(sorted(acl.user(o["id"]) for o in owners if o.get("id")))

    async def user(self, upn: str) -> Principals:
        return await self._cached(("user", upn.lower()), lambda: self._fetch_user(upn))

    async def _fetch_user(self, upn: str) -> Principals:
        try:
            user = await self._graph.get(f"/users/{seg(upn)}", params={"$select": "id"})
        except GraphError as e:
            if e.status not in _UNREADABLE:
                raise
            log.warning("user %s unreadable (Graph %d): User.ReadBasic.All missing?", upn, e.status)
            return None
        return (acl.user(user["id"]),)
