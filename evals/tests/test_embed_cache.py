"""The embedding cache: embedded once, keyed by model, dimensions, task and text."""
from sharepoint_evals import embed_cache


async def test_cache_hits_and_keys(tmp_path, monkeypatch):
    monkeypatch.setattr(embed_cache, "PATH", tmp_path / "e.sqlite3")
    calls = []

    async def fake(model, texts, task, dimensions):
        calls.append(list(texts))
        return [[float(len(t)), 1.0] for t in texts]

    monkeypatch.setattr(embed_cache, "_uncached", fake)
    first = await embed_cache.embed("m", ["a", "bb", "a"], "document", 2)
    assert first == [[1.0, 1.0], [2.0, 1.0], [1.0, 1.0]] and calls == [["a", "bb"]]
    assert await embed_cache.embed("m", ["bb", "a"], "document", 2) == [[2.0, 1.0], [1.0, 1.0]]
    assert len(calls) == 1
    await embed_cache.embed("m", ["a"], "query", 2)  # another task is another vector
    await embed_cache.embed("other", ["a"], "document", 2)
    assert calls[1:] == [["a"], ["a"]]
