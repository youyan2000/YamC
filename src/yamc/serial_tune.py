"""yamc/serial_tune — 动态调参（串口 0xFB 帧，无 Qt 依赖）。

帧协议（48 字节，与 GUI Tab3 逐字节一致）：
  [0]    HEAD = 0x00
  [1]    CMD  = 0x14
  [2:42] 10 × float32 系数槽位（slot 0..9）
  [42:46] float32 校验码（默认 π = 3.1415927）
  [46:48] 保留（0）

pyserial 为可选依赖：send/watch/list 缺它时报明确错误（exit 2 的 CLI 层处理）。
"""

from __future__ import annotations

import struct
from typing import Optional

FRAME_SIZE = 48
HEAD = 0x00
CMD = 0x14
DEFAULT_CHECK = 3.1415927


class SerialTuneError(Exception):
    """动态调参业务错误（pyserial 缺失/端口不可用等）。"""


def build_frame(slots: dict[int, float], check: float = DEFAULT_CHECK) -> bytes:
    """构造 48 字节 0xFB 帧。

    slots: {slot_index: value}，slot ∈ [0,9]；未给出的槽位按 0.0。
    """
    frame = bytearray(FRAME_SIZE)
    frame[0] = HEAD
    frame[1] = CMD
    coef = [0.0] * 10
    for idx, val in slots.items():
        if 0 <= idx < 10:
            coef[idx] = float(val)
    for i in range(10):
        struct.pack_into("<f", frame, 2 + i * 4, coef[i])
    struct.pack_into("<f", frame, 42, float(check))  # [42-45] 校验码
    return bytes(frame)


def slots_from_params(params: list[dict], values: dict) -> dict[int, float]:
    """把 {dotted_key: value} 映射到帧槽位。

    params: 拓扑 params schema 条目（含 key/slot），slot ∈ [0,9] 才可下发。
    返回 {slot: value}；schema 无 slot 的 key 被忽略。
    """
    slots: dict[int, float] = {}
    for p in params:
        key = str(p.get("key", ""))
        slot = p.get("slot", -1)
        if not isinstance(slot, int):
            try:
                slot = int(slot)
            except (TypeError, ValueError):
                slot = -1
        if 0 <= slot < 10 and key in values:
            slots[slot] = float(values[key])
    return slots


def list_ports() -> list[str]:
    """枚举可用串口（需 pyserial）。"""
    try:
        import serial.tools.list_ports
    except ImportError:
        raise SerialTuneError("pyserial 未安装 — 动态调参需要: pip install pyserial")
    return [p.device for p in serial.tools.list_ports.comports()]


def send_tune(port: str, frame: bytes, baud: int = 115200, timeout: float = 1.0) -> None:
    """下发一帧 0xFB（需 pyserial）。"""
    try:
        import serial
    except ImportError:
        raise SerialTuneError("pyserial 未安装 — 动态调参需要: pip install pyserial")
    try:
        ser = serial.Serial(port, baud, timeout=timeout)
    except Exception as exc:
        raise SerialTuneError(f"无法打开 {port}: {exc}")
    try:
        ser.write(frame)
    finally:
        ser.close()


def watch(port: str, baud: int = 115200, duration: Optional[float] = None) -> None:
    """接收并打印固件回显（Ctrl-C 或 duration 秒后退出）。"""
    try:
        import serial
    except ImportError:
        raise SerialTuneError("pyserial 未安装 — 动态调参需要: pip install pyserial")
    import time
    try:
        ser = serial.Serial(port, baud, timeout=0.1)
    except Exception as exc:
        raise SerialTuneError(f"无法打开 {port}: {exc}")
    try:
        deadline = None if duration is None else time.time() + duration
        while True:
            n = ser.in_waiting
            if n > 0:
                data = ser.read(n)
                try:
                    text = data.decode("utf-8", errors="replace")
                except Exception:
                    text = repr(data)
                if text.strip():
                    print(text.rstrip())
            if deadline is not None and time.time() > deadline:
                break
    except KeyboardInterrupt:
        pass
    finally:
        ser.close()