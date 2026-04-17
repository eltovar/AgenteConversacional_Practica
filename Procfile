web: gunicorn app:app -k uvicorn.workers.UvicornWorker --workers 2 --bind 0.0.0.0:$PORT --timeout 120 --keep-alive 25 --max-requests 500 --max-requests-jitter 50
