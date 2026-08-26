"""ymac_cfg — 外部工程 CMakeLists.txt 幂等接入 HardC.CMake.

对标 xr_cubemx_cfg 的构建集成: 往外部工程 CMakeLists.txt 注入一个
`YmaC HardC BEGIN/END` 块 (set HARDC_* 变量 + include + target_sources + link),
删除旧块重跑即可重建; 不改动 CubeMX 保护区.

注入位置: 首个 add_subdirectory 之后 (HardC.CMake 需 stm32cubemx 目标已定义),
退化: add_executable 之后 → 文件末尾.

块内容:
  set(HARDC_DIR  Middlewares/Third_Party/HardC)
  set(HARDC_DRIVER st)                 # st | c2000 | none
  set(HARDC_DEVICES "Devices/pwm/pwm_buckboost.c;Devices/adc/adc_dc_sampler.c")
  set(HARDC_STM32_SERIES F3)           # 仅 st
  set(C2000_SDK_DIR "...")             # 仅 c2000: C2000Ware/DigitalPower SDK 根
  include(${HARDC_DIR}/cmake/HardC.CMake)
  target_sources(${CMAKE_PROJECT_NAME} PRIVATE User/Application/app_main.c)
  target_link_libraries(${CMAKE_PROJECT_NAME} PRIVATE hardc)
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Optional

BEGIN_MARKER = "# ====== YmaC HardC BEGIN (ymac_cfg 生成, 勿手改) ======"
END_MARKER = "# ====== YmaC HardC END ======"

# 旧版手工集成 (User/Components... 平铺) 的检测特征
_OLD_USER_PATTERN = re.compile(r"User/(Components|Devices|Module)/")


def _has_old_integration(text: str) -> bool:
    """检测 YmaC 块之外的旧版 User/Components... 手工集成.

    先剔除注入块, 避免块内 target_sources(... User/Application/app_main.c) 误命中.
    """
    begin = text.find(BEGIN_MARKER)
    end = text.find(END_MARKER, begin) if begin != -1 else -1
    if begin != -1 and end > begin:
        text = text[:begin] + text[end + len(END_MARKER):]
    return bool(_OLD_USER_PATTERN.search(text))


def compute_devices(topo: dict) -> list[str]:
    """拓扑 pwm/adc/comm/per 段 → HardC Devices 闭包 (相对 HardC 根).

    仅列 Device 具体实现 .c; 父类/Module 由 HardC.CMake 的 core glob 覆盖.
    comm 段: 传输类按键名映射 (can→com_can, uart→com_uart, spi→com_spi,
             i2c→com_i2c, gpio→com_gpio); per 段: 外设名列表 → per_<name>.c.
    """
    out: list[str] = []
    pwm = topo.get("pwm") or {}
    adc = topo.get("adc") or {}
    if pwm.get("device"):
        out.append(f"Devices/pwm/{pwm['device']}.c")
    if adc.get("device"):
        out.append(f"Devices/adc/{adc['device']}.c")
    # comm: 传输类 (Key 已删, 去抖归 HMI; 按拓扑声明的总线键收录)
    _COMM_BUS = {"can": "com_can", "uart": "com_uart", "spi": "com_spi",
                 "i2c": "com_i2c", "gpio": "com_gpio"}
    comm = topo.get("comm") or {}
    for key, cls in _COMM_BUS.items():
        if key in comm:
            out.append(f"Devices/comm/{cls}.c")
    # per: 非总线外设 (OLED/IMU/测距/输出) → Devices/peripheral
    per = topo.get("per")
    if isinstance(per, list):
        for name in per:
            out.append(f"Devices/peripheral/per_{name}.c")
    return out


def series_from_family(family: str) -> str:
    """'STM32F3' → 'F3' (HARDC_STM32_SERIES). 未知返回 'F3'."""
    fam = (family or "").replace("STM32", "")
    return fam if fam in ("F0", "F1", "F2", "F3", "F4", "F7", "G0", "G4", "H7", "L0", "L4") else "F3"


def _build_block(hardc_rel: str, driver: str, devices: list[str],
                 app_rel: str, series: Optional[str] = None,
                 sdk_dir: Optional[str] = None,
                 irq_macros: Optional[dict] = None) -> str:
    lines = [
        "set(HARDC_DIR  " + hardc_rel + ")",
        f"set(HARDC_DRIVER {driver})",
        'set(HARDC_DEVICES "' + ";".join(devices) + '")',
    ]
    if driver == "st" and series:
        lines.append(f"set(HARDC_STM32_SERIES {series})")
    if driver == "c2000" and sdk_dir:
        # 正斜杠 + 引号: Windows 反斜杠会触发 CMake 转义, 路径含空格需引号包裹
        lines.append(f'set(C2000_SDK_DIR "{sdk_dir.replace(chr(92), "/")}")')
    lines.append("include(${HARDC_DIR}/cmake/HardC.CMake)")
    lines.append(f"target_sources(${{CMAKE_PROJECT_NAME}} PRIVATE {app_rel})")
    # 用 plain 签名 (无 PRIVATE): CubeMX 工程清单 target_link_libraries
    #   (${{CMAKE_PROJECT_NAME}} stm32cubemx) 是 plain, 同一 target 混用
    #   plain+keyword 会让 CMake 报 "must be either all-keyword or all-plain".
    #   hardc → stm32cubemx 是 PUBLIC, 无显式 scope 仍能传递 resolved deps.
    lines.append("target_link_libraries(${CMAKE_PROJECT_NAME} hardc)")
    # 三档中断宏 (由 .ioc NVIC 探测): FAST_CTRL_IRQN/SLOW_CTRL_IRQN/HMI_IRQN
    #   + 额外 HMI 源 HMI_IRQN_2..4 (多通信源时多个, 全部钉到抢优 2).
    irq = irq_macros or {}
    for macro in ("FAST_CTRL_IRQN", "SLOW_CTRL_IRQN", "HMI_IRQN",
                  "HMI_IRQN_2", "HMI_IRQN_3", "HMI_IRQN_4"):
        if macro in irq:
            lines.append(f"target_compile_definitions(${{CMAKE_PROJECT_NAME}} "
                         f"PRIVATE {macro}={irq[macro]})")
    return "\n".join(lines)


def _find_anchor(text: str) -> int:
    """返回注入点偏移: add_subdirectory → add_executable → EOF."""
    for i, line in enumerate(text.splitlines()):
        stripped = line.strip()
        if stripped.startswith("add_subdirectory"):
            end = text.find("\n", text.find(line)) + 1
            return end if end > 0 else len(text)
    for i, line in enumerate(text.splitlines()):
        stripped = line.strip()
        if stripped.startswith("add_executable"):
            end = text.find("\n", text.find(line)) + 1
            return end if end > 0 else len(text)
    return len(text)


def read_text_with_fallback(path: str) -> str:
    """参考 LibXR GeneratorSTM32CMake.read_text_with_fallback: utf-8 → utf-8-sig → gb18030.
    外部工程 CMakeLists.txt 在中文 Windows 可能是 GBK/带 BOM, 不能假设 UTF-8."""
    for enc in ("utf-8", "utf-8-sig", "gb18030"):
        try:
            return Path(path).read_text(encoding=enc)
        except UnicodeDecodeError:
            continue
    return Path(path).read_text(encoding="utf-8")


def inject_cmake_integration(cm_path: Path, hardc_rel: str, driver: str,
                             devices: list[str], app_rel: str,
                             series: Optional[str] = None,
                             sdk_dir: Optional[str] = None,
                             irq_macros: Optional[dict] = None) -> dict:
    """往 CMakeLists.txt 幂等注入 HardC 接入块.

    series: 仅 st (HARDC_STM32_SERIES); sdk_dir: 仅 c2000 (C2000_SDK_DIR);
    irq_macros: {FAST_CTRL_IRQN:.., SLOW_CTRL_IRQN:.., HMI_IRQN:..} 三档中断宏.
    返回 {updated: bool, inserted: bool, old_integration: bool}.
    """
    text = read_text_with_fallback(str(cm_path)) if cm_path.is_file() else ""
    old_integration = _has_old_integration(text)

    block = _build_block(hardc_rel, driver, devices, app_rel, series, sdk_dir, irq_macros)
    wrapped = f"{BEGIN_MARKER}\n{block}\n{END_MARKER}"

    begin_idx = text.find(BEGIN_MARKER)
    end_idx = text.find(END_MARKER)
    if begin_idx != -1 and end_idx != -1 and end_idx > begin_idx:
        # 已存在 → 替换块体. 吞掉 END 行及其换行, 后续内容重建为 "块 + 空行 + 内容",
        # 绝不吞掉 END 之后的下一行 (若 linter/编辑器删了块后空行, 旧逻辑会把下一行拼到 END).
        end_newline = text.find("\n", end_idx)
        if end_newline == -1:
            tail = ""
        else:
            tail = text[end_newline + 1:].lstrip("\r\n")
        updated = text[:begin_idx] + wrapped + "\n\n" + tail
        inserted = False
    else:
        anchor = _find_anchor(text)
        if text and not text.endswith("\n"):
            text += "\n"
        updated = text[:anchor] + "\n" + wrapped + "\n\n" + text[anchor:].lstrip("\r\n")
        inserted = True

    cm_path.parent.mkdir(parents=True, exist_ok=True)
    cm_path.write_text(updated, encoding="utf-8")
    return {"updated": True, "inserted": inserted, "old_integration": old_integration}


if __name__ == "__main__":
    import sys
    import tempfile

    target = Path(sys.argv[1] if len(sys.argv) > 1 else ".")
    cm = target / "CMakeLists.txt"
    if not cm.is_file():
        print(f"[FAIL] 未找到 {cm}", file=sys.stderr)
        sys.exit(1)
    with tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False, encoding="utf-8") as fh:
        fh.write(cm.read_text(encoding="utf-8"))
        tmp = Path(fh.name)
    r = inject_cmake_integration(tmp, "Middlewares/Third_Party/HardC", "st",
                                 ["Devices/pwm/pwm_buckboost.c", "Devices/adc/adc_dc_sampler.c"],
                                 "User/Application/app_main.c", "F3")
    print(f"inserted={r['inserted']} old_integration={r['old_integration']}")
    print("---- 注入后 ----")
    print(tmp.read_text(encoding="utf-8"))
