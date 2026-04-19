FROM python:3.12-slim

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    gcc \
    ffmpeg \
    libopus0 \
    libopus-dev \
    libjemalloc2 \
    && rm -rf /var/lib/apt/lists/*

# jemalloc: reemplaza malloc de glibc para devolver memoria al OS agresivamente.
# Python pymalloc asigna arenas que glibc NUNCA devuelve; jemalloc sí lo hace
# via madvise(MADV_DONTNEED). Reduce RSS real 50-70% vs glibc malloc.
ENV LD_PRELOAD=/usr/lib/x86_64-linux-gnu/libjemalloc.so.2

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

EXPOSE 8000

# Shell form to expand $PORT at runtime (Railway sets PORT env var)
# --timeout 30: mata worker rápido si el watchdog envía SIGTERM
# --graceful-timeout 15: fuerza SIGKILL si no sale en 15s post-SIGTERM
CMD ["sh", "-c", "gunicorn app:app --worker-class uvicorn.workers.UvicornWorker --workers 1 --bind 0.0.0.0:${PORT:-8000} --timeout 30 --graceful-timeout 15 --keep-alive 25 --max-requests 500 --max-requests-jitter 50"]
