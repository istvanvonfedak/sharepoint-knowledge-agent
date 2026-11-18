"""Per-user rate limits and the audit log, both in Postgres so every replica shares them."""
import hashlib
import hmac
import logging
from datetime import datetime, timezone

from psycopg.types.json import Jsonb
from psycopg_pool import AsyncConnectionPool

log = logging.getLogger(__name__)


class RateLimited(Exception):
    pass


def _window_start(window_seconds: int) -> datetime:
    now = datetime.now(timezone.utc).timestamp()
    return datetime.fromtimestamp(now - now % window_seconds, timezone.utc)


class Usage:
    def __init__(self, pool: AsyncConnectionPool, hmac_key: bytes) -> None:
        self._pool = pool
        self._key = hmac_key

    async def hit(self, user_id: str, bucket: str, limit: int, window_seconds: int) -> None:
        """Counts one call; raises RateLimited once `limit` calls were made in the current window."""
        async with self._pool.connection() as conn:
            row = await (await conn.execute(
                "INSERT INTO usage_limits (user_id, bucket, window_start, count) VALUES (%s, %s, %s, 1) "
                "ON CONFLICT (user_id, bucket, window_start) DO UPDATE SET count = usage_limits.count + 1 RETURNING count",
                (user_id, bucket, _window_start(window_seconds)),
            )).fetchone()
        if row[0] > limit:
            raise RateLimited(f"Rate limit reached for {bucket} ({limit} per {window_seconds // 60} min). Try again later.")

    async def tokens_used(self, user_id: str, bucket: str, window_seconds: int) -> int:
        async with self._pool.connection() as conn:
            row = await (await conn.execute(
                "SELECT tokens FROM usage_limits WHERE user_id = %s AND bucket = %s AND window_start = %s",
                (user_id, bucket, _window_start(window_seconds)),
            )).fetchone()
        return row[0] if row else 0

    async def add_tokens(self, user_id: str, bucket: str, window_seconds: int, tokens: int) -> None:
        async with self._pool.connection() as conn:
            await conn.execute(
                "INSERT INTO usage_limits (user_id, bucket, window_start, tokens) VALUES (%s, %s, %s, %s) "
                "ON CONFLICT (user_id, bucket, window_start) DO UPDATE SET tokens = usage_limits.tokens + EXCLUDED.tokens",
                (user_id, bucket, _window_start(window_seconds), tokens),
            )

    def query_hmac(self, query: str) -> str:
        # Keyed: a plain hash of a short query could be reversed by guessing.
        return hmac.new(self._key, query.encode(), hashlib.sha256).hexdigest()

    async def audit(
        self, user_id: str | None, action: str, *, query: str | None = None, source_ids: list[int] | None = None,
        tokens: int = 0, detail: dict | None = None,
    ) -> None:
        """Best effort: never fails the user's request, but logs loudly if the record is lost."""
        try:
            async with self._pool.connection() as conn:
                await conn.execute(
                    "INSERT INTO audit_log (user_id, action, query_hmac, source_ids, tokens_used, detail) VALUES (%s, %s, %s, %s, %s, %s)",
                    (user_id, action, self.query_hmac(query) if query is not None else None, source_ids or [], tokens, Jsonb(detail or {})),
                )
        except Exception:
            log.exception("audit record lost: user=%s action=%s", user_id, action)

    async def prune(self, keep_days: int = 2) -> None:
        async with self._pool.connection() as conn:
            await conn.execute("DELETE FROM usage_limits WHERE window_start < now() - %s * interval '1 day'", (keep_days,))
