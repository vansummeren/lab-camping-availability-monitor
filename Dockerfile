FROM mcr.microsoft.com/playwright/python:v1.45.0-jammy

WORKDIR /app
COPY watcher.py .

# state + discovery dumps live here; mount a volume
VOLUME /data

ENV STATE_FILE=/data/state.json \
    DISCOVERY_DIR=/data/discovery \
    CHECK_INTERVAL=300

CMD ["python", "watcher.py"]
