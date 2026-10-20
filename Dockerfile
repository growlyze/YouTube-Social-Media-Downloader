FROM python:3.13-slim-bookworm

# FFmpeg merges YouTube's separate video/audio streams; Node.js solves YouTube's
# JavaScript challenges for yt-dlp.
RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg libstdc++6 \
    && rm -rf /var/lib/apt/lists/*
COPY --from=node:22-bookworm-slim /usr/local/bin/node /usr/local/bin/node

WORKDIR /app
COPY requirements.txt .
RUN grep -v '^pytest' requirements.txt > /tmp/requirements.txt \
    && pip install --no-cache-dir -r /tmp/requirements.txt

COPY . .
RUN useradd --create-home --uid 1000 app \
    && mkdir -p /data /downloads \
    && chown app:app /data /downloads
USER app

# YTDL_HOST=0.0.0.0 is needed inside the container; docker-compose.yml publishes
# the port on 127.0.0.1 only, so the app is still reachable from this PC alone.
ENV PYTHONUNBUFFERED=1 \
    YTDL_IN_DOCKER=1 \
    YTDL_HOST=0.0.0.0 \
    YTDL_PORT=5000 \
    YTDL_DATA_DIR=/data \
    YTDL_DOWNLOADS_DIR=/downloads \
    YTDL_CREDENTIALS_FILE=/data/google-token.json

VOLUME ["/data", "/downloads"]
EXPOSE 5000
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s \
    CMD python -c "import os, urllib.request; urllib.request.urlopen(f'http://127.0.0.1:{os.environ[\"YTDL_PORT\"]}/', timeout=4)"

CMD ["python", "app.py"]
