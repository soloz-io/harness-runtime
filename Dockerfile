FROM python:3.11-slim

# UV_HTTP_TIMEOUT: large wheels (wasmtime, ~8 MiB) outlast uv's 30s default on
# a slow link. UV_LINK_MODE: packages are copied out of the cache mount below,
# which is a different filesystem from the install target.
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    UV_HTTP_TIMEOUT=300 \
    UV_LINK_MODE=copy

WORKDIR /app

# Downloads are kept in BuildKit cache mounts (apt's packages and lists, uv's
# wheels), on this machine and outside the image: a rebuilt layer, or a cleared
# layer cache, fetches only what is new. The slim image deletes apt's downloads
# after every install; keep them, so the mount has something to keep.
RUN rm -f /etc/apt/apt.conf.d/docker-clean \
    && echo 'Binary::apt::APT::Keep-Downloaded-Packages "true";' > /etc/apt/apt.conf.d/keep-cache

RUN --mount=type=cache,target=/root/.cache/pip \
    env -u PIP_NO_CACHE_DIR pip install uv

# System packages the runtime needs, in their own layer: never rebuilt for a
# change to the dependencies or the code.
RUN --mount=type=cache,target=/var/cache/apt,sharing=locked \
    --mount=type=cache,target=/var/lib/apt,sharing=locked \
    apt-get update && apt-get install -y --no-install-recommends \
    libpq5 \
    git \
    nodejs \
    npm \
    redis-server

# Python dependencies, exactly as uv.lock pins them -- the versions the tests
# run against -- with their hashes checked. Rebuilt only when pyproject.toml or
# uv.lock changes. `--locked` fails the build if the lock no longer matches
# pyproject.toml, rather than shipping versions nothing tested. The compiler and
# headers some wheels build with are installed and removed in this same layer,
# so they never reach the image.
COPY pyproject.toml README.md uv.lock ./
RUN --mount=type=cache,target=/var/cache/apt,sharing=locked \
    --mount=type=cache,target=/var/lib/apt,sharing=locked \
    --mount=type=cache,target=/root/.cache/uv \
    { apt-get install -y --no-install-recommends gcc libpq-dev \
      || { apt-get update && apt-get install -y --no-install-recommends gcc libpq-dev; }; } \
    && uv export --locked --no-dev --no-emit-project --format requirements-txt -o /tmp/requirements.txt \
    && uv pip install --system --require-hashes -r /tmp/requirements.txt \
    && rm /tmp/requirements.txt \
    && apt-get purge -y --auto-remove gcc libpq-dev

# Then the code, installed as the package itself: its dependencies are in place.
COPY __init__.py cli.py ./
COPY core/ ./core/
COPY models/ ./models/
COPY api/ ./api/
COPY scripts/ ./scripts/
RUN --mount=type=cache,target=/root/.cache/uv \
    uv pip install --system --no-deps -e "."

RUN useradd -r appuser && chown -R appuser:appuser /app && \
    chmod +x /app/scripts/ci/*.sh

USER appuser

ENTRYPOINT ["/app/scripts/ci/run.sh"]
