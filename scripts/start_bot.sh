#!/usr/bin/env bash
# Start the bizcard intake bot.
# Loads project-local .env (if present), validates required tokens,
# and execs the Python module so launchd can supervise it.

set -euo pipefail

PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$PROJECT_ROOT"

# Make `codex`, `gws` and Python discoverable when invoked via launchd (minimal PATH).
export PATH="$HOME/.local/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:${PATH:-}"

ENV_FILE="$PROJECT_ROOT/.env"
if [[ -f "$ENV_FILE" ]]; then
    # The loader uses only the standard library; any bootstrap Python works.
    bizcard_loader_python="$(command -v python3 || true)"
    if [[ -z "$bizcard_loader_python" && -n "${BIZCARD_PYTHON:-}" && -x "${BIZCARD_PYTHON}" ]]; then
        bizcard_loader_python="$BIZCARD_PYTHON"
    fi
    if [[ -z "$bizcard_loader_python" ]]; then
        echo "Python 3 not found to read $ENV_FILE. Set BIZCARD_PYTHON to an absolute interpreter path." >&2
        exit 1
    fi
    bizcard_env_loaded=0
    while IFS= read -r -d '' bizcard_setting; do
        if [[ -z "$bizcard_setting" ]]; then
            bizcard_env_loaded=1
        else
            export "$bizcard_setting"
        fi
    done < <("$bizcard_loader_python" "$PROJECT_ROOT/bizcard_intake/envfile.py" "$ENV_FILE")
    [[ "$bizcard_env_loaded" == 1 ]] || exit 1
fi

if [[ -z "${TELEGRAM_BOT_TOKEN:-}" ]]; then
    echo "TELEGRAM_BOT_TOKEN is required. Set it in $ENV_FILE or your shell env." >&2
    exit 1
fi

PYTHON_BIN="${BIZCARD_PYTHON:-$(command -v python3 || true)}"
if [[ ! -x "$PYTHON_BIN" ]]; then
    echo "Python interpreter not found at $PYTHON_BIN. Override with BIZCARD_PYTHON=." >&2
    exit 1
fi

exec "$PYTHON_BIN" -m bizcard_intake bot
