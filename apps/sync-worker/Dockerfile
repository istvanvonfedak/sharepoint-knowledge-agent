# Build from the repo root: docker build -f apps/sync-worker/Dockerfile .
# Note what is *not* copied: the mcp-server and its Entra client (packages/sharepoint) and credentials.
FROM python:3.13-slim

WORKDIR /app
COPY packages/rag packages/rag
COPY apps/sync-worker apps/sync-worker
RUN pip install --no-cache-dir ./packages/rag && pip install --no-cache-dir ./apps/sync-worker

RUN useradd --system --no-create-home app
USER app

CMD ["python", "-m", "sync_worker", "run"]
