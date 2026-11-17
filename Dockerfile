# One image for web, worker, beat and flower (command differs per service).
FROM python:3.12-slim AS base
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 UV_PROJECT_ENVIRONMENT=/opt/venv PATH="/opt/venv/bin:$PATH" \
    DJANGO_SETTINGS_MODULE=config.settings.prod
RUN apt-get update && apt-get install -y --no-install-recommends curl && rm -rf /var/lib/apt/lists/*
COPY --from=ghcr.io/astral-sh/uv:0.13.9 /uv /usr/local/bin/uv
WORKDIR /app

COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev

COPY . .
# Build CSS with the Tailwind standalone CLI (Linux binary; works in Docker).
RUN ./scripts/vendor.sh && ./bin/tailwindcss -i assets/tailwind.css -o static/css/app.css --minify
RUN DJANGO_SETTINGS_MODULE=config.settings.base DJANGO_SECRET_KEY=build python manage.py collectstatic --noinput

RUN useradd --create-home app && chown -R app /app
USER app
EXPOSE 8000
# $PORT is set by the hosting platform (Railway/Render/Fly); 8000 locally.
# --forwarded-allow-ips="*" trusts the platform proxy's X-Forwarded-Proto (https).
CMD ["sh", "-c", "uvicorn config.asgi:application --host 0.0.0.0 --port ${PORT:-8000} --proxy-headers --forwarded-allow-ips='*'"]
