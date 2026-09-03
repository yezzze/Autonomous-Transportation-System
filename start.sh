#!/usr/bin/env bash

set -uo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PID_FILE="${ROOT_DIR}/run/server.pid"
LOG_FILE="${ROOT_DIR}/logs/server.log"
CONFIG_FILE="${ROOT_DIR}/config/aoe_cluster_config.json"
# Conda 环境名称，设为空则不使用虚拟环境
CONDA_ENV="${CONDA_ENV-langmanus}"

# 是否启用 AOE Gossip 功能
export ENABLE_AOE_GOSSIP=1

usage() {
    echo "Usage: $0 [start|stop|status|restart]"
}

get_pid() {
    [[ -f "$PID_FILE" ]] && cat "$PID_FILE"
}

is_running() {
    local pid
    pid="$(get_pid)"
    [[ "$pid" =~ ^[0-9]+$ ]] && kill -0 "$pid" 2>/dev/null
}

activate_environment() {
    if [[ -z "$CONDA_ENV" ]]; then
        return 0
    fi

    if ! command -v conda >/dev/null 2>&1; then
        echo "Error: conda was not found in PATH." >&2
        return 1
    fi

    local conda_base
    conda_base="$(conda info --base 2>/dev/null)" || {
        echo "Error: unable to locate the conda installation." >&2
        return 1
    }

    # shellcheck disable=SC1091
    source "${conda_base}/etc/profile.d/conda.sh"
    conda activate "$CONDA_ENV" || {
        echo "Error: unable to activate conda environment '${CONDA_ENV}'." >&2
        return 1
    }
}

load_local_aoe_url() {
    local local_aoe_url

    if [[ ! -f "$CONFIG_FILE" ]]; then
        echo "Error: 请在 config/aoe_cluster_config.json 中设置非空的 local_aoe_url。" >&2
        return 1
    fi

    local_aoe_url="$(python -c 'import json, sys; value = json.load(open(sys.argv[1], encoding="utf-8")).get("local_aoe_url", ""); print(value.strip() if isinstance(value, str) else "")' "$CONFIG_FILE" 2>/dev/null)" || {
        echo "Error: 无法读取 config/aoe_cluster_config.json，请检查 JSON 格式并设置 local_aoe_url。" >&2
        return 1
    }

    if [[ -z "$local_aoe_url" ]]; then
        echo "Error: 请在 config/aoe_cluster_config.json 中设置非空的 local_aoe_url。" >&2
        return 1
    fi

    # 本地 AOE 服务地址
    export LOCAL_AOE_URL="$local_aoe_url"
}

start_server() {
    if is_running; then
        echo "server.py is already running (PID $(get_pid))."
        return 0
    fi

    if [[ -f "$PID_FILE" ]]; then
        rm -f "$PID_FILE"
    fi

    activate_environment || return 1
    load_local_aoe_url || return 1
    mkdir -p "$(dirname "$PID_FILE")" "$(dirname "$LOG_FILE")"

    cd "$ROOT_DIR" || return 1
    nohup python -u server.py >>"$LOG_FILE" 2>&1 &
    local pid=$!
    echo "$pid" >"$PID_FILE"

    sleep 1
    if kill -0 "$pid" 2>/dev/null; then
        echo "server.py started (PID $pid). Log: $LOG_FILE"
    else
        rm -f "$PID_FILE"
        echo "Error: server.py failed to start. Check log: $LOG_FILE" >&2
        return 1
    fi
}

stop_server() {
    if ! is_running; then
        [[ -f "$PID_FILE" ]] && rm -f "$PID_FILE"
        echo "server.py is not running."
        return 0
    fi

    local pid
    pid="$(get_pid)"
    kill "$pid"

    local count=0
    while kill -0 "$pid" 2>/dev/null && (( count < 60 )); do
        sleep 1
        ((count++))
    done

    if kill -0 "$pid" 2>/dev/null; then
        echo "server.py did not stop gracefully; sending SIGKILL (PID $pid)."
        kill -9 "$pid"
    fi

    rm -f "$PID_FILE"
    echo "server.py stopped."
}

status_server() {
    if is_running; then
        echo "server.py is running (PID $(get_pid))."
        return 0
    fi

    [[ -f "$PID_FILE" ]] && rm -f "$PID_FILE"
    echo "server.py is not running."
    return 3
}

case "${1:-}" in
    start)
        start_server
        ;;
    stop)
        stop_server
        ;;
    status)
        status_server
        ;;
    restart)
        stop_server && start_server
        ;;
    *)
        usage >&2
        exit 2
        ;;
esac
