"""ymac_cfg — STM32CubeMX .ioc → 外设 YAML (缓存 .hardc/periph.yaml).

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
# 注: 目前仅覆盖 F334 (单 HRTIM1, 死区键/时钟硬编码 HRTIM1); G474 双 HRTIM 需推广.
_DEADTIME_KEY = "HRTIM1.DeadTimeInsertion-Output_T{t}1T{t}2"


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
    """HRTIM1 子定时器 (Periode_TX) + 死区插入 + 输出引脚."""
    instances: list = []
    for ip in _ip_list(raw):
        if not ip.startswith("HRTIM"):
            continue
        timers = []
        for t in _HRTIM_TIMERS:
            period_key = f"{ip}.Periode_T{t}"
            if period_key in raw:
                complementary = raw.get(_DEADTIME_KEY.format(t=t), "").endswith("ENABLED")
                timers.append({
                    "name": t,
                    "period": _to_int(raw[period_key]) or 0,
                    "complementary": complementary,
                })
        if not timers:
            continue
        clk = _to_int(raw.get("RCC.HRTIM1Freq_Value")) or 0
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
