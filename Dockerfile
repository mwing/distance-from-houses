FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    HOUSEHUNT_HOST=0.0.0.0 \
    HOUSEHUNT_PORT=8000 \
    HOUSEHUNT_DATA_DIR=/data

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY househunt ./househunt

RUN useradd --system --uid 10001 --home /app househunt \
    && mkdir -p /data && chown househunt /data
USER househunt
VOLUME /data
EXPOSE 8000

HEALTHCHECK --interval=60s --timeout=5s CMD python -c "import os, urllib.request; urllib.request.urlopen('http://127.0.0.1:%s/api/session' % os.environ.get('HOUSEHUNT_PORT', '8000'))"

CMD ["python", "-m", "househunt", "web"]
