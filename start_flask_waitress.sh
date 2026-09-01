#!/usr/bin/env bash
# Start only the Flask HTTP application through Waitress.

set -Eeuo pipefail

readonly SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"
umask 077

usage() {
    cat <<'EOF'
Usage: ./start_flask_waitress.sh

Starts only the Flask HTTP application. For the complete application stack,
including collaboration WebSocket and Huey, use ./start_collab.sh instead.
EOF
}

info() { printf '\033[0;36m[INFO]\033[0m %s\n' "$*"; }
err()  { printf '\033[0;31m[ERR]\033[0m  %s\n' "$*" >&2; }

if (($#)); then
    case "$1" in
        -h|--help)
            usage
            exit 0
            ;;
        *)
            err "Unknown option: $1"
            usage >&2
            exit 2
            ;;
    esac
fi

if ! command -v uv >/dev/null 2>&1; then
    err "uv was not found. Install uv and run: uv sync --frozen"
    exit 1
fi

UV_BIN="$(command -v uv)"
UV_RUN=("$UV_BIN" run --frozen --no-sync)

if ! "${UV_RUN[@]}" python -c 'import flask, waitress' >/dev/null 2>&1; then
    err "The uv environment is missing or incomplete."
    err "Run: uv sync --frozen"
    exit 1
fi

export SSRL_ESP_HOST="${SSRL_ESP_HOST:-127.0.0.1}"
export SSRL_ESP_PORT="${SSRL_ESP_PORT:-8000}"
export SSRL_ESP_FLASK_THREADS="${SSRL_ESP_FLASK_THREADS:-16}"
export SSRL_ESP_CONNECTION_LIMIT="${SSRL_ESP_CONNECTION_LIMIT:-200}"
export SSRL_ESP_CHANNEL_TIMEOUT="${SSRL_ESP_CHANNEL_TIMEOUT:-120}"

ensure_secret() {
    local variable_name="$1"
    local secret_file="$2"
    local value="${!variable_name:-}"

    if [[ -n "$value" ]]; then
        info "$variable_name loaded from the environment"
        return
    fi

    if [[ -r "$secret_file" ]]; then
        IFS= read -r value < "$secret_file" || true
        if [[ -z "$value" ]]; then
            err "$secret_file is empty"
            exit 1
        fi
        info "$variable_name loaded from $(basename "$secret_file")"
    else
        value="$("${UV_RUN[@]}" python -c 'import secrets; print(secrets.token_hex(32))')"
        printf '%s\n' "$value" > "$secret_file"
        chmod 600 "$secret_file"
        info "$variable_name generated in $(basename "$secret_file")"
    fi

    printf -v "$variable_name" '%s' "$value"
    export "$variable_name"
}

ensure_secret SSRL_ESP_SECRET "$SCRIPT_DIR/.collab_secret"
ensure_secret COLLAB_INTERNAL_SECRET "$SCRIPT_DIR/.collab_internal_secret"

info "Starting Flask through Waitress on ${SSRL_ESP_HOST}:${SSRL_ESP_PORT}"
exec "${UV_RUN[@]}" waitress-serve \
    --listen="${SSRL_ESP_HOST}:${SSRL_ESP_PORT}" \
    --threads="$SSRL_ESP_FLASK_THREADS" \
    --connection-limit="$SSRL_ESP_CONNECTION_LIMIT" \
    --channel-timeout="$SSRL_ESP_CHANNEL_TIMEOUT" \
    app:app
