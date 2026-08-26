"""yamc_cfg — app_main.c/h 生成器 (拓扑 + .ioc 外设 → 物化模板 + BSP 绑定注入).

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

from .params import inject_config, render_config_block

BSP_BEGIN = "/* YMAC BSP BEGIN */"
BSP_END = "/* YMAC BSP END */"

# 模块接线锚点 (模板 app_main.c/h.tmpl; 契约见 docs/debug/build-toolchain-design.md §4.8)
MODULES_BEGIN = "/* YMAC MODULES BEGIN */"
MODULES_END = "/* YMAC MODULES END */"
CFGSTRUCT_BEGIN = "/* YMAC CFGSTRUCT BEGIN */"
CFGSTRUCT_END = "/* YMAC CFGSTRUCT END */"
ROOT_BEGIN = "/* YMAC ROOT BEGIN */"
ROOT_END = "/* YMAC ROOT END */"
# 模板默认注释行 → 拓扑需要 com_can 类型时启用 (Can/CanConfig, supercap_3ph 订阅分发)
COM_CAN_INCLUDE_COMMENT = '// #include "com_can.h"'

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
    """拓扑 + 覆盖参数 → control_module cfg 字段 dict (含非槽位通道/限幅).

    params: 平铺 dict {'vref': 12.5, 'pid_v.kp': 2.0}, 覆盖拓扑默认.
    roles → adc_ch_* 映射按 control_module 形态 (supercap 特例):
      mod_supercap: 相电流角色 i0/i1/i2 → adc_ch_i[] 数组 (ModSuperCapCfg 字段);
                    其它角色平铺; pwm duty/ch_drive 不进 cfg (该类型无此字段)
    """
    power = _flatten_params(topo.get("params"))
    for k, v in (params or {}).items():
        _set_nested(power, k, v)
    cm = (topo.get("control_module") or "").split("/")[-1]
    pwm = topo.get("pwm") or {}
    roles = (topo.get("adc") or {}).get("roles") or {}
    if cm == "mod_supercap":
        phases = int(topo.get("phases") or (topo.get("share") or {}).get("num_phases") or 1)
        power["num_phases"] = phases  # 非槽位: 缺省 0 会被 tick 钳位为单相 → 必须显式发射
        # 非槽位保护/滤波默认 (mod_supercap.h 注释: 短路 2×i_lim_a, 失平衡 0.5×i_lim_a, LPF 120Hz).
        # 必须显式发射 — apply_config 整块拷贝 g_cfg.power → me->cfg 会覆盖 init 期派生默认,
        # 缺省 0 会令 |i|>0 即触发短路/失平衡去抖 (假故障). setdefault: 槽位 params 优先.
        power.setdefault("power_lpf_fc", 120.0)
        power.setdefault("short_ilim", 2.0 * power.get("i_lim_a", 20.0))
        power.setdefault("unbalance_thr", 0.5 * power.get("i_lim_a", 20.0))
        for role, rcfg in roles.items():
            if role.startswith("i") and role[1:].isdigit():
                continue  # 相电流角色 → adc_ch_i[] 数组 (下方收集)
            power["adc_ch_" + role] = rcfg["ch"]
        i_roles = [(role, rcfg["ch"]) for role, rcfg in roles.items()
                   if role.startswith("i") and role[1:].isdigit()]
        i_roles.sort(key=lambda r: int(r[0][1:]))
        if i_roles:
            power["adc_ch_i"] = [ch for _, ch in i_roles]
    elif cm == "mod_vsi":
        # mod_vsi 用 PwmSvpwm / AdcAcSampler 实例 (非 cfg 通道字段), 不映射 PWM/ADC 平铺.
        # 非槽位控制/保护默认缺省由 mod_vsi_init 内部派生, 无需显式发射.
        pass
    else:
        for field in ("ch_drive", "duty_min", "duty_max"):
            if field in pwm:
                power[field] = pwm[field]
        for role, rcfg in roles.items():
            power["adc_ch_" + role] = rcfg["ch"]
    return power
def render_power_config(topo: dict, params: Optional[dict] = None) -> tuple[str, str]:
    """拓扑 → {power: ...} 渲染 C 块 + config_id. 拓扑含 share: 段时一并渲染 (.share)."""
    power = topo_to_power_cfg(topo, params)
    data = {"power": power}
    share = topo.get("share")
    if share:
        data["share"] = dict(share)
    rendered = render_config_block(data, indent_step=2)
    config_id = str(topo.get("name") or "default")
    return rendered, config_id


# ======== BSP 绑定块生成 (STM32 分支) ========

def gen_bsp_block(topo: dict, periph: dict) -> str:
    """拓扑 + periph → /* YMAC BSP */ 块 (每行无缩进, 由注入器补)."""
    if periph.get("platform") == "c2000":
        return gen_bsp_block_c2000(topo, periph)
    lines: list[str] = []
    lines.append("// ===== yamc 生成: BSP/外设绑定 (拓扑 + .ioc) ====")
    pwm_cfg = topo.get("pwm") or {}
    gen_fn = _PWM_DEVICE_GEN.get(pwm_cfg.get("device"))
    if gen_fn:
        lines.extend(gen_fn(pwm_cfg, periph))
    else:
        lines.append(f"// WARN: 未知 PWM 设备 {pwm_cfg.get('device')} — 跳过 PWM 绑定")
    adc_cfg = topo.get("adc") or {}
    adc_dev = adc_cfg.get("device", "adc_dc_sampler")
    if adc_dev == "adc_ac_sampler":
        lines.extend(_gen_adc_ac_sampler(adc_cfg, periph))
    else:
        lines.extend(_gen_adc(adc_cfg, periph))
    comm_cfg = topo.get("comm") or {}
    if comm_cfg.get("can"):
        lines.extend(_gen_can(comm_cfg, periph))
    return "\n".join(lines)


def _gen_pwm_buckboost(pwm_cfg: dict, periph: dict) -> list[str]:
    freq = int(pwm_cfg.get("freq_hz", 100000))
    deadtime = int(pwm_cfg.get("deadtime_ns", 0))
    sync_rect = bool(pwm_cfg.get("sync_rect", False))
    mode = str(pwm_cfg.get("mode", "Buck"))

    hrtims = periph["peripherals"]["hrtim"]
    if not hrtims:
        return ["// WARN: .ioc 无 HRTIM — 未生成 PWM 绑定"]

    # 多相 (phase_legs): 每相两腿 = 2 定时器 (supercap_3ph 3 相 × 2 腿 = 6 定时器)
    phase_legs = pwm_cfg.get("phase_legs")
    if phase_legs:
        return _gen_bb_phases(phase_legs, freq, deadtime, sync_rect, mode, hrtims)

    ch_drive = int(pwm_cfg.get("ch_drive", 0))
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


_BB_MAX_PHASES = 3  # 与 Devices/pwm/pwm_buckboost.h 的 PWM_BB_MAX_PHASES 一致


def _gen_bb_phases(phase_legs: list, freq: int, deadtime: int, sync_rect: bool, mode: str, hrtims: list) -> list[str]:
    """phase_legs → pwm_bb_init_phases (PwmBbPhaseCfg[]). 相位定时器 BSP_TIMER_A..F 平铺,
    落到单个 HRTIM 实例 (supercap_3ph 用 HRTIM1 的 A..F); 跨实例 (G474 双 HRTIM 分腿)
    需 PwmBuckBoost 扩展多 handle, 当前不支持 → 绑定首个实例并 WARN."""
    warn: list[str] = []
    valid_legs = []
    for leg in phase_legs:
        keys_ok = all(isinstance(leg.get(k), str) and leg[k] for k in ("timer_a", "mask_a", "timer_b", "mask_b"))
        names_ok = keys_ok and str(leg["timer_a"]).startswith("BSP_TIMER_") and str(leg["timer_b"]).startswith("BSP_TIMER_")
        if names_ok:
            valid_legs.append(leg)
        else:
            warn.append("// WARN: phase_legs 条目缺 timer_a/mask_a/timer_b/mask_b 或定时器名非 BSP_TIMER_A..F — 该腿已跳过")
    if len(valid_legs) == 0:
        return warn + ["// ERROR: 无合法 phase_legs 条目 — 未生成 PWM 绑定"]
    n = len(valid_legs)
    if n > _BB_MAX_PHASES:
        warn.append(f"// WARN: {n} 相超出上限 {_BB_MAX_PHASES}, 取前 {_BB_MAX_PHASES} 相")
        n = _BB_MAX_PHASES
    letters = [str(x).replace("BSP_TIMER_", "") for leg in valid_legs[:n] for x in (leg["timer_a"], leg["timer_b"])]

    def has_all(h: dict) -> bool:
        names = {t["name"] for t in h.get("timers", [])}
        return all(L in names for L in letters)

    hrtim = next((h for h in hrtims if has_all(h)), None)
    cross = hrtim is None
    if cross:
        hrtim = hrtims[0]
    clk_hz = int(hrtim.get("clk_hz") or 0)
    clk_src = "SystemCoreClock" if not clk_hz else f"{clk_hz}u"

    cfg_lines = []
    for leg in valid_legs[:n]:
        leg_b = "true" if leg.get("leg_b_used", True) else "false"
        cfg_lines.append(
            f"    {{{leg['timer_a']}, {leg['mask_a']}, {leg['timer_b']}, {leg['mask_b']}, /*leg_b_used=*/{leg_b}}},")

    lines = [
        f"// {hrtim['instance']} {n} 相 × 2 腿 = {2 * n} 定时器 — {freq} Hz {mode} "
        f"(sync_rect={str(sync_rect).lower()})",
    ]
    lines += warn
    if cross:
        lines.append(f"// WARN: 所需定时器 {','.join(letters)} 跨实例/缺失, 绑定到 {hrtim['instance']} — "
                     f"跨实例分腿需扩展 PwmBuckBoost 多 handle")
    lines += [
        f"static const PwmBbPhaseCfg sc_phase_cfg[{n}] = {{",
        *cfg_lines,
        "};",
        f"pwm_bb_init_phases(&g_root.drv_buck_pwm, {freq}u, {n}, sc_phase_cfg, "
        f"PwmMode_{mode}, /*sync_rect=*/{str(sync_rect).lower()});",
        # pwm_bb_init_phases 清零 bsp_cfg → 后填句柄/时钟, 再重算 period/deadtime
        f"g_root.drv_buck_pwm.bsp_cfg.handle = &{hrtim['handle']};",
        f"g_root.drv_buck_pwm.bsp_cfg.clk_hz = {clk_src};",
        f"pwm_bb_set_freq(&g_root.drv_buck_pwm, {freq}u);",
        f"pwm_bb_set_deadtime(&g_root.drv_buck_pwm, {deadtime}u);",
    ]
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
        f"static const float sc_adc_k[{num_ch}] = {{{k_str}}};",
        f"adc_dc_sampler_init(&g_root.drv_dc_adc, IO_ASYNC_FLAG, &{adc['handle']}, {dma_handle}, {num_ch}, "
        f"sc_adc_k, NULL, NULL);",
    ]
    for rank, c in enumerate(channels[:num_ch]):
        role = role_at.get(rank, "")
        role_comment = f" — {role}" if role else ""
        lines.append(f"//   CH{rank}=IN{c['channel']}({c['pin']}){role_comment}")
    return lines


def _gen_adc_ac_sampler(adc_cfg: dict, periph: dict) -> list[str]:
    """三相交流采样 (adc_device=adc_ac_sampler) 绑定生成. vsi_3ph 用.

    roles 约定: 电流角色 i_* (i_a/i_b/...), 电压角色 v_* 或 vdc/vab (差分),
    取前 num_v 个电压通道做线电压重构. 每个角色带 ch/gain.
    """
    roles = adc_cfg.get("roles") or {}
    if not roles:
        return []
    adcs = periph["peripherals"]["adc"]
    if not adcs:
        return ["// WARN: .ioc 无 ADC — 未生成 AC 采样绑定"]
    adc = adcs[0]

    # 按角色名分电流/电压 (i_* 前缀 = 电流, 其余含 'v' 或 'vdc'/'vab' = 电压)
    i_roles = [(k, v) for k, v in roles.items() if k.startswith("i_")]
    v_roles = [(k, v) for k, v in roles.items() if not k.startswith("i_")]
    # vsi_3ph: 期望 ia/ib + vdc/vab. 预取三相线电压重构需要 idx_vab, idx_vbc — 这里简化为按名
    if not i_roles or not v_roles:
        return ["// WARN: adc_ac_sampler roles 需电流 + 电压角色 (vsi 拓扑预期 i_a/v_ab 形) — 未生成"]
    # 稳定排序 (按 ch) 保证通道序与拓扑一致
    i_roles.sort(key=lambda r: r[1]["ch"])
    v_roles.sort(key=lambda r: r[1]["ch"])

    num_i = len(i_roles)
    num_v = len(v_roles)
    i_ch = ",".join(str(r[1]["ch"]) for r in i_roles)
    v_ch = ",".join(str(r[1]["ch"]) for r in v_roles)
    i_gain = ",".join(f"{r[1].get('gain', 1.0):.4f}f" for r in i_roles)
    v_gain = ",".join(f"{r[1].get('gain', 1.0):.4f}f" for r in v_roles)

    max_rank = max(v["ch"] for v in roles.values())
    num_ch = max_rank + 1
    dma = adc.get("dma") or {}
    dma_handle = f"&{dma['handle']}" if dma.get("handle") else "NULL"

    lines = [
        f"// ADC {adc['instance']} (三相交流: {num_i} 电流 + {num_v} 电压, {num_ch} 总通道)",
        f"static const uint8_t ac_i_ch[{num_i}] = {{{i_ch}}};",
        f"static const uint8_t ac_v_ch[{num_v}] = {{{v_ch}}};",
        f"static const float ac_i_gain[{num_i}] = {{{i_gain}}};",
        f"static const float ac_v_gain[{num_v}] = {{{v_gain}}};",
        f"adc_ac_sampler_init(&g_root.drv_ac_adc, IO_ASYNC_FLAG, &{adc['handle']}, {dma_handle},",
        f"                    {num_ch}, {num_v}, {num_i}, ac_i_ch, ac_v_ch, 0, ac_v_gain, NULL, ac_i_gain, NULL);",
    ]
    for role, rcfg in roles.items():
        lines.append(f"//   {role} → CH{rcfg['ch']} (gain {rcfg.get('gain', 1.0)})")
    return lines


def _gen_can(comm_cfg: dict, periph: dict) -> list[str]:
    """comm.can 段 → CAN 设备绑定 (can_init 挂 HAL 句柄). 订阅分发接缝在 MODULES 锚点 (spec §4.8.4)."""
    cans = periph.get("peripherals", {}).get("can")
    if not cans:
        return ["// WARN: 拓扑 comm.can 要求 CAN 设备, 但 .ioc 无 CAN — 订阅分发 0x061 不可用"]
    can = cans[0]
    return [
        f"// CAN {can['instance']} (handle {can['handle']}) — 帧订阅分发 (TX 0x051 / RX 订阅 0x061)",
        f"can_init(&g_root.drv_can, &(CanConfig) {{.hcan = &{can['handle']}}});",
    ]


# ======== SVPWM / SPWM 六开关逆变绑定生成器 ========
# vsi_3ph 拓扑 (pwm.device: pwm_svpwm): 三相 6 开关逆变, 3 个互补半桥定时器.
# SVPWM 设备 (Devices/pwm/pwm_svpwm.c) 用单个 bsph 句柄 + 依赖 .ioc 预配定时器.
#   发波仅 svpwm_set_vector(v_alpha_pu, v_beta_pu, v_dc) 写占空比; 周期/死区走
#   外层 bsp_pwm_set_freq_hz / bsp_pwm_set_deadtime_ns (需 handle 已绑定).

def _gen_pwm_svpwm(pwm_cfg: dict, periph: dict) -> list[str]:
    freq = int(pwm_cfg.get("freq_hz", 20000))
    deadtime = int(pwm_cfg.get("deadtime_ns", 500))
    dpwm = str(pwm_cfg.get("dpwm_mode", "7Seg"))

    hrtims = periph["peripherals"]["hrtim"]
    if not hrtims:
        return ["// WARN: .ioc 无 HRTIM — 未生成 SVPWM 绑定"]
    hrtim = hrtims[0]
    clk_hz = int(hrtim.get("clk_hz") or 0)
    clk_src = "SystemCoreClock" if not clk_hz else f"{clk_hz}u"

    # 3 相半桥 → 定时器 A/B/C + 互补掩码
    legs = [(f"BSP_TIMER_{chr(ord('A')+i)}", f"mask_{chr(ord('a')+i)}") for i in range(3)]
    mask_map = {"mask_a": "BSP_OUT_TIMER_A_PAIR",
                "mask_b": "BSP_OUT_TIMER_B_PAIR",
                "mask_c": "BSP_OUT_TIMER_C_PAIR"}
    timer_map = {"mask_a": "BSP_TIMER_A", "mask_b": "BSP_TIMER_B", "mask_c": "BSP_TIMER_C"}

    return [
        f"// {hrtim['instance']} 三相 6 开关 — {freq} Hz SVPWM ({dpwm} 段), 3 定时器 A/B/C",
        f"svpwm_init(&g_root.drv_svpwm, {freq}u, {deadtime}u,",
        f"            {timer_map['mask_a']}, {mask_map['mask_a']},",
        f"            {timer_map['mask_b']}, {mask_map['mask_b']},",
        f"            {timer_map['mask_c']}, {mask_map['mask_c']});",
        # svpwm_init 将 bsph 置 NULL → 绑定 handle + 重设频率/死区
        f"g_root.drv_svpwm.bsph = &{hrtim['handle']};",
        f"svpwm_set_mode(&g_root.drv_svpwm, SvpwmMode_{dpwm});",
        f"pwm_set_freq(&g_root.drv_svpwm.base, {freq}u);",
        f"pwm_set_deadtime(&g_root.drv_svpwm.base, {deadtime}u);",
    ]


def _gen_pwm_spwm(pwm_cfg: dict, periph: dict) -> list[str]:
    """SPWM (单相/三相正弦调制) 绑定. vsi_3ph 若 device=pwm_spwm 则 3 相 3 半桥."""
    freq = int(pwm_cfg.get("freq_hz", 20000))
    deadtime = int(pwm_cfg.get("deadtime_ns", 500))
    num_arms = int(pwm_cfg.get("num_arms", 3))

    hrtims = periph["peripherals"]["hrtim"]
    if not hrtims:
        return ["// WARN: .ioc 无 HRTIM — 未生成 SPWM 绑定"]
    hrtim = hrtims[0]

    pairs = [("BSP_TIMER_%c" % chr(ord('A') + i),
              "BSP_OUT_TIMER_%c_PAIR" % chr(ord('A') + i)) for i in range(num_arms)]
    cfg_lines = ",\n                 ".join("{%s, %s}" % p for p in pairs)
    return [
        f"// {hrtim['instance']} {num_arms} 相正弦调制 — {freq} Hz SPWM, {num_arms} 互补半桥",
        f"static const PwmSpwmArmCfg vsi_spwm_arms[{num_arms}] = {{",
        f"    {cfg_lines},",
        "};",
        f"pwm_spwm_init(&g_root.drv_spwm, {freq}u, {deadtime}u, {num_arms}, vsi_spwm_arms);",
        f"g_root.drv_spwm.bsp_cfg.handle = &{hrtim['handle']};",
        f"g_root.drv_spwm.bsp_cfg.clk_hz = {int(hrtim.get('clk_hz') or 0)}u;",
        f"pwm_spwm_set_freq(&g_root.drv_spwm, {freq}u);",
        f"pwm_spwm_set_deadtime(&g_root.drv_spwm, {deadtime}u);",
    ]


_PWM_DEVICE_GEN = {
    "pwm_buckboost": _gen_pwm_buckboost,
    "pwm_svpwm": _gen_pwm_svpwm,
    "pwm_spwm": _gen_pwm_spwm,
}


# ======== 模块接线生成 (MODULES 锚点, 契约 §4.8) ========

def replace_anchored_block(text: str, begin: str, end: str, block: str) -> str:
    """在 begin/end 锚点之间写入 block (继承锚点缩进). 锚点缺失 → 原样返回."""
    b = text.find(begin)
    e = text.find(end, b)
    if b == -1 or e == -1:
        return text
    line_start = text.rfind("\n", 0, b) + 1
    indent = text[line_start:b]
    inner = "\n".join(f"{indent}{l}" if l.strip() else l for l in block.splitlines())
    new = f"{indent}{begin}\n{inner}\n{indent}{end}"
    return text[:line_start] + new + text[e + len(end):]


def gen_modules_block(topo: dict) -> Optional[tuple]:
    """control_module → (c_block, h_cfgstruct, h_root); 未知模块 → None (保留模板默认, 零回归).

    c_block:     MODULES 锚点内容 (6 生成函数 + CAN 接缝)
    h_cfgstruct: CFGSTRUCT 锚点内容 (重定义 PowerCfg) 或 None (不注入)
    h_root:      ROOT 锚点内容 (电源域根成员) 或 None (不注入)
    """
    cm = (topo.get("control_module") or "").split("/")[-1]
    gen = _MODULE_GEN.get(cm)
    return gen(topo) if gen else None


# supercap_3ph: MODULES 锚点模板. 普通字符串 (C 花括号为字面量), 动态位用 __X__ 占位替换.
_SUPERCAP_C_TEMPLATE = """// ==== 生成: 拓扑=__TOPOLOGY__ (契约 §4.8; CAN 订阅分发: com_can 订阅表 → mod_can_on_frame) ====
// 事件日志接缝 (app 侧提供; 发射块内自足声明 — 模板未含 extern, 见 AGENT-SYNC)
extern void log_evt(uint8_t ev);
// CAN I/O 接缝 — 生产订阅分发: RX 经 can_poll(&g_root.drv_can) 分发订阅表 → can_rx_061 → mod_can_proto 解析
//   mod_can_bind(send=can_send_tx, poll=NULL, on_referee) — RX 由 com_can 订阅表驱动, 不再轮询 mod_can_poll
//   (spec §4.8.4 接缝名 can_send 与 com_can 全局 can_send 同名遮蔽 → 实现改名 can_send_tx)
static void can_send_tx(uint32_t id, const uint8_t *data, uint8_t dlc) {
  CommConstData d = {.ptr = data, .len = dlc};
  (void) can_send(&g_root.drv_can, id, d, IO_ASYNC_FLAG);
}
static void can_on_referee(const ModCanReferee *ref) {
  // 0x061 有效帧 → 超电功率指令 (命令邮箱, MAIN→FAST 周期边界生效)
  if (ref->enable_conv)
    mod_supercap_set_referee_power(&g_root.supercap, ref->power_limit_w);
}
static void can_rx_061(Can *me, const CanFrame *frame, void *ctx) {
  (void) me;
  (void) ctx;
  mod_can_on_frame(&g_root.can_proto, frame->id, frame->data, frame->dlc);
}

static void modules_apply_config(void) {
  // 均流 cfg 同步 + 主模块 cfg 整体拷贝 (PowerCfg == ModSuperCapCfg, 零漂移)
  g_root.share.cfg = g_cfg.share;
  g_root.supercap.cfg = g_cfg.power;
  // 派生: 功率环 PI 限幅/迟滞窗口 + share_gain 注入均流模块
  mod_supercap_sync_cfg(&g_root.supercap);
}

static void modules_board_init(void) {
  // 均流 + 主模块实例化 (cfg 来自 g_cfg, init 内部已 sync)
  mod_share_init(&g_root.share, &g_cfg.share);
  mod_supercap_init(&g_root.supercap, &g_cfg.power);
  // 致命绑定序: 先 share→pwm, 再 supercap→adc+share (base.pwm 快照; 反序急停不封波, mod_supercap.h 头注释)
  mod_share_bind(&g_root.share, &g_root.drv_buck_pwm);
  mod_supercap_bind(&g_root.supercap, &g_root.drv_dc_adc, &g_root.share);
  // CAN 协议 (ctx main): 订阅分发 — send/on_referee 接缝 + 0x061 订阅 (com_can 表满 → ERR_FULL, 启动期即暴露)
  mod_can_init(&g_root.can_proto);
  mod_can_bind(&g_root.can_proto, can_send_tx, NULL, can_on_referee);
  (void) can_register(&g_root.drv_can, MOD_CAN_RX_ID, can_rx_061, &g_root.can_proto);
  // 0xFB 串口调参: 槽位 0-9 → g_cfg.power → apply_config (见 modules_apply_tune)
  pid_tune_set_apply_cb(on_pid_tune_received);
}

static void modules_fast_tick(void) {
  // 先采样快照 (PingPong 交接, DMA 完成 ISR 标 pending), 再超电控制 (级联+保护+驱动 share→写 PWM)
  adc_dc_sampler_fetch(&g_root.drv_dc_adc);
  mod_supercap_tick(&g_root.supercap);
}

static void modules_slow_tick(void) {
  // 心跳 → 喂狗 (Heartbeat_Check 返回 true = 超时死锁; FAST 已 Heartbeat_Tick, SLOW 判死)
  // 健康才喂狗 — 死锁/停摆 → 不喂 → IWDG 硬件复位 (bsp_watchdog.h 头注释)
  if (!Heartbeat_Check(&g_root.heartbeat, 100))
    bsp_watchdog_feed();
  // 遥测: FAST→SLOW Latest 锁存 (监控面读 i_side 快照)
  (void) latch_peek(&g_root.supercap.telemetry);
  // 1kHz SLOW ÷5 = 200Hz 遥测发送 (mod_can_tx_telemetry → can_send_tx)
  static uint8_t can_tx_div;
  if (++can_tx_div >= 5) {
    ModCanTelemetry tel;
    tel.referee_power_limit_w = g_root.supercap.p_lim_hi;
    tel.chassis_power_w = g_root.supercap.va * g_root.supercap.ichassis;
    tel.referee_power_w = g_root.supercap.p_referee;
    tel.supercap_output_mx_w = g_root.supercap.p_lim_lo;
    tel.output_capability_pct = g_root.supercap.cap_health * 100.0f;
    mod_can_tx_telemetry(&g_root.can_proto, &tel);
    can_tx_div = 0;
  }
}

static void modules_main_loop(void) {
  // CAN 轮询 (DMA FIFO → 订阅表分发): 0x061 → can_rx_061 → mod_can_on_frame → can_on_referee
  can_poll(&g_root.drv_can);
  // 保护事件排空 (SPSC 环, FAST 单生产者; 事件字节 = comp_error.h 位掩码)
  uint8_t ev;
  while (mod_supercap_evt_pop(&g_root.supercap, &ev))
    log_evt(ev);
}

static void modules_apply_tune(const float coef[10]) {
  // 槽位 0-9 → g_cfg.power (同步回配置, 下次 YAML 注入保持一致)
  // 槽位映射与 Config/topologies/__TOPOLOGY__.yaml params.slot 一致: __SLOT_MAP__
__TUNE_BODY__
  // 同步到运行时实例 (整体拷贝 + PI 限幅派生)
  apply_config();
}"""


def _gen_mod_supercap(topo: dict) -> tuple[str, str, str]:
    """supercap_3ph: MODULES 锚点 (订阅分发 CAN) + .h 锚点 (CFGSTRUCT/ROOT)."""
    params = topo.get("params") or []
    slots = sorted((p for p in params if p.get("slot") is not None), key=lambda p: p["slot"])
    tune_body = "\n".join(f"  g_cfg.power.{p['key']} = coef[{p['slot']}];" for p in slots)
    slot_map = " ".join(f"[{p['slot']}]={p['key']}" for p in slots)
    name = str(topo.get("name") or "?")
    c_block = (_SUPERCAP_C_TEMPLATE.replace("__TOPOLOGY__", name)
               .replace("__SLOT_MAP__", slot_map)
               .replace("__TUNE_BODY__", tune_body))
    h_cfgstruct = (f"typedef ModSuperCapCfg PowerCfg;  // 拓扑主控制模块 "
                   f"(control_module: {topo.get('control_module')})")
    adc = topo.get("adc") or {}
    h_root = (
        f"// ==== 由 yamc_cfg 生成: 电源域根成员 (拓扑={name}) ====\n"
        f"// --- Power-OOP: 电力电子设备 ---\n"
        f"PwmBuckBoost drv_buck_pwm;  // 三相并联 Buck/Boost PWM (pwm_buckboost.h)\n"
        f"AdcDcSampler drv_dc_adc;    // 直流采样器 (adc_dc_sampler.h, {adc.get('num_ch', '?')} 通道)\n"
        f"Can drv_can;                // CAN 传输设备 (com_can.h, 订阅分发 0x061)\n"
        f"// --- Power-OOP: 电源控制 Module ---\n"
        f"ModCurrentShare share;      // 三相均流 (mod_current_share.h)\n"
        f"ModSuperCap supercap;       // 超级电容功率控制 (mod_supercap.h)"
    )
    return c_block, h_cfgstruct, h_root


_VSI_C_TEMPLATE = """static void modules_apply_config(void) {
  g_root.vsi.cfg = g_cfg.power;
  mod_vsi_sync_cfg(&g_root.vsi);
}

static void modules_board_init(void) {
  // ModVsi 实例化 (cfg 来自 g_cfg.power) + 绑定 SVPWM/AC采样设备
  mod_vsi_init(&g_root.vsi, &g_cfg.power, /*grid_freq_hz=*/__GRID_HZ__);
  mod_vsi_bind(&g_root.vsi, &g_root.drv_svpwm, &g_root.drv_ac_adc);
  // 0xFB 串口调参: 槽位 0-9 → g_cfg.power → apply_config
  pid_tune_set_apply_cb(on_pid_tune_received);
}

static void modules_fast_tick(void) {
  // 先采样快照 (AC transient 快照交接), 再 VSI 控制 (PLL→dq→SVPWM 发波)
  adc_ac_sampler_fast_fetch(&g_root.drv_ac_adc);
  mod_vsi_tick(&g_root.vsi);
}

static void modules_slow_tick(void) {
  // 心跳 → 喂狗 (监控三件套; FAST 已 Heartbeat_Tick, SLOW 判死锁)
  if (!Heartbeat_Check(&g_root.heartbeat, 100))
    bsp_watchdog_feed();
  // 遥测: FAST→SLOW Latest 锁存 (监控面读 vdc 快照)
  (void) latch_peek(&g_root.vsi.telemetry);
}

static void modules_main_loop(void) {
  // 保护事件排空 (SPSC 环, FAST 单生产者; 事件字节 = comp_error.h 位掩码)
  uint8_t ev;
  while (mod_vsi_evt_pop(&g_root.vsi, &ev))
    log_evt(ev);
}

static void modules_apply_tune(const float coef[10]) {
  // 槽位 0-9 → g_cfg.power (同步回配置, 下次 YAML 注入保持一致)
  // 槽位映射与 Config/topologies/__TOPOLOGY__.yaml params.slot 一致
__TUNE_BODY__
  // 同步到运行时实例 (整体拷贝 + PI 限幅派生)
  apply_config();
}"""


def _gen_mod_vsi(topo: dict) -> tuple[str, str, str]:
    """vsi_3ph: MODULES 锚点 + .h 锚点 (CFGSTRUCT/ROOT). dq 电流环 + SVPWM 发波."""
    params = topo.get("params") or []
    slots = sorted((p for p in params if p.get("slot") is not None), key=lambda p: p["slot"])
    tune_body = "\n".join(f"  g_cfg.power.{p['key']} = coef[{p['slot']}];" for p in slots)
    name = str(topo.get("name") or "?")
    grid_hz = float(topo.get("grid_freq_hz", 50.0))
    c_block = (_VSI_C_TEMPLATE.replace("__TOPOLOGY__", name)
               .replace("__TUNE_BODY__", tune_body)
               .replace("__GRID_HZ__", f"{grid_hz}f"))
    h_cfgstruct = (f"typedef ModVsiCfg PowerCfg;  // 拓扑主控制模块 "
                   f"(control_module: {topo.get('control_module')})")
    adc = topo.get("adc") or {}
    h_root = (
        f"// ==== 由 yamc_cfg 生成: 电源域根成员 (拓扑={name}) ====\n"
        f"// --- Power-OOP: 电力电子设备 ---\n"
        f"PwmSvpwm drv_svpwm;     // 三相 6 开关逆变 PWM (pwm_svpwm.h)\n"
        f"AdcAcSampler drv_ac_adc; // 三相交流采样 (adc_ac_sampler.h)\n"
        f"// --- Power-OOP: 电源控制 Module ---\n"
        f"ModVsi vsi;             // 三相 VSI 控制 (mod_vsi.h)"
    )
    return c_block, h_cfgstruct, h_root


_MODULE_GEN = {
    "mod_supercap": _gen_mod_supercap,
    "mod_vsi": _gen_mod_vsi,
}

# ======== BSP 绑定块生成 (C2000 分支, driverlib) ========

def gen_bsp_block_c2000(topo: dict, periph: dict) -> str:
    """C2000: 拓扑 + periph → /* YMAC BSP */ 块.

    与 STM32 分支差异: 无 HAL 句柄对象 (基址宏直接作 void* 句柄), 无 DMA
    (ePWM 触发单转换源), 时钟来自 c2000_syscfg 的 mcu.sysclk_hz.
    """
    lines: list[str] = []
    lines.append("// ===== yamc 生成: BSP/外设绑定 (拓扑 + main.syscfg, C2000 driverlib) ====")
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
        f"static const float sc_adc_k[{num_ch}] = {{{k_str}}};",
        f"adc_dc_sampler_init(&g_root.drv_dc_adc, IO_ASYNC_FLAG, (void*){adc['base']}, NULL, {num_ch}, "
        f"sc_adc_k, NULL, NULL);",
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
            lines.insert(i, '#include "driverlib.h"  // C2000: 必须先于 HardC 头 (app_main.h/bootloader_main.h; uint8_t=uint16_t, 见 hw_types.h)\n')
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
            out.append("// yamc: HAL 句柄 extern (CubeMX 在 main.c 定义, main.h 不导出)\n")
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


# ======== Bootloader 物化 (步骤 3) ========

# Bootloader 模板里的 yamc 锚点
BL_CFG_BEGIN = "/* YMAC BOOTLOADER_CFG BEGIN */"
BL_CFG_END = "/* YMAC BOOTLOADER_CFG END */"
# 生成的 bsp_flash_map.h (由 yamc/flash_map_gen.py 从 flash_map.yaml 生成)
BL_FLASH_MAP_H = "bsp_flash_map.h"


def _bootloader_cfg_block(boot_cfg: dict) -> str:
  """bootloader 拓扑段 → mod_bootloader 默认配置填充 (取 bsp_flash_map.h 宏).

  app_addr/app_max_size 优先取 bsp_flash_map.h 的 BSP_FLASH_APP_ADDR/SIZE (分区真相,
  保证 Bootloader 跳转地址与 app_flash.ld 一致); 可被拓扑显式覆盖.
  up_port 提示注释 (UART/CAN 选择随步骤 5 拓扑段接入).
  """
  app_addr_expr = boot_cfg.get("app_addr", "BSP_FLASH_APP_ADDR")
  app_max_expr = boot_cfg.get("app_max_size", "BSP_FLASH_APP_SIZE")
  up_port = str(boot_cfg.get("up_port") or "uart")
  lines = [
    "// yamc 注入: mod_bootloader 默认配置 (app 分区来自 bsp_flash_map.h, 与 app_flash.ld 同源)",
    f"  bl_cfg.app_addr = {app_addr_expr};",
    f"  bl_cfg.app_max_size = {app_max_expr};",
    f"  bl_cfg.rx_ring_size = 256u;  // 升级传输 '{up_port}'",
  ]
  return "\n".join(lines)


def _inject_boot_map_include(text: str) -> str:
  """在 #include \"bootloader_main.h\" 之后插入 #include \"bsp_flash_map.h\"."""
  lines = text.splitlines(keepends=True)
  out: list[str] = []
  inserted = False
  for line in lines:
    out.append(line)
    if not inserted and line.strip().startswith('#include "bootloader_main.h"'):
      out.append(f'#include "{BL_FLASH_MAP_H}"  // Flash 分区宏 (yamc/flash_map_gen.py)\n')
      inserted = True
  return "".join(out)


def _inject_main_h_boot(text: str) -> str:
  """Bootloader 版平台头注入: 在 `#include "bootloader_main.h"` 后插 `#include "main.h"` (STM32).

  与 _inject_main_h 逻辑相同, 但匹配 bootloader_main.h (app_main 用 _inject_main_h). 幂等.
  """
  lines = text.splitlines(keepends=True)
  out: list[str] = []
  inserted = False
  for line in lines:
    out.append(line)
    if not inserted and line.strip().startswith('#include "bootloader_main.h"'):
      out.append('#include "main.h"  // CubeMX HAL 类型 + 宏 (句柄在 main.c, 见下 extern)\n')
      inserted = True
  return "".join(out)


def gen_bootloader(topo: dict, periph: dict,
                   hardc_dir: Path, out_dir: Path) -> dict:
  """生成 bootloader_main.c/h 到 out_dir. 返回 {bl_c, bl_h}.

  复用 materialize + 锚点机制: 物化 bootloader_main.c/h.tmpl, 注入
    #include "bsp_flash_map.h" + 平台 HAL 头/extern + BOOTLOADER_CFG 配置块.

  bootloader 拓扑段 (topo['bootloader']):
    enable: true       # 是否生成引导固件
    up_port: uart|can  # 升级传输 (提示注释; 具体传输绑定随步骤 5 拓扑段接入)
    app_addr / app_max_size  # 可选显式覆盖 (默认取 bsp_flash_map.h 的 app 分区宏)
  """
  out_c = out_dir / "bootloader_main.c"
  out_h = out_dir / "bootloader_main.h"
  tmpl_c = hardc_dir / "App" / "bootloader_main.c.tmpl"
  tmpl_h = hardc_dir / "App" / "bootloader_main.h.tmpl"

  _bl_raw = topo.get("bootloader")
  _bl_is_dict = isinstance(_bl_raw, dict)
  boot_cfg = _bl_raw if _bl_is_dict else {}   # 非 dict (bootloader: true) 视为未配置

  text = tmpl_c.read_text(encoding="utf-8")
  h_text = tmpl_h.read_text(encoding="utf-8")

  # 1) 注入 #include "bsp_flash_map.h" (app 分区宏)
  if '#include "bsp_flash_map.h"' not in text:
    text = _inject_boot_map_include(text)

  # 2) 平台 HAL 头 + 句柄 extern (STM32: main.h + UART/CAN 句柄; C2000: driverlib.h)
  if periph.get("platform") == "c2000":
    text = _inject_driverlib_h(text)
  else:
    text = _inject_main_h_boot(text)
    text = _inject_handle_externs(text, _collect_handles(periph))

  # 3) BOOTLOADER_CFG 配置块 (仅当拓扑显式含 bootloader 段 dict 且 enable: true 才注入;
  #    缺 bootloader 段或非 dict → 仅物化骨架, 留模板 3b 兜底, 不注配置)
  if _bl_is_dict and boot_cfg.get("enable", False):
    block = _bootloader_cfg_block(boot_cfg)
    if BL_CFG_BEGIN not in text or BL_CFG_END not in text:
      raise RuntimeError(f"BOOTLOADER_CFG 锚点缺失: {tmpl_c}")
    text = replace_anchored_block(text, BL_CFG_BEGIN, BL_CFG_END, block)

  out_dir.mkdir(parents=True, exist_ok=True)
  out_c.write_text(text, encoding="utf-8")
  out_h.write_text(h_text, encoding="utf-8")
  return {"bl_c": out_c, "bl_h": out_h}


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

    # 模块接线: control_module → MODULES 锚点 + .h 锚点 (CFGSTRUCT/ROOT + com_can 启用)
    # 未知模块 → 不注入 → 保留模板默认 (Buck 参考, 零回归)
    mods = gen_modules_block(topo)

    text = tmpl_c.read_text(encoding="utf-8")
    h_text = tmpl_h.read_text(encoding="utf-8")
    if mods is not None:
        c_block, h_cfgstruct, h_root = mods
        if MODULES_BEGIN not in text or MODULES_END not in text:
            raise RuntimeError(f"MODULES 锚点缺失: {tmpl_c}")
        text = replace_anchored_block(text, MODULES_BEGIN, MODULES_END, c_block)
        if h_cfgstruct is not None:
            if CFGSTRUCT_BEGIN not in h_text or CFGSTRUCT_END not in h_text:
                raise RuntimeError(f"CFGSTRUCT 锚点缺失: {tmpl_h}")
            h_text = replace_anchored_block(h_text, CFGSTRUCT_BEGIN, CFGSTRUCT_END, h_cfgstruct)
        if h_root is not None:
            if ROOT_BEGIN not in h_text or ROOT_END not in h_text:
                raise RuntimeError(f"ROOT 锚点缺失: {tmpl_h}")
            h_text = replace_anchored_block(h_text, ROOT_BEGIN, ROOT_END, h_root)
        # 启用 com_can.h (Can/CanConfig 类型, 模板默认注释; 订阅分发必需)
        h_text = h_text.replace(COM_CAN_INCLUDE_COMMENT, COM_CAN_INCLUDE_COMMENT.lstrip("/ "))

    # app_main.c: 物化 → 平台头注入 (STM32: main.h + HAL 句柄 extern; C2000: driverlib.h)
    if periph.get("platform") == "c2000":
        text = _inject_driverlib_h(text)
    else:
        text = _inject_main_h(text)
        text = _inject_handle_externs(text, _collect_handles(periph))
    out_dir.mkdir(parents=True, exist_ok=True)
    out_c.write_text(text, encoding="utf-8")

    # app_main.h: 物化 + 锚点注入 (CFGSTRUCT/ROOT/com_can 已在 mods 分支处理)
    out_h.write_text(h_text, encoding="utf-8")

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
