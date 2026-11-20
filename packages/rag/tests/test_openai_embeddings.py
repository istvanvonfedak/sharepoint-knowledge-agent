"""OpenAI embeddings: batching by tokens, the quota error callers act on, and what reaches the trace."""
import httpx
import pytest
from langchain_openai import OpenAIEmbeddings
from openai import RateLimitError
from sharepoint_rag import models, openai_embeddings as oe
from sharepoint_rag.embeddings import Limiter, RateLimited, trace_inputs


def test_a_model_id_names_its_backend():
    assert models.parse("openai:text-embedding-3-small") == ("openai", "text-embedding-3-small")
    assert models.parse("gemini-embedding-001") == ("gemini", "gemini-embedding-001")  # ids from before OpenAI
    with pytest.raises(ValueError):
        models.parse("cohere:embed-v4")


def test_batches_stay_under_the_request_limits(monkeypatch):
    monkeypatch.setattr(oe, "BATCH_TEXTS", 3)
    monkeypatch.setattr(oe, "BATCH_TOKENS", 10)
    batches = oe._batches(["one two three four"] * 3 + ["a"] * 4)  # 4 tokens each, then 1
    assert [len(b) for b, _ in batches] == [2, 3, 2]  # the token cap, then the text cap
    assert [n for _, n in batches] == [8, 6, 2]


def test_a_text_over_the_input_limit_is_cut_not_sent_whole(monkeypatch):
    monkeypatch.setattr(oe, "MAX_INPUT_TOKENS", 5)
    text, n = oe._fit("word " * 50)
    assert n == 5 and len(oe.ENCODING.encode(text)) == 5


def test_retry_after_reads_what_openai_says():
    def error(message: str, headers: dict | None = None) -> RateLimitError:
        response = httpx.Response(429, headers=headers or {}, request=httpx.Request("POST", "https://api.openai.com"))
        return RateLimitError(message, response=response, body=None)

    assert oe.retry_after(error("x", {"retry-after": "7"})) == 7.0
    assert oe.retry_after(error("Rate limit reached. Please try again in 1.5s.")) == 1.5
    assert oe.retry_after(error("Please try again in 120ms.")) == pytest.approx(0.12)
    assert oe.retry_after(error("You exceeded your current quota")) == oe.UNHINTED_429_WAIT


async def test_a_429_is_rate_limited_and_holds_documents_back(monkeypatch):
    monkeypatch.setattr(oe, "LIMITER", Limiter(10_000, 0.1))

    async def refuse(self, texts, chunk_size=None, **kwargs):
        response = httpx.Response(429, headers={"retry-after": "30"}, request=httpx.Request("POST", "https://x"))
        raise RateLimitError("Rate limit reached", response=response, body=None)

    monkeypatch.setattr(OpenAIEmbeddings, "aembed_documents", refuse)
    monkeypatch.setenv("OPENAI_API_KEY", "test")
    with pytest.raises(RateLimited) as e:
        await oe.embed(["some text"], "text-embedding-3-small", 8, query=False)
    assert e.value.retry_after == 30.0
    assert oe.LIMITER.paused_for() > 29


async def test_vectors_come_back_normalized_in_order(monkeypatch):
    async def fake(self, texts, chunk_size=None, **kwargs):
        assert self.dimensions == 2 and not self.check_embedding_ctx_length
        return [[3.0, 4.0] if t == "a" else [0.0, 2.0] for t in texts]

    monkeypatch.setattr(OpenAIEmbeddings, "aembed_documents", fake)
    monkeypatch.setenv("OPENAI_API_KEY", "test")
    assert await models.embed("openai:text-embedding-3-small", ["a", "b"], "document", 2) == [[0.6, 0.8], [0.0, 1.0]]


def test_the_trace_gets_counts_not_document_text():
    doc = trace_inputs({"texts": ["secret contract text"], "model": "m", "dimensions": 8, "query": False})
    assert doc == {"model": "m", "dimensions": 8, "texts": 1}
    query = trace_inputs({"texts": ["tire pressure"], "model": "m", "dimensions": 8, "query": True})
    assert query["query"] == "tire pressure"
