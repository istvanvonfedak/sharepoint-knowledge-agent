"""Principals: the strings an ACL is made of, shared by the indexer (who may read a file) and the
search side (who is asking).

A file's ACL lists `user:<entra object id>`, `group:<entra object id>`, and markers. A user's principals are
their own id, every group they belong to (nested included), and `org:members` if they are a full member.
"""
from dataclasses import dataclass
from datetime import datetime

from . import config

ORG_MEMBERS = "org:members"  # "anyone in the organization" links, "Everyone except external users"; not guests
ORG_EVERYONE = "org:everyone"  # SharePoint's "Everyone" claim: every signed-in user of the tenant, guests included
ORG_ANONYMOUS = "org:anonymous"  # "anyone with the link"; nobody holds it, so such grants alone show no one
UNRESOLVED = "acl:unresolved"  # placed on chunks whose file's permissions could not all be resolved


def user(object_id: str) -> str:
    return f"user:{object_id}"


def group(object_id: str) -> str:
    return f"group:{object_id}"


@dataclass(frozen=True)
class AclRead:
    """One read of a file's permissions from Graph. `read_at` is when the request was sent: of two reads,
    the later one wins, so a slow job can't write back permissions that were revoked since it read them."""

    principals: tuple[str, ...]
    resolved: bool
    read_at: datetime

    @property
    def status(self) -> str:
        return "resolved" if self.resolved else "unresolved"


def effective(principals: list[str] | tuple[str, ...], status: str) -> list[str]:
    """The ACL copied onto chunks. A file with any permission we could not resolve is visible to nobody,
    or only to the live check when UNRESOLVED_ACL_POLICY=live_check (see `query_principals`)."""
    return sorted(set(principals)) if status == "resolved" else [UNRESOLVED]


def query_principals(user_id: str, group_ids: list[str], is_member: bool) -> list[str]:
    principals = [user(user_id), *(group(g) for g in group_ids), ORG_EVERYONE]
    if is_member:
        principals.append(ORG_MEMBERS)
    if config.UNRESOLVED_ACL_POLICY == "live_check":
        principals.append(UNRESOLVED)
    return principals
