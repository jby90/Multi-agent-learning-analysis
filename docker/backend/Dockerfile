FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

COPY source_package/requirements.txt /tmp/requirements.txt
RUN python -m pip install --no-cache-dir --requirement /tmp/requirements.txt \
    && rm /tmp/requirements.txt

COPY source_package/ /app/
COPY docker/backend/entrypoint.sh /usr/local/bin/multiagent-backend

RUN useradd --create-home --uid 10001 appuser \
    && mkdir -p /app/runtime \
    && chown -R appuser:appuser /app/runtime \
    && chmod 0555 /usr/local/bin/multiagent-backend

USER appuser
EXPOSE 8765
ENTRYPOINT ["/usr/local/bin/multiagent-backend"]
