"""动态调参 golden: 0xFB 48 字节帧逐字节断言（与 GUI Tab3 帧协议一致）。"""

from __future__ import annotations

import struct

from yamc import serial_tune as S


def test_frame_layout() -> None:
    frame = S.build_frame({})
    assert isinstance(frame, bytes)
    assert len(frame) == 48
    assert frame[0] == 0x00  # HEAD
    assert frame[1] == 0x14  # CMD
    check = struct.unpack("<f", frame[42:46])[0]
    assert abs(check - 3.1415927) < 1e-6


def test_frame_slots() -> None:
    frame = S.build_frame({0: 1.0, 9: -2.5})
    assert struct.unpack("<f", frame[2:6])[0] == 1.0
    assert struct.unpack("<f", frame[38:42])[0] == -2.5
    # 未给槽位为 0
    assert struct.unpack("<f", frame[6:10])[0] == 0.0


def test_frame_golden_bytes() -> None:
    """给 slot 1 = 2.0 → offset 6:10 应为小端 float32 2.0。"""
    frame = S.build_frame({1: 2.0})
    assert frame[6:10] == struct.pack("<f", 2.0)


def test_slots_from_params() -> None:
    params = [
        {"key": "pid_v.kp", "slot": 0},
        {"key": "pid_v.ki", "slot": 1},
        {"key": "vref"},  # 无 slot → 忽略
    ]
    slots = S.slots_from_params(params, {"pid_v.kp": 1.5, "pid_v.ki": 0.2, "vref": 12.0})
    assert slots == {0: 1.5, 1: 0.2}