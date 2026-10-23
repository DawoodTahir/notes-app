# Gist — single-container deployment.
#
# One process, one replica. That is not a simplification to be fixed later:
# sessions live in memory and the job pool runs in-process, so a second
# replica would serve half the polls from a worker that never ran the job.
# Scaling out means moving both out first.

FROM python:3.13-slim

# ffmpeg is not optional: audio is downloaded and compressed before it goes to
# speech-to-text, and without the compression step a long video exceeds the
# provider's upload limit. gosu drops us to a non-root user at the last moment,
# after the mounted disk has been chowned.
RUN apt-get update \
 && apt-get install -y --no-install-recommends ffmpeg gosu ca-certificates curl \
 && rm -rf /var/lib/apt/lists/*

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    DATA_DIR=/var/data \
    COOKIE_SECURE=1

WORKDIR /srv

# Requirements first so edits to the app do not reinstall the world.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app/ ./app/
COPY docker-entrypoint.sh /usr/local/bin/
RUN chmod +x /usr/local/bin/docker-entrypoint.sh \
 && useradd --system --uid 10001 --create-home --shell /usr/sbin/nologin app

# Render injects PORT; 8000 is only the local default.
ENV PORT=8000
EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD curl -fsS "http://127.0.0.1:${PORT}/api/health" || exit 1

ENTRYPOINT ["docker-entrypoint.sh"]
