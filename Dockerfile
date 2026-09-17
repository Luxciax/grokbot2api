# grokbot2api — stdlib-only Cursor sand InferenceService proxy
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    GROK_BUILD_PROXY_API_KEY="" \
    SAND_INFERENCE_RENEWAL_CREDENTIAL=""

WORKDIR /app

# Copy only what the proxy needs at runtime.
COPY grokbot2api.py sand_inference.py api_common.py responses_api.py messages_api.py model_catalogue.py ./
COPY config.example.toml ./

RUN useradd --create-home --uid 10001 appuser \
    && mkdir -p /data \
    && chown -R appuser:appuser /app /data

USER appuser

EXPOSE 8765

# Bind 0.0.0.0 inside the container; require API key via env (enforced by CLI for non-loopback).
# Persist admin_config.json on /data.
VOLUME ["/data"]

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
  CMD python3 -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8765/health', timeout=3).read()"

ENTRYPOINT ["python3", "grokbot2api.py"]
CMD ["--listen", "0.0.0.0", "--port", "8765", "--admin-config", "/data/admin_config.json", "--api-key-env", "GROK_BUILD_PROXY_API_KEY"]
