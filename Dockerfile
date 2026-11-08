FROM node:22-bookworm-slim AS frontend
WORKDIR /web
COPY web/package.json web/package-lock.json ./
RUN npm ci
COPY web/index.html web/vite.config.js ./
COPY web/src ./src
RUN npm run build

FROM python:3.12-slim
ENV PYTHONUNBUFFERED=1 PROMO_PUBLIC=1 PORT=10000 UV_CACHE_DIR=/tmp/uv-cache
WORKDIR /app
RUN pip install --no-cache-dir uv && useradd --create-home --uid 1000 app
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev
COPY api.py config.yaml ./
COPY src ./src
COPY data/examples ./data/examples
COPY data/public_demo ./data/public_demo
COPY --from=frontend /web/dist ./web/dist
RUN mkdir -p runs && chown -R app:app /app
USER app
EXPOSE 10000
CMD ["sh", "-c", "exec /app/.venv/bin/uvicorn api:app --host 0.0.0.0 --port ${PORT:-10000} --proxy-headers"]
