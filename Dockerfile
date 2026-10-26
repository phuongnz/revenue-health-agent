# The submission image (build contract §4). Nothing about the host is baked in: credentials
# and service addresses come from the environment at `docker run`.
#
#   docker build -t revenue-health-agent .
#   docker run --rm -p 8000:8000 -e SAMPLE_MODE=true revenue-health-agent      # no keys, no network
#   docker run --rm -p 8000:8000 --env-file .env \
#       -e METERLESS_BASE_URL=http://host.docker.internal:8088/v1 \
#       -e ZETARIS_REST_URL=http://host.docker.internal:8888/api/v1.0 revenue-health-agent
#
# host.docker.internal is Docker Desktop's name for the host; on Linux add
# `--add-host=host.docker.internal:host-gateway`, or give the gateways' real addresses.
FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PORT=8000 \
    LOG_DIR=/app/logs

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY app ./app
COPY run.py .
COPY data/sample ./data/sample
RUN mkdir -p /app/logs && python -m compileall -q app run.py

EXPOSE 8000
CMD ["python", "run.py"]
