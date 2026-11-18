"""Daily counters in Postgres (graph_429, layer2_checked, layer2_dropped, agent_tokens, ...), read by
`python -m sync_worker status`. Best effort: a failed increment never fails the caller's request."""
import logging

import psycopg

log = logging.getLogger(__name__)


async def incr(conn: psycopg.AsyncConnection, name: str, by: int = 1) -> None:
    if by == 0:
        return
    try:
        await conn.execute(
            "INSERT INTO metrics (name, value) VALUES (%s, %s) "
            "ON CONFLICT (name, day) DO UPDATE SET value = metrics.value + EXCLUDED.value",
            (name, by),
        )
    except psycopg.Error as e:
        log.warning("metric %s not recorded: %s", name, e)
