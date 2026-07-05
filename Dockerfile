FROM python:3.12-slim-bookworm

WORKDIR /app

# Playwright explizit installieren + Chromium samt System-Dependencies,
# damit der Build unabhängig vom Basis-Image immer funktioniert.
RUN pip install --no-cache-dir playwright==1.53.0 \
 && playwright install --with-deps chromium \
 && rm -rf /var/lib/apt/lists/*

COPY watcher.py .

# state + discovery dumps live here; mount a volume
VOLUME /data

ENV STATE_FILE=/data/state.json \
    DISCOVERY_DIR=/data/discovery \
    CHECK_INTERVAL=600

CMD ["python", "watcher.py"]
