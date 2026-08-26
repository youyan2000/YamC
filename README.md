# yamc

> **HardC 的"装配线"** —— 把 STM32 / C2000 的嵌入式工程，变成跑上 [HardC](https://github.com/youyan2000/HardC) 电源/电机控制算法的成品：一条命令做库接入、外设探测、代码生成、编译出 `.elf/.hex`，再用 GUI 或 CLI 调参。

![Python](https://img.shields.io/badge/Python-3.9%2B-blue)
![Platforms](https://img.shields.io/badge/platform-STM32%20%7C%20C2000-green)
![license](https://img.shields.io/badge/license-MIT-blue)

HardC 是**库**（一堆 C99 组件，PWM/PID/ADC/PLL/保护…），yamc 是**装配线**：你给它一个 CubeMX（`.ioc`）或 CCS（`main.syscfg`）工程 + 一个拓扑名，它自动完成下面第 ②~⑤ 步，之后你在 App 层写自己的 HMI。

```
 ① 已有 CubeMX/CCS 工程（.ioc / main.syscfg）
 ② HardC 库接入（git submodule）
 ③ 外设自动探测（HRTIM/ADC/UART/CAN… → 外设表）
 ④ 生成 App 骨架（app_main.c/h + 拓扑/参数绑定）
 ⑤ CMake 集成 + 编译（.elf / 合并 .hex）
```

---

## ✨ 它解决什么问题

嵌入式开发里重复的脏活：**从 CubeMX 到"能跑的控制 App"** 之间是长串手工步骤——加库、读 .ioc 抄外设句柄、写 CMake、配中断优先级、烧录前合并 bootloader。yamc 全部自动化，并给你两套操作方式：

| | CLI（脚本化/CI/远程） | GUI（交互式探索） |
|---|---|---|
| 接入工程 | `yamc cfg_run -d <工程> --topology buck` | 「项目识别」Tab → 填工程根 → 运行完整接入 |
| 改参数 | `yamc params` / `yamc tune_static` | 「参数注入」Tab → 表格直接改 → 保存/注入/编译 |
| 运行时调参 | `yamc serial tune --port COMx --param pid_v.kp=2.0` | 「运行时调参」Tab → 串口 0xFB 下发 |
| 看环境 | `yamc probe` / `yamc check` | 「项目识别」Tab → 自检 / 探测 |

> 每个 GUI 按钮旁边都显示它对应的 `yamc ...` 命令并一键复制——**在 GUI 学会 → 拿到脚本**。

---

## 🚀 快速上手

### 安装

```bash
git clone https://github.com/youyan2000/YamC.git
cd YamC
pip install .            # 核心（pyyaml）
pip install ".[gui]"     # 可选：PyQt6 GUI
pip install ".[serial]"  # 可选：串口动态调参
```

装完获得 `yamc`（伞命令）和 22 个 `yamc_*` 命令，**任意目录可用**。

### 方式 A：GUI

```bash
yamc gui
```

四个 Tab，跟着走一遍就成了：

| Tab | 你做什么 |
|---|---|
| **① 项目识别** | 填工程根 → 「探测」确认平台 → 「运行完整接入」一键做完 ②~⑤ |
| **② 拓扑选择** | 左边选拓扑（buck / supercap_3ph…）→ 填参数 → 「生成工程」 |
| **③ 参数注入** | 选一个参数变体 → 表格改值 → 「保存到 YAML」或「仅注入 C」→「编译」 |
| **④ 运行时调参** | 连串口 → 表格填 PID 系数 → 「下发 0xFB」即时生效 |

每个 Tab 底部都是日志面板；顶部命令栏可把当前动作复制成 CLI。

### 方式 B：CLI

**接入一个真实工程**（最常用的一条命令）：

```bash
yamc cfg_run -d D:/proj/my_psu --topology buck --hardc-path F:/My_Projects/HardC/hardc --no-build
```

它做了什么：探测平台 → 接 HardC → 解析外设 → 生成 `app_main.c/h` → 注入 CMake →（去掉 `--no-build` 就编译）。

**手动一步步做**：

```bash
yamc probe                              # 这个目录/平台能识别吗？hardc 库根在哪？
yamc ioc_parse -d <工程根>              # .ioc → 外设表 YAML
yamc gen_code -d <工程根> -t buck       # 拓扑+外设 → app_main.c/h
yamc cmake_inject -d <工程根> -t buck   # 注入 CMake
yamc build -d <工程根>                  # 编译
```

**调参**：

```bash
yamc params list -d <工程根>                                    # 看有哪些参数变体
yamc tune_static -d <工程根> --variant default --set pid_v.kp=2.0 --apply   # 静态改参(改代码)
yamc serial list                                                # 找串口
yamc serial tune --port COM5 --param pid_v.kp=2.0 -t buck       # 动态改参(运行时下发)
```

---

## 📦 命令总览

**22 个独立命令**（`yamc <tool> <action> ...` 与 `yamc_<tool>_<action> ...` 等价）：

| 组 | 命令 | 一句话 |
|---|---|---|
| **接入** | `cfg_run` / `gen_code` / `gen_bootloader` / `cmake_inject` / `build` | 一条龙接入 / 生成 App / Bootloader / 注入 CMake / 编译 |
| **解析** | `parse` / `ioc_parse` / `syscfg_parse` | 平台感知分析 / STM32 .ioc / C2000 syscfg → 外设表 |
| **工程生成** | `cubemx_generate` / `ccs_generate` | stm32 / c2000 工程自动重生成 |
| **分区/烧录** | `flash list\|show\|gen` / `merge` | Flash 分区 → 链接脚本 / HEX 合并 |
| **工具链** | `switch` | gcc ⇄ clang 切换（CMakePresets） |
| **拓扑** | `topo list\|show\|gen` | 选拓扑 / 看 schema / 生成工程骨架 |
| **脚手架** | `scaffold scan\|deps\|gen` | MANIFEST 扫描 / 依赖解析 / 骨架生成 |
| **调参** | `params` / `tune_static` / `serial` | 参数变体 / 静态改参 / 动态(串口 0xFB) |
| **诊断** | `probe` / `check` / `--version` | 探测环境 / 自检 / 版本 |
| **GUI** | `gui` | 启动图形界面 |

退出码契约：`0` = 全部通过 / `1` = 失败 / `2` = 平台或依赖不支持。全局 `--debug`；多数命令 `-d` 缺省自当前目录向上找工程根。

---

## 💡 概念速览

- **拓扑（topology）**：一个电源/控制方案，如 `buck`（降压）、`supercap_3ph`（三相超级电容）。定义在 HardC 库根 `Config/topologies/<name>.yaml`，含 `status: ready` 才可生成。
- **参数变体（variant）**：一组可注入的配置（`Config/params/*.yaml`），`config_id` 标识，改完「注入 C」写进 `app_main.c` 的 `CONFIG BEGIN/END` 区。
- **静态调参**：改参数 → 重新注入 C 代码（编译期生效）。
- **动态调参**：串口发 48 字节 0xFB 帧，按拓扑 params 的 `slot` 布局下发（运行期生效，不重编译）。

hardc 库根定位顺序：`HARDC_LIB_DIR` 环境变量 → `--hardc-path` → 同级 `../hardc` → 工程内 submodule。校验：须含 `cmake/HardC.CMake`。

---

## 🧱 目录结构

```
YamC/
├── pyproject.toml          # 打包配置（23 个命令入口）
├── src/yamc/               # 源码（src-layout，与 libxr 一致）
│   ├── cli.py              # 伞命令 + 全部 yamc_* 入口
│   ├── engine.py           # 接入流水线编排
│   ├── gen_app.py          # 拓扑+外设 → app_main.c/h
│   ├── ioc_parse.py / c2000_syscfg.py   # 外设解析
│   ├── params.py / serial_tune.py       # 静态/动态调参
│   ├── yaml_config_builder.py           # PyQt6 GUI
│   └── ...
├── scripts/                # 冒烟脚本 & 兼容 shim
└── tests/                  # pytest 套件
```

---

## 🧪 开发

```bash
pip install ".[gui,serial]"
pip install pytest
pytest tests -q
```

CI（`.github/workflows/ci.yml`）在 Windows / Linux 上跑：安装 → 编译检查 → CLI 冒烟 → 全量测试。

---

## 📚 相关

- **[HardC](https://github.com/youyan2000/HardC)** —— 零件库（C99 电源/电机控制组件）
- **[YamC](https://github.com/youyan2000/YamC)** —— 本库：配置/接入/生成工具链

## License

MIT
