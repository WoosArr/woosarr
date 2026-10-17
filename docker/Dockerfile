# syntax=docker/dockerfile:1

# ---- Stage 1: build the React UI ----
# Static output is the same on every platform, so build it natively (no QEMU).
FROM --platform=$BUILDPLATFORM node:22-alpine AS ui
WORKDIR /ui
COPY frontend/package.json ./
# Use the lockfile when present (npm ci), otherwise fall back to npm install.
COPY frontend/package-lock.json* ./
RUN if [ -f package-lock.json ]; then npm ci; else npm install; fi
COPY frontend/ ./
RUN npm run build

# ---- Stage 2: runtime ----
FROM python:3.12-slim AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    WOOSARR_CONFIG_DIR=/config \
    WOOSARR_STATIC_DIR=/app/static \
    WOOSARR_PORT=31337

RUN apt-get update \
    && apt-get install -y --no-install-recommends gosu \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd -g 99 woosarr \
    && useradd -u 99 -g 99 -d /app -s /usr/sbin/nologin woosarr

WORKDIR /app
COPY backend/pyproject.toml backend/constraints.txt ./
COPY backend/woosarr ./woosarr
# Pinned versions (P10-05); see `make lock`.
RUN pip install --no-cache-dir -c constraints.txt .

COPY --from=ui /ui/dist ./static
COPY docker/entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh && mkdir -p /config && chown woosarr:woosarr /config

VOLUME ["/config"]
EXPOSE 31337

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD python -c "import os,sys,urllib.request; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:%s/api/health' % os.environ.get('WOOSARR_PORT', '31337'), timeout=4).status == 200 else 1)"

ENTRYPOINT ["/entrypoint.sh"]
# Host/port come from WOOSARR_HOST / WOOSARR_PORT (core/config.py).
CMD ["python", "-m", "woosarr"]
