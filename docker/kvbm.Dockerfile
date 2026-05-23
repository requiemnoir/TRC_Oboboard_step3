# Kvaser Bus Manager — sviluppo Mac (mock CAN, avvio rapido)
FROM python:3.12-slim-bookworm

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PYTHONPATH=/app \
    KBSM_AUTOSTART=0 \
    KBSM_AUTO_START_LOGGING=0 \
    KBSM_VEHICLE_MODE=0 \
    COPILOT_PROVIDER=none

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        gcc g++ libffi-dev libxml2-dev libxslt1-dev curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app/kvaser_bus_manager

COPY docker/kvbm-requirements-mac.txt /tmp/requirements.txt
RUN pip install --upgrade pip wheel \
    && pip install -r /tmp/requirements.txt

COPY mf4_standalone_decoder/ /app/mf4_standalone_decoder/
COPY kvaser_bus_manager/backend/ /app/kvaser_bus_manager/backend/
COPY kvaser_bus_manager/frontend/ /app/kvaser_bus_manager/frontend/
COPY kvaser_bus_manager/config/ /app/kvaser_bus_manager/config/
COPY databases/ /app/databases/

RUN mkdir -p logs projects/pdx \
    && cp config/app_config.example.json config/app_config.json

EXPOSE 5000

HEALTHCHECK --interval=20s --timeout=10s --start-period=300s --retries=8 \
    CMD curl -fsS http://127.0.0.1:5000/api/health || exit 1

CMD ["python", "backend/app.py"]
