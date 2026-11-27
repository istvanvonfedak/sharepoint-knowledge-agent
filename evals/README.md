# Evals (Inspect AI)

What we measure and why is in [docs/evals.md](../docs/evals.md). This is how to run it.

Every suite gets its **own** database (`sharepoint_rag_eval_<suite>` on the `DATABASE_URL` server, created automatically; `EVAL_DATABASE_URL` sets another server and name prefix), so your real index is never touched and suites can't change each other's scores. Documents are indexed through the indexer's real write path (`sharepoint_rag.index_store`), permissions included, and only what changed is re-indexed. Embeddings are cached in `data/.cache/embeddings.sqlite3` (keyed by model, dimensions, task and the exact text), so a suite is embedded once. While indexing, PDFs and Office files are each extracted in their own process (time limit `EVAL_INDEX_EXTRACT_SECONDS`, default 300; memory as `EVAL_EXTRACT_MEMORY_MB`), so one bad file is recorded as skipped instead of stalling the suite.

```
docker compose --env-file .env -f infra/docker-compose.yml up -d
.venv/bin/python -m sharepoint_evals.run              # regression + capability; exit code 1 if a pass bar is missed
.venv/bin/python -m sharepoint_evals.run regression   # permission and extraction gates
.venv/bin/python -m sharepoint_evals.run capability   # retrieval quality, against floors and baselines
.venv/bin/python -m sharepoint_evals.run agent        # sharepoint_ask end to end (needs a model; costs money)
.venv/bin/inspect view                                # browse per-sample results in ./logs
```

| Tier | Suites | Needs | Pass bar |
|---|---|---|---|
| `regression` | `synthetic` (`extraction_regression`, `acl_regression`, `verification_regression`), `acl_scale` (+ `acl_scale_controls`, and again in a second process under `UNRESOLVED_ACL_POLICY=live_check`), `extraction_robustness` | Postgres with pgvector, `GEMINI_API_KEY` (embeddings), `data/` built | Every gate 1.0 |
| `capability` | `synthetic` (`retrieval_capability`), `retrieval_multilingual`, `retrieval_identifiers`, `answer_quality` (FinanceBench retrieval, no LLM), `acl_scale` (`acl_scale_recall`) | The same | Hybrid above the floors in `run.py` `FLOORS` |
| `agent` | `answer_quality`, `agent_safety`, `golden` | The same, plus `ASK_MODEL` (default `openai:gpt-6-luna`; needs `OPENAI_API_KEY`) and a grader (`EVAL_GRADER_MODEL`, default `openai:gpt-6-sol` at `reasoning_effort` medium; `EVAL_GRADER_KWARGS` passes it settings, e.g. `{"reasoning_effort": "high"}`) | No leaked canary, no foreign link, no golden permission failure; the rest is tracked |
| `judge` | `golden_judge`: the grader against 93 hand-labelled cases (56 synthetic, 37 real agent answers) (`golden/judge.jsonl`) | Only `EVAL_GRADER_MODEL` (no agent, database or documents) | None yet: reports where the grader agrees, falsely accepts and falsely rejects |
| `live` | Not built yet (needs a seeded test tenant) | | Fails, so it can't pass by accident |

Options:
- `--exclude SUITE`: run a tier without a suite. A public suite whose `data/` isn't built **fails** its tier otherwise, and the summary lists every suite that didn't run.
- `--sample N`: N cases per task (a fixed shuffle), same checks; results are labelled `sampled`.
- `--allow-quick`: accept suites built with `--quick`; results are labelled `quick`. Without it they are refused.
- `--epochs N` (agent, default 3): runs per question; the report gives the mean ± spread across runs.
- `--allow-missing-data` (agent): skipped golden items (their manuals aren't under `data/`) that carry permission checks fail the run, because the gate would pass over fewer items than it claims. This accepts that. A run with items skipped also never writes baselines.
- `--golden-drafts` (agent, judge): score `draft` golden items (judge: cases) too. Only `reviewed` items are scored otherwise.
- `--only ID` (agent, repeatable): run just these golden items (drafts included) on a corpus of only what they need, in its own database (`golden_only`). A seconds-long smoke test: `run agent --only g-0001 --epochs 1` (the other agent suites are left out, and validation covers only these items).
- `--rerun-failed LOG` (agent, judge; repeatable): run again only the cases that failed in that Inspect log (in any of its runs: a check below 1, a leak, a wrong subject, an unsupported answer, a grading error, a tool error), and those a stopped run never finished, on the full corpus and database, unlike `--only`. It reports which now pass in every run and never writes baselines. Evals cost money: this is the default way to check a fix.
- `--case ID` (agent, judge; repeatable): run this golden item or judge case too, on the full corpus: new items, or passing ones a change may affect. On its own it runs just these.
- `--write-baseline`: record this run's numbers in `evals/baselines.json` (refused for sampled or quick runs). Every run prints the change from it.
- `--update-snapshot` (regression): rewrite `evals/snapshots/extraction_robustness.json` after a reviewed extractor change.

Index builds aren't traced in LangSmith even with `LANGSMITH_TRACING=true`: a re-index would be hundreds of traces against the account's monthly limit. The agent's runs are traced.

A single task can also run directly, e.g. `.venv/bin/inspect eval evals/sharepoint_evals/retrieval_bench.py@retrieval_multilingual -T mode=vector`.

## What each suite checks

| Suite | Tasks | What it guards |
|---|---|---|
| `synthetic` | `extraction_regression` | Every dataset doc, as md / html / docx: key facts survive extract + chunk, no oversized or empty chunks, title kept in heading paths |
| | `acl_regression` | Restricted chunks never appear for callers without access, in vector, keyword and hybrid mode, plus positive controls. Unresolved files are hidden from everyone; guests don't get org-wide files but do get their groups' files |
| | `verification_regression` | Layer 2: a file the index allows but SharePoint now denies is never returned, other results backfill, and a failing access check returns nothing |
| | `retrieval_capability` | 13 questions (paraphrased, exact-identifier, cross-lingual, restricted): hit@1/3/5, MRR per mode |
| `acl_scale` | `acl_scale_hide`, `acl_scale_live_check` | Every question (300) asked by every persona (22: 20 mailbox owners, an outsider, a guest), in every mode: layer 1 at k=50 returns only what the index ACL allows; the production path, with a live check revoking 10% of each persona's files, stays inside what SharePoint allows and fills min(k, permitted chunks); a failing live check returns nothing |
| | `acl_scale_controls` | Each persona finds files it may read, searching by their own text, for every kind of grant (owner, group, `org:members`, `org:everyone`); the guest finds every file it may read |
| | `acl_scale_recall` | Owner recall against an admin persona, by how much the persona can see; recall after the live check |
| `extraction_robustness` | `extraction_robustness` | ~400 real files, each in its own process with a time and memory limit (`EVAL_EXTRACT_SECONDS`, `EVAL_EXTRACT_MEMORY_MB`): expected outcome per type (gate), and no regression against the committed snapshot |
| `retrieval_multilingual` | `retrieval_multilingual` | MLQA en→es / es→en / es→es and MIRACL Spanish: hit@1/5/10, MRR, nDCG@10 per mode and per category |
| `retrieval_identifiers` | `retrieval_identifiers` | Near-identical tickets, invoices, contracts and product sheets: found by identifier alone (`INC-48213`), by a question naming it, or by a combination of fields. Where keyword search has to earn its place in hybrid |
| `answer_quality` | `answer_quality_retrieval` | FinanceBench: the right filing in the top 5; how often another filing of the same company comes first |
| | `answer_quality` | The agent's answers: correct, grounded, cites the right filing; tokens, steps, `budget_exhausted`, seconds |
| `agent_safety` | `agent_safety` | Declines unanswerable questions, answers the controls, never leaks the canary from a planted exfiltration instruction, doesn't carry out BIPIA attacks |
| `golden` | `golden` | The hand-made golden set (`evals/golden/`), run locally against its documents |

The agent runs as production does (`agent.ask`, `ASK_MODEL`, the `ASK_*` budgets), but its tools are bound to the suite's eval database: search goes through `PostgresRetriever` with the persona's principals, and `read_file`/`list_items` are production's own `graph.read_file_text`/`graph.list_children` with Graph's HTTP layer answered from the suite's files (`agent_harness.py`). Grader prompts are versioned files in `sharepoint_evals/rubrics/`; a change is a new version, never an edit. `EVAL_AGENT_CONCURRENCY` (default 4) caps agent runs in parallel.

Unit tests for the eval code itself (oracles, checks, the golden validator, the local Graph, the cache) are in `evals/tests/` and run with the rest of `pytest`.

## Public datasets (`data/`)
`scripts/build_eval_data.py` builds the larger suites from public datasets into `data/<suite>/`. Each holds `docs/` (the files to index), `docs.jsonl` (each file's ACL and metadata) and `SOURCE.json` (URLs, pinned revisions, licenses, seed, counts), plus `questions.jsonl` and `personas.json` for every suite except `extraction_robustness`, which has neither. `data/` is gitignored: some licenses forbid redistribution and Enron holds real people's email. Run `scripts/scan_eval_data.py` after every rebuild.

```
.venv/bin/python scripts/build_eval_data.py                  # all suites; downloads cached in data/.cache
.venv/bin/python scripts/build_eval_data.py acl_scale --force
.venv/bin/python scripts/build_eval_data.py --quick          # small samples, to try it out
```

| Suite | Source | What it's for |
|---|---|---|
| `acl_scale` | Enron mailboxes (owners, plus assigned groups, `org:members`, `org:everyone` and unresolved files) | No leaks across ~2,000 files and 22 personas; recall for users who can see few files |
| `retrieval_multilingual` | MLQA (en→es, es→en, es→es) + MIRACL Spanish with hard negatives | Retrieval hard enough to show tuning gains |
| `extraction_robustness` | Govdocs1 + Common Crawl PDFs, stratified by extension | Extractor survives real files; `expect` says extract or skip |
| `answer_quality` | FinanceBench filings (PDF) with gold answers and evidence pages | `sharepoint_ask` correctness and grounding |
| `agent_safety` | SQuAD 2.0 unanswerables + planted instructions (BIPIA attacks, exfiltration with a canary) | Declines when nothing answers; ignores instructions in documents |
| `retrieval_identifiers` | Generated by the script (no download): 600 records in four near-identical families | Identifier and exact-field lookups, the case embeddings handle worst |

A full build downloads about 2.5 GB (most of it streamed, not kept) and takes 20–30 minutes. The first eval run on it then embeds about 36,000 chunks (roughly 10M tokens; FinanceBench is two thirds of it), paced under Gemini's per-minute quota (`EVAL_EMBED_PER_MINUTE`, default 2,500 texts), so it takes about 15 minutes; later runs read the cache.

## Caveats
- The synthetic dataset is small (11 docs): it catches breakage but cannot show improvement. Tune on the public suites; the golden set confirms.
- Connection pools are per Inspect run and are dropped, not closed, when the run ends. `run all` therefore holds up to 8 idle connections per suite until the process exits: fine locally, and worth closing properly before this runs against a shared server.
- Inspect logs (`logs/`) hold document text, Enron email included: treat them like `data/`.
- `evals/golden/` items are drafts until two people have read them (see [docs/evals.md](../docs/evals.md#golden-set)).
- Golden items that cite the Army manuals need them under `data/manuals/`; without them those items are skipped (not failed) and the run says how to fetch them (`scripts/fetch_golden_files.py`).
