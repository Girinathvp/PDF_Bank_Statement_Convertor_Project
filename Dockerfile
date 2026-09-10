FROM python:3.11-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
    tesseract-ocr \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY backend/requirements.txt backend/requirements.txt
RUN pip install --no-cache-dir -r backend/requirements.txt

COPY backend/ backend/
COPY frontend/ frontend/

WORKDIR /app/backend

ENV PORT=5000
EXPOSE 5000

CMD gunicorn -w 1 --threads 4 --timeout 300 -b 0.0.0.0:$PORT app:app
