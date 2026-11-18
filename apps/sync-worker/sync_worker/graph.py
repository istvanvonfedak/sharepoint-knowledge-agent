"""Microsoft Graph as the sync-worker's own app (client credentials, signed with a certificate).

All replicas share one throttle: when Graph answers 429/503 with Retry-After, the deadline is written to
Postgres (`graph_throttle`) and every replica waits it out before its next request.
"""
import asyncio
import logging
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from urllib.parse import quote, urlsplit

import httpx
import msal
from psycopg_pool import AsyncConnectionPool
from sharepoint_rag import metrics

from . import config

log = logging.getLogger(__name__)

GRAPH = "https://graph.microsoft.com/v1.0"
GRAPH_SCOPE = "https://graph.microsoft.com/.default"
MAX_ATTEMPTS = 6
MAX_RETRY_WAIT = 300.0
THROTTLE_CHECK_SECONDS = 2.0  # how often a replica re-reads the shared throttle
BATCH_SIZE = 20  # Graph JSON batching limit
_TRANSIENT = {429, 500, 502, 503, 504}


def seg(value: str) -> str:
    """Percent-encode an id for use as a single URL path segment."""
    return quote(value, safe="")


class FileTooLarge(Exception):
    """The download went over its byte limit: retrying gives the same result."""


class GraphError(Exception):
    def __init__(self, status: int, body: str) -> None:
        super().__init__(f"Graph {status}: {body[:500]}")
        self.status = status


@dataclass(frozen=True)
class BatchResult:
    status: int
    body: dict
    sent_at: datetime  # when the attempt that produced this answer was sent ("newest permissions win")


def _retry_after(headers, default: float) -> float:
    value = headers.get("Retry-After", "") if headers else ""
    return min(float(value), MAX_RETRY_WAIT) if value.isdigit() else default


class AppGraph:
    def __init__(self, pool: AsyncConnectionPool) -> None:
        self._pool = pool
        with open(config.CERT_PATH) as f:
            pem = f.read()
        if "PRIVATE KEY-----" not in pem or "BEGIN CERTIFICATE-----" not in pem:
            raise RuntimeError(f"{config.CERT_PATH} must hold both the private key and the certificate (see docs/setup.md)")
        self._msal = msal.ConfidentialClientApplication(
            # MSAL derives the SHA-256 thumbprint from the certificate, so the PEM file is all it needs.
            config.CLIENT_ID, client_credential={"private_key": pem, "public_certificate": pem},
            authority=f"https://login.microsoftonline.com/{config.TENANT_ID}",
        )
        self._http = httpx.AsyncClient(timeout=httpx.Timeout(60, connect=15), follow_redirects=True)
        self._slots = asyncio.Semaphore(config.GRAPH_CONCURRENCY)
        self._throttle_until = 0.0  # epoch seconds
        self._throttle_checked = 0.0

    async def close(self) -> None:
        await self._http.aclose()

    async def _token(self, scope: str = GRAPH_SCOPE) -> str:
        # MSAL caches the app token (per scope) in memory and only calls Entra when it is about to expire.
        result = await asyncio.to_thread(self._msal.acquire_token_for_client, [scope])
        if "access_token" not in result:
            raise RuntimeError(f"App-only token request failed: {result.get('error_description', result.get('error'))}")
        return result["access_token"]

    async def _wait_for_throttle(self) -> None:
        now = time.time()
        if now - self._throttle_checked > THROTTLE_CHECK_SECONDS:
            self._throttle_checked = now
            async with self._pool.connection() as conn:
                row = await (await conn.execute("SELECT extract(epoch FROM until) FROM graph_throttle WHERE id = 1")).fetchone()
            if row:
                self._throttle_until = max(self._throttle_until, float(row[0]))
        if (wait := self._throttle_until - time.time()) > 0:
            await asyncio.sleep(wait)

    async def _throttled(self, seconds: float) -> None:
        self._throttle_until = max(self._throttle_until, time.time() + seconds)
        until = datetime.now(timezone.utc) + timedelta(seconds=seconds)
        async with self._pool.connection() as conn:
            await conn.execute(
                "INSERT INTO graph_throttle (id, until) VALUES (1, %s) "
                "ON CONFLICT (id) DO UPDATE SET until = greatest(graph_throttle.until, EXCLUDED.until)",
                (until,),
            )
            await metrics.incr(conn, "graph_429")

    async def request(self, method: str, path: str, *, scope: str = GRAPH_SCOPE, **kwargs) -> httpx.Response:
        """Retries throttling and 5xx (honouring Retry-After); raises GraphError for any other error status."""
        url = path if path.startswith("https://") else f"{GRAPH}{path}"
        for attempt in range(MAX_ATTEMPTS):
            await self._wait_for_throttle()
            headers = {"Authorization": f"Bearer {await self._token(scope)}", **kwargs.pop("headers", {})}
            sent_at = datetime.now(timezone.utc)
            try:
                async with self._slots:
                    res = await self._http.request(method, url, headers=headers, **kwargs)
                res.sent_at = sent_at  # the successful attempt's send time, for permission reads
            except httpx.TransportError as e:
                if attempt == MAX_ATTEMPTS - 1:
                    raise
                log.info("Graph transport error (%s), retrying", e)
                await asyncio.sleep(2**attempt)
                kwargs["headers"] = {k: v for k, v in headers.items() if k != "Authorization"}
                continue
            if res.status_code in _TRANSIENT and attempt < MAX_ATTEMPTS - 1:
                wait = _retry_after(res.headers, 2.0**attempt)
                if res.status_code in (429, 503):
                    await self._throttled(wait)
                else:
                    await asyncio.sleep(wait)
                kwargs["headers"] = {k: v for k, v in headers.items() if k != "Authorization"}
                continue
            if res.is_error:
                raise GraphError(res.status_code, res.text)
            return res
        raise AssertionError("unreachable")

    async def get(self, path: str, **kwargs) -> dict:
        return (await self.request("GET", path, **kwargs)).json()

    async def get_all(self, path: str, **kwargs) -> list[dict]:
        """Every page. Unlike the user-facing client this never truncates: a partial permission or drive
        list would be wrong data, not a shorter answer."""
        return (await self.get_all_timed(path, **kwargs))[0]

    async def get_all_timed(self, path: str, **kwargs) -> tuple[list[dict], datetime]:
        """Every page, plus when the request that returned the first page was sent (retries included)."""
        res = await self.request("GET", path, **kwargs)
        sent_at, body = res.sent_at, res.json()
        items: list[dict] = body.get("value", [])
        while next_link := body.get("@odata.nextLink"):
            body = await self.get(next_link)
            items += body.get("value", [])
        return items, sent_at

    async def sharepoint_get_all(self, url: str) -> list[dict]:
        """Every page of a SharePoint REST collection (`https://<tenant>.sharepoint.com/.../_api/...`), with an
        app token for that host. SharePoint throttling pauses Graph too: they share the tenant's budget."""
        host = urlsplit(url).netloc
        scope = f"https://{host}/.default"
        headers = {"Accept": "application/json;odata=nometadata"}
        items: list[dict] = []
        while url:
            if urlsplit(url).netloc != host:  # never send this host's token anywhere else
                raise GraphError(502, f"SharePoint nextLink left {host}: {url}")
            body = (await self.request("GET", url, scope=scope, headers=dict(headers))).json()
            items += body.get("value", [])
            url = body.get("odata.nextLink")
        return items

    async def download(self, drive_id: str, item_id: str, max_bytes: int) -> bytes:
        """File bytes, refusing anything over max_bytes. Kept in memory only, never written to disk."""
        url = f"{GRAPH}/drives/{seg(drive_id)}/items/{seg(item_id)}/content"
        for attempt in range(MAX_ATTEMPTS):
            await self._wait_for_throttle()
            # httpx drops the Authorization header when Graph redirects to the pre-authenticated download host.
            headers = {"Authorization": f"Bearer {await self._token()}"}
            async with self._slots, self._http.stream("GET", url, headers=headers) as res:
                if res.status_code in _TRANSIENT and attempt < MAX_ATTEMPTS - 1:
                    wait = _retry_after(res.headers, 2.0**attempt)
                else:
                    if res.is_error:
                        raise GraphError(res.status_code, (await res.aread()).decode(errors="replace"))
                    data = bytearray()
                    async for part in res.aiter_bytes():
                        data += part
                        if len(data) > max_bytes:
                            raise FileTooLarge(f"file is larger than {max_bytes} bytes")
                    return bytes(data)
            if res.status_code in (429, 503):
                await self._throttled(wait)
            else:
                await asyncio.sleep(wait)
        raise AssertionError("unreachable")

    async def batch_get(self, paths: list[str]) -> list[BatchResult]:
        """GETs up to 20 paths in one $batch call, one result per path in order. Throttled, failed and missing
        sub-requests are retried (honouring Retry-After); after that any status is returned as is."""
        if len(paths) > BATCH_SIZE:
            raise ValueError(f"at most {BATCH_SIZE} requests per batch")
        if not paths:
            return []
        results: dict[int, BatchResult] = {}
        pending = list(range(len(paths)))
        for attempt in range(MAX_ATTEMPTS):
            body = {"requests": [{"id": str(i), "method": "GET", "url": paths[i]} for i in pending]}
            res = await self.request("POST", "/$batch", json=body)
            by_id = {int(r["id"]): r for r in res.json().get("responses", [])}
            retry, wait, throttled = [], 1.0, False
            for i in pending:
                r = by_id.get(i) or {}
                status = r.get("status", 0)
                if (status in _TRANSIENT or status == 0) and attempt < MAX_ATTEMPTS - 1:
                    retry.append(i)
                    wait = max(wait, _retry_after(r.get("headers", {}), 2.0**attempt))
                    throttled |= status in (429, 503)
                else:
                    results[i] = BatchResult(status, r.get("body") or {}, res.sent_at)
            if not retry:
                break
            if throttled:  # only real throttling pauses every replica; a flaky item just waits here
                await self._throttled(wait)
            else:
                await asyncio.sleep(wait)
            pending = retry
        return [results[i] for i in range(len(paths))]
