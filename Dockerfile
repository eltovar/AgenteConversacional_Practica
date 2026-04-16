FROM python:3.12-slim

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    gcc \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

EXPOSE 8000

# --max-requests 500: cada worker se reinicia después de 500 requests (anti-leak de emergencia).
# Si quedan pools Redis huérfanas no atrapadas por los fixes, el worker se recicla limpiamente
# sin downtime. --max-requests-jitter 50 evita que todos los workers reinicien al mismo tiempo.
CMD ["gunicorn", "app:app", \
     "--worker-class", "uvicorn.workers.UvicornWorker", \
     "--workers", "2", \
     "--bind", "0.0.0.0:8000", \
     "--timeout", "120", \
     "--keep-alive", "25", \
     "--max-requests", "500", \
     "--max-requests-jitter", "50"]
