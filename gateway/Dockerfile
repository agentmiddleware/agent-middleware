FROM python:3.12-slim

WORKDIR /app

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

# Install system dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    && rm -rf /var/lib/apt/lists/*

# Hash-pinned install. requirements.lock is the closed resolution of
# requirements.txt. --require-hashes makes pip refuse any distribution
# that is missing a hash, so a rebuild cannot swap in newer code.
COPY requirements.lock .
RUN pip install --no-cache-dir --require-hashes -r requirements.lock

# Copy application code
COPY . .

# Release contexts must carry a commit stamp generated from the exact Git tree.
COPY .build_commit_sha /app/.build_commit_sha

RUN chmod +x scripts/docker_entrypoint.sh \
    && groupadd --system app \
    && useradd --system --gid app --no-create-home --home-dir /app app \
    && chown -R app:app /app

USER app

# Expose port
EXPOSE 8000

# Health check
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import os, urllib.request; urllib.request.urlopen(f\"http://localhost:{os.getenv('PORT', '8000')}/health\")"

# Optional: RUN_MIGRATIONS_ON_START=true + DATABASE_URL runs Alembic before uvicorn
# (fail-closed if the flag is set without DATABASE_URL).
ENTRYPOINT ["scripts/docker_entrypoint.sh"]
