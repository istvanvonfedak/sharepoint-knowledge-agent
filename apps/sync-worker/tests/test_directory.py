from sharepoint_rag import acl
from sync_worker.directory import Directory
from sync_worker.graph import GraphError

SITE = "https://contoso.sharepoint.com/sites/intranet"


class FakeGraph:
    def __init__(self) -> None:
        self.groups: dict[str, list[str]] = {}  # site group id -> member login names
        self.owners: dict[str, list[str]] = {}  # Microsoft 365 group id -> owner user ids
        self.users: dict[str, str] = {}  # UPN -> object id
        self.calls: list[str] = []

    async def sharepoint_get_all(self, url: str) -> list[dict]:
        self.calls.append(url)
        group_id = url.split("getbyid(")[1].split(")")[0]
        if group_id not in self.groups:
            raise GraphError(403, "accessDenied")
        return [{"LoginName": login} for login in self.groups[group_id]]

    async def get_all(self, path: str, **kwargs) -> list[dict]:
        self.calls.append(path)
        group_id = path.split("/groups/")[1].split("/")[0]
        if group_id not in self.owners:
            raise GraphError(404, "notFound")
        return [{"id": i} for i in self.owners[group_id]]

    async def get(self, path: str, **kwargs) -> dict:
        self.calls.append(path)
        upn = path.rsplit("/", 1)[1].replace("%40", "@")
        if upn not in self.users:
            raise GraphError(404, "notFound")
        return {"id": self.users[upn]}


async def test_members_owners_users_and_claims():
    g = FakeGraph()
    g.groups["5"] = [
        "SHAREPOINT\\system",
        "c:0o.c|federateddirectoryclaimprovider|m365",
        "c:0o.c|federateddirectoryclaimprovider|m365_o",
        "c:0t.c|tenant|sg1",
        "i:0#.f|membership|alice@contoso.com",
        "c:0-.f|rolemanager|spo-grid-all-users/t1",
    ]
    g.owners["m365"] = ["o1"]
    g.users["alice@contoso.com"] = "a1"
    assert await Directory(g).site_group(SITE, "5") == (
        "group:m365", "group:sg1", acl.ORG_MEMBERS, "user:a1", "user:o1",
    )


async def test_one_unresolvable_member_makes_the_group_unresolved():
    g = FakeGraph()
    g.groups["5"] = ["c:0o.c|federateddirectoryclaimprovider|m365", "i:0#.f|membership|gone@contoso.com"]
    assert await Directory(g).site_group(SITE, "5") is None
    g.groups["6"] = ["c:0+.w|s-1-5-21-unknown-claim"]
    assert await Directory(g).site_group(SITE, "6") is None


async def test_unreadable_groups_are_unresolved_and_empty_groups_grant_nothing():
    g = FakeGraph()
    g.groups["4"] = []
    d = Directory(g)
    assert await d.site_group(SITE, "4") == ()
    assert await d.site_group(SITE, "7") is None  # 403: no SharePoint permission, or membership hidden
    assert await d.group_owners("unknown") is None
    assert await d.site_group(SITE, "not-a-number") is None


async def test_answers_are_cached_per_site_and_group():
    g = FakeGraph()
    g.groups["5"] = ["c:0o.c|federateddirectoryclaimprovider|m365"]
    d = Directory(g)
    perms = [{"roles": ["read"], "grantedToV2": {"siteGroup": {"id": "5"}}}]
    for _ in range(3):
        lookups = await d.lookups(SITE, perms)
    assert lookups.site_groups == {"5": ("group:m365",)}
    assert len(g.calls) == 1
    await d.site_group(SITE + "2", "5")
    assert len(g.calls) == 2


async def test_transient_errors_are_raised_not_cached():
    g = FakeGraph()

    async def flaky(url):
        raise GraphError(503, "busy")

    g.sharepoint_get_all = flaky
    d = Directory(g)
    for _ in range(2):
        try:
            await d.site_group(SITE, "5")
        except GraphError as e:
            assert e.status == 503
        else:
            raise AssertionError("expected GraphError")


async def test_cache_stays_bounded_when_every_entry_is_fresh(monkeypatch):
    from sync_worker import directory

    monkeypatch.setattr(directory, "_MAX_ENTRIES", 100)
    g = FakeGraph()
    d = Directory(g)
    for n in range(250):
        g.groups[str(n)] = []
        await d.site_group(SITE, str(n))
        assert len(d._cache) <= 100
    await d.site_group(SITE, "249")  # the newest are kept
    assert len(g.calls) == 250
