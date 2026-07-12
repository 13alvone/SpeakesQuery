FROM python:3.14-slim

# ── System deps ─────────────────────────────────────────────────────────────
# `curl` for healthcheck; the libpango/libcairo/libgdk-pixbuf/libffi/
# shared-mime-info bundle is required by WeasyPrint to render the
# Schedule Operations Report PDF (tools/schedule_pdf.py).
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        curl \
        libpango-1.0-0 \
        libpangoft2-1.0-0 \
        libharfbuzz0b \
        libgdk-pixbuf-2.0-0 \
        libcairo-gobject2 \
        libcairo2 \
        libffi8 \
        shared-mime-info \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# ── Python deps (cached layer - only rebuilds when requirements change) ──────
# Install CPU-only torch FIRST so the sentence-transformers dependency in
# requirements.txt resolves against it instead of pulling the default PyPI
# CUDA build (~4 GB of nvidia-* wheels the container can never use - compose
# doesn't request a GPU). Semantic search runs all-MiniLM-L6-v2 on CPU.
# Keeps the image ~4 GB smaller and first install several times faster.
COPY requirements.txt ./
RUN pip install --no-cache-dir --upgrade pip \
    && pip install --no-cache-dir torch --index-url https://download.pytorch.org/whl/cpu \
    && pip install --no-cache-dir -r requirements.txt

# ── Copy full project source ─────────────────────────────────────────────────
COPY . .

# ── Ensure runtime directories exist ─────────────────────────────────────────
RUN mkdir -p indexes lookups frontend/static/temp

# ── Bake default / test indexes for seeding into empty volumes ──────────────
# These are copied into the volume-mounted indexes/ dir at startup by the
# entrypoint script (only when the target file doesn't already exist).
RUN mkdir -p _default_indexes \
    && cp -a indexes/default_test _default_indexes/default_test 2>/dev/null || true \
    && cp -a indexes/archive _default_indexes/archive 2>/dev/null || true \
    && cp -a indexes/sample _default_indexes/sample 2>/dev/null || true

# ── Permissions ──────────────────────────────────────────────────────────────
# Make /app world-writable so the container works regardless of which UID
# docker-compose passes via `user:`.  The server creates SQLite databases,
# YAML config files, and temp files throughout the project root at runtime.
# On Linux, volume mounts preserve host UID/GID - the `user:` directive in
# docker-compose.yml ensures the container process matches the host owner.
RUN chmod -R a+rwX /app

# ── Home directory ───────────────────────────────────────────────────────────
# Set HOME to /app so that paths like ~/.speakes-query resolve inside the
# project tree rather than to / (which is unwritable for non-root UIDs).
ENV HOME=/app

# ── Passwd entry for the runtime UID ────────────────────────────────────────
# docker-compose.yml's `user: "${DOCKER_UID:-1000}:${DOCKER_GID:-1000}"`
# defaults to UID 1000. python:3.14-slim's /etc/passwd does NOT include
# a UID-1000 entry, so libraries that call ``pwd.getpwuid(os.getuid())``
# directly (huggingface_hub's cache init, ``getpass.getuser()`` if USER/
# LOGNAME env vars are absent, some BLAS/SciPy startup paths) raise
# ``KeyError: 'getpwuid(): uid not found: 1000'`` on first import.
# Caught 2026-05-17 on slice 3a's first real bootstrap of the embedder.
#
# Adding the passwd + group entries at image build time means the lookup
# succeeds for the default UID 1000 setup. For non-default UIDs (operators
# overriding DOCKER_UID), pair this with the USER/LOGNAME env vars in
# docker-compose.yml's environment block (those are checked FIRST by
# getpass.getuser before falling through to passwd lookup).
RUN echo "speakesquery:x:1000:1000:SpeakesQuery runtime user:/app:/bin/bash" >> /etc/passwd \
    && echo "speakesquery:x:1000:" >> /etc/group

# ── Cache dirs for ML libraries ─────────────────────────────────────────────
# huggingface_hub / sentence-transformers / transformers all default to
# ~/.cache/<lib>. With HOME=/app that resolves to /app/.cache/<lib>.
# Pre-create + chmod so the first model download works regardless of
# umask / UID; the directories themselves are inside the writable image
# layer so they're not affected by bind mounts.
RUN mkdir -p /app/.cache/huggingface \
    && chmod -R a+rwX /app/.cache

EXPOSE 5111

# ── Entrypoint seeds default indexes into empty volume mounts ───────────────
RUN chmod +x /app/desktop_app/entrypoint.sh
ENTRYPOINT ["/app/desktop_app/entrypoint.sh"]

# ── Healthcheck - lets Docker and install.sh know when the server is ready ───
# Probes /healthz (exempt from the W11b access-token gate by design) so
# the container reports healthy even when the gate is active. Probing /
# would 401 forever and mark a perfectly healthy container unhealthy.
HEALTHCHECK --interval=5s --timeout=3s --start-period=10s --retries=3 \
    CMD curl -f http://localhost:5111/healthz || exit 1

CMD ["python", "desktop_app/server.py"]
