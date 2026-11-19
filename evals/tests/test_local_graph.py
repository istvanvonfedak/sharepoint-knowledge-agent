"""The agent harness answers production's graph.read_file_text and list_children from a suite's files."""
import pytest
from sharepoint_client import graph
from sharepoint_rag import acl

from sharepoint_evals import agent_harness
from sharepoint_evals.corpus import Corpus, Doc


@pytest.fixture(autouse=True)
def fresh_graph(monkeypatch):
    local = agent_harness.LocalGraph()
    monkeypatch.setattr(agent_harness, "local_graph", local)
    monkeypatch.setattr(graph, "_get", graph._get)  # restored after the test
    return local


async def test_reads_text_and_refuses_pdf_like_production(tiny, fresh_graph):
    fresh_graph.register(tiny)
    res = await graph.read_file_text("ann", "tiny", "own.md", 5)
    assert res["text"] == "# Tit" and res["truncated"] and res["parentId"] == agent_harness.FOLDER
    with pytest.raises(ValueError, match="Unsupported file type"):
        await graph.read_file_text("ann", "tiny", "report.pdf", 1000)


async def test_files_the_persona_cannot_open_are_not_found(tiny, fresh_graph):
    fresh_graph.register(tiny)
    with pytest.raises(RuntimeError, match="404"):
        await graph.read_file_text("ann", "tiny", "group.md", 1000)
    names = {c["id"] for c in await graph.list_children("guest", "tiny", agent_harness.FOLDER)}
    assert names == tiny.readable(tiny.personas["guest"])


async def test_listing_pages_like_graph(tmp_path, fresh_graph):
    docs = []
    for i in range(450):
        (tmp_path / f"f{i:03}.txt").write_text("x")
        docs.append(Doc(f"f{i:03}.txt", (acl.ORG_MEMBERS,), True, tmp_path / f"f{i:03}.txt"))
    fresh_graph.register(Corpus("big", docs, {"ann": acl.query_principals("ann", [], True)}))
    children = await graph.list_children("ann", "big", None)
    assert len(children) == 450 and len({c["id"] for c in children}) == 450
