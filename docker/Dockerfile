# syntax=docker/dockerfile:1

# ---- Stage 1: build the React UI ----
# Static output is the same on every platform, so build it natively (no QEMU).
FROM --platform=$BUILDPLATFORM node:24-alpine AS ui
WORKDIR /ui
COPY frontend/package.json ./
# Use the lockfile when present (npm ci), otherwise fall back to npm install.
COPY frontend/package-lock.json* ./
RUN if [ -f package-lock.json ]; then npm ci; else npm install; fi
COPY frontend/ ./
RUN npm run build

# ---- Stage 2: runtime ----
# Floating minor tag: each build gets the newest 3.14 patch and Debian security fixes.
FROM python:3.14-slim AS runtime

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
# Dependencies first, in their own layer: constraints.txt is exactly the pinned runtime
# set (P10-05, `make lock`), so this layer stays cached until a dependency changes. Code
# changes then rebuild only the small layer below, which matters most for the arm64 image
# (built under QEMU, where installing every dependency takes minutes).
# The list comes from pyproject.toml and the pins from constraints.txt as constraints (-c),
# so pins that don't apply to this Python (e.g. audioop-lts, only needed on 3.13+) are ignored.
COPY backend/pyproject.toml backend/constraints.txt ./
RUN python -c "import tomllib; print('\n'.join(tomllib.load(open('pyproject.toml', 'rb'))['project']['dependencies']))" \
        > /tmp/requirements.txt \
    && pip install --no-cache-dir -r /tmp/requirements.txt -c constraints.txt \
    && rm /tmp/requirements.txt
COPY backend/woosarr ./woosarr
# pip is only needed to build the image. Removing it also removes its bundled copies of
# certifi, urllib3, msgpack and pkg_resources, which Trivy reported as vulnerable even though
# WooSarr never runs them (it uses its own, up-to-date packages).
RUN pip install --no-cache-dir --no-deps . && pip check && pip uninstall -y pip

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
