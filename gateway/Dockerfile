FROM python:3.12-slim

WORKDIR /app

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

# Install system dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    && rm -rf /var/lib/apt/lists/*

# Install Python dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application code. A release archive may already include
# .build_commit_sha; that file is gitignored and is not in a clean checkout.
COPY . .

# Bake /app/.build_commit_sha from the staged file or RELEASE_COMMIT_SHA.
# A named COPY of the gitignored stamp fails before the build starts.
ARG RELEASE_COMMIT_SHA
RUN python scripts/write_build_commit_sha.py

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
