# Codebase Pilot API + worker image (G5). Needs git and Node because Pilot runs the scanned repository's tests.
FROM python:3.12-slim

RUN apt-get update \
 && apt-get install -y --no-install-recommends git curl ca-certificates \
 && curl -fsSL https://deb.nodesource.com/setup_20.x | bash - \
 && apt-get install -y --no-install-recommends nodejs \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app/backend
COPY backend/requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt azure-identity==1.19.0

COPY backend/ ./
ENV PYTHONUNBUFFERED=1 PYTHONUTF8=1 PILOT_EMBEDDED_WORKER=1 CODEBASE_PILOT_DB=/data/codebase_pilot.db \
    CODEBASE_PILOT_WORKSPACE=/data/workspace CHECKPOINT_DB=/data/checkpoints.db REPORTS_DIR=/data/reports
VOLUME /data
EXPOSE 8000
CMD ["uvicorn", "app.api.main:app", "--host", "0.0.0.0", "--port", "8000"]
