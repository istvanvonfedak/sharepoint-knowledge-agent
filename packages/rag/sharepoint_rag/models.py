"""Embedding backend: hosted Gemini.

A model id is `gemini:<name>`, e.g. `gemini:gemini-embedding-001`; a bare name means Gemini. This runs wherever
embedding happens: inside the inference-server, or in-process when INFERENCE_URL is unset (local use, evals).
Callers normally go through `sharepoint_rag.inference`.
"""
from typing import Literal

from . import embeddings

Task = Literal["document", "query"]


def parse(model: str) -> tuple[str, str]:
    backend, sep, name = model.partition(":")
    if not sep:
        return "gemini", model
    if backend != "gemini":
        raise ValueError(f"Unknown model backend {backend!r} in {model!r} (expected gemini:<model>)")
    return backend, name


async def embed(model: str, texts: list[str], task: Task, dimensions: int) -> list[list[float]]:
    if not texts:
        return []
    _, name = parse(model)
    task_type = "RETRIEVAL_QUERY" if task == "query" else "RETRIEVAL_DOCUMENT"
    return await embeddings.embed(texts, task_type, name, dimensions)
