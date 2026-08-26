#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
YmaC/flash_map_gen.py — Flash 分区真相 → bsp_flash_map.h + 两个链接脚本
========================================================================
读 Config/flash_map.yaml（分区真相）→ 生成:
  - bsp_flash_map.h       const BspFlashRegion 数组（bootloader/app/param 等地址）
  - bootloader_flash.ld     Bootloader 段链接脚本（FLASH = bootloader 分区）
  - app_flash.ld            App 段链接脚本（FLASH = app 分区）

这是"YmaC 双固件生成"（阶段 6）最独立、最基础的一步，host 可测、纯函数。

CLI:
  python YmaC/flash_map_gen.py list [--map <yaml>]                  # 列出全部 MCU 及分区概况
  python YmaC/flash_map_gen.py show <mcu> [--map <yaml>]            # 展示某 MCU 的完整解析分区（校验 + 推导值）
  python YmaC/flash_map_gen.py gen [--mcu <mcu>] [--out <dir>] [--map <yaml>]   # 生成 bsp_flash_map.h + 两个 .ld

  参数: --map flash_map.yaml 路径（默认 Config/flash_map.yaml，相对仓库根）

仅依赖 Python 标准库 + pyyaml（与 scaffold.py 一致）。
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

import yaml

# ---- 常量 ----

# 仓库根判定层目录（与 scaffold.py 一致）
_SCAN_DIRS = ["BSP", "Components", "Devices", "Module", "App"]

# 链接脚本模板（相对仓库根）；生成时替换 __FLASH_ORIGIN / __FLASH_LENGTH 两行
BOOT_LD_TPL = "cmake/bootloader_flash.ld"
APP_LD_TPL = "cmake/app_flash.ld"

# 生成物命名
OUT_FLASH_MAP_H = "bsp_flash_map.h"
OUT_BOOT_LD = "bootloader_flash.ld"
OUT_APP_LD = "app_flash.ld"

# 枚举标准分区（便于生成 bsp_flash_map.h 的稳定数组顺序 + 便捷宏）
STD_REGIONS = ("bootloader", "app", "param")

# ---- 错误 ----

class FlashMapError(Exception):
  """flash_map 业务错误，携带可直接打印的中文信息。"""


# ---- 工具 ----

def _as_list(v):
  if v is None:
    return []
  if isinstance(v, str):
    return [v]
  if isinstance(v, (list, tuple)):
    return list(v)
  return [v]


def find_repo_root(start=None):
  """从 start 向上搜索仓库根（含 BSP/Components/Devices/Module/App 层目录）。与 scaffold 同规则。"""
  here = Path(start or os.getcwd()).resolve()
  for d in [here] + list(here.parents):
    hits = [name for name in _SCAN_DIRS if (d / name).is_dir()]
    if len(hits) >= 3:
      return d
  return None


def parse_size(text):
  """解析 '64K' / '512K' / '2K' / '1M' / '240K' / 纯字节数字 → int 字节。

  大小写不敏感，K=1024 / M=1024*1024。非法则抛 FlashMapError。
  """
  if text is None:
    raise FlashMapError("size/base 缺失")
  s = str(text).strip().upper()
  mult = 1
  if s.endswith("K"):
    mult = 1024
    s = s[:-1]
  elif s.endswith("M"):
    mult = 1024 * 1024
    s = s[:-1]
  try:
    val = int(s, 0)  # 0x 前缀也支持
  except ValueError:
    raise FlashMapError(f"无法解析大小/地址: '{text}'")
  return val * mult


def resolve_partitions(mcu_cfg):
  """把某 MCU 的原始 YAML 段解析为规范化字典：
     { "flash_base": int, "flash_size": int,
       "page_size": int|None, "sector_size": int|None,
       "partitions": {"name": {"addr": int, "size": int}, ...} }

  解析字节，并做基础合法性校验（地址/大小带符号、分区不越界）。
  """
  if not isinstance(mcu_cfg, dict):
    raise FlashMapError("MCU 段不是 YAML 映射")
  fb = parse_size(mcu_cfg.get("flash_base"))
  fs = parse_size(mcu_cfg.get("flash_size"))
  page = parse_size(mcu_cfg["page_size"]) if mcu_cfg.get("page_size") else None
  sector = parse_size(mcu_cfg["sector_size"]) if mcu_cfg.get("sector_size") else None
  raw_parts = mcu_cfg.get("partitions")
  if not isinstance(raw_parts, dict) or not raw_parts:
    raise FlashMapError("缺少 partitions 映射")

  parts = {}
  for name, spec in raw_parts.items():
    if not isinstance(spec, dict):
      raise FlashMapError(f"分区 '{name}' 不是映射（应为 {{addr, size}}）")
    addr = parse_size(spec.get("addr"))
    size = parse_size(spec.get("size"))
    if size <= 0:
      raise FlashMapError(f"分区 '{name}' size 必须 > 0")
    if addr < fb or addr + size > fb + fs:
      raise FlashMapError(
        f"分区 '{name}' [{addr:#x}, {addr + size:#x}) 越出 Flash [{fb:#x}, {fb + fs:#x})"
      )
    parts[name] = {"addr": addr, "size": size}

  # 重叠检测：只拒绝"交叉"重叠（边界互相穿切）。完全被包含（子区，如 fw_stage 内嵌于 app）视为合法。
  #   合法: a 在 b 内 / b 在 a 内（A/B 暂存区常内嵌于 app）。
  #   非法: 两区分界交叉（既非互含又不含于对方、却重叠）。
  #
  # 用两两全检（分区数极小，≤几项）：任何一对分区，若重叠且既非 a⊆b 也非 b⊆a → 交叉 → 拒绝。
  # 只用单一面扫描（如"与当前末尾最远区间比较"）会漏掉同被一个容器包含的交叉对（如
  # app 内两个 `fw_stage` 子区互叠），故必须全对比较。
  def _contains(x, y):
    return x["addr"] <= y["addr"] and y["addr"] + y["size"] <= x["addr"] + x["size"]

  segs = sorted(parts.items(), key=lambda kv: kv[1]["addr"])
  for i in range(len(segs)):
    an, a = segs[i]
    for j in range(i + 1, len(segs)):
      bn, b = segs[j]
      if _contains(a, b) or _contains(b, a):
        continue  # 互含=合法（子区/含容器）
      if a["addr"] < b["addr"] + b["size"] and b["addr"] < a["addr"] + a["size"]:
        raise FlashMapError(f"分区 '{an}' 与 '{bn}' 交叉重叠")

  return {
    "flash_base": fb,
    "flash_size": fs,
    "page_size": page,
    "sector_size": sector,
    "partitions": parts,
  }


def load_flash_map(map_path):
  """读取 flash_map.yaml，返回 { mcu_name: cfg | FlashMapError }（保持 YAML 顺序）。

  容错：单个 MCU 段解析失败不中止整个文件——用 FlashMapError 占位，
  list/show 仍可列出其余 MCU；只有对坏 MCU 发起 show/gen/lookup 时才报错。
  """
  p = Path(map_path)
  if not p.is_file():
    raise FlashMapError(f"flash_map.yaml 不存在: {p}")
  with open(p, "r", encoding="utf-8") as fh:
    raw = yaml.safe_load(fh)
  if not isinstance(raw, dict):
    raise FlashMapError(f"{p}: 内容不是 YAML 映射")
  out = {}
  for name, cfg in raw.items():
    try:
      out[name] = resolve_partitions(cfg)
    except FlashMapError as exc:
      out[name] = exc
  if not out:
    raise FlashMapError(f"{p}: 未定义任何 MCU")
  return out


def lookup_mcu(maps, mcu, arg_name="--mcu"):
  """按名字取 MCU；若 mcu 为空且只有一个，默认取它；否则报错列出可选。arg_name 供错误提示。"""
  if mcu:
    key = mcu.strip()
    if key not in maps:
      raise FlashMapError(f"未找到 MCU '{key}'。可选: {', '.join(maps)}")
    return key
  if len(maps) == 1:
    return next(iter(maps))
  raise FlashMapError(f"需指定 {arg_name}。可选: {', '.join(maps)}")


def get_mcu_cfg(maps, name):
  """取某 MCU 的解析 cfg；若它是占位错误则抛出。"""
  item = maps[name]
  if isinstance(item, FlashMapError):
    raise FlashMapError(f"MCU '{name}' 配置无效: {item}")
  return item


def is_arm_mcu(cfg):
  """判断是否 STM32 风格（有 page_size → 用 GNU ld 链接脚本）。
     C2000 无 page_size 而有 sector_size → 用 TI .cmd，本步骤暂不入 .ld 生成。"""
  return cfg["page_size"] is not None


# ---- 子命令：list ----

def cmd_list(maps):
  print(f"=== flash_map.yaml — MCU 分区概况 ({len(maps)}) ===")
  for name, item in maps.items():
    if isinstance(item, FlashMapError):
      print(f"[{name}] (解析失败: {item})")
      continue
    cfg = item
    fs = cfg["flash_size"]
    fb = cfg["flash_base"]
    kind = "STM32" if is_arm_mcu(cfg) else "C2000"
    print(f"[{name}] ({kind}) Flash base 0x{fb:08X}, {fs} 字节")
    for pn, pc in cfg["partitions"].items():
      print(f"  {pn:<12} 0x{pc['addr']:08X} {pc['size']:>8} B")
  return 0


# ---- 子命令：show ----

def cmd_show(maps, mcu):
  name = lookup_mcu(maps, mcu, arg_name="<mcu>")
  cfg = get_mcu_cfg(maps, name)
  fb = cfg["flash_base"]
  fs = cfg["flash_size"]
  print(f"=== MCU: {name} ===")
  print(f"flash_base: 0x{fb:08X}  flash_size: {fs}  "
        f"page_size: {cfg['page_size'] or '-'}  sector_size: {cfg['sector_size'] or '-'}")
  print(f"arm_ld: {'是' if is_arm_mcu(cfg) else '否（C2000 用 TI .cmd，本步骤不生成 .ld）'}")
  for pn, pc in sorted(cfg["partitions"].items(), key=lambda kv: kv[1]["addr"]):
    print(f"  {pn:<12} [0x{pc['addr']:08X} .. 0x{pc['addr'] + pc['size'] - 1:08X}]  "
          f"size {pc['size']} B")
  # 推导值（供 mod_bootloader 配置）
  app = cfg["partitions"].get("app")
  if app:
    print(f"  app_addr=0x{app['addr']:08X}  app_max_size={app['size']}")
  return 0


# ---- 生成 bsp_flash_map.h ----

def _write_flash_map_h(cfg):
  parts = cfg["partitions"]
  target = cfg["flash_base"]

  lines = []
  lines.append("// 自动生成 — Flash 分区表 (由 YmaC/flash_map_gen.py 从 Config/flash_map.yaml 生成)")
  lines.append("// 文件: bsp_flash_map.h — 平台无关分区定义, 供 Bootloader / Database / 拓扑链接使用.")
  lines.append("// 不要手动编辑; 改分区真相应改 Config/flash_map.yaml 后重新生成.")
  lines.append("")
  lines.append("#ifndef BSP_FLASH_MAP_H")
  lines.append("#define BSP_FLASH_MAP_H")
  lines.append("")
  lines.append("#include <stdint.h>   // uint32_t (本头自包含，不依赖消费方先行 include)")
  lines.append("")

  # 平台/基址便捷宏
  ach = lambda v: f"0x{v:08X}u"
  lines.append(f"// 目标 MCU Flash 基址 / 总大小")
  lines.append(f"#define BSP_FLASH_BASE    {ach(target)}")
  lines.append(f"#define BSP_FLASH_SIZE    {cfg['flash_size']}u")
  lines.append("")

  # BspFlashRegion 结构体
  lines.append("// 分区条目 — name 分区名, addr 起始地址, size 大小(字节)")
  lines.append("typedef struct {")
  lines.append("  const char   *name;")
  lines.append("  uint32_t      addr;")
  lines.append("  uint32_t      size;")
  lines.append("} BspFlashRegion;")
  lines.append("")

  # 便捷宏：每分区的地址/大小，保证 mod_bootloader 配置直接取
  for pn, pc in parts.items():
    macro = f"BSP_FLASH_{pn.upper()}".replace("-", "_")
    lines.append(f"#define {macro}_ADDR  {ach(pc['addr'])}")
    lines.append(f"#define {macro}_SIZE  {pc['size']}u")
  lines.append("")

  # 分区数组（枚举顺序 + 其余附加分区按地址序）
  names = [n for n in STD_REGIONS if n in parts]
  names += sorted((n for n in parts if n not in STD_REGIONS),
                  key=lambda n: parts[n]["addr"])
  lines.append(f"// 分区条目数")
  lines.append(f"#define BSP_FLASH_REGION_COUNT  {len(parts)}u")
  lines.append("")
  lines.append("// 分区数组（bootloader/app/param 标准序 + 其余附加分区按地址序）— 上层可遍历或用便捷宏直接引用")
  lines.append("static const BspFlashRegion kFlashRegions[] = {")
  for n in names:
    lines.append(f"  {{ \"{n}\", {ach(parts[n]['addr'])}, {parts[n]['size']}u }},")
  lines.append("};")
  lines.append("")
  lines.append("#endif  // BSP_FLASH_MAP_H")
  lines.append("")
  return "\n".join(lines)


# ---- 生成两个链接脚本 ----

def _subst_flash(root, name, cfg, part_name, out):
  """把链接脚本模板里 __FLASH_ORIGIN / __FLASH_LENGTH 两行替换为指定分区值。

  part_name: 取自 cfg 的哪个分区（bootloader / app）。ARM 链接脚本模板含完整段定义，
  只改 FLASH 起点/长度，保持其他（向量表/堆栈/段）不变。
  分区必须存在（与 app 对称），缺失即报错——避免"无 bootloader 配置静默得到超大 boot .ld"。
  """
  tpl = root / name
  if not tpl.is_file():
    raise FlashMapError(f"链接脚本模板不存在: {tpl}")
  pc = cfg["partitions"]
  if part_name not in pc:
    raise FlashMapError(f"MCU 缺少分区 '{part_name}'，无法生成 {name}")
  origin = pc[part_name]["addr"]
  length = pc[part_name]["size"]
  nline = f"__FLASH_ORIGIN = 0x{origin:08X};  /* 分区 '{part_name}' · YmaC/flash_map_gen 注入 */"
  lline = f"__FLASH_LENGTH = {length};   /* 分区 '{part_name}' · YmaC/flash_map_gen 注入 */"
  lines = []
  src = tpl.read_text(encoding="utf-8").splitlines()
  for ln in src:
    if ln.lstrip().startswith("__FLASH_ORIGIN ="):
      lines.append(nline)
    elif ln.lstrip().startswith("__FLASH_LENGTH ="):
      lines.append(lline)
    else:
      lines.append(ln)
  # 模板必须含 __FLASH_ORIGIN 与 __FLASH_LENGTH 两行赋值，缺任一即报错（防止漏换保留旧值）
  defined = [l.split("=")[0].strip() for l in lines if "=" in l]
  missing = [c for c in ("__FLASH_ORIGIN", "__FLASH_LENGTH") if c not in defined]
  if missing:
    raise FlashMapError(f"模板 {name} 缺少 {', '.join(missing)} 赋值，无法注入")
  text = "\n".join(lines)
  out.write_text(text, encoding="utf-8", newline="\n")


# ---- 子命令：gen ----

def cmd_gen(root, map_path, mcu, out_dir):
  maps = load_flash_map(map_path)
  name = lookup_mcu(maps, mcu)
  cfg = get_mcu_cfg(maps, name)

  # ---- 先校验，再写任何输出（避免出错时留下半个产物）----
  if is_arm_mcu(cfg):
    missing = [p for p in ("bootloader", "app") if p not in cfg["partitions"]]
    if missing:
      raise FlashMapError(
        f"MCU '{name}' 缺少分区 {', '.join(missing)}，无法生成链接脚本"
      )

  out = Path(out_dir).resolve() if out_dir else (root / "build" / "gen" / name)
  out.mkdir(parents=True, exist_ok=True)

  # 1) bsp_flash_map.h
  (out / OUT_FLASH_MAP_H).write_text(
    _write_flash_map_h(cfg), encoding="utf-8", newline="\n")
  print(f"生成: {out / OUT_FLASH_MAP_H}")

  # 2) 两个链接脚本（仅 ARM/STM32 风格；C2000 .cmd 留待阶段 6 后续）
  if is_arm_mcu(cfg):
    _subst_flash(root, BOOT_LD_TPL, cfg, "bootloader", out / OUT_BOOT_LD)
    _subst_flash(root, APP_LD_TPL, cfg, "app", out / OUT_APP_LD)
    print(f"生成: {out / OUT_BOOT_LD}  (分区 'bootloader')")
    print(f"生成: {out / OUT_APP_LD}  (分区 'app')")
  else:
    print(f"提示: MCU '{name}' 是 C2000（TI linker 需 .cmd 而非 GNU .ld），" +
          "链路脚本 .cmd 生成留待 YmaC 阶段 6 后续。本步未生成 .ld。")
  print(f"输出目录: {out}")
  return 0


# ---- CLI ----

def main() -> int:
  parser = argparse.ArgumentParser(
    prog="flash_map_gen",
    description="Flash 分区真相 → bsp_flash_map.h + 两个链接脚本",
  )
  sub = parser.add_subparsers(dest="command", required=True)

  # 每个子命令都接受 --map <path>（默认 Config/flash_map.yaml，相对仓库根解析）
  def _add_common(sp):
    sp.add_argument("--map", default="Config/flash_map.yaml",
                    help="flash_map.yaml 路径（默认 Config/flash_map.yaml，相对仓库根）")

  p_list = sub.add_parser("list", help="列出 flash_map.yaml 全部 MCU 及分区概况")
  _add_common(p_list)

  p_show = sub.add_parser("show", help="展示某 MCU 的完整解析分区")
  p_show.add_argument("mcu", nargs="?", default=None, help="MCU 名（默认唯一）")
  _add_common(p_show)

  p_gen = sub.add_parser("gen", help="生成 bsp_flash_map.h + 链接脚本")
  p_gen.add_argument("--mcu", default=None, help="MCU 名（默认唯一）")
  p_gen.add_argument("--out", default=None, help="输出目录（默认 build/gen/<mcu>/）")
  _add_common(p_gen)

  args = parser.parse_args()

  root = find_repo_root()
  if root is None:
    print("错误: 未找到仓库根（需包含 BSP/Components/Devices/Module/App 层目录）")
    return 1

  map_path = Path(args.map)
  if not map_path.is_absolute():
    map_path = root / map_path

  try:
    if args.command == "list":
      return cmd_list(load_flash_map(map_path))
    if args.command == "show":
      return cmd_show(load_flash_map(map_path), args.mcu)
    if args.command == "gen":
      return cmd_gen(root, map_path, args.mcu, args.out)
  except FlashMapError as exc:
    print(f"错误: {exc}")
    return 1
  except yaml.YAMLError as exc:
    print(f"错误: YAML 解析失败: {exc}")
    return 1
  return 0


if __name__ == "__main__":
  sys.exit(main())
