from datetime import datetime, timezone

from sharepoint_rag import acl
from sync_worker.permissions import Lookups, claim_principal, needs, owners_group, to_acl, user_upn

NOW = datetime(2026, 1, 1, tzinfo=timezone.utc)


def test_users_groups_and_org_links():
    perms = [
        {"roles": ["write"], "grantedToV2": {"user": {"id": "u1"}, "siteUser": {"loginName": "i:0#.f|membership|a@x"}}},
        {"roles": ["read"], "grantedToV2": {"group": {"id": "g1"}}},
        {"roles": ["read"], "link": {"scope": "organization", "type": "view"}},
        {"roles": ["read"], "link": {"scope": "users"}, "grantedToIdentitiesV2": [{"user": {"id": "u2"}}]},
        {"roles": ["owner"], "grantedToV2": {"application": {"id": "app"}}},  # apps grant no person access
    ]
    read = to_acl(perms, NOW)
    assert read.resolved
    assert set(read.principals) == {"user:u1", "group:g1", acl.ORG_MEMBERS, "user:u2"}


def test_anonymous_link_is_a_marker_nobody_holds():
    read = to_acl([{"roles": ["read"], "link": {"scope": "anonymous"}}], NOW)
    assert read.principals == (acl.ORG_ANONYMOUS,) and read.resolved
    assert acl.ORG_ANONYMOUS not in acl.query_principals("u", [], True)


def test_site_groups_and_unknown_grants_are_unresolved():
    assert not to_acl([{"roles": ["read"], "grantedToV2": {"siteGroup": {"id": "3", "displayName": "Team Members"}}}], NOW).resolved
    assert not to_acl([{"roles": ["read"], "link": {"scope": "someNewScope"}}], NOW).resolved
    assert not to_acl([{"roles": ["read"]}], NOW).resolved


def test_everyone_claims():
    members = {"roles": ["read"], "grantedToV2": {"siteUser": {"loginName": "c:0-.f|rolemanager|spo-grid-all-users/abc"}}}
    everyone = {"roles": ["read"], "grantedToV2": {"siteUser": {"loginName": "c:0(.s|true"}}}
    assert to_acl([members], NOW).principals == (acl.ORG_MEMBERS,)
    assert to_acl([everyone], NOW).principals == (acl.ORG_EVERYONE,)


def test_a_site_group_named_like_a_claim_is_not_everyone():
    for name in ("Contractors spo-grid-all-users", "c:0(.s|true"):
        read = to_acl([{"roles": ["read"], "grantedToV2": {"siteGroup": {"loginName": name}}}], NOW)
        assert not read.resolved and read.principals == ()
    sneaky_user = {"roles": ["read"], "grantedToV2": {"siteUser": {"loginName": "i:0#.f|membership|spo-grid-all-users@evil"}}}
    assert not to_acl([sneaky_user], NOW).resolved


def test_permission_without_roles_grants_nothing():
    read = to_acl([{"roles": [], "grantedToV2": {"user": {"id": "u1"}}}], NOW)
    assert read.principals == () and read.resolved


def test_guests_get_neither_org_marker_nor_unresolved_by_default():
    principals = acl.query_principals("guest", ["g1"], is_member=False)
    assert acl.ORG_MEMBERS not in principals and acl.UNRESOLVED not in principals
    assert acl.ORG_EVERYONE in principals


OWNERS = {"roles": ["owner"], "grantedToV2": {
    "group": {"@odata.type": "#microsoft.graph.sharePointIdentity", "id": "m365"},
    "siteUser": {"id": "6", "loginName": "c:0o.c|federateddirectoryclaimprovider|m365_o"},
}}
MEMBERS_GROUP = {"roles": ["write"], "grantedToV2": {"siteGroup": {"id": "5", "displayName": "Team Members"}}}


def test_site_groups_resolve_through_lookups():
    lookups = Lookups(site_groups={"5": ("group:m365", acl.ORG_MEMBERS)})
    read = to_acl([MEMBERS_GROUP], NOW, lookups)
    assert read.resolved and read.principals == ("group:m365", acl.ORG_MEMBERS)
    assert not to_acl([MEMBERS_GROUP], NOW, Lookups(site_groups={"5": None})).resolved  # unreadable
    assert to_acl([MEMBERS_GROUP], NOW, Lookups(site_groups={"5": ()})).resolved  # empty group: nobody


def test_group_owners_are_not_its_members():
    # Graph labels the owners with the group's own id: taking that would show owner-only files to every member.
    assert not to_acl([OWNERS], NOW).resolved
    read = to_acl([OWNERS], NOW, Lookups(owners={"m365": ("user:o1",)}))
    assert read.resolved and read.principals == ("user:o1",)


def test_needs_lists_what_to_look_up():
    upn_only = {"roles": ["read"], "grantedToV2": {"siteUser": {"loginName": "i:0#.f|membership|Bob@x.com"}}}
    with_id = {"roles": ["read"], "grantedToV2": {"user": {"id": "u1"}, "siteUser": {"loginName": "i:0#.f|membership|a@x.com"}}}
    perms = [MEMBERS_GROUP, OWNERS, upn_only, with_id, {"roles": [], "grantedToV2": {"siteGroup": {"id": "9"}}}]
    assert needs(perms) == ({"5"}, {"m365"}, {"Bob@x.com"})


def test_upn_only_users_resolve_through_lookups():
    upn_only = {"roles": ["read"], "grantedToV2": {"siteUser": {"loginName": "i:0#.f|membership|Bob@x.com"}}}
    assert not to_acl([upn_only], NOW).resolved
    read = to_acl([upn_only], NOW, Lookups(users={"bob@x.com": ("user:b1",)}))
    assert read.resolved and read.principals == ("user:b1",)


def test_login_names():
    assert owners_group("c:0o.c|federateddirectoryclaimprovider|ABC_o") == "abc"
    assert owners_group("c:0o.c|federateddirectoryclaimprovider|abc") is None
    assert user_upn("i:0#.f|membership|Alice@contoso.com") == "Alice@contoso.com"
    assert claim_principal("c:0o.c|federateddirectoryclaimprovider|ABC") == "group:abc"
    assert claim_principal("c:0t.c|tenant|sg1") == "group:sg1"
    assert claim_principal("c:0-.f|rolemanager|spo-grid-all-users/t1") == acl.ORG_MEMBERS
    assert claim_principal("SHAREPOINT\\system") == ""
    for unknown in ("c:0o.c|federateddirectoryclaimprovider|abc_o", "i:0#.f|membership|a@x", "c:0+.w|s-1-5-21", ""):
        assert claim_principal(unknown) is None
