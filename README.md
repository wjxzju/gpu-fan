# GPU Fan Controller

一个轻量的 NVIDIA GPU 风扇温控工具。通过 `nvidia-smi` 读取核心温度，使用 `nvidia-settings` 按自定义曲线调节风扇转速，支持后台运行和接口故障后的自动恢复。

当前配置用于 NVIDIA RTX 5090 D v2，控制 GPU 0 的单个风扇 `fan:0`。其他硬件需要自行确认 GPU、风扇编号以及驱动是否支持手动控制。

## 环境要求

- Linux、Bash 4.4 或更高版本、Python 3；Python 仅使用标准库，无需安装 pip 依赖。
- 已安装 NVIDIA 驱动、`nvidia-smi` 和 `nvidia-settings`。
- 可访问的 NVIDIA Xorg 会话，且驱动允许读写风扇控制属性。仅 `nvidia-smi` 可用并不代表风扇控制可用。
- sudo / root 权限，用于写入风扇设置、PID 文件和日志。

脚本默认使用 `/usr/bin/python3`、`/usr/bin/nvidia-smi` 和 `/usr/bin/nvidia-settings`。如果本机安装路径不同，请修改脚本中的对应配置。

## 使用方法

进入项目目录，在已登录 Xorg 桌面的情况下运行：

```bash
# 启动；不传参数时也默认启动
bash gpu_fan.sh start

# 查看进程状态、GPU 温度和风扇转速
bash gpu_fan.sh status

# 查看最近 50 行日志并持续跟踪
bash gpu_fan.sh logs

# 停止程序，并尝试恢复 NVIDIA 自动控温
bash gpu_fan.sh stop
```

启动脚本会自动请求 sudo 权限，从 Xorg 进程参数和 X11 socket 中寻找可用的 `DISPLAY` / `XAUTHORITY` 组合，然后通过 `nohup` 启动后台 Python 进程。关闭启动终端后，程序仍会继续运行。项目不配置 systemd 或开机自启动。

`logs` 中按 Ctrl+C 只结束日志查看，不停止控制器。修改配置后，需要先执行 `stop`，再执行 `start` 才会生效。

## 默认风扇曲线

| GPU 核心温度 | 目标转速 |
| --- | --- |
| ≤ 30°C | 30% |
| 40°C | 30% |
| 50°C | 40% |
| 60°C | 55% |
| 70°C | 80% |
| ≥ 80°C | 100% |

节点之间采用线性插值。目标转速相对上次设置变化达到 3 个百分点时才更新，以减少频繁小幅调速；采样温度达到 80°C 时，即使变化不足 3 个百分点，也会将目标设置为 100%。

## 配置

配置位于 `gpu_fan.py` 顶部：

| 配置项 | 默认值 | 说明 |
| --- | --- | --- |
| `GPU_ID` | `0` | 控制的 GPU 编号 |
| `FAN_IDS` | `(0,)` | 控制的风扇编号；当前硬件只有一个风扇 |
| `POLL_INTERVAL` | `5` | 每轮正常温控结束后的等待时间，单位秒；实际采样间隔还包含命令执行时间 |
| `SPEED_HYSTERESIS` | `3` | 更新目标转速所需的最小变化，单位为百分点 |
| `LOG_INTERVAL` | `30` | 常规状态日志的最小间隔，单位秒 |
| `RECOVERY_INTERVAL` | `10` | 故障恢复轮次之间的等待时间，单位秒 |
| `COMMAND_TIMEOUT` | `5` | Python 调用 NVIDIA 外部命令时设置的超时，单位秒 |
| `CURRENT_SPEED_TOLERANCE` | `3` | 实际转速与目标偏差超过该百分点数才记录警告 |
| `FAN_CURVE` | 见上表 | 按温度升序排列的风扇曲线 |

通常使用 `gpu_fan.sh` 即可自动探测 Xorg 环境。直接运行 Python 时，需要以 root 身份提供正确的 `DISPLAY` 和 `XAUTHORITY`；Python 中保留的 `:1` 和 `/run/user/1001/gdm/Xauthority` 只是本机默认值，不适用于所有机器。

## 故障恢复与退出

温度读取、控制模式查询、转速设置或设置验证失败时，程序进入内部恢复流程：

1. 如果已尝试手动控制，优先恢复 NVIDIA Auto，并查询 `GPUFanControlState` 确认返回 `0`。
2. 每轮恢复 Auto 最多尝试 3 次，失败的尝试之间等待 1 秒；整轮失败后等待 10 秒，继续下一轮，不因前三次失败就退出。
3. 确认 Auto 后等待 10 秒，重新读取温度并验证控制接口，再按新温度接管风扇，不沿用故障前的目标转速。
4. 尚未进入手动控制时的接口错误，会等待 10 秒后重试。

用于日志的实际转速查询失败只记录警告，不打断温控。`31%` 与 `30%` 这类小幅差异不会产生偏差警告。

收到 SIGTERM / SIGINT 时停止温控；如果仍需恢复 Auto，退出前会再尝试一轮。无法确认 Auto 时记录严重错误并以非零状态退出。其他未预期的程序错误也会执行退出清理。SIGKILL 无法执行清理逻辑。

日志中的 `NVIDIA Auto mode confirmed (control_state=0)` 表示驱动报告已切换到自动模式，不代表已验证后续物理风扇的调速表现。驱动持续无响应时，程序只能继续尝试，不能保证命令生效。

## 日志与运行文件

| 路径 | 用途 |
| --- | --- |
| `/var/log/gpu-fan.log` | 追加写入的控制器日志 |
| `/run/gpu-fan.pid` | 启动脚本记录的进程 PID |

路径可在 `gpu_fan.sh` 中修改。日志目前没有自动轮转。

`status` 显示 `RUNNING` 仅表示进程存在；是否已经接管风扇或正在恢复，需要查看日志。`stop` 最多等待 20 秒，不会强制 SIGKILL；若提示超时，应继续检查进程和日志，恢复 Auto 的命令可能仍在执行。

## 文件说明

- `gpu_fan.py`：温度采样、曲线计算、风扇控制、故障恢复和信号处理。
- `gpu_fan.sh`：Xorg 环境探测及 `start`、`stop`、`status`、`logs` 命令。

不操作 GPU 的基础语法检查：

```bash
bash -n gpu_fan.sh
python3 -c 'import ast; from pathlib import Path; ast.parse(Path("gpu_fan.py").read_text())'
```
