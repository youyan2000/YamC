#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
yamc/scaffold.py — 项目脚手架生成工具
======================================
模仿 bsp-dev-c / xrobot 的 xrobot_gen_main 流程：
读 project.yaml → 解析 MANIFEST 依赖 → 生成 CMakeLists.txt + 依赖汇总头 + board_init 骨架。

CLI：
  python yamc/scaffold.py scan                  # 扫描仓库全部 MANIFEST 并校验
  python yamc/scaffold.py deps <module_id>      # 递归解析模块依赖
  python yamc/scaffold.py gen <project.yaml> [--out <dir>]  # 生成项目骨架

仅依赖 Python 标准库 + pyyaml。
"""

from __future__ import annotations

import argparse
import os
import re
import sys
from pathlib import Path

import yaml

# ---- 常量 ----

# 层名小写 → 真实目录名（id 前缀 → 目录映射）
LAYER_DIRS = {
  "bsp": "BSP",
  "components": "Components",
  "devices": "Devices",
  "module": "Module",
  "app": "App",
}

# 层拓扑顺序：BSP → Components → Devices → Module → App
LAYER_ORDER = ["BSP", "Components", "Devices", "Module", "App"]

# 上下文归位（三件事 → 三上下文）
CTX_VALUES = ("fast", "slow", "main")

# 扫描 MANIFEST 的层目录（顺序即扫描顺序，保证输出稳定）
_SCAN_DIRS = ["BSP", "Components", "Devices", "Module", "App"]


class ScaffoldError(Exception):
  """脚手架业务错误，携带可直接打印的中文信息。"""


def _as_list(v):
  """把 YAML 字段归一化为 list，容忍字符串 / None / 列表三种写法。"""
  if v is None:
    return []
  if isinstance(v, str):
    return [v]
  if isinstance(v, (list, tuple)):
    return list(v)
  return [v]


class Manifest:
  """单个 MANIFEST.yaml 的解析结果。"""

  def __init__(self, path, raw):
    self.path = path  # MANIFEST.yaml 绝对路径
    self.dir = path.parent  # 子系统目录，files 相对于此
    self.id = str(raw.get("id", "")).strip()
    self.layer = str(raw.get("layer", "")).strip()
    self.description = str(raw.get("description", "")).strip()
    self.files = [str(f).strip() for f in _as_list(raw.get("files")) if str(f).strip()]
    self.depends = [str(d).strip() for d in _as_list(raw.get("depends")) if str(d).strip()]
    self.constructors = [str(c).strip() for c in _as_list(raw.get("constructors")) if str(c).strip()]
    self.ctx = str(raw.get("ctx", "")).strip().lower()  # fast|slow|main (运行时 AI 填, 工具链校验)

  def rel_dir(self, root):
    """子系统目录相对仓库根的 posix 路径，如 Components/pid。"""
    return self.dir.relative_to(root).as_posix()

  def header_files(self):
    """files 中真正的 .h 文件。"""
    return [f for f in self.files if f.lower().endswith(".h")]

  def source_files(self):
    """files 中真正的 .c 源文件。"""
    return [f for f in self.files if f.lower().endswith(".c")]


# ---- 仓库根定位 ----

def find_repo_root(start=None):
  """从 start 向上搜索仓库根：包含至少 3 个层目录（BSP/Components/Devices/Module/App）。"""
  here = Path(start or os.getcwd()).resolve()
  for d in [here] + list(here.parents):
    hits = [name for name in _SCAN_DIRS if (d / name).is_dir()]
    if len(hits) >= 3:
      return d
  return None


# ---- MANIFEST 扫描与索引 ----

def _load_manifest(path):
  """读取并解析单个 MANIFEST.yaml。"""
  with open(path, "r", encoding="utf-8") as fh:
    raw = yaml.safe_load(fh)
  if not isinstance(raw, dict):
    raise ScaffoldError(f"{path}: MANIFEST 内容不是 YAML 映射")
  return Manifest(path, raw)


def _scan_manifests(root):
  """扫描 5 个层目录下的全部 MANIFEST.yaml，按路径排序保证稳定。"""
  out = []
  for name in _SCAN_DIRS:
    layer_dir = root / name
    if layer_dir.is_dir():
      for p in sorted(layer_dir.rglob("MANIFEST.yaml")):
        out.append(p)
  return out


def build_index(root):
  """扫描并加载全部 MANIFEST，返回 (小写 id → Manifest, 全部 Manifest 列表)。
  任一 MANIFEST 损坏即抛 ScaffoldError。
  """
  index = {}
  manifests = []
  for p in _scan_manifests(root):
    m = _load_manifest(p)
    manifests.append(m)
    if m.id:
      key = m.id.lower()
      if key not in index:
        index[key] = m
  return index, manifests


def _path_to_id(root, manifest_path):
  """由 MANIFEST 所在目录推出约定 id，用于与 yaml 内 id 比对。"""
  rel = manifest_path.parent.relative_to(root)
  parts = rel.parts
  head = parts[0].lower()
  if head not in LAYER_DIRS:
    return None
  if len(parts) == 1:
    return head  # bsp / app 扁平层
  return head + "/" + "/".join(p.lower() for p in parts[1:])


# ---- 依赖解析 ----

def _resolve_dep_file(root, dep):
  """把 depends 条目当作仓库相对路径解析；层前缀自动转成真实目录名。"""
  parts = dep.split("/")
  if parts and parts[0].lower() in LAYER_DIRS:
    parts = [LAYER_DIRS[parts[0].lower()]] + parts[1:]
  return root.joinpath(*parts)


def _norm_path(p):
  """归一化路径用于比较（Windows 大小写不敏感）。"""
  return os.path.normcase(str(Path(p).resolve()))


def _find_owner(root, file_path, manifests):
  """返回 files 列表包含该文件的模块，未找到返回 None。"""
  target = _norm_path(file_path)
  for m in manifests:
    for f in m.files:
      if _norm_path(m.dir / f) == target:
        return m
  return None


def _resolve_dep(root, dep, index, manifests):
  """depends 条目 → 模块：先按模块 id，再按文件路径找拥有者模块。无法解析返回 None。"""
  key = dep.strip().lower()
  if key in index:
    return index[key]
  p = _resolve_dep_file(root, dep)
  if p.is_file():
    return _find_owner(root, p, manifests)
  return None


def collect_modules(root, seed_refs, index, manifests):
  """DFS 递归解析依赖。
  返回按层拓扑排序的模块列表（BSP→Components→Devices→Module→App，同层按出现顺序）。
  环 / 依赖无法解析 / 模块不存在 → 抛 ScaffoldError。
  """
  state = {}  # 小写 id → 1 访问中 / 2 完成
  discover = []  # 首次出现顺序（同层排序依据）
  first_pos = {}  # 小写 id → discover 下标

  def visit(mid, path):
    key = mid.strip().lower()
    st = state.get(key)
    if st == 2:
      return
    if st == 1:
      start = path.index(mid)
      raise ScaffoldError("依赖成环: " + " → ".join(path[start:] + [mid]))
    if key not in index:
      raise ScaffoldError(f"模块未找到: '{mid}'")
    state[key] = 1
    path.append(mid)
    if key not in first_pos:
      first_pos[key] = len(discover)
      discover.append(index[key])
    for dep in index[key].depends:
      dm = _resolve_dep(root, dep, index, manifests)
      if dm is None:
        raise ScaffoldError(f"{index[key].id}: 依赖无法解析: '{dep}'")
      visit(dm.id, path)
    path.pop()
    state[key] = 2

  for ref in seed_refs:
    ref = str(ref).strip()
    if not ref:
      continue
    m = _resolve_dep(root, ref, index, manifests)
    if m is None:
      raise ScaffoldError(f"模块未找到: '{ref}'")
    visit(m.id, [])

  def layer_rank(m):
    try:
      return LAYER_ORDER.index(m.layer)
    except ValueError:
      return len(LAYER_ORDER)

  return sorted(discover, key=lambda m: (layer_rank(m), first_pos[m.id.lower()]))


# ---- 子命令 ----

def cmd_scan(root):
  """scan：扫描全部 MANIFEST 并校验（id 唯一 / 依赖存在 / 文件存在）。"""
  manifests = []
  errors = []
  warnings = []
  index = {}
  seen = {}  # 小写 id → manifest 路径

  # 扫描 + 加载，单个 MANIFEST 损坏不影响其余
  for p in _scan_manifests(root):
    try:
      m = _load_manifest(p)
    except ScaffoldError as exc:
      errors.append(str(exc))
      continue
    manifests.append(m)
    if not m.id:
      errors.append(f"[{p}] 缺少 id 字段")
      continue
    key = m.id.lower()
    if key in seen:
      errors.append(f"[{m.id}] id 重复: '{m.id}' 已在 {seen[key]} 声明")
    else:
      seen[key] = p
      index[key] = m

  # 逐模块校验
  for m in manifests:
    if not m.layer:
      errors.append(f"[{m.id}] 缺少 layer 字段")
    elif m.layer not in LAYER_ORDER:
      errors.append(f"[{m.id}] 非法 layer: '{m.layer}'")
    if m.ctx and m.ctx not in CTX_VALUES:
      errors.append(f"[{m.id}] 非法 ctx: '{m.ctx}' (期望 {', '.join(CTX_VALUES)})")
    elif m.layer == "Module" and not m.ctx:
      warnings.append(f"[{m.id}] Module 缺 ctx 字段 (fast|slow|main, 三上下文归位)")
    head = m.id.split("/")[0].lower()
    if head not in LAYER_DIRS:
      errors.append(f"[{m.id}] id 前缀非法: '{head}'")
    expected = _path_to_id(root, m.path)
    if expected and m.id.lower() != expected:
      warnings.append(f"[{m.id}] id 与目录不一致: 期望 '{expected}'")
    for f in m.files:
      if not (m.dir / f).is_file():
        errors.append(f"[{m.id}] 文件不存在: {f}")
    for dep in m.depends:
      key = dep.lower()
      if key in index:
        continue
      p = _resolve_dep_file(root, dep)
      if p.is_file():
        if _find_owner(root, p, manifests) is None:
          warnings.append(f"[{m.id}] 依赖 '{dep}' 文件存在但未列入任何模块 files")
        continue
      errors.append(f"[{m.id}] 依赖无法解析: '{dep}'")

  # 打印清单
  total_files = sum(len(m.files) for m in manifests)
  total_deps = sum(len(m.depends) for m in manifests)
  print(f"=== 仓库根: {root} ===")
  print(f"=== 子系统清单 ({len(manifests)}) ===")
  for m in manifests:
    print(f"[{m.id}] ({m.layer}) {m.description or '（无描述）'}")
    if m.files:
      print("  files:   " + ", ".join(m.files))
    if m.depends:
      print("  depends: " + ", ".join(m.depends))
    if m.constructors:
      print("  constructors: " + ", ".join(m.constructors))
    if m.ctx:
      print("  ctx: " + m.ctx)
  for w in warnings:
    print(f"警告: {w}")

  if not errors:
    print(f"{len(manifests)} 个子系统，{total_files} 个文件，{total_deps} 条依赖，校验通过")
    return 0
  for e in errors:
    print(f"错误: {e}")
  print(f"{len(manifests)} 个子系统，{total_files} 个文件，{total_deps} 条依赖，校验失败 ({len(errors)} 个错误)")
  return 1


def cmd_deps(root, module_id):
  """deps：递归解析模块依赖，打印拓扑序模块列表 + 完整文件列表。"""
  index, manifests = build_index(root)
  modules = collect_modules(root, [module_id], index, manifests)

  print(f"=== {module_id} 依赖展开 ({len(modules)} 个模块) ===")
  current = None
  for m in modules:
    if m.layer != current:
      current = m.layer
      print(f"-- {current} --")
    print(f"  {m.id}: {m.description or '（无描述）'}")

  files = []
  for m in modules:
    for f in m.files:
      files.append(f"{m.rel_dir(root)}/{f}")
  print(f"=== 完整文件列表 ({len(files)} 个) ===")
  for fp in files:
    print("  " + fp)
  return 0


def _render_cmake(root, out, project, mcu, sources, include_dirs, boot_cfg, n_modules):
  """渲染 CMakeLists 行列表。boot_cfg.enable → 双固件 (bootloader.elf + app.elf + 合并); 否则静态库.

  静态库: add_library(<project> STATIC ...)  —— 既有 scaffold 闭包形态 (供外部工程集成).
  双固件:
    add_executable(<project>_bootloader.elf bootloader_main.c + 闭包源)  链接 bootloader_flash.ld
    add_executable(<project>_app.elf        app_main.c        + 闭包源)  链接 app_flash.ld
    objcopy 各 .elf → .hex → merge_firmware.py merge → <project>_merged.hex
  链接脚本与 bsp_flash_map.h 由 flash_map_gen.py 生成到同一 out 目录 (build/gen/<project>/).
  """
  lines = []
  lines.append("# 自动生成 — 不要手动编辑（由 yamc/scaffold.py gen 生成）")
  lines.append(f"# 项目: {project}")
  if mcu:
    lines.append(f"# MCU:  {mcu}")
  lines.append(f"# 模块: {n_modules} / 源文件: {len(sources)} / include 路径: {len(include_dirs)}")
  lines.append("")
  lines.append("cmake_minimum_required(VERSION 3.13)")
  lines.append(f"project({project} C)")
  lines.append("")

  if include_dirs:
    inc_lines = "\n".join(f"  {d}" for d in include_dirs)
  else:
    inc_lines = "  # 无 include 路径"

  if not boot_cfg.get("enable"):
    lines.append(f"add_library({project} STATIC")
    if sources:
      lines.extend(f"  {s}" for s in sources)
    else:
      lines.append("  # 无 .c 源文件")
    lines.append(")")
    lines.append("")
    lines.append(f"target_include_directories({project} PUBLIC")
    lines.append(inc_lines)
    lines.append(")")
    lines.append("")
    return lines

  # ---- 双固件模式: 两个可执行 + 合并 ----
  lines.append("find_package(Python REQUIRED)")
  lines.append("")
  prefix = f"{project.upper()}".replace("-", "_")
  # 链接脚本目录: 默认本 CMakeLists 所在目录 (flash_map_gen --out 到 build/gen/<project> 即可);
  #   可用 project.yaml bootloader.flash_dir 指定子目录 (相对 CMakeLists).
  flash_dir = str((boot_cfg.get("flash_dir") or "").strip()).strip("/")
  root_posix = root.resolve().as_posix()
  lines.append("# HardC 仓库根 (绝对, 闭包源/含路径基于此解析)")
  lines.append(f"set({prefix}_HARDC_ROOT \"{root_posix}\")")
  link_dir_expr = (f"${{CMAKE_CURRENT_SOURCE_DIR}}/{flash_dir}"
                   if flash_dir else "${CMAKE_CURRENT_SOURCE_DIR}")
  lines.append("# 链接脚本目录 (bootloader_flash.ld/app_flash.ld, 由 flash_map_gen 生成)")
  lines.append(f"set({prefix}_LINK_DIR \"{link_dir_expr}\")")
  lines.append("")
  lines.append(f"set({prefix}_CLOSURE_SRCS")
  for s in sources:
    lines.append(f"  ${{{prefix}_HARDC_ROOT}}/{s}")
  lines.append(")")
  lines.append("")
  lines.append(f"set({prefix}_INCS")
  for d in include_dirs:
    lines.append(f"  ${{{prefix}_HARDC_ROOT}}/{d}")
  if not include_dirs:
    lines.append("  # 无 include 路径")
  lines.append(")")
  lines.append("")
  # 工具链在 CMAKE_EXE_LINKER_FLAGS 全局挂了默认 -T <HARDC_LINK_SCRIPT>; 双固件需逐 target
  #   用各自 .ld, 故先剔除全局默认 -T (LINK_OPTIONS 是追加, 无法覆盖):
  lines.append("# 剔除工具链全局默认链接脚本 (双固件逐 target 用各自 .ld, 见下)")
  lines.append("string(REGEX REPLACE \"-T[ \\t][^ ]+\" \"\" CMAKE_EXE_LINKER_FLAGS "
               "\"${CMAKE_EXE_LINKER_FLAGS}\")")
  lines.append("")
  for name, main_c, ld in (("bootloader", "bootloader_main.c", "bootloader_flash.ld"),
                           ("app", "app_main.c", "app_flash.ld")):
    target = f"{project}_{name}.elf"
    # 意图: 关闭 .elf 后缀 (CMake 会对带 .elf 的目标名再补扩展), 改跑输出为 <name>.elf
    lines.append(f"add_executable({target}")
    lines.append(f"  {main_c}")
    lines.append(f"  ${{{prefix}_CLOSURE_SRCS}}")
    lines.append(")")
    lines.append("")
    lines.append(f"target_include_directories({target} PRIVATE ${{{prefix}_INCS}})")
    # 每个可执行 <project>_bootloader.elf 带 .elf 后缀 → CMake 目标文件实际为 .elf.elf
    #   (无害; merge 用 target 名做 DEPENDS, objcopy 输出用 $<TARGET_FILE:...>)
    lines.append(f"set_target_properties({target} PROPERTIES")
    lines.append(f"  LINK_OPTIONS \"-T;${{{prefix}_LINK_DIR}}/{ld}\"")
    lines.append(")")
    lines.append(f"add_custom_command(TARGET {target} POST_BUILD")
    lines.append(f"  COMMAND ${{CMAKE_OBJCOPY}} -O ihex $<TARGET_FILE:{target}> "
                 f"${{CMAKE_CURRENT_BINARY_DIR}}/{name}.hex")
    lines.append(f"  COMMENT \"objcopy → {name}.hex\"")
    lines.append(")")
    lines.append("")
  # 合并两固件为单一烧录映像 (bootloader + app → one .hex)
  lines.append("# 合并两固件为单一烧录映像 (bootloader + app → one .hex)")
  lines.append(f"add_custom_target({project}_merge_firmware ALL")
  lines.append(f"  COMMAND ${{Python_EXECUTABLE}} "
               f"{(root / 'yamc' / 'merge_firmware.py').as_posix()} merge")
  lines.append(f"    -o ${{CMAKE_CURRENT_BINARY_DIR}}/{project}_merged.hex")
  lines.append(f"    ${{CMAKE_CURRENT_BINARY_DIR}}/bootloader.hex")
  lines.append(f"    ${{CMAKE_CURRENT_BINARY_DIR}}/app.hex")
  lines.append(f"  DEPENDS {project}_bootloader.elf {project}_app.elf")
  lines.append(f"  COMMENT \"合并 bootloader + app → {project}_merged.hex\"")
  lines.append(")")
  lines.append("")
  return lines


def cmd_gen(root, project_yaml, out_dir):
  """gen：读 project.yaml → 解析依赖 → 生成 CMakeLists / 依赖汇总头 / board_init 骨架。"""
  py_path = (root / project_yaml).resolve()
  if not py_path.is_file():
    raise ScaffoldError(f"project.yaml 不存在: {py_path}")
  with open(py_path, "r", encoding="utf-8") as fh:
    raw = yaml.safe_load(fh)
  if not isinstance(raw, dict):
    raise ScaffoldError(f"{py_path}: 内容不是 YAML 映射")

  project = str(raw.get("project") or "").strip() or py_path.stem
  mcu = str(raw.get("mcu") or "").strip()
  boot_cfg = raw.get("bootloader") or {}   # bootloader.enable → 双固件模式
  seed_refs = _as_list(raw.get("modules"))
  if not seed_refs:
    raise ScaffoldError(f"{py_path}: 缺少 modules 列表")

  index, manifests = build_index(root)
  modules = collect_modules(root, [str(r).strip() for r in seed_refs], index, manifests)

  out = Path(out_dir).resolve() if out_dir else (root / "build" / "gen" / project)
  out.mkdir(parents=True, exist_ok=True)

  # 汇总：源文件 / include 路径 / 头文件（按模块拓扑序）
  sources = []
  include_dirs = []
  headers = []  # (模块, 头文件)
  for m in modules:
    for f in m.source_files():
      sources.append((m.dir / f).relative_to(root).as_posix())
    for f in m.header_files():
      headers.append((m, f))
      if m.rel_dir(root) not in include_dirs:
        include_dirs.append(m.rel_dir(root))

  # ---- 1. CMakeLists.txt ----
  cm = _render_cmake(root, out, project, mcu, sources, include_dirs, boot_cfg, len(modules))
  (out / "CMakeLists.txt").write_text("\n".join(cm), encoding="utf-8", newline="\n")

  # ---- 2. <project>_deps.h ----
  guard = re.sub(r"[^A-Za-z0-9_]", "_", project.upper())
  if not guard or guard[0].isdigit():
    guard = "_" + guard
  guard += "_DEPS_H"
  dh = []
  dh.append("// 自动生成 — 依赖汇总头 — 不要手动编辑")
  dh.append(f"// 项目: {project}")
  dh.append("")
  dh.append(f"#ifndef {guard}")
  dh.append(f"#define {guard}")
  dh.append("")
  seen_h = set()
  dup_h = []
  current = None
  for m, f in headers:
    if m.layer != current:
      current = m.layer
      dh.append(f"// ---- {current} ----")
    if f in seen_h:
      dup_h.append(f)
      continue
    seen_h.add(f)
    dh.append(f'#include "{f}"')
  if dup_h:
    print(f"警告: 头文件重名已去重: {', '.join(sorted(set(dup_h)))}")
  dh.append("")
  dh.append(f"#endif // {guard}")
  dh.append("")
  (out / f"{project}_deps.h").write_text("\n".join(dh), encoding="utf-8", newline="\n")

  # ---- 3. board_init_stub.c ----
  bs = []
  bs.append("// 自动生成 — board_init 骨架 — 不要手动编辑")
  bs.append(f"// 项目: {project}")
  if mcu:
    bs.append(f"// MCU:  {mcu}")
  bs.append("")
  bs.append(f'#include "{project}_deps.h"')
  bs.append("")
  bs.append("// ===================== 模块实例化占位 =====================")
  for m in modules:
    title = f"{m.id}: {m.description}" if m.description else m.id
    bs.append(f"/* ===== {title} ===== */")
    if m.constructors:
      bs.append("// constructors: " + ", ".join(m.constructors))
    hs = m.header_files()
    if hs:
      bs.append("// TODO: " + ", ".join(hs) + " 在此实例化")
    else:
      bs.append("// TODO: 在此实例化")
    bs.append("")
  bs.append("// ===================== board_init =====================")
  bs.append("void board_init(void) {")
  bs.append("  // TODO: 按依赖顺序构造模块实例并绑定 ops")
  bs.append("}")
  bs.append("")
  (out / "board_init_stub.c").write_text("\n".join(bs), encoding="utf-8", newline="\n")

  print(f"生成完成: {len(modules)} 模块 / {len(sources)} 文件 / {len(include_dirs)} include 路径")
  print(f"输出目录: {out}")
  return 0


def main() -> int:
  """CLI 入口。"""
  parser = argparse.ArgumentParser(
    prog="scaffold",
    description="项目脚手架：MANIFEST 扫描 / 依赖解析 / project.yaml 骨架生成",
  )
  sub = parser.add_subparsers(dest="command", required=True)

  sub.add_parser("scan", help="扫描仓库内所有 MANIFEST.yaml 并校验")

  p_deps = sub.add_parser("deps", help="递归解析某模块的依赖")
  p_deps.add_argument("module_id", help="模块 id，如 components/pid")

  p_gen = sub.add_parser("gen", help="从 project.yaml 生成项目骨架")
  p_gen.add_argument("project_yaml", help="project.yaml 路径")
  p_gen.add_argument("--out", default=None, help="输出目录（默认 build/gen/<project>/）")

  args = parser.parse_args()

  root = find_repo_root()
  if root is None:
    print("错误: 未找到仓库根（需包含 BSP/Components/Devices/Module/App 层目录）")
    return 1

  try:
    if args.command == "scan":
      return cmd_scan(root)
    if args.command == "deps":
      return cmd_deps(root, args.module_id)
    if args.command == "gen":
      return cmd_gen(root, args.project_yaml, args.out)
  except ScaffoldError as exc:
    print(f"错误: {exc}")
    return 1
  except yaml.YAMLError as exc:
    print(f"错误: YAML 解析失败: {exc}")
    return 1
  return 0


if __name__ == "__main__":
  sys.exit(main())
