ARG PYTHON_IMAGE=python:3.12.14-slim-bookworm@sha256:0f5b26b9518d002b6173fd61daad821fa340635ebfec5bba471013f9ca114579
ARG NODE_IMAGE=node:20.19.4-bookworm-slim@sha256:6db5e436948af8f0244488a1f658c2c8e55a3ae51ca2e1686ed042be8f25f70a

FROM ${NODE_IMAGE} AS frontend
WORKDIR /src
COPY . .
RUN cd theme/static_src && npm ci && npm run build

FROM ${PYTHON_IMAGE} AS wheels
WORKDIR /build
RUN apt-get update \
    && apt-get install -y --no-install-recommends gcc libpq-dev \
    && rm -rf /var/lib/apt/lists/*
COPY requirements.lock .
RUN pip wheel --no-cache-dir --no-deps --require-hashes --wheel-dir /wheels -r requirements.lock

FROM ${PYTHON_IMAGE} AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PORT=8000

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends curl ffmpeg libpq5 libpcre2-8-0 \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd --gid 10001 brightbean \
    && useradd --uid 10001 --gid 10001 --create-home --home-dir /home/brightbean brightbean

COPY requirements.lock .
COPY --from=wheels /wheels /wheels
RUN pip install --no-cache-dir --no-deps --require-hashes --no-index --find-links=/wheels -r requirements.lock \
    && rm -rf /wheels

COPY --chown=brightbean:brightbean . .
COPY --from=frontend --chown=brightbean:brightbean /src/theme/static/css/dist /app/theme/static/css/dist

RUN DJANGO_SETTINGS_MODULE=config.settings.production \
    SECRET_KEY=build-placeholder \
    DATABASE_URL=sqlite:///tmp/build.db \
    python manage.py collectstatic --noinput \
    && chown -R brightbean:brightbean /app/staticfiles

USER 10001:10001

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=30s --retries=3 \
    CMD curl --fail --silent --show-error http://127.0.0.1:${PORT:-8000}/health/ || exit 1

CMD ["gunicorn", "config.wsgi:application", "--bind", "0.0.0.0:8000", "--workers", "2", "--threads", "2", "--timeout", "120"]
