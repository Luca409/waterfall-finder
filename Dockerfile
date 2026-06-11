FROM python:3.12-slim-bookworm

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
    gdal-bin \
    libgdal-dev \
    g++ \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY server.py analytics.py ./
COPY scripts/warm-cache.py scripts/build_cache.py ./
COPY data/cache/ data/cache/

ENV PYTHONUNBUFFERED=1

EXPOSE 8080

# One worker on 2 GB RAM; searches run in background threads.
CMD ["gunicorn", "--bind", "0.0.0.0:8080", "--workers", "1", "--timeout", "300", "--access-logfile", "-", "server:app"]
