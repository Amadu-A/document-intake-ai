# Dockerfile
# Один закреплённый образ API/worker; Office/PDF конвертация идёт отдельным ограниченным процессом.
FROM ghcr.io/astral-sh/uv:0.12.23 AS uv
FROM python:3.12.16-slim-bookworm
COPY --from=uv /uv /usr/local/bin/uv
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 UV_LINK_MODE=copy
RUN apt-get update && apt-get install -y --no-install-recommends libreoffice-writer \
    fonts-dejavu-core fonts-liberation libseccomp2 tini \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd --gid 10001 intake && useradd --uid 10001 --gid 10001 --create-home intake
WORKDIR /opt/app
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project
ENV PATH="/opt/app/.venv/bin:$PATH"
COPY app ./app
COPY prompts ./prompts
COPY migrations ./migrations
COPY alembic.ini .env.example ./
RUN mkdir -p /data && chown 10001:10001 /data && chmod 0700 /data
USER 10001:10001
ENTRYPOINT ["/usr/bin/tini", "-g", "--"]
CMD ["python", "-m", "app.main"]
