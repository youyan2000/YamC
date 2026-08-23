# YmaC 使用说明

**YmaC = HardC 的配置与接入工具链**（YAML → C 注入 + 拓扑生成 + 外部 CubeMX 工程接入 + 双固件/OTA 编排）。

一个工具链，三种干活方式：

| 方式 | 入口 | 适合 |
|------|------|------|
| **GUI** | `python YmaC/yaml_config_builder.py` | 想点按钮、看反馈、在工程目录内做骨架/调参 |
| **CLI 流水线** | `python YmaC/ymac_cfg.py -d <工程> --topology buck` | 一键把 HardC 接入**真实 CubeMX 工程**（含三宏注入 + 编译） |
| **CLI 单体命令** | `scaffold.py` / `flash_map_gen.py` / `merge_firmware.py` / `ymac_ioc.py` | 脚本化 / 与 GUI Tab4 逐项对应 |

本文件按"入门 → GUI → CLI → 真实工程教程 → 排查"组织。与 CLI 完全等价的 GUI 按钮都在 **Tab4「工具 (CLI)」**。

---

## 0. 安装与依赖

```bash
pip install pyyaml            # 必需（所有脚本）
pip install PyQt6 darkdetect  # GUI 必需
pip install pyserial          # Tab3 运行时调参（可选，缺了只是 Tab3 不可用）
```

编译需要 `cmake`（建议 `-G Ninja`），STM32 需 [`starm-clang`](https://www.st.com/) 工具链，C2000 需 `cl2000`/`C2000Ware`。见下文「工具链」一节。

---

## 1. 你真的用哪一种？

YmaC 处理两种项目形态，先分清，后面的教程才不会走错目录：

- **仓库内骨架工程（HardC 根就是工程）**：用 GUI Tab2，产出在 `build/gen/<name>/`，不碰外部 CubeMX。适合练手 / 快速看拓扑生成。
- **真实 CubeMX 工程（外部，`*.ioc`）**：用 CLI `ymac_cfg.py`（或 GUI「外部工程接入」），把 HardC 以 submodule 接进去、注入 CMake + 三宏、编译真实固件。**这是最终交付形态。**

> 三档中断宏 `FAST_CTRL_IRQN / SLOW_CTRL_IRQN / HMI_IRQN`：工具能**从 `.ioc` 的 NVIC 自动探测**并注入 CMake（无需你手填）。**HMI 可同挂多个通信/按键源**（UART + CAN + FDCAN + 按键 EXTI…每个都是独立 IRQn）：工具会把主源填 `HMI_IRQN`、其余源依次填 `HMI_IRQN_2/3/4` 一并注入，`bsp_irq_apply` 把它们统一钉到抢优 2。探测不到时会在日志 WARN，需手工定义。名义必须遵循 **FAST > SLOW > HMI** 三档（`bsp_irq_apply` 强制，配错会停机），见 `docs/coding/protection-hmi.md`。

---

## 2. GUI 使用（Tab1–Tab4）

启动（在仓库根或工程根）：

```bash
python YmaC/yaml_config_builder.py
```

### Tab1「参数注入」
- 自动发现配置变体（HardC 工程读 `Config/params/*.yaml`；legacy 工程读 `conf/*.yaml`）。
- 选变体 → 预览 → 注入到物化的 `app_main.c`（`/* CONFIG BEGIN */`…`/* CONFIG END */` 之间）→ 编译。
- 先确认顶部显示的项目根正确，再操作。

### Tab2「拓扑选择」
1. 左侧列出 `Config/topologies/*.yaml`。**只有 `status: ready` 的拓扑能生成**（当前 ready：`buck`、`supercap_3ph`；其余为 `planned` 只读预览）。
2. 填工程名 / MCU / 变体名。
3. 参数表按该拓扑 `params:` schema 动态生成（`QDoubleSpinBox`），改完点「写入参数」→ 写 `Config/params/<name>_<variant>.yaml`。
4. 勾选「Bootloader(双固件+OTA)」→ 生成会带上 `bootloader_main` + 双固件 CMake（bootloader/app 两个 target + `merge_firmware` 合并为单一烧录 `.hex`）。
5. 「⚒ 生成工程」→ scaffold 写出 `build/gen/<name>/`（CMakeLists + 依赖汇总头 + board_init 骨架）。
6. 「⟳ 注入 App」→ 物化 `build/gen/<name>/app_main.c`。
7. 「⚙ 编译」→ 用 **Ninja + starm-clang 工具链**（配置时自动定位 `cmake/starm-clang.cmake`）编译。

**外部工程接入（Tab2 底部 group）：**
- 填/浏览工程根（含 `.ioc`）→ 「探测」确认平台 → 勾 skip 选项 → 「▶ 运行完整接入」。这个按钮跑的就是 `ymac_cfg` 的**同一条流水线**（engine 后台线程），输出进日志。

### Tab3「运行时调参」
- 串口 0xFB 帧下发（48 字节，PID 调参协议，与离线 `params.slot` 同源）。需 `pip install pyserial`，缺了此 Tab 会提示且不可用。

### Tab4「工具 (CLI)」— GUI ≅ CLI 功能对齐
这里的每个按钮**直接以子进程调用对应 CLI 脚本本体**，因此 GUI 与命令行 `--help` 列出的命令完全等价，输出回显到下方日志：

| 按钮 | 等价 CLI |
|------|----------|
| scaffold scan | `python YmaC/scaffold.py scan` |
| scaffold gen | `python YmaC/scaffold.py gen <Config/projects/xxx.yaml>`（逐个跑当前工程） |
| scaffold deps <module_id> | `python YmaC/scaffold.py deps <module_id>` |
| flash_map list / show / gen | `python YmaC/flash_map_gen.py list / show / gen` |
| merge selftest / merge / info | `python YmaC/merge_firmware.py selftest / merge … / info <hex>` |

> 若在外部工程里开 GUI（非 HardC 根），Tab4 需在「工具运行根(HardC 仓库)」填 HardC 仓库根，否则 `find_repo_root` 找不到 `Config/`。

---

## 3. CLI 单体命令

### 3.1 `scaffold.py` — MANIFEST 扫描 / 依赖解析 / 骨架生成

```bash
# 校验全部 MANIFEST.yaml（结构 + 依赖合法性）
python YmaC/scaffold.py scan

# 查看某模块的传递闭包依赖（拓扑排序 BSP→…→App）
python YmaC/scaffold.py deps components/pid
python YmaC/scaffold.py deps module/power

# 从工程配置生成骨架 → build/gen/<project>/
python YmaC/scaffold.py gen Config/projects/buck.yaml
python YmaC/scaffold.py gen Config/projects/buck.yaml --out build/gen/my_buck
```

产物（`build/gen/<project>/`）：`CMakeLists.txt`（闭包全部 .c + include 路径）、`<project>_deps.h`（按层分组依赖头清单）、`board_init_stub.c`（逐模块 `// TODO` 占位）。

### 3.2 `flash_map_gen.py` — Flash 分区 → `bsp_flash_map.h` + 链接脚本

```bash
python YmaC/flash_map_gen.py list                      # 列出 flash_map.yaml 全部 MCU 与分区概况
python YmaC/flash_map_gen.py show --mcu stm32f334     # 展示某 MCU 完整解析分区
python YmaC/flash_map_gen.py gen --mcu stm32f334      # 生成 bsp_flash_map.h + bootloader/app 两个 .ld
python YmaC/flash_map_gen.py gen --out build/gen/x     # 自定输出目录
```

数据真相在 `Config/flash_map.yaml`（分区 = bootloader / app / param…）。双固件/OTA 的 Flash 骨架由此生成。

### 3.3 `merge_firmware.py` — Intel HEX 合并 / 信息 / 自检

```bash
# 合并多个 hex 为一个（bootloader.hex + app.hex → 单一烧录映像）
python YmaC/merge_firmware.py merge bootloader.hex app.hex -o merged.hex

# 查看单个 hex 的地址范围 / 段
python YmaC/merge_firmware.py info build/gen/buck/build/app.hex

# 内置往返自检
python YmaC/merge_firmware.py selftest
```

纯标准库实现，无第三方。`--overlap` 可选 `error`（默认，重叠即报错）/ `over`（后写覆盖）。这是把**双固件合并成单一 `.hex` 一次烧录**的落地者。

### 3.4 `ymac_ioc.py` — 解析 CubeMX `.ioc` → 外设 YAML + 摘要（对标 `xr_parse_ioc`）

```bash
python YmaC/ymac_ioc.py -d <工程根>                     # 摘要 + 写 .hardc/<stem>.periph.yaml
python YmaC/ymac_ioc.py -d <工程根> -o out.yaml        # 自定输出路径
python YmaC/ymac_ioc.py -d <工程根> --verbose           # 详细日志
python YmaC/ymac_ioc.py -d <工程根> --force             # 忽略缓存强制重解析
```

从 CubeMX `.ioc` 提取 MCU/HRTIM/ADC/UART/CAN 外设信息 → YAML + 控制台摘要（MCU 型号、外设计数）。等价 `xr_parse_ioc`，供 `ymac_cfg`/GUI 复用同一解析逻辑。

### 3.5 与 LibXR 工具链指令对照（对齐基准：`LibXR_CppCodeGenerator`）

| LibXR 指令 | 作用 | HardC 对应 | 状态 |
|-----------|------|-----------|------|
| `xr_cubemx_cfg` | 配置 CubeMX 工程 + submodule + 生成 | `ymac_cfg.py` | ✅ 对标（含三宏注入/编译）|
| `xr_parse_ioc` | `.ioc` → YAML + 摘要 | `ymac_ioc.py` | ✅ 新增 |
| `xr_parse` | 通用 YAML → 外设 | `ioc_parse.py`（库内） | ✅ 已内建 |
| `xr_gen_code_stm32` | YAML → STM32 代码 | `gen_app.py`（engine 调用） | ✅ 已内建 |
| `xr_gen_code` | YAML → 平台无关代码 | `scaffold.py gen` | ⚠️ 概念相近 |
| `xr_stm32_cmake` / `xr_libxr_cmake` | 生成/C库 CMake 注入 | `cmake/HardC.CMake` + `cmake_integrate.py` | ✅ 对标 |
| `xr_stm32_flash` | 型号 → Flash 扇区表 | `flash_map_gen.py list/show/gen` | ✅ 覆盖 |
| `xr_cubemx_generate` | 独立跑 CubeMX 脚本生成 | （不自动调 CubeMX GUI） | ⏳ 显式留白 |
| `xr_stm32_toolchain_switch` | 工具链切换 | starm-clang 由 PATH + preset 管理 | ⏳ 显式留白 |

> 说明：HardC 不自持 CubeMX GUI 自动化（`xr_cubemx_generate`）与多工具链切换（`xr_stm32_toolchain_switch`）两项——前者需登录/固件包等桌面环境、后者 CMake preset 已覆盖。如需再补可对齐实现。

---

## 4. CLI 流水线：接入真实 CubeMX 工程（端到端）

**这是把 HardC 接进你自己的 STM32/C2000 工程的正道。** 对标 `xr_cubemx_cfg`，一键完成：HardC submodule 接入 → `.ioc` 解析 → App 生成 → CMake 注入（含三档中断宏）→ 编译。

```bash
python YmaC/ymac_cfg.py -d <工程根> --topology buck
```

常用开关：

| 参数 | 含义 |
|------|------|
| `-d, --dir` | 外部工程根（含 `.ioc` 或 `.syscfg`）；默认 `.` |
| `--topology` | 拓扑名（`Config/topologies/<name>.yaml`）；默认 `buck` |
| `--git-source <url>` | HardC git 仓库 URL，**submodule add** 接入 |
| `--hardc-path <路径>` | 配合 `--no-submodule`：adopt 已有 HardC 目录（不建 submodule） |
| `--no-submodule` | 不做 submodule（adopt 现有目录），需 `--hardc-path` |
| `--no-build` | 只生成与集成，跳过构建 |
| `--params <yaml>` | 参数 YAML（平铺 `{'vref':12.5,'pid_v.kp':2}` 或 `config: {power:{…}}`）|
| `--sdk-dir` | c2000：C2000Ware/DigitalPower SDK 根（缺省自动探测） |

### 4.1 一次性思路（先明白要发生什么）

1. 工程必须是 CubeMX 生成的、含 `*.ioc`（STM32）或 `SysConfig` 产物（C2000）。
2. 工具会把 HardC 作为 **git submodule** 放进 `Middlewares/Third_Party/HardC`（`--no-submodule` 则用你已有目录）。
3. `.ioc` 被解析成外设 YAML → 生成 `app_main.c`（HRTIM 发波 + ADC 采样 + 控制骨架）。
4. 往工程 `CMakeLists.txt` 注入一个幂等 `YmaC HardC BEGIN/END` 块：
   - `set(HARDC_DIR …)` / `set(HARDC_DRIVER st)` / `set(HARDC_DEVICES "…")`
   - `include(${HARDC_DIR}/cmake/HardC.CMake)`
   - `target_sources(... app_main.c)` + `target_link_libraries(... hardc)`
   - `target_compile_definitions(... FAST_CTRL_IRQN=… SLOW_CTRL_IRQN=… HMI_IRQN=…)` — **三宏自动注入**
5. 编译（starm-clang / cl2000），产出 `.elf`（勾 bootloader 则再合并出单 `.hex`）。

### 4.2 示例：接入一个 STM32F334 工程

```bash
cd <你的_F334_CubeMX工程根>
python <HardC>/YmaC/ymac_cfg.py -d . --topology buck --git-source https://github.com/youyan2000/HardC.git
# 成功标志（日志）：
#   ✓ 生成 app_main.c/h
#   ✓ CMake 集成: 插入 HardC 块 (series=F3, irq=3)   <- irq=3 表示三宏已注入
#   ✓ 构建通过
```

`irq=3` 说明三宏已从 `.ioc` 探测到并注入；若日志是 WARN「未从 .ioc 探测到三档中断宏」，说明 `.ioc` 里 NVIC 没使能该类中断，需回 CubeMX 打开并重跑。

> **三档中断（在 CubeMX 里要配好，工具的探测依赖它）：**
> - **FAST**：HRTIM 发波定时器中断（如 `HRTIM1_TIMA_IRQn`）
> - **SLOW**：监控定时器更新中断（如 `TIM1_UP_TIM16_IRQn`）
> - **HMI**：通信/按键中断（如 `CAN_RX0_IRQn`、`USART2_IRQn`）
> 探测按名字分类：HRTIM→FAST、TIM*_UP→SLOW、CAN/FDCAN/UART/USART/LPUART→HMI；Cortex 核异常（SysTick/HardFault…）被自动排除。

---

## 5. 引导状态：三种工程接入方式小结（含"别踩的坑"）

| 你要的 | 推荐 | 备注 |
|--------|------|------|
| 快速看拓扑生成/骨架 | GUI Tab2，无需外部工程 | 产出在 `build/gen/<name>/` |
| 接入已有 CubeMX 工程 | CLI `ymac_cfg.py -d . --topology …` | 最常用 |

**红线（曾导致仓库损坏，务必遵守）：**
- **禁止**用 `mklink /J <同名> <HardC仓库根>` 这类"同名 junction"把源目录重定向成悬空链接——之前因此"仓库全丢"。接入用 **git submodule 或 xcopy 拷贝**。
- 改了代码就及时 **git commit + push**，别让本地长期挂未提交改动（事故时零兜底）。

---

## 6. 常见问题排查

| 症状 | 原因 / 处理 |
|------|------------|
| 编译报 `nmake: no such file or directory` | cmake 用了默认 NMake 生成器。用 `-G Ninja`（YmaC 现默认 Ninja）；确认本机装 Ninja |
| `cmake` 不在 PATH | 加 `cmake` 的 bin 到 PATH；找不到时 GUI 会弹"cmake 未找到" |
| `starm-clang: command not found` | 工具链不在 PATH。加 `<stm32cube>\bundles\st-arm-clang\<ver>\bin` 到用户 PATH，重开终端 |
| `unknown type name 'CAN_HandleTypeDef' / 'SPI_…'` | CubeMX 没使能该 HAL 外设，而 BSP 后端全量编译撞到缺失类型。需按拓扑裁剪：工程里 `set(HARDC_BSP "…")` 只列用到的后端（见 `cmake/HardC.CMake`） |
| `FAST_CTRL_IRQN / SLOW_CTRL_IRQN / HMI_IRQN` undeclared | 工具没从 `.ioc` 探测到（NVIC 未开对应中断）。回 CubeMX 打开，或手工在工程加三个宏 |
| `initializer element is not a compile-time constant`（bootloader）| `Module/bootloader` 默认不编，需 `set(HARDC_ENABLE_BOOTLOADER ON)` 才纳入 |
| `HARDC_DIR` 路径不对 | 注入块首行 `set(HARDC_DIR …)` 应指向真实 submodule 相对路径；外部工程一般是 `Middlewares/Third_Party/HardC` |
| `target_link_libraries … PRIVATE` 与 CubeMX 冲突 | HardC.CMake 与 CubeMX 链接应兼容 plain 签名；改回 `target_link_libraries(${PROJECT_NAME} hardc)` |
| 想合并 bootloader+app 成单一 `.hex` | `merge_firmware.py merge bootloader.hex app.hex -o out.hex`，或 GUI Tab2 勾 Bootloader 后由 CMake 自动合并 |
| GUI Tab3 打不开/不可用 | `pip install pyserial` |

---

## 7. 相关目录 / 文件

| 路径 | 内容 |
|------|------|
| `Config/topologies/*.yaml` | 拓扑定义（`status: ready` 才可生成；`params:` schema 驱动参数表） |
| `Config/projects/*.yaml` | 工程配置（声明式列出需要的子系统） |
| `Config/params/*.yaml` | 参数变体（`config_id` + `config: {…}`） |
| `Config/flash_map.yaml` | Flash 分区真相（bootloader/app/param…） |
| `cmake/HardC.CMake` | 库接入文件（BSP 裁剪 / CORE 裁剪 / 平台链接） |
| `cmake/starm-clang.cmake` | STM32 starm-clang 工具链文件 |
| `App/app_main.c.tmpl` | App 层实现模板（含 HMI/OTA 接缝） |
| `docs/coding/protection-hmi.md` | FAST>SLOW>HMI 三档约定与 `bsp_irq_apply` |

更多使用细节见 [../docs/guides/external-project-integration.md](../docs/guides/external-project-integration.md)。
