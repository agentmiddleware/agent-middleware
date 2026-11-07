FROM python:3.12-slim AS build-metadata

# The release archive may carry a gitignored stamp at its context root.
# Copying the context lets the writer validate that optional input without a
# named COPY that fails on clean trees. .dockerignore excludes local secrets.
WORKDIR /context
COPY . /context/
# Commit sources, highest precedence first: RELEASE_COMMIT_SHA (explicit
# override), RAILWAY_GIT_COMMIT_SHA (Railway passes it because it is declared
# here), GIT_COMMIT_SHA (CI build arg), then the staged root stamp. All set
# sources must agree. BUILD_COMMIT_SHA/COMMIT_SHA are not read (stale vars).
ARG RELEASE_COMMIT_SHA
ARG RAILWAY_GIT_COMMIT_SHA
ARG GIT_COMMIT_SHA
RUN mkdir /stamp \
    && STAGED_SHA_PATH=/context/.build_commit_sha \
       BUILD_SHA_PATH=/stamp/.build_commit_sha \
       python /context/gateway/scripts/write_build_commit_sha.py

FROM python:3.12-slim

WORKDIR /app

# b2a_sdk is imported while app.main loads. The build context is the
# repository root (not this directory): sdk/python is not inside gateway/.
#   docker build -f gateway/Dockerfile .
# The release stamp .build_commit_sha must sit at that context root.
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONPATH=/sdk/python/src

# Install system dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    && rm -rf /var/lib/apt/lists/*

# Install Python dependencies, then the in-repo SDK those imports require.
COPY sdk/python /sdk/python
COPY gateway/requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir -r /tmp/requirements.txt \
    && pip install --no-cache-dir /sdk/python

# Copy application code
COPY gateway/ /app/

# Transfer only the validated stamp, after the app copy. A stale stamp under
# gateway/ cannot bypass validation or overwrite this output.
COPY --from=build-metadata /stamp/.build_commit_sha /app/.build_commit_sha

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
