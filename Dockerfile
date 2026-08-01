# syntax=docker/dockerfile:1.7

ARG PYTHON_VERSION=3.13
ARG ALPINE_VERSION=3.23
ARG UV_VERSION=0.12.0

FROM ghcr.io/astral-sh/uv:${UV_VERSION}-python${PYTHON_VERSION}-alpine${ALPINE_VERSION} AS builder

ENV UV_LINK_MODE=copy \
    UV_NO_PROGRESS=1 \
    UV_PYTHON_DOWNLOADS=never

WORKDIR /app

# 先只复制依赖清单，源码变化不会使依赖层缓存失效。
COPY pyproject.toml uv.lock ./

RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --locked --no-dev --no-install-project

FROM ghcr.io/astral-sh/uv:${UV_VERSION}-python${PYTHON_VERSION}-alpine${ALPINE_VERSION} AS runtime

ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_NO_CACHE=1 \
    UV_NO_DEV=1 \
    UV_PYTHON_DOWNLOADS=never

# apk --no-cache 不写本地索引，也不会进入交互式安装流程。
RUN apk add --no-cache ca-certificates ffmpeg \
    && ffprobe -version >/dev/null 2>&1 \
    && ffmpeg -hide_banner -encoders 2>&1 | grep -q '[[:space:]]libx264[[:space:]]' \
    && ffmpeg -hide_banner -encoders 2>&1 | grep -q '[[:space:]]gif[[:space:]]' \
    && ffmpeg -hide_banner -decoders 2>&1 | grep -q '[[:space:]]hevc[[:space:]]' \
    && ffmpeg -hide_banner -decoders 2>&1 | grep -q '[[:space:]]vp9[[:space:]]' \
    && addgroup -S -g 10001 q2tg \
    && adduser -S -D -H -u 10001 -G q2tg q2tg \
    && apk add --no-cache su-exec \
    && mkdir -p /app/data /app/tmp \
    && chown q2tg:q2tg /app/data /app/tmp

WORKDIR /app

COPY --from=builder --chown=q2tg:q2tg /app/.venv /app/.venv
COPY --chown=q2tg:q2tg pyproject.toml uv.lock ./
COPY --chown=q2tg:q2tg main.py ./
COPY --chown=q2tg:q2tg src ./src
COPY --chmod=755 docker-entrypoint.sh /usr/local/bin/docker-entrypoint.sh

VOLUME ["/app/data"]
EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD ["su-exec", "q2tg", "python", "-c", "import os, urllib.request; urllib.request.urlopen('http://127.0.0.1:' + os.getenv('Q2TG_APP_PORT', '8000') + '/healthz', timeout=3).close()"]

ENTRYPOINT ["/usr/local/bin/docker-entrypoint.sh"]
CMD ["uv", "run", "--locked", "--no-sync", "python", "main.py"]
