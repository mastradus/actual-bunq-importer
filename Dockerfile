FROM python:3.11-slim

# System dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    supervisor \
    curl \
    && rm -rf /var/lib/apt/lists/*

# supercronic v0.2.44 — lightweight cron daemon, no root needed.
# Picks the binary for the build platform (amd64 or arm64) and verifies
# it against the SHA1 sums published in the supercronic release notes.
ARG TARGETARCH
ARG SUPERCRONIC_VERSION=v0.2.44
RUN set -eu; \
    case "${TARGETARCH:-amd64}" in \
      amd64) arch=amd64; sha1=6eb0a8e1e6673675dc67668c1a9b6409f79c37bc ;; \
      arm64) arch=arm64; sha1=6c6cba4cde1dd4a1dd1e7fb23498cde1b57c226c ;; \
      *) echo "unsupported architecture: ${TARGETARCH} (amd64 and arm64 are supported)" >&2; exit 1 ;; \
    esac; \
    curl -fsSL -o /usr/local/bin/supercronic \
      "https://github.com/aptible/supercronic/releases/download/${SUPERCRONIC_VERSION}/supercronic-linux-${arch}"; \
    echo "${sha1}  /usr/local/bin/supercronic" | sha1sum -c -; \
    chmod +x /usr/local/bin/supercronic

WORKDIR /app

# Install Python dependencies first (layer cache)
COPY requirements.txt requirements-web.txt ./
RUN pip install --no-cache-dir -r requirements.txt -r requirements-web.txt

# Copy application source
COPY . .

# Pre-create runtime directories (volumes will overlay these if mounted)
RUN mkdir -p config data logs templates

EXPOSE 8080

CMD ["/usr/bin/supervisord", "-n", "-c", "/app/supervisord.conf"]
