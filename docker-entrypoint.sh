#!/bin/sh
set -e

# Wire writable directories to the Railway volume mounted at /data.
# If /data is a mounted volume, it persists across deploys.
# If not mounted, /data is ephemeral (investigations still work, just reset on redeploy).

VOLUME=/data

# --- cases/ ---
# Baked-in case JSON answers (cases/HHG-*.json) stay in /app/cases.
# New investigation outputs go to /data/cases and are symlinked back.
if [ -d "$VOLUME/cases" ]; then
  # Copy any baked-in cases into the volume on first boot (don't overwrite existing)
  for f in /app/cases/*.json; do
    [ -f "$f" ] || continue
    base=$(basename "$f")
    [ -f "$VOLUME/cases/$base" ] || cp "$f" "$VOLUME/cases/$base"
  done
  # Replace /app/cases with the volume-backed dir
  rm -rf /app/cases
  ln -s "$VOLUME/cases" /app/cases
fi

# --- data/store/ (writable files only) ---
# The read-only model files (*.joblib, graphsleuth.duckdb, corpus/) stay baked in.
# memory.duckdb and approvals.json are writable at runtime.
if [ -d "$VOLUME/store" ]; then
  # Seed memory.duckdb from baked image if it doesn't exist yet on the volume
  [ -f "$VOLUME/store/memory.duckdb" ] || \
    { [ -f /app/data/store/memory.duckdb ] && cp /app/data/store/memory.duckdb "$VOLUME/store/memory.duckdb" || true; }

  [ -f "$VOLUME/store/approvals.json" ] || echo '{}' > "$VOLUME/store/approvals.json"

  # Symlink the two writable files into the baked store directory
  ln -sf "$VOLUME/store/memory.duckdb"  /app/data/store/memory.duckdb  2>/dev/null || true
  ln -sf "$VOLUME/store/approvals.json" /app/data/store/approvals.json 2>/dev/null || true
fi

echo "Starting GraphSleuth on port ${PORT:-8000}..."
exec python -m uvicorn api.main:app \
  --host 0.0.0.0 \
  --port "${PORT:-8000}" \
  --workers 1
