FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# git is required by the pipeline itself (Phase 4 resolves and validates the
# commit SHA), so it belongs to the image rather than to build tooling.
RUN apt-get update \
    && apt-get install --no-install-recommends -y git \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Requirements first, so a code change does not invalidate the dependency layer.
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# The service handles Git, registry and Jenkins credentials. It never writes
# them anywhere, and it does not run as root either. .dockerignore keeps .env
# out of the image entirely.
RUN useradd --create-home --uid 1000 app \
    && chown -R app:app /app
USER app

EXPOSE 8000

CMD ["python", "manage.py", "runserver", "0.0.0.0:8000"]
