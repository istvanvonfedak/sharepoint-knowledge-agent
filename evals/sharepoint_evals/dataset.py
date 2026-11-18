"""Synthetic SharePoint dataset (documents, permissions, personas) used by the evals. `acl` mirrors what the crawler would store."""
from dataclasses import dataclass
from importlib.resources import files

from sharepoint_rag import acl

EVERYONE = acl.ORG_MEMBERS  # "anyone in the organization": every full member holds it, guests don't


@dataclass(frozen=True)
class Doc:
    name: str  # file name, also the doc id used in eval targets
    acl: tuple[str, ...]
    resolved: bool = True  # False: the crawler couldn't resolve every grant (e.g. a SharePoint site group)

    def read(self) -> bytes:
        return files("sharepoint_evals").joinpath("dataset", self.name).read_bytes()


DOCS = [
    Doc("leave-policy.md", (EVERYONE,)),
    Doc("expenses.md", (EVERYONE,)),
    Doc("onboarding.md", (EVERYONE,)),
    Doc("security-incident.md", (EVERYONE,)),
    Doc("it-helpdesk.md", (EVERYONE,)),
    Doc("engineering-oncall.md", (EVERYONE,)),
    Doc("politica-vacaciones-espana.md", (EVERYONE,)),
    Doc("guia-teletrabajo.md", (EVERYONE,)),
    Doc("salaries-2025.md", ("group:hr",)),
    Doc("board-minutes.md", ("group:execs",)),
    Doc("reorg-plan.md", ("group:hr",), resolved=False),
]

# Principals per persona, as the mcp-server builds them (acl.query_principals): user id, group ids, org markers.
PERSONAS = {
    "alice": acl.query_principals("alice", [], is_member=True),
    "hr": acl.query_principals("hana", ["hr"], is_member=True),
    "exec": acl.query_principals("eve", ["execs"], is_member=True),
    "guest": acl.query_principals("gina", ["hr"], is_member=False),  # an external guest, even in the HR group
}
