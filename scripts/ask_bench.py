"""Runs `sharepoint_ask` live over a fixed set of questions, as the signed-in user, to compare token use and cost.

Run it inside the mcp-server container, which has the settings, the database and the user's Entra sign-in:

    docker compose --env-file .env -f infra/docker-compose.yml exec -T mcp-server python - --label before < scripts/ask_bench.py

Each run is traced to LangSmith with metadata `bench=<label>` and `question=<n>`; LangSmith prices the model calls.
Prints one JSON line per run (status, steps, tokens, seconds) and a summary per question. With several users signed
in, pass --user <the Entra id stored in msal_cache>.
"""
import argparse
import asyncio
import json
import statistics
import time

QUESTIONS = [
    "What is the part number for the clutch roller on the HMMWV transmission, and what is the oil capacity of the "
    "Abrams M1A2 turbine engine?",
    "What is the part number and NSN for the clutch roller on the M939 series truck?",
    "What is the recommended maintenance interval for the Abrams M1A2 turbine engine?",
    "How much does the engine oil capacity differ between the M998 HMMWV and the M1008 CUCV?",
    "What is the tire pressure for the M998 HMMWV?",
    "What are the steps to replace the bolster midwinch propeller shaft on the M815?",
]


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--label", required=True, help="Tags the traces, e.g. before or after")
    parser.add_argument("--repeats", type=int, default=3)
    parser.add_argument("--questions", default="", help="Comma-separated question numbers (1-based); default all")
    parser.add_argument("--concurrency", type=int, default=3)
    parser.add_argument("--user", default="")
    parser.add_argument("--no-sandbox", action="store_true", help="Run without the code interpreter, as ASK_SANDBOX=off")
    parser.add_argument("--max-steps", type=int, help="Override ASK_MAX_STEPS")
    parser.add_argument("--model", help="Override ASK_MODEL, e.g. openai:gpt-6.1-terra")
    parser.add_argument("--model-kwargs", help='Override ASK_MODEL_KWARGS (JSON), e.g. {"reasoning_effort": "low"}')
    parser.add_argument("--set", action="append", default=[], metavar="NAME=VALUE",
                        help="Override one of mcp_server.agent's integer constants for this run, e.g. RUN_OUTPUT_CHARS=2000")
    args = parser.parse_args()

    from mcp_server import agent, server, settings

    overrides = dict(s.split("=", 1) for s in args.set)
    if args.no_sandbox:
        server._sandbox = None
        settings.ASK_SANDBOX = "off"
        overrides["sandbox"] = "off"
    if args.model or args.model_kwargs:
        from langchain.chat_models import init_chat_model

        settings.ASK_MODEL = args.model or settings.ASK_MODEL
        kwargs = json.loads(args.model_kwargs) if args.model_kwargs else settings.ASK_MODEL_KWARGS
        server._ask_model = init_chat_model(settings.ASK_MODEL, **kwargs)
        overrides["model_kwargs"] = json.dumps(kwargs)
    if args.max_steps:
        settings.ASK_MAX_STEPS = args.max_steps
        overrides["max_steps"] = str(args.max_steps)
    for name, value in dict(s.split("=", 1) for s in args.set).items():
        if not isinstance(getattr(agent, name, None), (int, float)):
            raise SystemExit(f"agent.{name} is not a numeric constant")
        setattr(agent, name, type(getattr(agent, name))(value))

    await server.pool.open()
    try:
        user_id = args.user
        if not user_id:
            async with server.pool.connection() as conn:
                users = [r[0] for r in await (await conn.execute("SELECT user_id FROM msal_cache")).fetchall()]
            if len(users) != 1:
                raise SystemExit(f"{len(users)} users are signed in; pass --user")
            user_id = users[0]
        token = await server._graph_token(user_id)
        picked = [int(n) for n in args.questions.split(",") if n] or range(1, len(QUESTIONS) + 1)
        jobs = [(n, r) for n in picked for r in range(args.repeats)]
        limit = asyncio.Semaphore(args.concurrency)
        results: dict[int, list[dict]] = {}

        async def one(n: int, repeat: int) -> None:
            async with limit:
                started = time.monotonic()
                result = await server.run_ask(user_id, token, QUESTIONS[n - 1],
                                              metadata={"bench": args.label, "question": n, "repeat": repeat, **overrides})
                row = {"question": n, "repeat": repeat, "status": result.status, "steps": result.steps,
                       "input_tokens": result.input_tokens, "output_tokens": result.output_tokens,
                       "seconds": round(time.monotonic() - started, 1), "sources": len(result.sources)}
                print(json.dumps(row), flush=True)
                results.setdefault(n, []).append(row)

        print(json.dumps({"label": args.label, "model": settings.ASK_MODEL, "sandbox": settings.ASK_SANDBOX,
                          "max_steps": settings.ASK_MAX_STEPS, **overrides}), flush=True)
        await asyncio.gather(*(one(n, r) for n, r in jobs))
        for n in sorted(results):
            rows = results[n]
            print(json.dumps({"question": n, "runs": len(rows), "statuses": sorted(r["status"] for r in rows),
                              "median_tokens": statistics.median(r["input_tokens"] + r["output_tokens"] for r in rows),
                              "median_steps": statistics.median(r["steps"] for r in rows)}))
    finally:
        await server.pool.close()


if __name__ == "__main__":
    asyncio.run(main())
