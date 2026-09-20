FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# wildlife.py and the scrub-cache generator both shell out to ffmpeg/ffprobe;
# the image was missing it (confirmed live 2026-07-30, M7 in
# docs/scrub-cache-and-proxy-spec.md) -- this was already broken for
# wildlife.py, not just a new requirement for scrub-cache.
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg \
    && rm -rf /var/lib/apt/lists/*

COPY pyproject.toml README.md ./
COPY src/ ./src/

# http2: one long-lived HTTP/2 connection to the push relay instead of the
# HTTP/1.1 keep-alive fallback (push/transport.py logs a warning without it).
RUN pip install '.[http2]'

ENV MARCELLUS_CONFIG=/etc/marcellus/sidecar.yml

# Drop root: nothing here needs it. The inputs are bind-mounted read-only and
# /data is the only thing written, so the compose file's data dir must be owned
# by (or group-writable for) this uid.
RUN useradd --system --uid 10001 --create-home sidecar \
    && mkdir -p /data \
    && chown -R sidecar:sidecar /data
USER sidecar

EXPOSE 5001

HEALTHCHECK --interval=30s --timeout=10s --start-period=20s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:5001/healthz', timeout=5)"

ENTRYPOINT ["python", "-m", "marcellus"]
CMD ["serve"]
