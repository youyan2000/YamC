# yamc 使用说明

**yamc = HardC 的配置与接入工具链**（YAML → C 注入 + 拓扑生成 + 外部 CubeMX 工程接入 + 双固件/OTA 编排）。
本仓库是独立工具仓库（GitHub `YamC`）；零件库在 [hardc](../hardc)（GitHub `HardC`），文档在根容器 [../docs](../docs)（本地）。

一个工具链，三种干活方式：

| 方式 | 入口 | 适合 |
|------|------|------|
| **GUI** | `python yaml_config_builder.py` | 想点按钮、看反馈、对工程做骨架/调参 |
| **CLI 流水线** | `python yamc_cfg.py -d <工程> --topology buck` | 一键把 HardC 接入**真实 CubeMX 工程**（含三宏注入 + 编译） |
| **CLI 单体命令** | `scaffold.py` / `flash_map_gen.py` / `merge_firmware.py` / `yamc_ioc.py` | 脚本化 / 与 GUI Tab4 逐项对应 |

> 本文件按"库根定位 → GUI → CLI → 真实工程教程 → 排查"组织。所有命令在**本仓库根**执行（脚本已扁平化，不再有 `yamc/` 子目录前缀）。

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

库根被 `ensure_hardc_dir` 校验：必须含 `cmake/HardC.CMake`。找不到会报"找不到 hardc 库根: 设 HARDC_LIB_DIR 或 --hardc-path"。

> 例：本机三仓库平铺时，直接 `python yamc_cfg.py -d <工程> --topology buck` 即命中 `../hardc`，无需任何参数。

---

## 1. 安装与依赖

```bash
pip install pyyaml            # 必需（所有脚本）
pip install PyQt6 darkdetect  # GUI 必需
pip install pyserial          # Tab3 运行时调参（可选，缺了只是 Tab3 不可用）
```

编译需要 `cmake`（建议 `-G Ninja`），STM32 需 `starm-clang` 工具链（在 hardc 库根 `cmake/`），C2000 需 `cl2000`/`C2000Ware`。

---

## 2. GUI 使用（Tab1–Tab4）

启动（在本仓库根）：
```bash
python yaml_config_builder.py
```

### Tab1「参数注入」
- 自动发现配置变体（工程参数在库根 `Config/params/*.yaml` 或工程侧，视布局）。
- 选变体 → 页内可编辑参数表 → 预览 → 注入物化的 `app_main.c` → 编译。

### Tab2「拓扑选择」
1. 左侧列出库根 `Config/topologies/*.yaml`。**只有 `status: ready` 的拓扑能生成**（当前 ready：`buck`、`supercap_3ph`）。
2. 填工程名 / MCU / 变体名；参数表按拓扑 `params:` schema 动态生成。
3. 勾选「Bootloader(双固件+OTA)」→ 生成 `bootloader_main` + 双固件 CMake + 合并单 `.hex`。
4. 「⚒ 生成工程」→ scaffold 写 `build/gen/<name>/`。「⟳ 注入 App」→ 物化 `app_main.c`。
5. 「⚙ 编译」→ Ninja + starm-clang（工具链从库根定位）。

**外部工程接入（Tab2 底部）**：填工程根（含 `.ioc`）→ 探测 → 运行完整接入（与 `yamc_cfg` 同流水线，engine 后台线程）。

### Tab3「运行时调参」
串口 0xFB 帧下发（48 字节，PID 调参）。需 `pip install pyserial`。

### Tab4「工具 (CLI)」— GUI ≅ CLI 对齐

| 按钮 | 等价 CLI（仓库根执行） |
|------|----------|
| scaffold scan | `python scaffold.py scan` |
| scaffold gen | `python scaffold.py gen <Config/projects/xxx.yaml>` |
| scaffold deps <id> | `python scaffold.py deps <module_id>` |
| flash_map list/show/gen | `python flash_map_gen.py list / show / gen` |
| merge selftest/merge/info | `python merge_firmware.py selftest / merge … / info <hex>` |

> Tab4 的「工具运行根」指 **hardc 库根**（scaffold/flash_map 读库根的 Config/、cmake/）；默认取 `HARDC_LIB_DIR` 或同级 `../hardc`。

---

## 3. CLI 单体命令（仓库根执行）

### 3.1 `scaffold.py` — MANIFEST 扫描 / 依赖解析 / 骨架生成
```bash
python scaffold.py scan                     # 校验库根全部 MANIFEST.yaml
python scaffold.py deps components/pid      # 传递闭包依赖
python scaffold.py gen Config/projects/buck.yaml --out build/gen/my_buck
```

### 3.2 `flash_map_gen.py` — Flash 分区 → bsp_flash_map.h + 链接脚本
```bash
python flash_map_gen.py list
python flash_map_gen.py show --mcu stm32f334
python flash_map_gen.py gen --mcu stm32f334 --out build/gen/x
```
数据真相在库根 `Config/flash_map.yaml`。

### 3.3 `merge_firmware.py` — Intel HEX 合并 / 信息 / 自检
```bash
python merge_firmware.py merge bootloader.hex app.hex -o merged.hex
python merge_firmware.py info build/gen/buck/build/app.hex
python merge_firmware.py selftest
```

### 3.4 `yamc_ioc.py` — 解析 CubeMX `.ioc` → 外设 YAML + 摘要（对标 xr_parse_ioc）
```bash
python yamc_ioc.py -d <工程根>                  # 摘要 + .hardc/<stem>.periph.yaml
python yamc_ioc.py -d <工程根> -o out.yaml --verbose --force
```

### 3.5 与 LibXR 工具链指令对照

| LibXR | 作用 | yamc 对应 | 状态 |
|-------|------|-----------|------|
| `xr_cubemx_cfg` | 配置 CubeMX 工程 | `yamc_cfg.py` | ✅ 对标 |
| `xr_parse_ioc` | .ioc → YAML | `yamc_ioc.py` | ✅ |
| `xr_gen_code_stm32` | YAML → STM32 代码 | `gen_app.py`（engine 调用） | ✅ 内建 |
| `xr_stm32_cmake` | CMake 注入 | `cmake_integrate.py` + 库根 `HardC.CMake` | ✅ |
| `xr_stm32_flash` | 型号 → Flash 扇区表 | `flash_map_gen.py` | ✅ 覆盖 |
| `xr_cubemx_generate` / `xr_stm32_toolchain_switch` | CubeMX 脚本化 / 工具链切换 | 显式留白 | ⏳ |

---

## 4. CLI 流水线：接入真实 CubeMX 工程（端到端）

```bash
python yamc_cfg.py -d <工程根> --topology buck --git-source https://github.com/youyan2000/HardC.git
```
常用开关：

| 参数 | 含义 |
|------|------|
| `-d, --dir` | 外部工程根（含 `.ioc`/`.syscfg`）；默认 `.` |
| `--topology` | 拓扑名（库根 `Config/topologies/<name>.yaml`）；默认 `buck` |
| `--git-source <url>` | HardC git URL，submodule add |
| `--hardc-path <路径>` | adopt 已有 hardc 目录（不建 submodule） |
| `--no-submodule` | 不做 submodule（配合 `--hardc-path`） |
| `--no-build` | 只生成与集成，跳过构建 |
| `--params <yaml>` | 参数 YAML（平铺或 `config: {power:{…}}`）|
| `--sdk-dir` | c2000：C2000Ware 根 |

**做了什么**：HardC submodule 接入 → `.ioc` 解析 → `app_main.c` 生成（工程 `User/Application/`）→ CMake 注入幂等 `yamc HardC BEGIN/END` 块（含三宏 `FAST/SLOW/HMI`）→ 编译 `.elf`（勾 bootloader → 合并单 `.hex`）。

> 三档中断宏：工具从 `.ioc` NVIC **自动探测**整组并注入（`HMI_IRQN` + `HMI_IRQN_2..4` 多源）；探测不到 WARN。FAST>SLOW>HMI 由 `bsp_irq_apply` 强制，配错停机。

---

## 5. 红线（曾导致仓库损坏）

- **禁止** `mklink /J <同名> <仓库根>` 同名 junction —— 曾致"仓库全丢"事故（lessons #74）。接入用 **git submodule 或 xcopy**。
- 改了代码及时 **git commit + push**，别长期滞留未提交改动。

---

## 6. 常见问题排查

| 症状 | 原因 / 处理 |
|------|------------|
| `nmake: no such file or directory` | 生成器默认 NMake。用 `-G Ninja`（yamc 默认已是） |
| `starm-clang: command not found` | 工具链不在 PATH。加 `<stm32cube>\bundles\st-arm-clang\<ver>\bin` |
| `unknown type name 'CAN_HandleTypeDef'` | CubeMX 未使能该 HAL。工程 `set(HARDC_BSP "…")` 裁剪（见库根 HardC.CMake） |
| `FAST_CTRL_IRQN … undeclared` | 三宏未探测到（NVIC 未开）→ 回 CubeMX 打开重跑 |
| **`找不到 hardc 库根`** | 工具定位不到库：设 `HARDC_LIB_DIR` 或用 `--hardc-path`（见 §0） |
| 想合并 bootloader+app 单 `.hex` | `merge_firmware.py merge ...` 或 GUI 勾 Bootloader |

---

## 7. 相关路径（库根 hardc / 根容器 docs）

| 路径 | 内容 |
|------|------|
| （库根）`Config/topologies/*.yaml` | 拓扑定义（ready 才可生成） |
| （库根）`Config/flash_map.yaml` | Flash 分区真相 |
| （库根）`cmake/HardC.CMake` | 库接入（BSP/CORE 裁剪 + 平台链接） |
| （库根）`cmake/*.cmake` | 工具链文件（starm-clang/gcc/c2000） |
| （库根）`App/*.tmpl` | App 模板 |
| [../docs/guides/external-project-integration.md](../docs/guides/external-project-integration.md) | 真实工程接入指南（本地 docs） |