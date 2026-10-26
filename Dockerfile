# The submission image (build contract §4). Credentials are never baked in: pass them with
# `docker run -e ZETARIS_API_KEY -e METERLESS_RUNTIME_TOKEN ...` from the environment.
# From inside Docker the Nightshift gateway on the host is host.docker.internal.
FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PORT=8000 \
    LOG_DIR=/app/logs \
    METERLESS_BASE_URL=http://host.docker.internal:8088/v1

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY app ./app
COPY run.py .
RUN mkdir -p /app/logs && python -m compileall -q app run.py

EXPOSE 8000
CMD ["python", "run.py"]
