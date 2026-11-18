"""Turns a driveItem's Graph permissions into ACL principals (see sharepoint_rag.acl).

Anything we can't map to an Entra user, an Entra group or a known marker makes the whole file `unresolved`
(shown to nobody by default): a partial ACL could hide a file from people who may read it, but more
importantly an unknown grant type must never be guessed at. SharePoint site groups (Owners/Members/Visitors)
and Microsoft 365 group owners aren't expanded by Graph: `sync_worker.directory` reads them and passes them in
as `Lookups`; one that couldn't be read leaves the file unresolved.
"""
from collections.abc import Iterator, Mapping
from dataclasses import dataclass, field
from datetime import datetime

from sharepoint_rag import acl
from sharepoint_rag.acl import AclRead

# SharePoint claims (login names), matched on siteUser and site group members only: a site group's own
# loginName is its title, which anyone with Manage Permissions can choose.
_MEMBERS_CLAIM_PREFIX = "c:0-.f|rolemanager|spo-grid-all-users/"  # "Everyone except external users"
_EVERYONE_CLAIMS = {"c:0(.s|true", "c:0-.f|rolemanager|everyone"}  # "Everyone" (guests included)
_M365_GROUP_CLAIM = "c:0o.c|federateddirectoryclaimprovider|"  # a Microsoft 365 group; "<id>_o": its owners
_TENANT_GROUP_CLAIM = "c:0t.c|tenant|"  # an Entra security group
_USER_CLAIM = "i:0#.f|membership|"  # an Entra user, by UPN
_SYSTEM_LOGINS = {"sharepoint\\system"}  # SharePoint's own account: no person
_PEOPLE_FACETS = ("user", "group", "siteUser", "siteGroup")


@dataclass(frozen=True)
class Lookups:
    """Principals behind site groups (by SharePoint group id), Microsoft 365 group owners (by group id) and
    users known only by UPN (lowercased); None for one that couldn't be read."""

    site_groups: Mapping[str, tuple[str, ...] | None] = field(default_factory=dict)
    owners: Mapping[str, tuple[str, ...] | None] = field(default_factory=dict)
    users: Mapping[str, tuple[str, ...] | None] = field(default_factory=dict)


def owners_group(login: str) -> str | None:
    """The Microsoft 365 group id if `login` stands for that group's owners."""
    login = login.lower()
    if login.startswith(_M365_GROUP_CLAIM) and login.endswith("_o"):
        return login[len(_M365_GROUP_CLAIM):-2] or None
    return None


def user_upn(login: str) -> str | None:
    if login.lower().startswith(_USER_CLAIM):
        return login[len(_USER_CLAIM):] or None
    return None


def claim_principal(login: str) -> str | None:
    """Principal for a login name that needs no lookup; "" for one that grants no person access; None if it
    isn't one of these (owners and users by UPN are resolved by `sync_worker.directory`)."""
    login = login.lower()
    if login.startswith(_MEMBERS_CLAIM_PREFIX):
        return acl.ORG_MEMBERS
    if login in _EVERYONE_CLAIMS:
        return acl.ORG_EVERYONE
    if login in _SYSTEM_LOGINS:
        return ""
    for prefix in (_M365_GROUP_CLAIM, _TENANT_GROUP_CLAIM):
        if login.startswith(prefix) and not login.endswith("_o") and (gid := login[len(prefix):]):
            return acl.group(gid)
    return None


def _granted(perm: dict) -> Iterator[dict]:
    """The identities a permission grants to (link permissions: whoever redeemed the link)."""
    if perm.get("link"):
        yield from perm.get("grantedToIdentitiesV2") or perm.get("grantedToIdentities") or []
    elif granted := perm.get("grantedToV2") or perm.get("grantedTo"):
        yield granted
    else:
        yield from perm.get("grantedToIdentitiesV2") or perm.get("grantedToIdentities") or []


def _login(identity: dict) -> str:
    return (identity.get("siteUser") or {}).get("loginName") or ""


def needs(permissions: list[dict]) -> tuple[set[str], set[str], set[str]]:
    """Site group ids, Microsoft 365 group ids (owners) and UPNs these permissions need looked up."""
    site_groups, owners, upns = set(), set(), set()
    for perm in permissions:
        if not perm.get("roles"):
            continue
        for identity in _granted(perm):
            if gid := (identity.get("siteGroup") or {}).get("id"):
                site_groups.add(str(gid))
            elif gid := owners_group(_login(identity)):
                owners.add(gid)
            elif not (identity.get("user") or {}).get("id") and (upn := user_upn(_login(identity))):
                upns.add(upn)
    return site_groups, owners, upns


def _identity(identity: dict, lookups: Lookups) -> tuple[str, ...] | None:
    """Principals for one identitySet, or None if it can't be resolved. Apps/devices grant no person access
    and map to ()."""
    if (site_group := identity.get("siteGroup") or {}).get("id"):
        return lookups.site_groups.get(str(site_group["id"]))
    login = _login(identity)
    # Before `group`: Graph labels a Microsoft 365 group's owners with the group's own id.
    if gid := owners_group(login):
        return lookups.owners.get(gid)
    group, user = identity.get("group") or {}, identity.get("user") or {}
    if group.get("id"):
        return (acl.group(group["id"]),)
    if user.get("id"):
        return (acl.user(user["id"]),)
    if upn := user_upn(login):  # a SharePoint user Graph gave no Entra id for (some guests, older accounts)
        return lookups.users.get(upn.lower())
    if (p := claim_principal(login)) is not None:
        return (p,) if p else ()
    if not any(identity.get(f) for f in _PEOPLE_FACETS) and (identity.get("application") or identity.get("device")):
        return ()
    return None


def to_acl(permissions: list[dict], read_at: datetime, lookups: Lookups = Lookups()) -> AclRead:
    principals: set[str] = set()
    resolved = True

    def add(identity: dict | None) -> None:
        nonlocal resolved
        p = _identity(identity or {}, lookups)
        if p is None:
            resolved = False
        else:
            principals.update(p)

    for perm in permissions:
        if not perm.get("roles"):
            continue  # no access granted (e.g. a pending invitation)
        link = perm.get("link")
        if link:
            scope = link.get("scope")
            if scope == "anonymous":
                principals.add(acl.ORG_ANONYMOUS)
            elif scope == "organization":
                principals.add(acl.ORG_MEMBERS)
            elif scope not in ("users", "existingAccess"):
                resolved = False
            # Specific-people links list who redeemed them; organization links may too (harmless duplicates).
            for identity in _granted(perm):
                add(identity)
            continue
        identities = list(_granted(perm))
        if not identities:
            resolved = False  # a grant we don't understand
        for identity in identities:
            add(identity)
    return AclRead(principals=tuple(sorted(principals)), resolved=resolved, read_at=read_at)
