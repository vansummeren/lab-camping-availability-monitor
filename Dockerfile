FROM python:3.12-slim-bookworm
WORKDIR /app
COPY watcher.py .
VOLUME /data
ENV STATE_FILE=/data/state.json \
    CHECK_INTERVAL=300
CMD ["python", "watcher.py"]
