FROM python:3.11-slim

# System dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    supervisor \
    curl \
    && rm -rf /var/lib/apt/lists/*

# supercronic v0.2.44 — lightweight cron daemon, no root needed
RUN curl -fsSL \
    https://github.com/aptible/supercronic/releases/download/v0.2.44/supercronic-linux-amd64 \
    -o /usr/local/bin/supercronic \
    && chmod +x /usr/local/bin/supercronic

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
