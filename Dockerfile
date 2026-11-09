FROM python:3.12-slim
COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv
WORKDIR /app
ENV UV_COMPILE_BYTECODE=1 PATH="/app/.venv/bin:$PATH" PYTHONUNBUFFERED=1 \
    LOGS_ROOT=/data/logs EMBEDDING_CACHE_DIR=/opt/fastembed
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project
COPY alembic.ini ./
COPY alembic ./alembic
COPY app ./app
# bake the embedding model into the image so first use needs no download
RUN python -c "from fastembed import TextEmbedding; TextEmbedding('BAAI/bge-small-en-v1.5', cache_dir='/opt/fastembed')"
COPY scripts ./scripts
RUN useradd -m app && mkdir -p /data/logs && chown -R app /data /opt/fastembed
USER app
EXPOSE 8000
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
