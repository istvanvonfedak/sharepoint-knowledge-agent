# Build from the repo root: docker build -f apps/inference-server/Dockerfile .
FROM python:3.13-slim

WORKDIR /app
COPY packages/rag packages/rag
COPY apps/inference-server apps/inference-server
RUN pip install --no-cache-dir ./packages/rag && pip install --no-cache-dir ./apps/inference-server

RUN useradd --system --no-create-home app
USER app

ENV INFERENCE_BIND_HOST=0.0.0.0
CMD ["python", "-m", "inference_server"]
