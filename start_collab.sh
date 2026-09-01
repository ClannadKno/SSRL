#!/usr/bin/env bash
# Start the Flask HTTP server, collaboration WebSocket server, and Huey worker.

set -Eeuo pipefail

readonly SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"
umask 077

BUILD_FRONTEND=false
NO_HUEY=false
PUBLIC_URL=""

usage() {
    cat <<'EOF'
Usage: ./start_collab.sh [options]

Options:
  --build                 Run npm ci and build the collaborative editor first.
  --no-huey               Start only HTTP and WebSocket services.
  --public-url URL        Browser-facing WebSocket base URL, for example
                          wss://example.com/ws.
  -h, --help              Show this help message.

Configuration is read from environment variables. See README.md and
deploy/ssrl-esp.env.example.
EOF
}

info() { printf '\033[0;36m[INFO]\033[0m %s\n' "$*"; }
ok()   { printf '\033[0;32m[OK]\033[0m   %s\n' "$*"; }
warn() { printf '\033[0;33m[WARN]\033[0m %s\n' "$*" >&2; }
err()  { printf '\033[0;31m[ERR]\033[0m  %s\n' "$*" >&2; }

while (($#)); do
    case "$1" in
        --build)
            BUILD_FRONTEND=true
            shift
            ;;
        --no-huey)
            NO_HUEY=true
            shift
            ;;
        --public-url)
            if (($# < 2)) || [[ -z "$2" ]]; then
                err "--public-url requires a URL"
                exit 2
            fi
            PUBLIC_URL="$2"
            shift 2
            ;;
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
done

if ! command -v uv >/dev/null 2>&1; then
    err "uv was not found. Install uv and run: uv sync --frozen"
    exit 1
fi

UV_BIN="$(command -v uv)"
UV_RUN=("$UV_BIN" run --frozen --no-sync)

if ! "${UV_RUN[@]}" python -c 'import flask, huey, waitress, uvicorn, websockets, pycrdt, pycrdt.websocket' >/dev/null 2>&1; then
    err "The uv environment is missing or incomplete."
    err "Run: uv sync --frozen"
    exit 1
fi

export SSRL_ESP_HOST="${SSRL_ESP_HOST:-127.0.0.1}"
export SSRL_ESP_PORT="${SSRL_ESP_PORT:-8000}"
export SSRL_ESP_FLASK_THREADS="${SSRL_ESP_FLASK_THREADS:-16}"
export SSRL_ESP_CONNECTION_LIMIT="${SSRL_ESP_CONNECTION_LIMIT:-200}"
export SSRL_ESP_CHANNEL_TIMEOUT="${SSRL_ESP_CHANNEL_TIMEOUT:-120}"
export COLLAB_WS_HOST="${COLLAB_WS_HOST:-127.0.0.1}"
export COLLAB_WS_PORT="${COLLAB_WS_PORT:-8001}"
export SSRL_ESP_HUEY_WORKERS="${SSRL_ESP_HUEY_WORKERS:-8}"
export SSRL_ESP_HUEY_WORKER_CLASS="${SSRL_ESP_HUEY_WORKER_CLASS:-thread}"

if [[ -n "$PUBLIC_URL" ]]; then
    export COLLAB_WS_EXTERNAL_URL="${PUBLIC_URL%/}"
fi

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

if [[ "$BUILD_FRONTEND" == true ]]; then
    if ! command -v npm >/dev/null 2>&1; then
        err "npm was not found; Node.js 20 or newer is required for --build"
        exit 1
    fi
    info "Installing frontend dependencies and building the editor"
    (
        cd "$SCRIPT_DIR/frontend/collaborative-editor"
        npm ci
        npm run build
    )
    ok "Frontend build completed"
fi

if [[ -z "${COLLAB_WS_EXTERNAL_URL:-}" ]]; then
    warn "COLLAB_WS_EXTERNAL_URL is not set; the browser will use the internal WebSocket address"
    warn "For a public server, pass --public-url wss://example.com/ws"
fi

CHILD_PIDS=()

cleanup() {
    local exit_code=$?
    trap - EXIT INT TERM

    if ((${#CHILD_PIDS[@]})); then
        warn "Stopping managed services"
        kill "${CHILD_PIDS[@]}" 2>/dev/null || true
        for pid in "${CHILD_PIDS[@]}"; do
            wait "$pid" 2>/dev/null || true
        done
    fi

    exit "$exit_code"
}

trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

info "Starting collaboration WebSocket service on ${COLLAB_WS_HOST}:${COLLAB_WS_PORT}"
"${UV_RUN[@]}" uvicorn services.collaboration_server.app:app \
    --host "$COLLAB_WS_HOST" \
    --port "$COLLAB_WS_PORT" \
    --ws websockets \
    --log-level info &
CHILD_PIDS+=("$!")

if [[ "$NO_HUEY" == false ]]; then
    info "Starting Huey with ${SSRL_ESP_HUEY_WORKERS} ${SSRL_ESP_HUEY_WORKER_CLASS} worker(s)"
    "${UV_RUN[@]}" huey_consumer huey_instance.huey \
        -k "$SSRL_ESP_HUEY_WORKER_CLASS" \
        -w "$SSRL_ESP_HUEY_WORKERS" &
    CHILD_PIDS+=("$!")
fi

info "Starting Flask through Waitress on ${SSRL_ESP_HOST}:${SSRL_ESP_PORT}"
"${UV_RUN[@]}" waitress-serve \
    --listen="${SSRL_ESP_HOST}:${SSRL_ESP_PORT}" \
    --threads="$SSRL_ESP_FLASK_THREADS" \
    --connection-limit="$SSRL_ESP_CONNECTION_LIMIT" \
    --channel-timeout="$SSRL_ESP_CHANNEL_TIMEOUT" \
    app:app &
CHILD_PIDS+=("$!")

sleep 1
for pid in "${CHILD_PIDS[@]}"; do
    if ! kill -0 "$pid" 2>/dev/null; then
        err "A service failed during startup; check the preceding log output"
        exit 1
    fi
done

ok "All services started"
info "HTTP: http://${SSRL_ESP_HOST}:${SSRL_ESP_PORT}"
info "WebSocket: ws://${COLLAB_WS_HOST}:${COLLAB_WS_PORT}"
info "Press Ctrl+C to stop all services"

set +e
wait -n "${CHILD_PIDS[@]}"
exit_code=$?
set -e

if ((exit_code == 0)); then
    exit_code=1
fi
err "A managed service exited; stopping the remaining services"
exit "$exit_code"
