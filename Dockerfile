FROM python:3.12-slim
WORKDIR /app
COPY pyproject.toml uv.lock ./
RUN pip install --no-cache-dir uv && uv sync --frozen --no-dev --no-install-project
COPY server ./server
ENV PATH="/app/.venv/bin:$PATH" DB_PATH=/data/agenthub.db PORT=8000
VOLUME ["/data"]
EXPOSE 8000
CMD ["sh", "-c", "uvicorn server.app:app --host 0.0.0.0 --port ${PORT}"]
