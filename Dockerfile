# The same server as a container, for any host that runs one (Cloud Run, ECS,
# Kubernetes, a VM behind nginx). Render uses render.yaml instead.
FROM python:3.12.7-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /srv
COPY requirements.txt .
RUN pip install -r requirements.txt

COPY app ./app

# Not root inside the container.
RUN useradd --create-home --uid 10001 api
USER api

ENV PORT=8000 WEB_CONCURRENCY=2
EXPOSE 8000
CMD ["sh", "-c", "exec uvicorn app.main:create_app --factory --host 0.0.0.0 --port ${PORT} --workers ${WEB_CONCURRENCY} --no-server-header --no-access-log --timeout-keep-alive 20 --timeout-graceful-shutdown 20"]
