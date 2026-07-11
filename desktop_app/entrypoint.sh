#!/bin/sh
# ── Seed default / test indexes into the volume mount ───────────────────────
# The volume mount overlays /app/indexes with the host directory.  If the host
# directory is missing the default fixtures (e.g. fresh clone before committing
# fixtures, or a bare deployment), copy them from the baked-in image copy.
#
# Baked-in defaults live at /app/_default_indexes (copied during image build).
# We only copy files that don't already exist - never overwrite user data.

DEFAULTS_SRC="/app/_default_indexes"
INDEXES_DST="/app/indexes"

if [ -d "$DEFAULTS_SRC" ]; then
    # Use cp with -n (no-clobber) to avoid overwriting existing files.
    # Recreate directory structure first.
    cd "$DEFAULTS_SRC" || exit 1
    find . -type f | while IFS= read -r f; do
        dst="$INDEXES_DST/$f"
        if [ ! -f "$dst" ]; then
            mkdir -p "$(dirname "$dst")"
            cp "$f" "$dst"
            echo "[entrypoint] Seeded default index: $f"
        fi
    done
    cd /app
fi

# Hand off to the main process
exec "$@"
