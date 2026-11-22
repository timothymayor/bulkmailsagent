# Bulk email agent: web UI (default) or the command-line sender.
#   docker compose up -d             -> http://127.0.0.1:8025
#   docker compose run --rm bulkmail python /app/bulkmail.py <recipients.csv> <template.txt> ...
FROM python:3.13-slim

# DejaVu fonts let the PDF report print non-English names (accents, rupee sign, etc.).
RUN apt-get update \
 && apt-get install -y --no-install-recommends fonts-dejavu-core \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY bulkmail.py server.py report.py ./
COPY web ./web

# Code lives in /app; settings (.env), templates, sent_log.csv and reports live in /data,
# which docker-compose mounts from the project folder so nothing is lost on rebuild.
ENV BULKMAIL_DATA_DIR=/data \
    BULKMAIL_HOST=0.0.0.0 \
    BULKMAIL_PORT=8025 \
    PYTHONUNBUFFERED=1 \
    PYTHONIOENCODING=utf-8
RUN useradd --create-home --uid 1000 bulkmail && mkdir /data && chown bulkmail /data
USER bulkmail
WORKDIR /data

EXPOSE 8025
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8025/api/status', timeout=4)"

CMD ["python", "/app/server.py"]
