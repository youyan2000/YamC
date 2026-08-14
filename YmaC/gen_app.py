"""ymac_cfg — app_main.c/h 生成器 (拓扑 + .ioc 外设 → 物化模板 + BSP 绑定注入).

对标 xr_cubemx_cfg: 根据拓扑 YAML 与 .ioc 解析出的外设, 在外部工程生成
带完整外设绑定的 app_main.c/h:
  1. 物化 App/app_main.c.tmpl + app_main.h.tmpl 到工程 User/Application/
  2. 注入 `#include "main.h"` (CubeMX 生成的 HAL 句柄: hhrtim1/hadc1/hdma_adc1)
  3. 在 /* YMAC BSP BEGIN/END */ 之间写入外设绑定 (PWM + ADC 初始化)
  4. 在 /* CONFIG BEGIN/END */ 之间写入拓扑 params (render_config_block 复用)

拓扑 → 设备映射 (buck.yaml pwm/adc 段):
  pwm:  {device: pwm_buckboost, mode, ch_drive, sync_rect, freq_hz, deadtime_ns}
  adc:  {device: adc_dc_sampler, roles: {vout:{ch,gain}, iout:{ch,gain}, vin:{ch,gain}}}

关键时序 (pwm_buckboost.c): pwm_bb_init 会清零 bsp_cfg (handle/clk_hz) 并据此算
period → 必须先 init, 再填 .handle/.clk_hz, 最后 pwm_bb_set_freq/deadtime 重算.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Optional

import yaml

from yaml_config_builder import inject_config, render_config_block

BSP_BEGIN = "/* YMAC BSP BEGIN */"
BSP_END = "/* YMAC BSP END */"

# ======== 配置 → C 块 ========

def _set_nested(d: dict, dotted: str, value):
    """'pid_v.kp' → d['pid_v']['kp'] = value."""
    parts = dotted.split(".")
    cur = d
    for p in parts[:-1]:
        cur = cur.setdefault(p, {})
    cur[parts[-1]] = value


def _flatten_params(params) -> dict:
    """拓扑 params (list[{key,default}] 或 dict) → 嵌套 dict."""
    out: dict = {}
    if isinstance(params, dict):
        items = list(params.items())
    else:
        items = []
        for p in (params or []):
            key = p.get("key") or p.get("name")
            value = p.get("default") if "default" in p else p.get("value")
            items.append((key, value))
    for key, value in items:
        if key:
            _set_nested(out, key, value)
    return out


def topo_to_power_cfg(topo: dict, params: Optional[dict] = None) -> dict:
    """拓扑 + 覆盖参数 → ModBuckCfg 字段 dict (含非槽位通道/限幅).

    params: 平铺 dict {'vref': 12.5, 'pid_v.kp': 2.0}, 覆盖拓扑默认.
    """
    power = _flatten_params(topo.get("params"))
    for k, v in (params or {}).items():
        _set_nested(power, k, v)
    pwm = topo.get("pwm") or {}
    for field in ("ch_drive", "duty_min", "duty_max"):
        if field in pwm:
            power[field] = pwm[field]
    roles = (topo.get("adc") or {}).get("roles") or {}
    for role, rcfg in roles.items():
        power["adc_ch_" + role] = rcfg["ch"]
    return power


def render_power_config(topo: dict, params: Optional[dict] = None) -> tuple[str, str]:
    """拓扑 → {power: ...} 渲染 C 块 + config_id."""
    power = topo_to_power_cfg(topo, params)
    rendered = render_config_block({"power": power}, indent_step=2)
    config_id = str(topo.get("name") or "default")
    return rendered, config_id


# ======== BSP 绑定块生成 (STM32 分支) ========

def gen_bsp_block(topo: dict, periph: dict) -> str:
    """拓扑 + periph → /* YMAC BSP */ 块 (每行无缩进, 由注入器补)."""
    if periph.get("platform") == "c2000":
        return gen_bsp_block_c2000(topo, periph)
    lines: list[str] = []
    lines.append("// ===== YmaC 生成: BSP/外设绑定 (拓扑 + .ioc) ====")
    pwm_cfg = topo.get("pwm") or {}
    gen_fn = _PWM_DEVICE_GEN.get(pwm_cfg.get("device"))
    if gen_fn:
        lines.extend(gen_fn(pwm_cfg, periph))
    else:
        lines.append(f"// WARN: 未知 PWM 设备 {pwm_cfg.get('device')} — 跳过 PWM 绑定")
    adc_cfg = topo.get("adc") or {}
    lines.extend(_gen_adc(adc_cfg, periph))
    return "\n".join(lines)


def _gen_pwm_buckboost(pwm_cfg: dict, periph: dict) -> list[str]:
    freq = int(pwm_cfg.get("freq_hz", 100000))
    deadtime = int(pwm_cfg.get("deadtime_ns", 0))
    sync_rect = bool(pwm_cfg.get("sync_rect", False))
    ch_drive = int(pwm_cfg.get("ch_drive", 0))
    mode = str(pwm_cfg.get("mode", "Buck"))

    hrtims = periph["peripherals"]["hrtim"]
    if not hrtims:
        return ["// WARN: .ioc 无 HRTIM — 未生成 PWM 绑定"]
    hrtim = hrtims[0]

    letter = chr(ord("A") + min(ch_drive, 5))
    timer_enum = f"BSP_TIMER_{letter}"
    out_mask = f"BSP_OUT_TIMER_{letter}_PAIR" if sync_rect else f"BSP_OUT_T{letter}1"
    clk_hz = int(hrtim.get("clk_hz") or 0)
    clk_src = "SystemCoreClock" if not clk_hz else f"{clk_hz}u"

    lines = [
        f"// {hrtim['instance']} Timer {letter} — {freq} Hz {mode} "
        f"(sync_rect={str(sync_rect).lower()}, 输出 {out_mask})",
        f"pwm_bb_init(&g_root.drv_buck_pwm, {freq}u, {timer_enum}, {out_mask},",
        f"            PwmMode_{mode}, /*sync_rect=*/{str(sync_rect).lower()});",
        # pwm_bb_init 清零 bsp_cfg → 后填句柄/时钟, 再重算 period/deadtime
        f"g_root.drv_buck_pwm.bsp_cfg.handle = &{hrtim['handle']};",
        f"g_root.drv_buck_pwm.bsp_cfg.clk_hz = {clk_src};",
        f"pwm_bb_set_freq(&g_root.drv_buck_pwm, {freq}u);",
        f"pwm_bb_set_deadtime(&g_root.drv_buck_pwm, {deadtime}u);",
    ]
    sig_prefix = f"{hrtim['instance']}_CH{letter}"
    for o in hrtim.get("outputs", []):
        if o.get("pin") and o.get("signal", "").startswith(sig_prefix):
            lines.append(f"//   {o['pin']} → {o['signal']}")
    return lines


def _gen_adc(adc_cfg: dict, periph: dict) -> list[str]:
    roles = adc_cfg.get("roles") or {}
    if not roles:
        return []
    adcs = periph["peripherals"]["adc"]
    if not adcs:
        return ["// WARN: .ioc 无 ADC — 未生成采样绑定"]
    adc = adcs[0]
    channels = adc.get("channels") or []

    max_rank = max(r["ch"] for r in roles.values())
    num_ch = max_rank + 1
    # 注: 拓扑 roles.ch 按 .ioc 转换序列 rank 序解释 (DMA 扫描序). 仅校验通道数,
    # 不校验 .ioc 实际 channel 号 — 若 .ioc 的 rank 序与拓扑假定不符会串通道,
    # 靠下方 CH 注释人工核对 (F334 单 ADC 无此风险).
    if len(channels) < num_ch:
        return [
            f"// ERROR: .ioc {adc['instance']} 仅 {len(channels)} 通道, "
            f"拓扑需要 {num_ch} — 请检查 adc.roles.ch"
        ]

    gains = [1.0] * num_ch
    role_at: dict = {}
    for role, rcfg in roles.items():
        gains[rcfg["ch"]] = float(rcfg.get("gain", 1.0))
        role_at[rcfg["ch"]] = role

    k_str = ", ".join(f"{g:.4f}f" for g in gains)
    dma = adc.get("dma") or {}
    dma_handle = f"&{dma['handle']}" if dma.get("handle") else "NULL"

    lines = [
        f"// ADC {adc['instance']} ({num_ch} 通道, {dma.get('instance', '无DMA')})",
        f"adc_dc_sampler_init(&g_root.drv_dc_adc, &{adc['handle']}, {dma_handle}, {num_ch},",
        f"                    (float[]){{{k_str}}}, NULL, NULL);",
    ]
    for rank, c in enumerate(channels[:num_ch]):
        role = role_at.get(rank, "")
        role_comment = f" — {role}" if role else ""
        lines.append(f"//   CH{rank}=IN{c['channel']}({c['pin']}){role_comment}")
    return lines


_PWM_DEVICE_GEN = {
    "pwm_buckboost": _gen_pwm_buckboost,
}


# ======== BSP 绑定块生成 (C2000 分支, driverlib) ========

def gen_bsp_block_c2000(topo: dict, periph: dict) -> str:
    """C2000: 拓扑 + periph → /* YMAC BSP */ 块.

    与 STM32 分支差异: 无 HAL 句柄对象 (基址宏直接作 void* 句柄), 无 DMA
    (ePWM 触发单转换源), 时钟来自 c2000_syscfg 的 mcu.sysclk_hz.
    """
    lines: list[str] = []
    lines.append("// ===== YmaC 生成: BSP/外设绑定 (拓扑 + main.syscfg, C2000 driverlib) ====")
    pwm_cfg = topo.get("pwm") or {}
    if pwm_cfg.get("device") != "pwm_buckboost":
        lines.append(f"// WARN: C2000 分支仅支持 pwm_buckboost, 跳过 {pwm_cfg.get('device')}")
    else:
        lines.extend(_gen_pwm_c2000(pwm_cfg, periph))
    adc_cfg = topo.get("adc") or {}
    lines.extend(_gen_adc_c2000(adc_cfg, periph))
    return "\n".join(lines)


def _gen_pwm_c2000(pwm_cfg: dict, periph: dict) -> list[str]:
    freq = int(pwm_cfg.get("freq_hz", 100000))
    deadtime = int(pwm_cfg.get("deadtime_ns", 0))
    sync_rect = bool(pwm_cfg.get("sync_rect", False))
    ch_drive = int(pwm_cfg.get("ch_drive", 0))
    mode = str(pwm_cfg.get("mode", "Buck"))
    sysclk = int((periph.get("mcu") or {}).get("sysclk_hz") or 0)
    if not sysclk:
        return ["// WARN: c2000_syscfg 无 SYSCLK (sysclk_hz) — 未生成 PWM 绑定"]

    # topo pwm.ch_drive → BSP_TIMER_A..F → 在 syscfg ePWM 实例中找同字母 timer
    letter = chr(ord("A") + min(max(ch_drive, 0), 5))
    epwms = (periph.get("peripherals") or {}).get("epwm") or []
    match = next((e for e in epwms if e.get("timer") == letter), None)
    if not match:
        return [f"// WARN: syscfg 无 ePWM 绑到 BSP_TIMER_{letter} — 未生成 PWM 绑定"
                f" (检查 topo pwm.ch_drive={ch_drive} 与工程 ePWM 实例对齐)"]

    timer_enum = f"BSP_TIMER_{letter}"
    out_mask = f"BSP_OUT_TIMER_{letter}_PAIR" if sync_rect else f"BSP_OUT_T{letter}1"
    return [
        f"// {match['instance']} ({match['base']}) — {freq} Hz {mode} "
        f"(sync_rect={str(sync_rect).lower()}, 输出 {out_mask})",
        f"pwm_bb_init(&g_root.drv_buck_pwm, {freq}u, {timer_enum}, {out_mask},",
        f"            PwmMode_{mode}, /*sync_rect=*/{str(sync_rect).lower()});",
        # pwm_bb_init 清零 bsp_cfg (含 clk_hz) → 后填时钟再重算 period/deadtime (同 STM32 分支)
        f"g_root.drv_buck_pwm.bsp_cfg.handle = 0;  // C2000 无句柄对象, 模块基址由 timer 枚举推导 (bsp_c2000_epwm)",
        f"g_root.drv_buck_pwm.bsp_cfg.clk_hz = {sysclk}u;  // SYSCLK (clocktree.h)",
        # pwm_bb_init 内 bsp_init 缓存 s_epwm_clk_hz 时 clk_hz 仍为 0 → 必须重调 bsp_init 重新注入时钟,
        # 否则死区换算 db_tick_from_ns (s_epwm_clk_hz==0 → 0 tick) 全零 → 互补管无死区 → 直通风险.
        # (set_freq 走 bsp_cfg.clk_hz 直接算 period, 不受此缓存影响; 仅死区路径依赖缓存)
        f"bsp_init(&g_root.drv_buck_pwm.bsp_cfg);  // 重注入 SYSCLK (缓存 s_epwm_clk_hz, 见 bsp_c2000_epwm.c)",
        f"pwm_bb_set_freq(&g_root.drv_buck_pwm, {freq}u);",
        f"pwm_bb_set_deadtime(&g_root.drv_buck_pwm, {deadtime}u);",
        f"// 接线: {match['instance']} ISR (ADC 触发链) 调 App_OnControlTick()",
    ]


def _gen_adc_c2000(adc_cfg: dict, periph: dict) -> list[str]:
    roles = adc_cfg.get("roles") or {}
    if not roles:
        return []
    adcs = (periph.get("peripherals") or {}).get("adc") or []
    if not adcs:
        return ["// WARN: syscfg 无 ADC — 未生成采样绑定"]
    adc = adcs[0]  # 单 ADC 假设; 多 ADC 需 roles.adc 指定, 现支持 syscfg 首实例

    max_rank = max(r["ch"] for r in roles.values())
    num_ch = max_rank + 1

    gains = [1.0] * num_ch
    role_at: dict = {}
    for role, rcfg in roles.items():
        gains[rcfg["ch"]] = float(rcfg.get("gain", 1.0))
        role_at[rcfg["ch"]] = role

    k_str = ", ".join(f"{g:.4f}f" for g in gains)
    lines = [
        f"// ADC {adc['instance']} ({adc['base']}) — {num_ch} 通道, ePWM 触发 (单转换源, 每控制周期一次 SOC)",
        f"adc_dc_sampler_init(&g_root.drv_dc_adc, (void*){adc['base']}, NULL, {num_ch},",
        f"                    (float[]){{{k_str}}}, NULL, NULL);",
    ]
    for rank in range(num_ch):
        role = role_at.get(rank, "")
        role_comment = f" — {role}" if role else ""
        lines.append(f"//   CH{rank} = {adc['instance']}IN{rank}{role_comment}  // SOC 序由 SysConfig ADC 触发配置决定")
    return lines


# ======== 物化 + 注入 ========

def _inject_main_h(text: str) -> str:
    """在 #include "app_main.h" 之后插入 #include "main.h"."""
    lines = text.splitlines(keepends=True)
    out: list[str] = []
    inserted = False
    for line in lines:
        out.append(line)
        if not inserted and line.strip().startswith('#include "app_main.h"'):
            out.append('#include "main.h"  // CubeMX HAL 类型 + 宏 (句柄在 main.c, 见下 extern)\n')
            inserted = True
    return "".join(out)


def _inject_driverlib_h(text: str) -> str:
    """C2000: 在文件最前插入 #include "driverlib.h".

    TI 约定: driverlib.h 必须是第一个 include — C28x 字寻址, CGT <stdint.h> 不定义
    uint8_t, 它只来自 hw_types.h 的 `typedef uint16_t uint8_t`; 若在 app_main.h
    之后引入, 所有 HardC 头的 uint8_t 使用会先报未定义再冲突.
    """
    lines = text.splitlines(keepends=True)
    for i, line in enumerate(lines):
        if line.strip().startswith('#include'):
            lines.insert(i, '#include "driverlib.h"  // C2000: 必须先于 app_main.h (uint8_t=uint16_t, 见 hw_types.h)\n')
            break
    return "".join(lines)


# 外设实例前缀 → HAL 句柄类型 (CubeMX 在 main.c 定义全局句柄, 不导出到 main.h)
def _collect_handles(periph: dict) -> list[tuple[str, str]]:
    """生成代码引用的 HAL 句柄 → [(变量名, C 类型)], 供 extern 注入."""
    out: list[tuple[str, str]] = []
    seen: set[str] = set()

    def add(name: str, ctype: str) -> None:
        if name and name not in seen:
            seen.add(name)
            out.append((name, ctype))

    for hrtim in periph["peripherals"]["hrtim"]:
        add(hrtim["handle"], "HRTIM_HandleTypeDef")
    for adc in periph["peripherals"]["adc"]:
        add(adc["handle"], "ADC_HandleTypeDef")
        dma = adc.get("dma") or {}
        if dma.get("handle"):
            add(dma["handle"], "DMA_HandleTypeDef")
    return out


def _inject_handle_externs(text: str, handles: list[tuple[str, str]]) -> str:
    """在 #include "main.h" 之后插入 HAL 句柄 extern 声明."""
    if not handles:
        return text
    lines = text.splitlines(keepends=True)
    out: list[str] = []
    inserted = False
    for line in lines:
        out.append(line)
        if not inserted and line.strip().startswith('#include "main.h"'):
            out.append("// YmaC: HAL 句柄 extern (CubeMX 在 main.c 定义, main.h 不导出)\n")
            for name, ctype in handles:
                out.append(f"extern {ctype} {name};\n")
            inserted = True
    return "".join(out)


def materialize(tmpl: Path, out: Path) -> None:
    """模板 → 工程文件 (app_main.h 等无注入的纯拷贝)."""
    text = tmpl.read_text(encoding="utf-8")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(text, encoding="utf-8")


def inject_bsp_block(out_c: Path, block: str) -> bool:
    """在 YMAC BSP BEGIN/END 之间写入 block (继承标记缩进)."""
    text = out_c.read_text(encoding="utf-8")
    b = text.find(BSP_BEGIN)
    e = text.find(BSP_END, b)
    if b == -1 or e == -1:
        return False
    line_start = text.rfind("\n", 0, b) + 1
    indent = text[line_start:b]
    inner = "\n".join(f"{indent}{l}" if l.strip() else l for l in block.splitlines())
    new = f"{indent}{BSP_BEGIN}\n{inner}\n{indent}{BSP_END}"
    text = text[:line_start] + new + text[e + len(BSP_END):]
    out_c.write_text(text, encoding="utf-8")
    return True


# ======== 顶层编排 ========

def load_topology(hardc_dir: Path, name: str) -> dict:
    """从 submodule 读 Config/topologies/<name>.yaml."""
    path = hardc_dir / "Config" / "topologies" / f"{name}.yaml"
    if not path.is_file():
        raise FileNotFoundError(f"拓扑不存在: {path}")
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError(f"拓扑格式错误: {path}")
    return data


def gen_app(topo: dict, periph: dict, params: Optional[dict],
            hardc_dir: Path, out_dir: Path) -> dict:
    """生成 app_main.c/h 到 out_dir. 返回 {app_c, app_h, config_id}."""
    out_c = out_dir / "app_main.c"
    out_h = out_dir / "app_main.h"
    tmpl_c = hardc_dir / "App" / "app_main.c.tmpl"
    tmpl_h = hardc_dir / "App" / "app_main.h.tmpl"

    # app_main.c: 物化 → 平台头注入 (STM32: main.h + HAL 句柄 extern; C2000: driverlib.h)
    text = tmpl_c.read_text(encoding="utf-8")
    if periph.get("platform") == "c2000":
        text = _inject_driverlib_h(text)
    else:
        text = _inject_main_h(text)
        text = _inject_handle_externs(text, _collect_handles(periph))
    out_dir.mkdir(parents=True, exist_ok=True)
    out_c.write_text(text, encoding="utf-8")

    materialize(tmpl_h, out_h)

    if not inject_bsp_block(out_c, gen_bsp_block(topo, periph)):
        raise RuntimeError(f"未找到 {BSP_BEGIN}/{BSP_END} 锚点: {out_c}")

    rendered, config_id = render_power_config(topo, params)
    if not inject_config(out_c, rendered, config_id):
        raise RuntimeError(f"CONFIG 注入失败: {out_c}")

    return {"app_c": out_c, "app_h": out_h, "config_id": config_id}


if __name__ == "__main__":
    # 诊断: 加载 buck.yaml + 工程外设 (.ioc → STM32 / main.syscfg → C2000) → 打印生成块
    import tempfile

    from project_probe import find_ioc

    root = Path(sys.argv[1] if len(sys.argv) > 1 else ".")
    hardc = Path(sys.argv[2] if len(sys.argv) > 2 else ".")
    ioc = find_ioc(root)
    if ioc:
        from ioc_parse import load_or_parse, cache_path_for
        periph = load_or_parse(ioc, cache_path_for(root, ioc))
        print(f"[stm32] periph platform={periph.get('platform')} ({ioc})")
    elif (root / "main.syscfg").is_file():
        from c2000_syscfg import extract_peripherals
        periph = extract_peripherals(root)
        print(f"[c2000] periph platform={periph.get('platform')} ({root / 'main.syscfg'})")
    else:
        print(f"[FAIL] 未找到 .ioc 或 main.syscfg: {root}", file=sys.stderr)
        sys.exit(1)
    topo = load_topology(hardc, "buck")
    print("==== BSP 绑定块 ====")
    print(gen_bsp_block(topo, periph))
    print()
    print("==== CONFIG 块 ====")
    print(render_power_config(topo)[0])
    with tempfile.TemporaryDirectory() as td:
        out = gen_app(topo, periph, None, hardc, Path(td))
        print()
        print(f"==== 生成: {out['app_c']} (config_id={out['config_id']}) ====")
