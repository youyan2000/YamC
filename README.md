# yamc 使用说明

**yamc = HardC 的配置与接入工具链**（YAML → C 注入 + 拓扑生成 + 外部 CubeMX 工程接入 + 双固件/OTA 编排）。
本仓库是独立工具仓库（GitHub `YamC`）；零件库在 [hardc](../hardc)（GitHub `HardC`），文档在根容器 [../docs](../docs)（本地）。

一个工具链，三种干活方式：

| 方式 | 入口 | 适合 |
|------|------|------|
| **GUI** | `python yaml_config_builder.py` | 点按钮、看反馈、对工程做骨架/调参 |
| **CLI 流水线** | `python yamc_cfg.py -d <工程> --topology buck` | 一键把 HardC 接入**真实 CubeMX 工程**（含三宏注入 + 编译） |
| **CLI 单体命令** | `scaffold.py` / `flash_map_gen.py` / `merge_firmware.py` / `yamc_ioc.py` | 脚本化 / 与 GUI Tab4 逐项对应 |

> 命令一律在**本仓库根**执行（脚本已扁平化，无 `yamc/` 前缀）。本文件是**完整操作手册**：怎么定位库根、怎么配置、怎么接入真实工程、怎么排查。

---

## 0. 先懂：yamc 怎么找 hardc 库根（拆分后关键）

工具本身**不含**零件库（cmake/、Config/ 都在 hardc 仓库）。运行时的**库根解析顺序**：

1. **`HARDC_LIB_DIR`** 环境变量（最高优先）
   ```powershell
   $env:HARDC_LIB_DIR = "F:\My_Projects\HardC\hardc"
   ```
2. **`--hardc-path`**（显式给命令）
   ```powershell
   python yamc_cfg.py -d <工程> --topology buck --no-submodule --hardc-path F:\My_Projects\HardC\hardc
   ```
3. **同级 `../hardc`**（本地三仓库平铺时自动命中：`F:\My_Projects\HardC\hardc`）
4. **工程内 submodule**（`Middlewares/Third_Party/HardC`，`--git-source` 接入时）

库根被 `ensure_hardc_dir` 校验：必须含 `cmake/HardC.CMake`。找不到报"找不到 hardc 库根: 设 HARDC_LIB_DIR 或 --hardc-path"。

> 本地三仓库平铺时，直接 `python yamc_cfg.py -d <工程> --topology buck` 即命中 `../hardc`，无需任何参数。

---

## 1. 安装与依赖

```bash
pip install pyyaml            # 必需（所有脚本）
pip install PyQt6 darkdetect  # GUI 必需
pip install pyserial          # Tab3 运行时调参（可选）
```

编译需 `cmake`（建议 `-G Ninja`），STM32 需 `starm-clang`（在库根 `cmake/`），C2000 需 `cl2000`/`C2000Ware`。

---

## 2. GUI 使用（Tab1–Tab4）

启动（本仓库根）：`python yaml_config_builder.py`

- **Tab1 参数注入**：选配置变体 → 页内可编辑参数表 → 预览 → 注入物化的 `app_main.c` → 编译。
- **Tab2 拓扑选择**：列库根 `Config/topologies/*.yaml`（`status: ready` 才可生成：buck/supercap_3ph）→ 填工程名/MCU/变体 → 参数表编辑 → 可选勾「Bootloader(双固件+OTA)」→ 生成/注入/编译（Ninja + starm-clang）。
  - 底部「外部工程接入」：填工程根（含 .ioc）→ 探测 → 运行完整接入（与 `yamc_cfg` 同流水线）。
- **Tab3 运行时调参**：串口 0xFB 帧下发（需 pyserial）。
- **Tab4 工具 (CLI)**：GUI ≅ CLI 对齐（scaffold scan/gen/deps、flash_map list/show/gen、merge selftest/merge/info）。「工具运行根」指 hardc 库根；默认取 `HARDC_LIB_DIR` 或同级 `../hardc`。

---

## 3. CLI 单体命令（仓库根执行）

### scaffold.py — MANIFEST 扫描 / 依赖解析 / 骨架生成
```bash
python scaffold.py scan                     # 校验库根全部 MANIFEST
python scaffold.py deps components/pid      # 传递闭包依赖
python scaffold.py gen Config/projects/buck.yaml --out build/gen/my_buck
```

### flash_map_gen.py — Flash 分区 → bsp_flash_map.h + 链接脚本
```bash
python flash_map_gen.py list
python flash_map_gen.py show --mcu stm32f334
python flash_map_gen.py gen --mcu stm32f334 --out build/gen/x
```

### merge_firmware.py — Intel HEX 合并 / 信息 / 自检
```bash
python merge_firmware.py merge bootloader.hex app.hex -o merged.hex
python merge_firmware.py info build/gen/buck/build/app.hex
python merge_firmware.py selftest
```

### yamc_ioc.py — 解析 CubeMX `.ioc` → 外设 YAML + 摘要
```bash
python yamc_ioc.py -d <工程根>                  # 摘要 + .hardc/<stem>.periph.yaml
python yamc_ioc.py -d <工程根> -o out.yaml --verbose --force
```

### 与 LibXR 工具链对照
| LibXR | yamc 对应 | 状态 |
|-------|-----------|------|
| `xr_cubemx_cfg` | `yamc_cfg.py` | ✅ 对标 |
| `xr_parse_ioc` | `yamc_ioc.py` | ✅ |
| `xr_gen_code_stm32` | `gen_app.py`（engine 调用） | ✅ 内建 |
| `xr_stm32_cmake` | `cmake_integrate.py` + 库根 `HardC.CMake` | ✅ |
| `xr_stm32_flash` | `flash_map_gen.py` | ✅ 覆盖 |
| `xr_cubemx_generate` / `xr_stm32_toolchain_switch` | 显式留白 | ⏳ |

---

## 4. 接入真实 CubeMX 工程（端到端，核心）

**这是把 HardC 接进你自己的 STM32/C2000 工程的正道。**

### 4.1 CubeMX 侧先配好（STM32F334 实例）

| 外设 | 用途 | 三档 |
|------|------|------|
| **HRTIM1** Timer A (CHTA1/CHTA2 → PA8/PA9) | 发波（Buck） | **FAST** → `HRTIM1_TIMA_IRQn` |
| **TIM1** (update) | 监控定时器 | **SLOW** → `TIM1_UP_TIM16_IRQn` |
| **CAN 或 USART2** | 通信 / HMI | **HMI** → `CAN_RX0_IRQn` / `USART2_IRQn` |
| **ADC1** + **DMA1 CH1** | 采样 vout/iout/vin | — |

在 CubeMX NVIC 勾 Ena. 上表中断（工具从 `.ioc` 探测三宏，依赖此处）。探测不到会 WARN，回 CubeMX 补开再重跑。

### 4.2 一键接入

```bash
cd <你的_F334_CubeMX工程根>
python <yamc仓库>/yamc_cfg.py -d . --topology buck \
  --git-source https://github.com/youyan2000/HardC.git
python <yamc仓库>/yamc_cfg.py -d . --topology buck --no-submodule --hardc-path <已有hardc目录>  # adopt 已有库
```

它做 5 件事：submodule 接入 → `.ioc` 解析 → `User/Application/app_main.c/h` 生成 → CMake 注入幂等 `yamc HardC BEGIN/END` 块（`set(HARDC_DIR…)` + `include(HardC.CMake)` + `target_link_libraries` + 三宏 `target_compile_definitions`）→ 编译 `.elf`。

**日志重点**：`✓ CMake 集成: 插入 HardC 块 (series=F3, irq=3)` = 三宏注入成功；`irq=3` 表示 FAST/SLOW/HMI 三宏已注入。

> 三档中断宏：yamc 从 `.ioc` NVIC **自动探测整组**并注入（`HMI_IRQN` + `HMI_IRQN_2..4` 多源：UART/CAN/FDCAN/EXTI 各独立 IRQn）。探测规则按名字：HRTIM→FAST、TIM*_UP→SLOW、CAN/FDCAN/UART/USART/LPUART→HMI；Cortex 核异常自动排除。FAST>SLOW>HMI 由 `bsp_irq_apply` 强制，配错停机。

### 4.3 之后写你的 App 层 HMI

生成物含可编译骨架（`User/Application/app_main.c`）。在 `Board_init` / `App_OnControlTick`(FAST ISR) / `App_OnHmiTick`(HMI ISR) / `BackgroundTask`(主循环) 里写你的逻辑：按键去抖/OLED/串口/CAN 归 **HMI**，控制算法归 **FAST**，慢 I/O/printf 归 **BackgroundTask**（不许在 ISR 里 printf）。模板预留 `HMI USER AREA`/`OTA AREA` 接缝。

### 4.4 验证闭环 checklist

- [ ] `.ioc` 三档中断已在 NVIC 打开
- [ ] `yamc_cfg -d . --topology …` 日志 `irq=3`
- [ ] 编译 0 错误，产出 `.elf`
- [ ] （OTA/双固件）`build/gen/…/build/*.hex` + merge → 单 `_merged.hex`
- [ ] App 层 HMI 在你的上下文跑通
- [ ] 改动已 commit + push

---

## 5. 常见问题排查

| 症状 | 原因 / 处理 |
|------|------------|
| `nmake: no such file or directory` | 生成器默认 NMake → `-G Ninja`（yamc 默认已是） |
| `starm-clang: command not found` | 工具链不在 PATH → 加 `<stm32cube>\bundles\st-arm-clang\<ver>\bin`，重开终端 |
| `unknown type name 'CAN_HandleTypeDef'` | CubeMX 未使能该 HAL → 工程 `set(HARDC_BSP "…")` 裁剪（见库根 HardC.CMake） |
| `FAST_CTRL_IRQN … undeclared` | 三宏未探测到（NVIC 未开）→ 回 CubeMX 打开重跑 |
| **`找不到 hardc 库根`** | 工具定位不到库 → 设 `HARDC_LIB_DIR` 或用 `--hardc-path`（§0） |
| `HARDC_DIR` 路径不对 | 注入块首行 `set(HARDC_DIR …)` 应指向 submodule 相对路径（一般是 `Middlewares/Third_Party/HardC`） |
| `target_link_libraries` 签名冲突 | CubeMX plain vs 注入 PRIVATE → 改 plain `target_link_libraries(${PROJECT_NAME} hardc)` |
| bootloader `not a compile-time constant` | `Module/bootloader` 默认不编；要 OTA 才 `set(HARDC_ENABLE_BOOTLOADER ON)` |
| 想合并 bootloader+app 单 `.hex` | `merge_firmware.py merge ...` 或 GUI 勾 Bootloader |

---

## 6. 红线（曾导致仓库损坏）

- **禁止** `mklink /J <同名> <仓库根>` 同名 junction —— 曾致"仓库全丢"事故（lessons #74）。接入用 **git submodule 或 xcopy**。
- 改了代码及时 **git commit + push**，别长期滞留未提交改动。

---

## 7. 相关路径（库根 hardc / 根容器 docs）

| 路径 | 内容 |
|------|------|
| （库根）`Config/topologies/*.yaml` | 拓扑定义（ready 才可生成） |
| （库根）`Config/flash_map.yaml` | Flash 分区真相 |
| （库根）`cmake/HardC.CMake` | 库接入（BSP/CORE 裁剪 + 平台链接） |
| （库根）`cmake/*.cmake` | 工具链文件 |
| （库根）`App/*.tmpl` | App 模板 |
| [../docs](../docs) | 项目全部文档（设计原则/计划/子系统/历史） |