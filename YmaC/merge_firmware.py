#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
YmaC/merge_firmware.py — 合并 bootloader.hex + app.hex 为单一 .hex
=================================================================
阶段 6（YmaC 双固件）步骤 2：把两个（或多个）独立 Intel HEX 固件
映像按地址拼接合并成**一个**烧录映像。Bootloader 段 + App 段 + 参数区
各自来自不同 .elf 生成的 .hex，地址不重叠，合并后一次烧录整片 Flash。

纯 Python 标准库实现 Intel HEX 解析/合并（无第三方、不依赖 srec_cat），
host 可测。

CLI:
  python YmaC/merge_firmware.py merge -o merged.hex boot.hex app.hex
  python YmaC/merge_firmware.py merge --overlap over -o merged.hex app.hex param.hex
  python YmaC/merge_firmware.py info boot.hex
  python YmaC/merge_firmware.py selftest

仅依赖 Python 标准库。
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

# ---- 常量 ----

_REC_TYPE_DATA = 0   # 数据
_REC_TYPE_EOF = 1    # 文件结束
_REC_TYPE_ESA = 2    # 扩展段地址 (16 位基址 << 4)
_REC_TYPE_SSA = 3    # 起始段地址
_REC_TYPE_ELA = 4    # 扩展线性地址 (基址 << 16)
_REC_TYPE_SLA = 5    # 起始线性地址

_MAX_BYTES_PER_LINE = 255  # Intel HEX 单行数据字节上限


# ---- 错误 ----

class MergeError(Exception):
  """合并业务错误，携带可直接打印的中文信息。"""


# ---- Intel HEX 解析 ----

class Segment:
  """一段连续可写字节：[start, end) 绝对地址。data 为切好的字节。"""

  def __init__(self, start, data):
    self.start = start
    self.end = start + len(data)
    self.data = data


class HexImage:
  """单个 HEX 文件的解析结果。"""

  def __init__(self, source):
    self.source = source
    self.segments = []      # Segment 列表（解析后未排序）
    self.start_addr = None  # (记录类型, 值) 或 None


def _parse_hex(path):
  """解析单个 Intel HEX 文件 → HexImage。"""
  img = HexImage(str(path))
  base = 0
  start_addr = None
  eof_seen = False
  try:
    fh = open(path, "r", encoding="utf-8", errors="replace")
  except OSError as exc:
    raise MergeError(f"无法读取输入文件 {path}: {exc.strerror or exc}")
  with fh:
    for line_no, raw in enumerate(fh, start=1):
      ln = raw.strip()
      if not ln:
        continue
      if not ln.startswith(":"):
        raise MergeError(f"{img.source}:{line_no}: 行不以 ':' 开头")
      if len(ln) < 11 or (len(ln) % 2 != 1):
        raise MergeError(f"{img.source}:{line_no}: 行长非法")
      if not all(c in "0123456789abcdefABCDEF" for c in ln[1:]):
        raise MergeError(f"{img.source}:{line_no}: 含非十六进制字符")
      body = bytes.fromhex(ln[1:])
      n = body[0]
      if ((sum(body[:-1]) + body[-1]) & 0xFF) != 0:
        raise MergeError(f"{img.source}:{line_no}: 校验和错误")
      if len(body) != n + 5:
        raise MergeError(f"{img.source}:{line_no}: 行长与长度字段不符")
      addr = (body[1] << 8) | body[2]
      rtype = body[3]
      data = body[4:4 + n]

      if rtype == _REC_TYPE_DATA:
        img.segments.append(Segment(base + addr, data))
      elif rtype == _REC_TYPE_EOF:
        eof_seen = True
      elif rtype == _REC_TYPE_ELA:
        if len(data) >= 2:
          base = (data[0] << 24) | (data[1] << 16)
      elif rtype == _REC_TYPE_ESA:
        if len(data) >= 2:
          base = ((data[0] << 8) | data[1]) << 4
      elif rtype in (_REC_TYPE_SLA, _REC_TYPE_SSA):
        if len(data) == 4 and start_addr is None:
          start_addr = (rtype, int.from_bytes(data, "big"))
      else:
        pass  # 未知/非标准类型：忽略（向前兼容）
  if not eof_seen:
    raise MergeError(f"{img.source}: 缺少 EOF (类型 01) 记录")
  img.start_addr = start_addr
  return img


def _coalesce_segments(segments):
  """把 Segment 列表按地址排序，相邻/重叠合并（重叠处后者覆盖前者）。"""
  segs = sorted(segments, key=lambda x: x.start)
  out = []
  for seg in segs:
    if out and seg.start <= out[-1].end:
      prev = out[-1]
      new_len = seg.end - prev.start
      nd = bytearray(prev.data) + bytearray(b"\xFF" * (new_len - len(prev.data)))
      off = seg.start - prev.start
      nd[off:off + len(seg.data)] = seg.data
      out[-1] = Segment(prev.start, bytes(nd))
    else:
      out.append(seg)
  return out


# ---- 合并 ----

def merge(images, overlap_policy="error"):
  """多个 HexImage → 排序合并段 + 起始地址。

  overlap_policy: 'error' 任何地址重叠即抛 MergeError（默认，符合"拼接不重叠"）；
                  'over'  后者覆盖（参数区覆盖、显式 A/B 场景）。
  返回 (segments: [Segment], start_addr: (type, val) | None)。
  """
  if not images:
    raise MergeError("没有可合并的输入")

  segs = []  # [Segment, source]
  for img in images:
    for seg in _coalesce_segments(img.segments):
      segs.append([seg, img.source])
  segs.sort(key=lambda x: x[0].start)

  if overlap_policy == "error":
    for i in range(1, len(segs)):
      a, sa = segs[i - 1]
      b, sb = segs[i]
      if b.start < a.end:  # b 起点落在 a 内 → 交叉
        raise MergeError(
          f"地址重叠: {sb} [0x{b.start:08X}..0x{b.end:08X}) 与 "
          f"{sa} [0x{a.start:08X}..0x{a.end:08X})"
        )
    merged = [s[0] for s in segs]
  else:  # 'over'
    merged = _coalesce_segments([s[0] for s in segs])

  # 起始地址：优先 SLA，否则首个 SSA
  start = None
  for img in images:
    if not img.start_addr:
      continue
    t, v = img.start_addr
    if t == _REC_TYPE_SLA:
      start = img.start_addr
      break
    if start is None:
      start = img.start_addr
  return merged, start


# ---- Intel HEX 输出 ----

def render_hex(segments, start_addr=None, bytes_per_line=16):
  """把 [Segment] 渲染成 Intel HEX 文本（地址升序，64KB 分窗，校验和/EOF）。

  bytes_per_line: 每行数据字节数（上限 _MAX_BYTES_PER_LINE，默认 16）。
  """
  if bytes_per_line < 1 or bytes_per_line > _MAX_BYTES_PER_LINE:
    raise MergeError(f"bytes_per_line 须在 [1,{_MAX_BYTES_PER_LINE}]，实为 {bytes_per_line}")
  lines = []
  segs = sorted(segments, key=lambda x: x.start)

  def emit(body: bytes):
    lines.append(":" + (body + bytes([(-sum(body)) & 0xFF])).hex().upper())

  upper = None  # 当前 64KB 窗口（addr>>16）
  for seg in segs:
    addr = seg.start
    pos = 0
    total = len(seg.data)
    while pos < total:
      low = addr & 0xFFFF
      room_in_win = 0x10000 - low
      n = min(bytes_per_line, total - pos, room_in_win)

      win = (addr >> 16) & 0xFFFF
      if win != upper:
        upper = win
        emit(bytes([2, 0, 0, _REC_TYPE_ELA, (win >> 8) & 0xFF, win & 0xFF]))

      chunk = seg.data[pos:pos + n]
      emit(bytes([len(chunk), (low >> 8) & 0xFF, low & 0xFF, _REC_TYPE_DATA]) + chunk)
      addr += len(chunk)
      pos += len(chunk)

  if start_addr is not None:
    t, v = start_addr
    # 仅回写 SLA（8051 线性起始地址）；SSA(type 03/段基址) 对 ARM/C2000 无意义，不回写
    if t == _REC_TYPE_SLA and isinstance(v, int):
      emit(bytes([4, 0, 0, _REC_TYPE_SLA,
                  (v >> 24) & 0xFF, (v >> 16) & 0xFF, (v >> 8) & 0xFF, v & 0xFF]))
  emit(bytes([0, 0, 0, _REC_TYPE_EOF]))
  return "\n".join(lines)


# ---- host 自检 ----

def _selftest():
  """内置往返自检：构造段 → 编码 → 解析 → 比对字节 + 起始地址。"""
  import tempfile
  import os
  segs = [
    Segment(0x08000000, bytes(range(32))),
    Segment(0x08002000, b"\xAA\xBB\xCC\xDD"),
    Segment(0x08010000, b"\x01\x02"),  # 跨 64KB 窗口边界，验分窗
  ]
  hex_text = render_hex(segs, start_addr=(_REC_TYPE_SLA, 0x08000000))
  fd, tmp = tempfile.mkstemp(suffix=".hex")
  try:
    with os.fdopen(fd, "w") as fh:
      fh.write(hex_text + "\n")
    img = _parse_hex(tmp)
    got = {}
    for seg in _coalesce_segments(img.segments):
      for offset, byte in enumerate(seg.data):
        got[seg.start + offset] = byte
    expected = {}
    for seg in segs:
      for offset, byte in enumerate(seg.data):
        expected[seg.start + offset] = byte
    assert got == expected, "字节往返不一致"
    assert img.start_addr == (_REC_TYPE_SLA, 0x08000000), "起始地址丢失"
    return 0
  finally:
    os.remove(tmp)


# ---- 子命令 ----

def cmd_info(path):
  img = _parse_hex(path)
  if not img.segments:
    print(f"文件: {path}")
    print("（无数据段，仅含 EOF/元数据记录）")
    return 0
  min_a = min((s.start for s in img.segments))
  max_a = max((s.end for s in img.segments))
  total = sum(s.end - s.start for s in img.segments)
  print(f"文件: {path}")
  print(f"地址范围: [0x{min_a:08X} .. 0x{max_a:08X}]  总字节: {total}")
  for seg in _coalesce_segments(img.segments):
    print(f"  段 [0x{seg.start:08X} .. 0x{seg.end:08X})  {seg.end - seg.start} B")
  if img.start_addr:
    print(f"起始地址: type={img.start_addr[0]} value=0x{img.start_addr[1]:08X}")
  return 0


def cmd_merge(args):
  if len(args.inputs) < 2:
    raise MergeError("合并至少需要 2 个输入文件")
  images = [_parse_hex(p) for p in args.inputs]
  merged, start = merge(images, overlap_policy=args.overlap)
  hex_text = render_hex(merged, start_addr=start, bytes_per_line=args.chunk)
  out = Path(args.out)
  out.write_text(hex_text + "\n", encoding="utf-8", newline="\n")
  total = sum(s.end - s.start for s in merged)
  print(f"合并完成: {len(merged)} 段 / {total} 字节 → {out}")
  for seg in merged:
    print(f"  [0x{seg.start:08X} .. 0x{seg.end:08X})  {seg.end - seg.start} B")
  return 0


# ---- CLI ----

def main() -> int:
  parser = argparse.ArgumentParser(
    prog="merge_firmware",
    description="把 bootloader.hex + app.hex（等）合并成一个 Intel HEX 烧录映像",
  )
  sub = parser.add_subparsers(dest="command", required=True)

  p_merge = sub.add_parser("merge", help="合并多个 hex 为一个")
  p_merge.add_argument("inputs", nargs="+", help="输入 hex 文件（>=2）")
  p_merge.add_argument("-o", "--out", required=True, help="输出合并 hex 路径")
  p_merge.add_argument("--overlap", choices=["error", "over"], default="error",
                       help="地址重叠处理：error 报错 / over 后写覆盖（默认 error）")
  p_merge.add_argument("--chunk", type=int, default=16,
                       help="Intel HEX 每行数据字节数（默认 16，上限 255）")

  p_info = sub.add_parser("info", help="查看单个 hex 的地址范围/段")
  p_info.add_argument("path", help="hex 文件")

  sub.add_parser("selftest", help="运行内置往返自检")

  args = parser.parse_args()

  try:
    if args.command == "merge":
      return cmd_merge(args)
    if args.command == "info":
      return cmd_info(args.path)
    if args.command == "selftest":
      return _selftest()
  except MergeError as exc:
    print(f"错误: {exc}")
    return 1
  return 0


if __name__ == "__main__":
  sys.exit(main())
