# syntax=docker/dockerfile:1

FROM python:3.12-slim AS builder

WORKDIR /build

COPY pyproject.toml README.md app.py ./
COPY codex_partner ./codex_partner
COPY static ./static

RUN python -m pip wheel --no-cache-dir --wheel-dir /wheels .


FROM python:3.12-slim AS runtime

ARG CODEX_VERSION=latest
ARG APP_VERSION=0.0.3

LABEL org.opencontainers.image.title="Codex Partner" \
      org.opencontainers.image.description="Self-hosted management for persistent Codex tasks" \
      org.opencontainers.image.version="${APP_VERSION}"

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    HOME=/home/codex \
    PATH=/home/codex/.local/bin:${PATH} \
    CODEX_HOME=/home/codex/.codex \
    CODEX_BIN=/home/codex/.local/bin/codex \
    CODEX_DASHBOARD_HOST=0.0.0.0 \
    CODEX_DASHBOARD_PORT=8787 \
    CODEX_DASHBOARD_DATA=/var/lib/codex-partner \
    CODEX_DASHBOARD_DEFAULT_WORKSPACE=/workspace \
    CODEX_DASHBOARD_WORKSPACE_ROOTS=/workspace

RUN apt-get update \
    && apt-get install --no-install-recommends -y \
        ca-certificates \
        git \
        nodejs \
        npm \
        openssh-client \
        tini \
    && useradd --create-home --uid 10001 --shell /bin/bash codex \
    && install -d -o codex -g codex \
        /home/codex/.local \
        /home/codex/.codex \
        /home/codex/.ssh \
        /var/lib/codex-partner \
        /workspace \
    && npm install --global --prefix /home/codex/.local "@openai/codex@${CODEX_VERSION}" \
    && chown -R codex:codex /home/codex/.local \
    && /home/codex/.local/bin/codex --version \
    && rm -rf /var/lib/apt/lists/* /root/.npm

COPY --from=builder /wheels /wheels
RUN python -m pip install --no-cache-dir --no-index --find-links=/wheels codex-partner \
    && rm -rf /wheels

USER codex
WORKDIR /workspace

VOLUME ["/var/lib/codex-partner", "/home/codex/.codex", "/workspace"]
EXPOSE 8787
STOPSIGNAL SIGTERM

HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
  CMD ["python", "-c", "import os,urllib.request; port=os.getenv('CODEX_DASHBOARD_PORT','8787'); urllib.request.urlopen('http://127.0.0.1:'+port+'/api/live',timeout=4).read()"]

ENTRYPOINT ["tini", "--"]
CMD ["codex-partner"]
