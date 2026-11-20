"""Embedding backends: hosted OpenAI (through LangChain, traced in LangSmith) or hosted Gemini.

A model id is `<backend>:<name>`, e.g. `openai:text-embedding-3-small` or `gemini:gemini-embedding-001`; a bare name
means Gemini, as ids were before OpenAI was added. This runs wherever embedding happens: inside the inference-server,
or in-process when INFERENCE_URL is unset (local use, evals). Callers normally go through `sharepoint_rag.inference`.
"""
from typing import Literal

from . import embeddings

Task = Literal["document", "query"]
BACKENDS = ("openai", "gemini")


def parse(model: str) -> tuple[str, str]:
    backend, sep, name = model.partition(":")
    if not sep:
        return "gemini", model
    if backend not in BACKENDS:
        raise ValueError(f"Unknown model backend {backend!r} in {model!r} (expected openai:<model> or gemini:<model>)")
    return backend, name


async def embed(model: str, texts: list[str], task: Task, dimensions: int) -> list[list[float]]:
    if not texts:
        return []
    backend, name = parse(model)
    if backend == "openai":
        from . import openai_embeddings  # imported when first used: it loads a tokenizer

        return await openai_embeddings.embed(texts, name, dimensions, query=task == "query")
    task_type = "RETRIEVAL_QUERY" if task == "query" else "RETRIEVAL_DOCUMENT"
    return await embeddings.embed(texts, task_type, name, dimensions)
