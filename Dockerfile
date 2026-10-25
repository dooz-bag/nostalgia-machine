# Scheduler + traffic engine.
#
# FFmpeg is installed here rather than in the media layer because the scheduler
# probes durations and renders bump cards. ErsatzTV Next ships its own ffmpeg.
FROM python:3.12-slim AS base

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        ffmpeg \
        fonts-liberation \
        tzdata \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY pyproject.toml README.md ./
COPY src ./src

RUN pip install --no-cache-dir ".[web]"

# Media and generated state are mounted, never baked in.
VOLUME ["/app/config", "/app/media", "/app/data", "/app/playout", "/app/xmltv"]

EXPOSE 8099

# Default: keep the channel's playout rolling. `docker compose run scheduler
# nostalgia <cmd>` for one-off commands.
ENTRYPOINT ["python", "-m", "nostalgia.cli"]
CMD ["--root", "/app", "emit"]
