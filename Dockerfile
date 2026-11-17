# One image for web, worker, beat and flower (command differs per service).
FROM python:3.12-slim AS base
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 UV_PROJECT_ENVIRONMENT=/opt/venv PATH="/opt/venv/bin:$PATH"
RUN apt-get update && apt-get install -y --no-install-recommends curl && rm -rf /var/lib/apt/lists/*
COPY --from=ghcr.io/astral-sh/uv:0.5.11 /uv /usr/local/bin/uv
WORKDIR /app

COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev

COPY . .
# Build CSS with the Tailwind standalone CLI (Linux binary; works in Docker).
RUN ./scripts/vendor.sh && ./bin/tailwindcss -i static/src/app.css -o static/css/app.css --minify
RUN DJANGO_SETTINGS_MODULE=config.settings.base DJANGO_SECRET_KEY=build python manage.py collectstatic --noinput

RUN useradd --create-home app && chown -R app /app
USER app
EXPOSE 8000
CMD ["uvicorn", "config.asgi:application", "--host", "0.0.0.0", "--port", "8000", "--proxy-headers"]
