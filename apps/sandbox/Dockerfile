# Build from the repo root: docker build -f apps/sandbox/Dockerfile .
# nsjail isn't packaged in Debian 13: built from a pinned release.
FROM debian:13-slim AS nsjail
RUN apt-get update && apt-get install -y --no-install-recommends ca-certificates git make g++ pkg-config bison flex \
        libprotobuf-dev protobuf-compiler libnl-route-3-dev \
    && rm -rf /var/lib/apt/lists/*
RUN git clone --depth 1 --branch 3.4 https://github.com/google/nsjail /nsjail && make -C /nsjail -j4

FROM python:3.13-slim
# What commands get: bash, coreutils, grep, sed, awk and python3 (standard library) come with the base image.
RUN apt-get update && apt-get install -y --no-install-recommends ripgrep libprotobuf32t64 libnl-route-3-200 \
    && rm -rf /var/lib/apt/lists/*
COPY --from=nsjail /nsjail/nsjail /usr/local/bin/nsjail
COPY apps/sandbox/bin/section /usr/local/bin/section

WORKDIR /app
COPY apps/sandbox apps/sandbox
RUN pip install --no-cache-dir ./apps/sandbox
# The service runs as root inside its gVisor sandbox: it mounts each session's tmpfs and starts nsjail, which drops
# every command to the session's own uid. Commands never run as root.
ENV SANDBOX_BIND_HOST=0.0.0.0
CMD ["python", "-m", "sandbox_server"]
