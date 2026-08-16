FROM python:3.11-slim

WORKDIR /app

COPY embedding_service/requirements.txt /tmp/embedding-requirements.txt

RUN pip install --no-cache-dir -r /tmp/embedding-requirements.txt \
    && pip install --no-cache-dir torch --index-url https://download.pytorch.org/whl/cpu

COPY embedding_service /app/embedding_service

ENV PYTHONUNBUFFERED=1
ENV EMBEDDING_SERVICE_PORT=6003

CMD ["python", "-m", "embedding_service.server"]
