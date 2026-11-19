"""python -m sync_worker [run]                     run the worker (default)
python -m sync_worker sites add <site>          index a site (Graph site id or hostname:/sites/path)
python -m sync_worker sites disable <site-id>   stop indexing a site and purge its data
python -m sync_worker sites list
python -m sync_worker status                    sync lag, queue, file and permission states, errors
"""
import argparse
import asyncio
import logging

import psycopg
from sharepoint_rag import config as rag_config
from sharepoint_rag import db, queue

from . import jobs, worker
from .graph import AppGraph


async def _sites(args) -> None:
    db.migrate()
    pool = db.create_pool(min_size=1, max_size=2)
    graph = AppGraph(pool) if args.action == "add" else None
    await pool.open(wait=True)
    try:
        if args.action == "add":
            try:
                site = await jobs.add_site(jobs.Context(pool, graph, heartbeat=_noop), args.site)
            finally:
                await graph.close()
            async with pool.connection() as conn:
                await queue.enqueue(conn, "discover_sites", dedupe_key="discover_sites")
            print(f"Added {site.get('displayName')} ({site['id']}); its drives are crawled on the next discovery run.")
        elif args.action == "disable":
            async with pool.connection() as conn, conn.transaction():
                cur = await conn.execute("UPDATE sites SET enabled = false WHERE id = %s", (args.site,))
                if cur.rowcount == 0:
                    raise SystemExit(f"No site {args.site!r}; see `sites list`")
                drives = await (await conn.execute("SELECT id FROM drives WHERE site_id = %s", (args.site,))).fetchall()
                for (drive_id,) in drives:
                    await queue.enqueue(conn, "purge_drive", {"drive_id": drive_id}, dedupe_key=f"purge:{drive_id}")
            print(f"Disabled; {len(drives)} drive(s) will be purged.")
        else:
            async with pool.connection() as conn:
                rows = await (await conn.execute(
                    "SELECT s.id, s.name, s.enabled, count(d.id) FROM sites s LEFT JOIN drives d ON d.site_id = s.id "
                    "GROUP BY s.id ORDER BY s.name"
                )).fetchall()
            for site_id, name, enabled, drives in rows:
                print(f"{'on ' if enabled else 'off'}  {name}  ({drives} drives)  {site_id}")
    finally:
        await pool.close()


async def _noop() -> None:
    pass


def _status() -> None:
    db.migrate()
    with psycopg.connect(rag_config.require_database_url()) as conn:
        def show(title: str, query: str) -> None:
            rows = conn.execute(query).fetchall()
            print(f"\n{title}")
            for r in rows or [("(none)",)]:
                print("  " + "  ".join("" if v is None else str(v) for v in r))

        show("Drives (sync lag, cursor state, last error)",
             "SELECT d.name, coalesce(to_char(now() - st.synced_at, 'HH24:MI:SS'), 'never') AS lag, "
             "CASE WHEN st.crawl_id IS NOT NULL THEN 'full crawl' WHEN st.next_link IS NOT NULL THEN 'mid-crawl' ELSE '' END, "
             "left(st.last_error, 120) FROM drives d LEFT JOIN sync_state st ON st.drive_id = d.id WHERE d.enabled ORDER BY d.name")
        show("Queue (kind, state, jobs, oldest ready)",
             "SELECT kind, state, count(*), min(run_at) FILTER (WHERE state = 'queued' AND run_at <= now()) "
             "FROM jobs WHERE state IN ('queued', 'running') OR finished_at > now() - interval '1 day' "
             "GROUP BY kind, state ORDER BY kind, state")
        show("Files by status and permission state",
             "SELECT status, acl_status, count(*) FROM sources WHERE status <> 'folder' GROUP BY 1, 2 ORDER BY 1, 2")
        show("Generations", "SELECT id, status, model, dimensions FROM index_generations ORDER BY id")
        show("Partly indexed files (cut off at an extraction limit)",
             "SELECT name, left(status_note, 120) FROM sources WHERE status = 'indexed' AND status_note LIKE 'partly indexed:%' "
             "ORDER BY name LIMIT 20")
        show("Recent file errors", "SELECT name, attempts, left(status_note, 120) FROM sources WHERE status = 'error' "
             "ORDER BY indexed_at DESC NULLS LAST LIMIT 10")
        show("Failed jobs (last day)", "SELECT kind, payload, left(last_error, 120) FROM jobs WHERE state = 'failed' "
             "AND finished_at > now() - interval '1 day' ORDER BY finished_at DESC LIMIT 10")
        show("Counters (today)", "SELECT name, value FROM metrics WHERE day = current_date ORDER BY name")


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    parser = argparse.ArgumentParser(prog="python -m sync_worker")
    sub = parser.add_subparsers(dest="cmd")
    sub.add_parser("run")
    sites = sub.add_parser("sites")
    sites.add_argument("action", choices=["add", "disable", "list"])
    sites.add_argument("site", nargs="?")
    sub.add_parser("status")
    args = parser.parse_args()
    if args.cmd == "sites":
        if args.action != "list" and not args.site:
            parser.error("sites add/disable need a site")
        asyncio.run(_sites(args))
    elif args.cmd == "status":
        _status()
    else:
        asyncio.run(worker.main())


if __name__ == "__main__":
    main()
