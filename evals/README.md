# Evals (Inspect AI)

Two kinds, run against a **separate** database (`sharepoint_rag_eval`, created automatically; override with `EVAL_DATABASE_URL`) so your real index is never touched. They need Postgres running and `GEMINI_API_KEY` in `.env` (embeddings only; no LLM key, tasks use Inspect's `mockllm` since nothing is generated).

```
docker compose --env-file .env -f infra/docker-compose.yml up -d
.venv/bin/python -m sharepoint_evals.run            # all suites; exit code 1 on failure
.venv/bin/python -m sharepoint_evals.run regression
.venv/bin/python -m sharepoint_evals.run capability
.venv/bin/inspect view                                # browse per-sample results in ./logs
```

## Regression (must score 1.0)
| Task | What it guards |
|---|---|
| `extraction_regression` | Every dataset doc, as md / html / docx: key facts survive extract + chunk, no oversized or empty chunks, title kept in heading paths |
| `acl_regression` | Restricted chunks never appear for callers without access, in vector, keyword and hybrid mode, plus positive controls (HR sees HR docs, execs see exec docs; HR does not see exec docs and vice versa). Files with unresolved permissions are hidden from everyone; guests don't get org-wide files but do get their groups' files. |
| `verification_regression` | Layer 2: a file the index ACL allows but SharePoint now denies (access revoked since indexing) is never returned, other results backfill, and a failing access check returns nothing (fail closed) |

## Capability (a quality gradient, not pass/fail)
`retrieval_capability` asks 13 questions (paraphrased, exact-identifier, cross-lingual EN/ES, and permission-restricted) and reports hit@1/3/5 and MRR per search mode. `run.py` prints a comparison and fails only if **hybrid** falls below the floors in `CAPABILITY_FLOORS`. Use it to compare changes to chunk size, embedding model/dimensions or RRF weights: `inspect eval evals/sharepoint_evals/capability.py -T mode=vector`.

The index is built through the indexer's real write path (`sharepoint_rag.index_store`), permissions included. Generation swaps and out-of-order permission writes are covered by the database tests (`packages/rag/tests/test_index_store.py`), and the agent's loop by `apps/mcp-server/tests/test_agent.py` with a scripted model; `sharepoint_ask` has no answer-quality eval yet.

## Caveats
- The dataset is small (10 docs), so vector search already scores 1.0: this catches breakage but cannot yet show improvement. Add harder distractors and more questions before tuning against it.
- Answer-quality/groundedness evals (LLM-generated answers) are not included; they need a model API key and the answering layer, which doesn't exist yet.
- The dataset and questions are in `sharepoint_evals/dataset/` and `questions.py`. Changing a doc triggers a re-embed on the next run.
