
#!/bin/bash

set -euo pipefail

# ============================================================
# NVIDIA GPU Fan Controller Launcher
#
# 用法：
#   ./gpu_fan.sh           # 默认启动
#   ./gpu_fan.sh start
#   ./gpu_fan.sh stop
#   ./gpu_fan.sh status
#   ./gpu_fan.sh logs
#
# 自动检测：
#   DISPLAY
#   XAUTHORITY
#
# 自动使用 sudo 获取 GPU 风扇写入权限
# ============================================================

BASE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

PYTHON_SCRIPT="$BASE_DIR/gpu_fan.py"
PYTHON_BIN="/usr/bin/python3"
NVIDIA_SETTINGS="/usr/bin/nvidia-settings"

PID_FILE="/run/gpu-fan.pid"
LOG_FILE="/var/log/gpu-fan.log"

ACTION="${1:-start}"

# 非 root 用户自动提权
if [[ $EUID -ne 0 ]]; then
    exec sudo -- /bin/bash "$0" "$@"
fi


# ============================================================
# 检查 Python 进程
# ============================================================

get_running_pid() {
    [[ -f "$PID_FILE" ]] || return 1

    local pid
    read -r pid < "$PID_FILE" || return 1

    [[ "$pid" =~ ^[0-9]+$ ]] || return 1
    [[ -r "/proc/$pid/cmdline" ]] || return 1

    # 避免 PID 已被其他进程复用时误操作
    local cmdline
    cmdline=$(tr '\0' ' ' < "/proc/$pid/cmdline")

    [[ "$cmdline" == *"$PYTHON_SCRIPT"* ]] || return 1

    printf '%s\n' "$pid"
}


# ============================================================
# 自动查找 DISPLAY / XAUTHORITY
# ============================================================

detect_xorg() {
    local pid auth socket display_num result
    local -a argv
    local i

    DISPLAY_VALUE=""
    XAUTHORITY_VALUE=""

    # 优先查找当前桌面用户的 Xorg
    while IFS= read -r pid; do

        [[ -r "/proc/$pid/cmdline" ]] || continue

        argv=()
        mapfile -d '' -t argv < "/proc/$pid/cmdline"

        # 从 Xorg 启动参数提取 -auth 路径
        auth=""

        for ((i=0; i+1<${#argv[@]}; i++)); do
            if [[ "${argv[$i]}" == "-auth" ]]; then
                auth="${argv[$((i+1))]}"
                break
            fi
        done

        [[ -n "$auth" && -r "$auth" ]] || continue

        # 枚举当前 X11 DISPLAY socket
        for socket in /tmp/.X11-unix/X[0-9]*; do

            [[ -S "$socket" ]] || continue

            display_num="${socket##*/}"
            display_num="${display_num#X}"

            # 通过 NVIDIA 只读查询验证组合
            if result=$(
                env \
                    DISPLAY=":$display_num" \
                    XAUTHORITY="$auth" \
                    "$NVIDIA_SETTINGS" \
                    -t -q '[gpu:0]/GPUFanControlState' \
                    2>/dev/null
            ); then

                if [[ "$result" =~ ^[01]$ ]]; then
                    DISPLAY_VALUE=":$display_num"
                    XAUTHORITY_VALUE="$auth"
                    return 0
                fi

            fi
        done

    done < <(pgrep -x Xorg || true)

    return 1
}


# ============================================================
# 启动
# ============================================================

start() {
    local pid

    if pid=$(get_running_pid); then
        echo "GPU fan controller is already running (PID=$pid)"
        return 0
    fi

    rm -f "$PID_FILE"

    if [[ ! -f "$PYTHON_SCRIPT" ]]; then
        echo "ERROR: Python script not found: $PYTHON_SCRIPT"
        return 1
    fi

    echo "Detecting Xorg..."

    if ! detect_xorg; then
        echo "ERROR: Unable to locate a usable NVIDIA Xorg session"
        echo "Please log in to the Xorg desktop first."
        return 1
    fi

    echo "DISPLAY=$DISPLAY_VALUE"
    echo "XAUTHORITY=$XAUTHORITY_VALUE"

    touch "$LOG_FILE"

    # 脱离当前终端，后台运行 Python
    nohup env \
        DISPLAY="$DISPLAY_VALUE" \
        XAUTHORITY="$XAUTHORITY_VALUE" \
        "$PYTHON_BIN" -u "$PYTHON_SCRIPT" \
        >> "$LOG_FILE" 2>&1 < /dev/null &

    pid=$!
    echo "$pid" > "$PID_FILE"

    sleep 2

    if get_running_pid > /dev/null; then
        echo "GPU fan controller started (PID=$pid)"
        echo "Log: $LOG_FILE"
    else
        echo "ERROR: Controller exited after startup"
        rm -f "$PID_FILE"
        tail -n 30 "$LOG_FILE"
        return 1
    fi
}


# ============================================================
# 停止
# ============================================================

stop() {
    local pid

    if ! pid=$(get_running_pid); then
        echo "GPU fan controller is not running"
        rm -f "$PID_FILE"
        return 0
    fi

    echo "Stopping GPU fan controller (PID=$pid)..."

    # SIGTERM 将触发 Python 的恢复 Auto 逻辑
    kill -TERM "$pid"

    # 最多等待 20 秒，不强制 SIGKILL
    for ((i=0; i<20; i++)); do
        if ! kill -0 "$pid" 2>/dev/null; then
            rm -f "$PID_FILE"
            echo "GPU fan controller stopped"
            echo "Check logs to confirm NVIDIA Auto was restored."
            return 0
        fi

        sleep 1
    done

    echo "WARNING: Process did not exit in 20 seconds"
    return 1
}


# ============================================================
# 状态
# ============================================================

status() {
    local pid

    if pid=$(get_running_pid); then
        echo "GPU fan controller: RUNNING"
        echo "PID: $pid"

        nvidia-smi \
            --query-gpu=temperature.gpu,fan.speed \
            --format=csv,noheader
    else
        echo "GPU fan controller: STOPPED"
    fi
}


# ============================================================
# 日志
# ============================================================

logs() {
    if [[ ! -f "$LOG_FILE" ]]; then
        echo "Log file does not exist: $LOG_FILE"
        return 1
    fi

    tail -n 50 -f "$LOG_FILE"
}


# ============================================================
# 命令分发
# ============================================================

case "$ACTION" in
    start)
        start
        ;;
    stop)
        stop
        ;;
    status)
        status
        ;;
    logs)
        logs
        ;;
    *)
        echo "Usage: $0 {start|stop|status|logs}"
        exit 1
        ;;
esac

