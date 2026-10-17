# syntax=docker/dockerfile:1

# ---- Stage 1: build the React UI ----
FROM node:22-alpine AS ui
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
    WOOSARR_PORT=2000

RUN apt-get update \
    && apt-get install -y --no-install-recommends gosu \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd -g 99 woosarr \
    && useradd -u 99 -g 99 -d /app -s /usr/sbin/nologin woosarr

WORKDIR /app
COPY backend/pyproject.toml ./pyproject.toml
COPY backend/woosarr ./woosarr
RUN pip install --no-cache-dir .

COPY --from=ui /ui/dist ./static
COPY docker/entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh && mkdir -p /config && chown woosarr:woosarr /config

VOLUME ["/config"]
EXPOSE 2000

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:2000/api/health', timeout=4).status == 200 else 1)"

ENTRYPOINT ["/entrypoint.sh"]
CMD ["python", "-m", "uvicorn", "woosarr.main:app", "--host", "0.0.0.0", "--port", "2000"]
