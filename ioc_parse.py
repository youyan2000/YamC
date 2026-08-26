"""yamc_cfg — STM32CubeMX .ioc → 外设 YAML (缓存 .hardc/periph.yaml).

对标 xr_cubemx_cfg 的 .ioc 解析: 提取 HRTIM/ADC/UART/CAN 实例 + DMA + 引脚映射,
供 gen_app.py 生成带完整外设绑定的 app_main.c/h.

输出结构 (periph):
  platform: stm32
  mcu:      {family, name, subfamily, hclk_hz, hrtim_hz}
  peripherals:
    hrtim: [ {instance, handle, clk_hz, timers:[{name,period,complementary}], outputs:[{pin,timer,out,signal}]} ]
    adc:   [ {instance, handle, dma:{handle,instance}, channels:[{rank,channel,pin}], nbr_of_conversion} ]
    uart:  [ {instance, handle, baud, pins:{TX,RX}} ]
    can:   [ {instance, handle, pins:{RX,TX}} ]

CubeMX 句柄命名: h + 实例小写 (HRTIM1→hhrtim1, ADC1→hadc1, USART3→huart3);
DMA 句柄: hdma_ + adc 实例小写 (ADC1→hdma_adc1).
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Optional

import yaml

from project_probe import find_ioc, _read_ioc, _to_int

# HRTIM 子定时器: Periode_TA 存在 → Timer A 被使用
_HRTIM_TIMERS = ("A", "B", "C", "D", "E", "F")
# 死区插入标志: DeadTimeInsertion-Output_TA1TA2=...ENABLED → 互补输出.
# 键按实例前缀 (HRTIM1/HRTIM2) 生成 → 支持 G474 双 HRTIM.
_DEADTIME_KEY = "{ip}.DeadTimeInsertion-Output_T{t}1T{t}2"
# HRTIM 时钟键: G474 双实例各有 RCC.HRTIM1Freq_Value / RCC.HRTIM2Freq_Value.
_CLK_KEY = "RCC.{ip}Freq_Value"


def extract_peripherals(raw: dict) -> dict:
    """解析后的 .ioc dict → periph dict (纯函数, 可单测)."""
    periph = {
        "platform": "stm32",
        "mcu": _extract_mcu(raw),
        "peripherals": {
            "hrtim": _extract_hrtim(raw),
            "adc": _extract_adc(raw),
            "uart": _extract_uart(raw),
            "can": _extract_can(raw),
        },
    }
    return periph


def _enabled_irqns(raw: dict) -> list[str]:
    """.ioc 中已使能 NVIC.*_IRQn 的名字 (顺序稳定)。

    CubeMX 的 NVIC 行是冒号分隔字段, 首字段 'true' 表示已使能, 例如:
      'NVIC.HRTIM1_TIMA_IRQn=true:0:0:false:false:true:true:true:true'
    返回去掉 'NVIC.' 前缀与 '_IRQn' 后缀的枚举名: 'HRTIM1_TIMA'.
    注意: Cortex 核异常 (HardFault/SysTick/... ) 默认也首个为 true, 由调用方
    按 HRTIM/TIM/CAN/USART 名字分类剔除。
    """
    out: list[str] = []
    for key, val in raw.items():
        if not key.startswith("NVIC."):
            continue
        if not key.endswith("_IRQn"):
            continue
        first = str(val).split(":")[0].strip().lower() if val is not None else ""
        if first != "true":
            continue
        out.append(key[len("NVIC."):-len("_IRQn")])
    return out


def probe_irq_macros(root: Path) -> dict[str, str]:
    """从工程 .ioc 探测三档中断宏 → {FAST_CTRL_IRQN, SLOW_CTRL_IRQN, HMI_IRQN}.

    分类规则 (按 IRQn 名字, 已剔除 Cortex 核异常):
      FAST = HRTIM*_TIMA (发波定时器, 抢 0) → 退而求其次任一 HRTIM*.
      SLOW = 非 HRTIM 的 TIM*_UP* (监控定时器, 抢 1), 优先专用 _UP 结尾,
             其次共享 `_UP_` (TIM1_UP_TIM16), 再退而其次 TIM*_.
      HMI  = 通信类 (CAN/FDCAN/USART/UART/LPUART, 抢 2), 再退而其次剩余
             任一非核异常 IRQn.
    找不到对应位 → 该 key 不存在 (cmake_integrate 输出占位/跳过, 工程需手补).
    """
    # Cortex-M 核异常 (默认使能, 永远不是合法 FAST/SLOW/HMI 目标) + 常见调试伪中断
    _CORE_EXCEPT = {
        "NonMaskableInt", "HardFault", "MemoryManagement", "BusFault",
        "UsageFault", "SVCall", "DebugMonitor", "PendSV", "SysTick",
    }

    macros: dict[str, str] = {}
    ioc = find_ioc(root)
    if ioc is None:
        return macros
    names = [n for n in _enabled_irqns(_read_ioc(ioc)) if n not in _CORE_EXCEPT]
    if not names:
        return macros

    # FAST: HRTIM (优先 *_TIMA, 其次 *_TIM*, 再其次任意 HRTIM*)
    hrtim = [n for n in names if n.startswith("HRTIM")]
    fast = next((n for n in hrtim if n.endswith("_TIMA")), None) or \
        next((n for n in hrtim if "_TIM" in n), None) or (hrtim[0] if hrtim else None)
    if fast:
        macros["FAST_CTRL_IRQN"] = fast + "_IRQn"

    # SLOW: 非 HRTIM 的定时器更新中断. 共享名如 TIM1_UP_TIM16_IRQn 含 "_UP_"（
    #   F334 的 TIM1_UP_TIM16 就是这样名字）, 专用名以 "_UP" 结尾.
    slow_cands = [n for n in names if n.startswith("TIM") and not n.startswith("HRTIM")]
    slow = next((n for n in slow_cands if n.endswith("_UP")), None) or \
        next((n for n in slow_cands if "_UP_" in n), None) or \
        (slow_cands[0] if slow_cands else None)
    if slow:
        macros["SLOW_CTRL_IRQN"] = slow + "_IRQn"

    # HMI: 通信类优先 (CAN/FDCAN/UART/USART/LPUART, 兼容 UART4_5 / USART2 等名),
    #      退而其次剩余任一非核异常 IRQn (排除已选中的 FAST/SLOW).
    used = set(macros.values())
    # HMI: 通信类 (CAN/FDCAN/UART/USART/LPUART, 兼容 UART4_5 / USART2 等名).
    #   关键: HMI 可同时有多个来源 (按键EXti / UART / CAN / FDCAN...), 每个都是独立
    #   中断且都应钉到抢优 2. 这里把所有通信类 IRQn 组成 HMI 组:
    #     HMI_IRQN     = 第一个 (主 HMI, 驱动 hmi_tick 时基)
    #     HMI_IRQN_2..4 = 其余 (额外 HMI 源, 由 app_main board_init 登记并共钉到 2)
    #   无通信类时退而其次剩余任一非核异常 IRQn 作主 HMI.
    comm = [n for n in names if n.startswith(("CAN", "FDCAN", "UART", "USART", "LPUART"))]
    if comm:
        macros["HMI_IRQN"] = comm[0] + "_IRQn"
        for i, extra in enumerate(comm[1:4], start=2):  # HMI_IRQN_2..HMI_IRQN_4 (最多3个副源, 对齐 app_main.tmpl)
            macros[f"HMI_IRQN_{i}"] = extra + "_IRQn"
    else:
        rest = [n for n in names if (n + "_IRQn") not in used]
        if rest:
            macros["HMI_IRQN"] = rest[0] + "_IRQn"

    return macros


def _extract_mcu(raw: dict) -> dict:
    subfamily = raw.get("ADC1.SubFamily", "") or raw.get("Mcu.SubFamily", "")
    hclk = _to_int(raw.get("RCC.HCLKFreq_Value")) or 0
    hrtim_hz = _to_int(raw.get("RCC.HRTIM1Freq_Value")) or hclk
    return {
        "family": raw.get("Mcu.Family", ""),
        "name": raw.get("Mcu.Name", ""),
        "subfamily": subfamily,
        "hclk_hz": hclk,
        "hrtim_hz": hrtim_hz,
    }


def _extract_hrtim(raw: dict) -> list:
    """HRTIM 实例子定时器 (Periode_TX) + 死区插入 + 输出引脚."""
    instances: list = []
    for ip in _ip_list(raw):
        if not ip.startswith("HRTIM"):
            continue
        timers = []
        for t in _HRTIM_TIMERS:
            period_key = f"{ip}.Periode_T{t}"
            if period_key in raw:
                complementary = raw.get(_DEADTIME_KEY.format(ip=ip, t=t), "").endswith("ENABLED")
                timers.append({
                    "name": t,
                    "period": _to_int(raw[period_key]) or 0,
                    "complementary": complementary,
                })
        if not timers:
            continue
        clk = _to_int(raw.get(_CLK_KEY.format(ip=ip))) or _to_int(raw.get("RCC.HRTIM1Freq_Value")) or 0
        instances.append({
            "instance": ip,
            "handle": _handle(ip),
            "clk_hz": clk,
            "timers": timers,
            "outputs": _pin_signals(raw, prefix=f"{ip}_CH"),
        })
    return instances


def _extract_adc(raw: dict) -> list:
    """ADC 转换序列 (rank 序) + DMA + 物理通道→引脚."""
    instances: list = []
    for ip in _ip_list(raw):
        if not ip.startswith("ADC") and not ip.startswith("SADC"):
            continue
        if f"{ip}.NbrOfConversion" not in raw and f"{ip}.Channel-0#ChannelRegularConversion" not in raw:
            continue

        # rank N → ADC_CHANNEL_X (N = DMA 扫描序; _read_ioc 已把 `\#` 还原为 `#`)
        channels = []
        rank = 0
        chan_key = f"{ip}.Channel-{rank}#ChannelRegularConversion"
        while chan_key in raw:
            chan_spec = raw[chan_key]  # "ADC_CHANNEL_1"
            chan_num = _channel_number(chan_spec)
            pin = _pin_of_signal(raw, f"{ip}_IN{chan_num}") if chan_num else "?"
            channels.append({"rank": rank, "channel": chan_num, "pin": pin})
            rank += 1
            chan_key = f"{ip}.Channel-{rank}#ChannelRegularConversion"

        nbr = _to_int(raw.get(f"{ip}.NbrOfConversion")) or len(channels)
        dma = _extract_dma(raw, ip)
        instances.append({
            "instance": ip,
            "handle": _handle(ip),
            "dma": dma,
            "channels": channels,
            "nbr_of_conversion": nbr,
        })
    return instances


def _extract_dma(raw: dict, adc_instance: str) -> Optional[dict]:
    """Dma.<ADC>.0.Instance=DMA1_Channel1 → {handle, instance}."""
    for key, val in raw.items():
        if not key.startswith(f"Dma.{adc_instance}."):
            continue
        if key.endswith(".Instance"):
            return {
                "handle": f"hdma_{adc_instance.lower()}",
                "instance": val,
            }
    return None


def _extract_uart(raw: dict) -> list:
    out = []
    for ip in _ip_list(raw):
        if not ip.startswith("USART") and not ip.startswith("UART"):
            continue
        # 简化: 直接扫引脚信号
        tx = _first_pin_of(raw, f"{ip}_TX")
        rx = _first_pin_of(raw, f"{ip}_RX")
        if tx is None and rx is None:
            continue
        out.append({
            "instance": ip,
            "handle": _handle(ip),
            "baud": _to_int(raw.get(f"{ip}.BaudRate")) or 115200,
            "pins": {"TX": tx or "", "RX": rx or ""},
        })
    return out


def _extract_can(raw: dict) -> list:
    out = []
    for ip in _ip_list(raw):
        if not ip.startswith("CAN"):
            continue
        rx = _first_pin_of(raw, "CAN_RX")
        tx = _first_pin_of(raw, "CAN_TX")
        if rx is None and tx is None:
            continue
        out.append({
            "instance": ip,
            "handle": _handle(ip),
            "pins": {"RX": rx or "", "TX": tx or ""},
        })
    return out


# ======== 内部工具 ========

def _ip_list(raw: dict) -> list:
    """按 Mcu.IPn 顺序返回外设实例名列表."""
    ips = []
    n = 0
    while f"Mcu.IP{n}" in raw:
        ips.append(raw[f"Mcu.IP{n}"])
        n += 1
    return ips


def _handle(instance: str) -> str:
    """CubeMX 句柄命名: HRTIM1→hhrtim1, ADC1→hadc1, USART3→huart3.

    HAL 把 USART 实例句柄命名为 huart3 (非 husart3), 见 LibXR
    GeneratorCodeSTM32.py 的 h{lower().replace("usart","uart")} 同款处理."""
    return "h" + instance.lower().replace("usart", "uart")


def _channel_number(chan_spec: str) -> int:
    """'ADC_CHANNEL_1' → 1."""
    if not chan_spec:
        return 0
    try:
        return int(chan_spec.rsplit("_", 1)[-1])
    except ValueError:
        return 0


def _pin_signals(raw: dict, prefix: str) -> list:
    """扫描 `<PIN>.Signal=<prefix>...` 行 → [{pin, signal}]."""
    out = []
    for key, val in raw.items():
        if not key.endswith(".Signal"):
            continue
        pin = key.rsplit(".", 1)[0]
        if val.startswith(prefix):
            out.append({"pin": pin, "signal": val})
    return out


def _pin_of_signal(raw: dict, signal: str) -> str:
    """反查 `<PIN>.Signal=<signal>` → PIN (找不到返回 '?')."""
    for key, val in raw.items():
        if key.endswith(".Signal") and val == signal:
            return key.rsplit(".", 1)[0]
    return "?"


def _first_pin_of(raw: dict, signal_prefix: str) -> Optional[str]:
    for s in _pin_signals(raw, prefix=signal_prefix):
        return s["pin"]
    return None


def parse_and_cache(ioc_path: Path, cache_path: Path) -> dict:
    """解析 .ioc 并缓存到 .hardc/periph.yaml. 返回 periph dict."""
    raw = _read_ioc(ioc_path)
    periph = extract_peripherals(raw)
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    cache_path.write_text(
        yaml.safe_dump(periph, allow_unicode=True, sort_keys=False),
        encoding="utf-8",
    )
    return periph


def _cache_is_fresh(ioc_path: Path, cache_path: Path) -> bool:
    """缓存有效: .ioc 未被改动 (缓存 mtime >= .ioc mtime)."""
    try:
        return cache_path.stat().st_mtime_ns >= ioc_path.stat().st_mtime_ns
    except OSError:
        return False


def load_or_parse(ioc_path: Path, cache_path: Path, force: bool = False) -> dict:
    """缓存存在且 .ioc 未变 → 直接读缓存; 否则重解析."""
    if not force and cache_path.is_file() and _cache_is_fresh(ioc_path, cache_path):
        try:
            cached = yaml.safe_load(cache_path.read_text(encoding="utf-8"))
            if cached and cached.get("platform") == "stm32":
                return cached
        except Exception:
            pass
    return parse_and_cache(ioc_path, cache_path)


def cache_path_for(root: Path, ioc_path: Path) -> Path:
    return root / ".hardc" / f"{ioc_path.stem}.periph.yaml"


if __name__ == "__main__":
    import json

    root = Path(sys.argv[1] if len(sys.argv) > 1 else ".")
    ioc = find_ioc(root)
    if not ioc:
        print(f"[FAIL] 未找到 .ioc: {root}", file=sys.stderr)
        sys.exit(1)
    periph = parse_and_cache(ioc, cache_path_for(root, ioc))
    print(json.dumps(periph, indent=2, ensure_ascii=False))
