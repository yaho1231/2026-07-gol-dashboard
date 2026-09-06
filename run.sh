#!/usr/bin/env bash
set -Eeuo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$PROJECT_DIR"

ENV_FILE="$PROJECT_DIR/.env"
if [ ! -f "$ENV_FILE" ]; then
    echo "[setup] .env file not found. Copying .env.example..."
    cp "$PROJECT_DIR/.env.example" "$ENV_FILE"
    echo "[setup] Edit .env and set DISCORD_BOT_TOKEN before starting the bot."
fi

if ! grep -q '^API_KEY=..*' "$ENV_FILE"; then
    GENERATED_API_KEY="$(openssl rand -hex 24)"
    if grep -q '^API_KEY=' "$ENV_FILE"; then
        sed -i '' "s/^API_KEY=\$/API_KEY=${GENERATED_API_KEY}/" "$ENV_FILE"
    else
        printf '\n# 외부에서 쓰기(POST/PUT/DELETE) 요청 시 X-API-Key 헤더로 보내야 하는 키\nAPI_KEY=%s\n' "$GENERATED_API_KEY" >> "$ENV_FILE"
    fi
    echo "[setup] Generated API_KEY in .env (required for external write requests)."
fi

ENV_OVERRIDE_KEYS=(
    BACKEND_HOST
    BACKEND_PORT
    PUBLIC_URL
    PUBLIC_MODE
    START_DASHBOARD
    START_BOT
    KEEP_AWAKE
    AUTO_INSTALL_DEPS
    DASHBOARD_PORT
    PYTHON_CMD
    STREAMLIT_CMD
    DISCORD_BOT_TOKEN
    CLOUDFLARED_TUNNEL
    DUCKDNS_DOMAIN
    DUCKDNS_TOKEN
)

for key in "${ENV_OVERRIDE_KEYS[@]}"; do
    if eval '[ "${'"$key"'+x}" = x ]'; then
        printf -v "__RUN_SH_ORIGINAL_${key}" '%s' "${!key}"
        export "__RUN_SH_ORIGINAL_${key}"
    fi
done

set -a
# shellcheck disable=SC1090
source "$ENV_FILE"
set +a

for key in "${ENV_OVERRIDE_KEYS[@]}"; do
    original_key="__RUN_SH_ORIGINAL_${key}"
    if eval '[ "${'"$original_key"'+x}" = x ]'; then
        printf -v "$key" '%s' "${!original_key}"
        export "$key"
        unset "$original_key"
    fi
done

BACKEND_HOST="${BACKEND_HOST:-127.0.0.1}"
BACKEND_PORT="${BACKEND_PORT:-8000}"
LOCAL_BACKEND_URL="http://127.0.0.1:${BACKEND_PORT}"
CLOUDFLARED_TUNNEL="${CLOUDFLARED_TUNNEL:-}"
# What gets published is the dashboard; the API stays local-only for the bot.
# quick: free trycloudflare.com URL, changes on every run, no domain needed
# named: stable URL via `cloudflared tunnel run`, requires a domain on Cloudflare
# port:  router port forwarding + DNS record; just verifies PUBLIC_URL
# none:  local only, no public deployment
if [ -n "$CLOUDFLARED_TUNNEL" ]; then
    PUBLIC_MODE="${PUBLIC_MODE:-named}"
else
    PUBLIC_MODE="${PUBLIC_MODE:-quick}"
fi
PUBLIC_URL="${PUBLIC_URL:-}"
DUCKDNS_DOMAIN="${DUCKDNS_DOMAIN:-}"
DUCKDNS_TOKEN="${DUCKDNS_TOKEN:-}"
DUCKDNS_INTERVAL_SECONDS="${DUCKDNS_INTERVAL_SECONDS:-300}"
START_DASHBOARD="${START_DASHBOARD:-1}"
START_BOT="${START_BOT:-1}"
KEEP_AWAKE="${KEEP_AWAKE:-1}"
AUTO_INSTALL_DEPS="${AUTO_INSTALL_DEPS:-1}"
DASHBOARD_PORT="${DASHBOARD_PORT:-8501}"

LOG_DIR="$PROJECT_DIR/logs"
mkdir -p "$LOG_DIR"
TUNNEL_LOG="$LOG_DIR/cloudflared.log"
PUBLIC_URL_FILE="$LOG_DIR/public_url.txt"

PYTHON_CMD="${PYTHON_CMD:-python3}"
STREAMLIT_CMD="${STREAMLIT_CMD:-streamlit}"

if [ -x "$PROJECT_DIR/venv/bin/python" ]; then
    PYTHON_CMD="$PROJECT_DIR/venv/bin/python"
elif [ -x "/Users/skul/venv/bin/python" ]; then
    PYTHON_CMD="/Users/skul/venv/bin/python"
fi

if [ -x "$PROJECT_DIR/venv/bin/streamlit" ]; then
    STREAMLIT_CMD="$PROJECT_DIR/venv/bin/streamlit"
elif [ -x "/Users/skul/venv/bin/streamlit" ]; then
    STREAMLIT_CMD="/Users/skul/venv/bin/streamlit"
fi

PIDS=()
CLEANED_UP=0

cleanup() {
    if [ "$CLEANED_UP" = "1" ]; then
        return 0
    fi
    CLEANED_UP=1
    echo
    echo "[stop] Stopping services..."
    for pid in "${PIDS[@]:-}"; do
        if kill -0 "$pid" >/dev/null 2>&1; then
            kill "$pid" >/dev/null 2>&1 || true
        fi
    done
    wait >/dev/null 2>&1 || true
}
trap cleanup EXIT
trap 'cleanup; exit 130' INT TERM

have_cmd() {
    command -v "$1" >/dev/null 2>&1
}

wait_for_url() {
    local url="$1"
    local attempts="${2:-30}"
    local i
    for ((i = 1; i <= attempts; i++)); do
        if curl -fsS "$url" >/dev/null 2>&1; then
            return 0
        fi
        sleep 1
    done
    return 1
}

port_is_listening() {
    local port="$1"
    if have_cmd lsof; then
        lsof -iTCP:"$port" -sTCP:LISTEN >/dev/null 2>&1
    else
        nc -z 127.0.0.1 "$port" >/dev/null 2>&1
    fi
}

ensure_python_deps() {
    if "$PYTHON_CMD" - <<'PY' >/dev/null 2>&1
import fastapi, uvicorn, sqlalchemy, pydantic_settings, apscheduler, pandas, httpx, discord, streamlit
PY
    then
        return 0
    fi

    if [ "$AUTO_INSTALL_DEPS" != "1" ]; then
        echo "[deps] Python dependencies are missing. Set AUTO_INSTALL_DEPS=1 or install requirements manually."
        return 1
    fi

    echo "[deps] Installing missing Python dependencies..."
    "$PYTHON_CMD" -m pip install -r backend/requirements.txt -r bot/requirements.txt -r dashboard/requirements.txt
}

ensure_cloudflared() {
    if have_cmd cloudflared; then
        return 0
    fi
    if [ "$AUTO_INSTALL_DEPS" = "1" ] && have_cmd brew; then
        echo "[deploy] cloudflared not found. Installing with Homebrew..."
        brew install cloudflared
        return 0
    fi
    echo "[deploy] cloudflared is not installed. Run: brew install cloudflared"
    return 1
}

start_keep_awake() {
    # macOS 가 유휴 절전에 들어가면 봇의 게이트웨이 연결이 끊기고, 깨어난 뒤 밀린
    # 인터랙션이 3초 시한을 넘겨 도착해 /명령이 10062(Unknown interaction)로 죽는다.
    # 스크립트가 살아 있는 동안만 시스템·디스크 절전을 막는다(디스플레이는 그대로 꺼짐).
    if [ "$KEEP_AWAKE" != "1" ] || ! have_cmd caffeinate; then
        return 0
    fi
    caffeinate -sim -w "$$" &
    echo "[keepawake] System sleep disabled while this script runs (KEEP_AWAKE=0 to opt out)."
}

start_backend() {
    echo "[1/4] Starting FastAPI backend at ${LOCAL_BACKEND_URL}..."

    if port_is_listening "$BACKEND_PORT"; then
        if wait_for_url "${LOCAL_BACKEND_URL}/docs" 3; then
            echo "[backend] Port ${BACKEND_PORT} is already serving FastAPI. Reusing it."
            return 0
        fi
        echo "[backend] Port ${BACKEND_PORT} is already in use, but ${LOCAL_BACKEND_URL}/docs is not responding."
        echo "[backend] Stop that process or set BACKEND_PORT to another port in .env."
        return 1
    fi

    "$PYTHON_CMD" -m uvicorn app.main:app \
        --host "$BACKEND_HOST" \
        --port "$BACKEND_PORT" \
        --app-dir backend &
    PIDS+=("$!")

    if ! wait_for_url "${LOCAL_BACKEND_URL}/docs" 45; then
        echo "[backend] FastAPI did not become ready at ${LOCAL_BACKEND_URL}/docs."
        return 1
    fi
}

start_ddns() {
    if [ -z "$DUCKDNS_DOMAIN" ] || [ -z "$DUCKDNS_TOKEN" ]; then
        return 0
    fi
    echo "[ddns] DuckDNS updater started: ${DUCKDNS_DOMAIN}.duckdns.org (every $((DUCKDNS_INTERVAL_SECONDS / 60)) min)"
    (
        while true; do
            result="$(curl -fsS "https://www.duckdns.org/update?domains=${DUCKDNS_DOMAIN}&token=${DUCKDNS_TOKEN}&ip=" 2>/dev/null || echo "KO")"
            if [ "$result" != "OK" ]; then
                echo "[ddns] DuckDNS update failed (${result}). Retrying in $((DUCKDNS_INTERVAL_SECONDS / 60)) min."
            fi
            sleep "$DUCKDNS_INTERVAL_SECONDS"
        done
    ) &
    PIDS+=("$!")
}

start_public_dashboard() {
    echo "[3/4] Checking public dashboard deployment..."

    if [ "$PUBLIC_MODE" = "none" ]; then
        echo "[deploy] Skipped. PUBLIC_MODE=none"
        return 0
    fi

    local LOCAL_DASHBOARD_URL="http://127.0.0.1:${DASHBOARD_PORT}"
    if [ "$START_DASHBOARD" = "1" ] && ! wait_for_url "$LOCAL_DASHBOARD_URL" 30; then
        echo "[deploy] Dashboard is not responding locally at ${LOCAL_DASHBOARD_URL}."
    fi

    if [ "$PUBLIC_MODE" = "port" ]; then
        if [ -z "$PUBLIC_URL" ]; then
            echo "[deploy] PUBLIC_MODE=port requires PUBLIC_URL in .env."
            return 1
        fi
        echo "[deploy] Port-forwarding mode. Verifying ${PUBLIC_URL}..."
        if wait_for_url "$PUBLIC_URL" 10; then
            echo "[deploy] Public dashboard is live: ${PUBLIC_URL}"
        else
            echo "[deploy] ${PUBLIC_URL} did not respond from inside the network."
            echo "[deploy] Check router port forwarding (external ${DASHBOARD_PORT} -> this machine) and DNS."
            echo "[deploy] Note: some routers block NAT loopback - test from mobile LTE before assuming it is down."
        fi
        printf '%s\n' "$PUBLIC_URL" > "$PUBLIC_URL_FILE"
        return 0
    fi

    ensure_cloudflared || return 1
    : > "$TUNNEL_LOG"

    if [ "$PUBLIC_MODE" = "named" ]; then
        if [ -z "$CLOUDFLARED_TUNNEL" ]; then
            echo "[deploy] PUBLIC_MODE=named requires CLOUDFLARED_TUNNEL in .env."
            return 1
        fi
        echo "[deploy] Starting named Cloudflare Tunnel: ${CLOUDFLARED_TUNNEL}"
        cloudflared tunnel --no-autoupdate run "$CLOUDFLARED_TUNNEL" >>"$TUNNEL_LOG" 2>&1 &
        PIDS+=("$!")
        if [ -n "$PUBLIC_URL" ]; then
            if wait_for_url "$PUBLIC_URL" 30; then
                echo "[deploy] Public dashboard is live: ${PUBLIC_URL}"
            else
                echo "[deploy] Tunnel started but ${PUBLIC_URL} is not responding yet."
                echo "[deploy] Check the tunnel's ingress/DNS settings. Log: ${TUNNEL_LOG}"
            fi
        else
            echo "[deploy] Set PUBLIC_URL in .env to the hostname routed to this tunnel."
        fi
    else
        echo "[deploy] Starting Quick Tunnel for ${LOCAL_DASHBOARD_URL} (free, URL changes each run)..."
        cloudflared tunnel --no-autoupdate --url "$LOCAL_DASHBOARD_URL" >>"$TUNNEL_LOG" 2>&1 &
        PIDS+=("$!")

        local i
        PUBLIC_URL=""
        for ((i = 1; i <= 30; i++)); do
            PUBLIC_URL="$(grep -oE 'https://[a-z0-9-]+\.trycloudflare\.com' "$TUNNEL_LOG" | head -1 || true)"
            if [ -n "$PUBLIC_URL" ]; then
                break
            fi
            sleep 1
        done

        if [ -z "$PUBLIC_URL" ]; then
            echo "[deploy] Could not find the tunnel URL. Log: ${TUNNEL_LOG}"
            return 1
        fi

        if wait_for_url "$PUBLIC_URL" 90; then
            echo "[deploy] Public dashboard is live: ${PUBLIC_URL}"
        else
            echo "[deploy] Tunnel URL issued: ${PUBLIC_URL}"
            echo "[deploy] DNS may take another minute to propagate. Log: ${TUNNEL_LOG}"
        fi
    fi

    if [ -n "$PUBLIC_URL" ]; then
        printf '%s\n' "$PUBLIC_URL" > "$PUBLIC_URL_FILE"
        export PUBLIC_URL
    fi
}

start_dashboard() {
    if [ "$START_DASHBOARD" != "1" ]; then
        echo "[2/4] Dashboard skipped. START_DASHBOARD=${START_DASHBOARD}"
        return 0
    fi

    echo "[2/4] Starting Streamlit dashboard at http://127.0.0.1:${DASHBOARD_PORT}..."
    if port_is_listening "$DASHBOARD_PORT"; then
        echo "[dashboard] Port ${DASHBOARD_PORT} is already in use. Reusing existing dashboard/process."
        return 0
    fi

    # 127.0.0.1 전용 바인드 — 공개는 Cloudflare Tunnel이 담당하므로 대시보드
    # 포트가 LAN·인터넷에 직접 뜰 이유가 없다(평문 HTTP 직접 노출 차단).
    # 터널 없이 LAN에서 직접 붙어야 하면 DASHBOARD_BIND=0.0.0.0 으로 덮어쓴다.
    "$STREAMLIT_CMD" run dashboard/app.py \
        --server.port "$DASHBOARD_PORT" \
        --server.address "${DASHBOARD_BIND:-127.0.0.1}" \
        --server.headless true \
        --browser.gatherUsageStats false &
    PIDS+=("$!")
}

start_bot() {
    if [ "$START_BOT" != "1" ]; then
        echo "[4/4] Discord bot skipped. START_BOT=${START_BOT}"
        return 0
    fi

    if [ -z "${DISCORD_BOT_TOKEN:-}" ] || [ "${DISCORD_BOT_TOKEN:-}" = "your_discord_bot_token_here" ]; then
        echo "[bot] DISCORD_BOT_TOKEN is not configured. Bot will not start."
        return 0
    fi

    echo "[4/4] Starting Discord bot..."
    BACKEND_URL="$LOCAL_BACKEND_URL" "$PYTHON_CMD" -m bot.main &
    PIDS+=("$!")
}

echo "=========================================================="
echo "Discord AI League Inhouse Manager"
echo "=========================================================="
echo "Project:       $PROJECT_DIR"
echo "Local backend: $LOCAL_BACKEND_URL (local-only, used by the bot)"
echo "Deploy mode:   $PUBLIC_MODE"
echo "=========================================================="

ensure_python_deps
start_keep_awake
start_backend
start_dashboard
start_ddns
start_public_dashboard
start_bot

echo "=========================================================="
echo "All requested services are running."
echo "- FastAPI backend:   ${LOCAL_BACKEND_URL}/docs (local-only)"
echo "- Dashboard (local): http://127.0.0.1:${DASHBOARD_PORT}"
if [ -n "$PUBLIC_URL" ]; then
    echo "- Dashboard (public): ${PUBLIC_URL}"
    echo "                      (saved to ${PUBLIC_URL_FILE})"
fi
echo "Press Ctrl+C to stop services started by this script."
echo "=========================================================="

while true; do
    sleep 3600
done
