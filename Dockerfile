# Automation dashboard image: the app + Python + Playwright's Chromium, running as a normal (non-root) user.
# Build context is this folder (app/). Secrets and data are NOT in the image: see .dockerignore and docker-compose.yml.
FROM python:3.11-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PLAYWRIGHT_BROWSERS_PATH=/ms-playwright \
    AUTOMATION_STORAGE_DIR=/data/storage \
    AUTOMATION_BROWSER_CHANNEL=chromium \
    AUTOMATION_HEADLESS=true \
    AUTOMATION_TRUST_PROXY=true

RUN useradd --uid 10001 --create-home --home-dir /home/automation --shell /usr/sbin/nologin automation

WORKDIR /app
COPY backend/requirements-docker.txt backend/requirements-docker.txt
# --with-deps installs the system libraries and fonts Chromium needs (this is why the build takes a few minutes).
RUN pip install -r backend/requirements-docker.txt \
 && python -m playwright install --with-deps chromium \
 && rm -rf /var/lib/apt/lists/* \
 && chmod -R a+rX /ms-playwright

COPY --chown=automation:automation backend/app backend/app
COPY --chown=automation:automation backend/runner backend/runner
COPY --chown=automation:automation frontend frontend
COPY --chown=automation:automation automations automations
COPY deploy/docker/entrypoint.sh /usr/local/bin/entrypoint.sh

# /data is the persistent volume: the database, every run's files, and the Office Ally logins.
RUN chmod +x /usr/local/bin/entrypoint.sh \
 && mkdir -p /data/storage /data/secrets \
 && chown -R automation:automation /data /app

USER automation
WORKDIR /app/backend
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/healthz', timeout=3)" || exit 1

ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]
CMD ["python", "-m", "uvicorn", "app.main:create_app", "--factory", "--host", "0.0.0.0", "--port", "8000"]
