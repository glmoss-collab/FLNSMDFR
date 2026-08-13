#!/usr/bin/env bash
# =============================================================================
# Container entrypoint — selects Streamlit UI or Dropbox intake worker.
#
# Cloud Run *services* keep the default MODE=ui (Streamlit).
# Cloud Run *jobs* set MODE=intake and override command/args as needed.
# =============================================================================
set -euo pipefail

MODE="${MODE:-ui}"

case "$MODE" in
  ui)
    exec streamlit run guaranteed_insulation_app.py \
      --server.port="${PORT:-8501}" \
      --server.address=0.0.0.0 \
      --server.headless=true \
      --server.enableCORS=false \
      --server.enableXsrfProtection=true \
      --browser.gatherUsageStats=false
    ;;
  intake)
    # Prefer Dropbox API path for cloud; local root must stay unset on Jobs.
    ROOT="${DROPBOX_PROJECTS_ROOT:-${DROPBOX_LOCAL_ROOT:-/}}"
    EXTRA_ARGS=()
    if [ -n "${PRICEBOOK_PATH:-}" ]; then
      EXTRA_ARGS+=(--pricebook "$PRICEBOOK_PATH")
    fi
    if [ -n "${PRICE_MARKUP:-}" ]; then
      EXTRA_ARGS+=(--markup "$PRICE_MARKUP")
    fi
    # Allow Job --args to append/override (e.g. --force, --project).
    exec python dropbox_intake.py --root "$ROOT" "${EXTRA_ARGS[@]}" "$@"
    ;;
  *)
    echo "Unknown MODE='$MODE' (expected ui or intake)" >&2
    exit 1
    ;;
esac
