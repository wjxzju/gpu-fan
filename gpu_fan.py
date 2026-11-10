
#!/usr/bin/env python3
"""
NVIDIA RTX 5090 D v2 - Automatic GPU Fan Controller

功能：
1. 根据 GPU 温度自动调节风扇转速
2. 支持多风扇同步控制
3. 线性插值风扇曲线
4. 避免频繁小幅调速
5. 检测手动控制状态是否生效
6. 温度读取失败时恢复 NVIDIA Auto
7. SIGTERM / SIGINT 退出时恢复 Auto
8. 接口异常后持续恢复，恢复正常后重新接管风扇

依赖：
    nvidia-smi
    nvidia-settings

运行权限：
    root
"""

import logging
import os
import re
import signal
import subprocess
import threading
import time


# ============================================================
# 配置
# ============================================================

GPU_ID = 0
FAN_IDS = (0, )

# GPU 温度采样间隔（秒）
POLL_INTERVAL = 5

# 风扇速度变化小于该数值时不更新
SPEED_HYSTERESIS = 3

# 常规状态日志间隔（秒）
LOG_INTERVAL = 30

# 故障恢复的重试间隔；保留单次命令超时，避免无限等待
RECOVERY_INTERVAL = 10
COMMAND_TIMEOUT = 5

# 实际转速允许的小幅偏差（百分点）
CURRENT_SPEED_TOLERANCE = 3

# GPU 温度 -> 风扇目标转速
# 按温度升序排列
FAN_CURVE = [
    (30, 30),
    (40, 30),
    (50, 40),
    (60, 55),
    (70, 80),
    (80, 100),
]

NVIDIA_SMI = "/usr/bin/nvidia-smi"
NVIDIA_SETTINGS = "/usr/bin/nvidia-settings"

# 已确认的 Xorg 会话
os.environ.setdefault("DISPLAY", ":1")
os.environ.setdefault(
    "XAUTHORITY",
    "/run/user/1001/gdm/Xauthority",
)

stop_event = threading.Event()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)

log = logging.getLogger("gpu-fan")

# 外部命令和返回数据的错误可以恢复；其他程序错误仍然退出并恢复 Auto。
RECOVERABLE_ERRORS = (
    subprocess.SubprocessError,
    OSError,
    ValueError,
    RuntimeError,
)


# ============================================================
# 命令执行
# ============================================================

def run_cmd(args):
    """执行外部命令，检查返回码和 NVIDIA 错误信息。"""

    result = subprocess.run(
        args,
        capture_output=True,
        text=True,
        timeout=COMMAND_TIMEOUT,
    )

    output = (
        (result.stdout or "")
        + "\n"
        + (result.stderr or "")
    )

    if result.returncode != 0:
        raise RuntimeError(
            f"Command failed: {args}\n"
            f"Return code: {result.returncode}\n"
            f"{output.strip()}"
        )

    # 部分 nvidia-settings 错误可能出现在输出文本中
    if re.search(r"(?im)^\s*ERROR\s*:", output):
        raise RuntimeError(
            f"NVIDIA command error:\n{output.strip()}"
        )

    return result.stdout.strip()


def nvidia_settings(*args):
    return run_cmd([NVIDIA_SETTINGS, *args])


def query_int(attribute):
    """查询 NVIDIA 整数属性。"""

    output = nvidia_settings(
        "-t",
        "-q",
        attribute,
    )

    return int(output.strip())


# ============================================================
# GPU 状态
# ============================================================

def get_gpu_temperature():
    """读取 GPU 核心温度。"""

    output = run_cmd([
        NVIDIA_SMI,
        "-i", str(GPU_ID),
        "--query-gpu=temperature.gpu",
        "--format=csv,noheader,nounits",
    ])

    if not output:
        raise ValueError("GPU temperature query returned no data")

    temp = float(output.splitlines()[0].strip())

    # 防止异常传感器读数
    if not 10 <= temp <= 110:
        raise ValueError(
            f"Invalid GPU temperature: {temp}"
        )

    return temp


def get_control_state():
    """0 = NVIDIA Auto, 1 = Manual。"""

    return query_int(
        f"[gpu:{GPU_ID}]/GPUFanControlState"
    )


def get_fan_target(fan_id):
    return query_int(
        f"[fan:{fan_id}]/GPUTargetFanSpeed"
    )


def get_fan_current(fan_id):
    return query_int(
        f"[fan:{fan_id}]/GPUCurrentFanSpeed"
    )


# ============================================================
# 风扇曲线
# ============================================================

def calculate_speed(temp):
    """根据 GPU 温度进行线性插值。"""

    if temp <= FAN_CURVE[0][0]:
        return FAN_CURVE[0][1]

    if temp >= FAN_CURVE[-1][0]:
        return FAN_CURVE[-1][1]

    for (t1, s1), (t2, s2) in zip(
        FAN_CURVE[:-1],
        FAN_CURVE[1:],
    ):
        if t1 <= temp <= t2:
            ratio = (temp - t1) / (t2 - t1)
            speed = s1 + ratio * (s2 - s1)

            return max(30, min(100, round(speed)))

    raise RuntimeError("Invalid fan curve")


# ============================================================
# 风扇控制
# ============================================================

def set_fan_speed(speed, enable_manual=False):
    """
    设置 FAN_IDS 中配置的风扇。

    enable_manual=True 时先开启手动模式。
    将所有赋值放在一次 nvidia-settings 调用中。
    """

    args = []

    if enable_manual:
        args.extend([
            "-a",
            f"[gpu:{GPU_ID}]/GPUFanControlState=1",
        ])

    for fan_id in FAN_IDS:
        args.extend([
            "-a",
            f"[fan:{fan_id}]/GPUTargetFanSpeed={speed}",
        ])

    nvidia_settings(*args)

    # 检查手动模式是否真正生效
    if get_control_state() != 1:
        raise RuntimeError(
            "GPU manual fan control is not active"
        )

    # 检查所配置风扇的目标值
    for fan_id in FAN_IDS:
        actual_target = get_fan_target(fan_id)

        if actual_target != speed:
            raise RuntimeError(
                f"Fan {fan_id}: expected target "
                f"{speed}%, got {actual_target}%"
            )


def restore_auto_mode():
    """恢复 NVIDIA 原生自动风扇控制。"""

    nvidia_settings(
        "-a",
        f"[gpu:{GPU_ID}]/GPUFanControlState=0",
    )

    if get_control_state() != 0:
        raise RuntimeError(
            "Failed to restore NVIDIA Auto mode"
        )

    log.info("NVIDIA Auto mode confirmed (control_state=0)")


def safe_restore_auto():
    """一轮最多尝试三次；运行中的恢复循环会继续发起下一轮。"""

    for attempt in range(1, 4):
        try:
            restore_auto_mode()
            return True

        except Exception as exc:
            log.error(
                "Restore Auto failed (%d/3): %s",
                attempt,
                exc,
            )

            if attempt < 3:
                time.sleep(1)

    log.critical("NVIDIA Auto mode could not be confirmed after 3 attempts")

    return False


# ============================================================
# 信号处理
# ============================================================

def handle_signal(signum, frame):
    log.info("Received signal %s, stopping", signum)
    stop_event.set()


# ============================================================
# 辅助状态日志
# ============================================================

def log_status(temp, target_speed):
    """实际转速仅供观察，查询失败不打断温控。"""

    try:
        current_speed = get_fan_current(FAN_IDS[0])
    except RECOVERABLE_ERRORS as exc:
        log.warning("Fan status query failed; continuing control: %s", exc)
        return

    log.info(
        "GPU=%.1f C, target=%d%%, fan%d_current=%d%%",
        temp, target_speed, FAN_IDS[0], current_speed,
    )

    if abs(current_speed - target_speed) > CURRENT_SPEED_TOLERANCE:
        log.warning(
            "Fan %d current speed differs from target: %d%% vs %d%%",
            FAN_IDS[0], current_speed, target_speed,
        )


# ============================================================
# 主程序
# ============================================================

def main():

    signal.signal(signal.SIGTERM, handle_signal)
    signal.signal(signal.SIGINT, handle_signal)

    if os.geteuid() != 0:
        log.error(
            "Root privileges are required "
            "for GPU fan control"
        )
        return 1

    log.info("GPU Fan Controller starting")
    log.info("GPU ID: %d", GPU_ID)
    log.info("Fan IDs: %s", FAN_IDS)
    log.info("DISPLAY: %s", os.environ["DISPLAY"])

    last_speed = None
    last_log_time = 0

    # 是否尝试过进入手动模式
    # 即使后续部分操作失败，也应恢复 Auto
    manual_attempted = False
    restore_pending = False
    exit_code = 0

    try:
        while not stop_event.is_set():
            # Auto 未确认前持续重试，绝不直接重新开启手动模式。
            if restore_pending:
                if not safe_restore_auto():
                    log.error(
                        "Auto restore still pending; retrying in %ds",
                        RECOVERY_INTERVAL,
                    )
                    stop_event.wait(RECOVERY_INTERVAL)
                    continue

                manual_attempted = False
                restore_pending = False
                log.info(
                    "Auto confirmed; retrying GPU control in %ds",
                    RECOVERY_INTERVAL,
                )
                if stop_event.wait(RECOVERY_INTERVAL):
                    break

            try:
                # 每次重新接管都读取新温度，不沿用故障前的转速。
                temp = get_gpu_temperature()
                state = get_control_state()

                if state not in (0, 1):
                    raise RuntimeError(f"Invalid control state: {state}")

                if last_speed is not None and state != 1:
                    raise RuntimeError(
                        f"GPU fan control unexpectedly switched to state={state}"
                    )

                target_speed = calculate_speed(temp)

                # 收到退出信号后不再开始新的手动控制。
                if stop_event.is_set():
                    break

                if last_speed is None:
                    manual_attempted = True
                    set_fan_speed(target_speed, enable_manual=True)
                    last_speed = target_speed
                    log.info(
                        "GPU control active: temperature=%.1f C, target=%d%%",
                        temp, last_speed,
                    )

                elif (
                    abs(target_speed - last_speed) >= SPEED_HYSTERESIS
                    or (temp >= 80 and last_speed < 100)
                ):
                    set_fan_speed(target_speed)
                    last_speed = target_speed
                    log.info(
                        "Fan updated: temperature=%.1f C, speed=%d%%",
                        temp, last_speed,
                    )

                now = time.monotonic()
                if now - last_log_time >= LOG_INTERVAL:
                    log_status(temp, last_speed)
                    last_log_time = now

            except RECOVERABLE_ERRORS:
                log.exception(
                    "GPU control interrupted; entering recovery "
                    "(retry interval=%ds)", RECOVERY_INTERVAL,
                )
                last_speed = None
                last_log_time = 0
                restore_pending = manual_attempted
                if not restore_pending:
                    stop_event.wait(RECOVERY_INTERVAL)
                continue

            stop_event.wait(POLL_INTERVAL)

        log.info("GPU Fan Controller stopping")

    except Exception:
        log.exception("GPU Fan Controller failed")
        exit_code = 1

    finally:
        # 无论正常退出还是异常退出，
        # 只要尝试过手动控制就恢复 Auto。
        if manual_attempted:
            if not safe_restore_auto():
                log.critical("Stopping without confirmed Auto; manual intervention required")
                exit_code = 1

    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
