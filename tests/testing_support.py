"""Helpers shared by the test suites (importable because pytest.ini puts the repo root on sys.path)."""
import hashlib
import math

TEST_DIMENSIONS = 8


def fake_vector(text: str, dimensions: int = TEST_DIMENSIONS) -> list[float]:
    """Deterministic unit vector per text, so tests never call an embedding API."""
    digest = hashlib.sha256(text.encode()).digest()
    v = [b - 127.5 for b in digest[:dimensions]]
    norm = math.sqrt(sum(x * x for x in v))
    return [x / norm for x in v]
