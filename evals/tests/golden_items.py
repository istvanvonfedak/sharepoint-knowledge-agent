"""Helpers for tests that build golden or judge items: change a field without saying which bucket it lives in."""
import copy

INPUTS = {"question", "persona"}
METADATA = {"id", "status", "kind", "language", "origin", "author", "reviewer", "added", "notes"}
JUDGE_INPUTS = {"question", "language", "reference", "facts", "answer", "sources"}


def bucket(field: str) -> str:
    """Where a golden item keeps this field."""
    return "inputs" if field in INPUTS else "metadata" if field in METADATA else "expected_outputs"


def judge_bucket(field: str) -> str:
    """Where a judge case keeps this field."""
    return "inputs" if field in JUDGE_INPUTS else "expected_outputs" if field == "labels" else "metadata"


def change(item: dict, bucket_of, **changes) -> dict:
    """A copy of the item with these fields set (or removed, for None), each in the bucket `bucket_of` names."""
    out = copy.deepcopy(item)
    for field, value in changes.items():
        if value is None:
            out[bucket_of(field)].pop(field)
        else:
            out[bucket_of(field)][field] = value
    return out


def changed(item: dict, **changes) -> dict:
    return change(item, bucket, **changes)
