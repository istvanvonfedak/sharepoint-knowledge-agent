"""Small corpora built in a temporary folder, so these tests need no data/ and no database."""
import pytest
from sharepoint_rag import acl

from sharepoint_evals.corpus import Corpus, Doc


@pytest.fixture
def tiny(tmp_path):
    def doc(name, principals, resolved=True, text="# Title\n\nSome text.\n"):
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(text if isinstance(text, bytes) else text.encode())
        return Doc(name, tuple(principals), resolved, path)

    docs = [
        doc("own.md", ["user:ann"]),
        doc("bobs.md", ["user:bob"]),
        doc("group.md", ["user:bob", "group:legal"]),
        doc("org.md", ["user:bob", acl.ORG_MEMBERS]),
        doc("everyone.md", ["user:bob", acl.ORG_EVERYONE]),
        doc("unresolved.md", ["user:ann"], resolved=False),
        doc("report.pdf", [acl.ORG_MEMBERS], text=b"%PDF-1.4 not really"),
    ]
    personas = {
        "ann": acl.query_principals("ann", [], True),
        "bob": acl.query_principals("bob", ["legal"], True),
        "guest": acl.query_principals("gus", ["legal"], False),
    }
    return Corpus("tiny", docs, personas)
