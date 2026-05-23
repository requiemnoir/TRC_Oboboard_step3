# mirror_logger — immagine per sviluppo Mac / CI (FakeCapture, no Kvaser)
FROM python:3.12-slim-bookworm

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

COPY mirror_logger/requirements.txt /tmp/requirements.txt
RUN pip install --upgrade pip wheel \
    && pip install -r /tmp/requirements.txt

COPY mirror_logger/ /app/

RUN mkdir -p logs config \
    && if [ ! -f config/.token ]; then python -c "import secrets; print(secrets.token_urlsafe(32))" > config/.token; fi

EXPOSE 5050

HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:5050/api/health', timeout=3)"

CMD ["python", "app.py"]
