FROM docker.io/library/python:3.13-slim-bookworm

ARG CHUNKER_VERSION=1.17.0
ARG CHUNKER_RELEASE_DATE=unknown
ARG IMAGE_BUILD_DATE=unknown
ARG TARGETARCH

ENV PYTHONUNBUFFERED=1 \
    HOST=0.0.0.0 \
    PORT=8080 \
    CHUNKER_BIN=/opt/chunker-cli/bin/chunker-cli \
    CHUNKER_VERSION=${CHUNKER_VERSION} \
    CHUNKER_RELEASE_DATE=${CHUNKER_RELEASE_DATE} \
    IMAGE_BUILD_DATE=${IMAGE_BUILD_DATE} \
    CHUNKER_OUTPUT_FORMAT=BEDROCK_R26_20 \
    MAX_UPLOAD_BYTES=536870912 \
    CHUNKER_TIMEOUT_SECONDS=1200 \
    CLEANUP_SUCCESSFUL_JOBS=true \
    FAILED_JOB_RETENTION_SECONDS=86400 \
    ASYNC_JOB_RETENTION_SECONDS=3600

RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates curl unzip \
    && rm -rf /var/lib/apt/lists/*

RUN set -eux; \
    case "${TARGETARCH:-amd64}" in \
      amd64) chunker_asset="chunker-cli-linux-amd64.zip" ;; \
      *) echo "Unsupported architecture for Chunker native CLI: ${TARGETARCH:-unknown}" >&2; exit 1 ;; \
    esac; \
    curl -fsSL "https://github.com/HiveGamesOSS/Chunker/releases/download/${CHUNKER_VERSION}/${chunker_asset}" -o /tmp/chunker-cli.zip; \
    unzip -q /tmp/chunker-cli.zip -d /opt; \
    chmod +x /opt/chunker-cli/bin/chunker-cli; \
    /opt/chunker-cli/bin/chunker-cli --version; \
    rm -f /tmp/chunker-cli.zip

WORKDIR /app
COPY web_converter.py /app/web_converter.py

RUN mkdir -p /app/conversions

EXPOSE 8080
VOLUME ["/app/conversions"]

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/healthz', timeout=3).read()"

CMD ["python", "/app/web_converter.py"]
