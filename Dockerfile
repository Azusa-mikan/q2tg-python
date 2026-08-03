# syntax=docker/dockerfile:1.7

ARG PYTHON_VERSION=3.13

FROM edasriyan/lottie-to-gif@sha256:0eb24cf4f38c6c62b66f37bfba463fff4de4f64cb9a6127df0b9543fc4b9c649 AS lottie-converter

FROM ghcr.io/astral-sh/uv:python${PYTHON_VERSION}-trixie-slim AS builder

ENV UV_LINK_MODE=copy \
    UV_NO_PROGRESS=1 \
    UV_PYTHON_DOWNLOADS=never

WORKDIR /app

# pilk 包含原生 SILK 编解码扩展，仅在依赖构建阶段需要 C 工具链。
RUN apt-get update \
    && apt-get install --yes --no-install-recommends build-essential \
    && rm -rf /var/lib/apt/lists/*

# 先只复制依赖清单，源码变化不会使依赖层缓存失效。
COPY pyproject.toml uv.lock ./

# pilk 只提供 sdist，需在本阶段现场编译。--refresh-package 让它忽略可能来自
# 其他 libc（如 alpine/musl）的旧构建缓存，避免复用不匹配当前 glibc 的扩展。
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --locked --no-dev --no-install-project --refresh-package pilk

# 校验 pilk 原生扩展确实为当前 glibc 环境编译（后缀应为 *-linux-gnu.so）。
# 若命中跨 libc 的旧缓存会得到 *-musl.so，运行时无法加载，这里提前失败。
RUN set -eu; \
    so="$(find /app/.venv -name '_pilk*.so' -print -quit)"; \
    if [ -z "$so" ]; then echo "pilk 原生扩展缺失" >&2; exit 1; fi; \
    case "$so" in \
        *-linux-gnu.so) echo "pilk 扩展就绪: $so" ;; \
        *) echo "pilk 扩展 libc 不匹配: $so" >&2; exit 1 ;; \
    esac

FROM ghcr.io/astral-sh/uv:python${PYTHON_VERSION}-trixie-slim AS runtime

ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_NO_CACHE=1 \
    UV_NO_DEV=1 \
    UV_PYTHON_DOWNLOADS=never

# 运行时只保留媒体工具、证书和降权启动工具。
RUN apt-get update \
    && apt-get install --yes --no-install-recommends bash ca-certificates ffmpeg gosu \
    && rm -rf /var/lib/apt/lists/* \
    && ffprobe -version >/dev/null 2>&1 \
    && ffmpeg -hide_banner -encoders 2>&1 | grep -q '[[:space:]]libx264[[:space:]]' \
    && ffmpeg -hide_banner -encoders 2>&1 | grep -q '[[:space:]]gif[[:space:]]' \
    && ffmpeg -hide_banner -encoders 2>&1 | grep -q '[[:space:]]libopus[[:space:]]' \
    && ffmpeg -hide_banner -decoders 2>&1 | grep -q '[[:space:]]hevc[[:space:]]' \
    && ffmpeg -hide_banner -decoders 2>&1 | grep -q '[[:space:]]libvpx-vp9[[:space:]]' \
    && groupadd --gid 10001 q2tg \
    && useradd --uid 10001 --gid q2tg --no-create-home --shell /usr/sbin/nologin q2tg \
    && mkdir -p /app/data /app/tmp \
    && chown q2tg:q2tg /app/data /app/tmp

WORKDIR /app

RUN touch /app/.q2tg-container

COPY --from=builder --chown=q2tg:q2tg /app/.venv /app/.venv
COPY --from=lottie-converter --chmod=755 \
    /usr/bin/lottie_common.sh \
    /usr/bin/lottie_to_gif.sh \
    /usr/bin/lottie_to_png \
    /usr/bin/gifski \
    /usr/local/bin/
COPY --chown=q2tg:q2tg pyproject.toml uv.lock ./
COPY --chown=q2tg:q2tg alembic.ini ./
COPY --chown=q2tg:q2tg alembic ./alembic
COPY --chown=q2tg:q2tg main.py ./
COPY --chown=q2tg:q2tg src ./src
COPY --chmod=755 docker-entrypoint.sh /usr/local/bin/docker-entrypoint.sh

VOLUME ["/app/data"]
EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD ["gosu", "q2tg", "python", "-c", "import os, urllib.request; urllib.request.urlopen('http://127.0.0.1:' + os.getenv('Q2TG_APP_PORT', '8000') + '/healthz', timeout=3).close()"]

ENTRYPOINT ["/usr/local/bin/docker-entrypoint.sh"]
CMD ["uv", "run", "--locked", "--no-sync", "python", "main.py"]
