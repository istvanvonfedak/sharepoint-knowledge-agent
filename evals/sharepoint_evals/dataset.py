"""Synthetic SharePoint dataset (documents, permissions, personas) used by the evals. `acl` mirrors what the crawler would store."""
from dataclasses import dataclass
from importlib.resources import files

EVERYONE = "everyone"  # org-wide marker; every caller's principals include it


@dataclass(frozen=True)
class Doc:
    name: str  # file name, also the doc id used in eval targets
    acl: tuple[str, ...]

    def read(self) -> bytes:
        return files("sharepoint_evals").joinpath("dataset", self.name).read_bytes()


DOCS = [
    Doc("leave-policy.md", (EVERYONE,)),
    Doc("expenses.md", (EVERYONE,)),
    Doc("onboarding.md", (EVERYONE,)),
    Doc("security-incident.md", (EVERYONE,)),
    Doc("it-helpdesk.md", (EVERYONE,)),
    Doc("engineering-oncall.md", (EVERYONE,)),
    Doc("politica-ferias-portugal.md", (EVERYONE,)),
    Doc("guia-teletrabalho.md", (EVERYONE,)),
    Doc("salaries-2025.md", ("group:hr",)),
    Doc("board-minutes.md", ("group:execs",)),
]

# Principals per persona = [user id, group ids..., EVERYONE]
PERSONAS = {
    "alice": ["user:alice", EVERYONE],
    "hr": ["user:hana", "group:hr", EVERYONE],
    "exec": ["user:eve", "group:execs", EVERYONE],
}
