# Build from the repo root: docker build -f apps/mcp-server/Dockerfile .
FROM python:3.13-slim

WORKDIR /app
COPY packages/sharepoint packages/sharepoint
COPY packages/rag packages/rag
COPY apps/mcp-server apps/mcp-server
# apps depend on packages/ by bare name, so install the packages first.
RUN pip install --no-cache-dir ./packages/sharepoint ./packages/rag && pip install --no-cache-dir ./apps/mcp-server

RUN useradd --system --no-create-home app
USER app

CMD ["python", "-m", "mcp_server"]
