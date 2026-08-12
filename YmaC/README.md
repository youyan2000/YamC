# YAML Config Builder — 使用说明 & 格式规范

## 概述

**YAML Config Builder** 是一个通用的 YAML → C 代码配置注入工具。它将 `conf/` 目录中的 YAML 配置文件渲染为 C designated initializer（指定初始化器），并自动注入目标 C 文件中的标记区域。

- **跨平台**：Windows / Debian Linux
- **原生暗黑模式**：自动跟随系统主题
- **依赖**：`pip install PyQt6 pyyaml darkdetect`

## 快速入门

### 1. 目录结构

将你的项目组织为以下结构：

```
<项目根目录>/
  conf/                         ← 存放 YAML 配置变体
    device_a.yaml
    device_b.yaml
    ...
  User/
    app/
      app_main.c                ← 注入目标（需含标记对）
```

工具启动时自动从当前目录向上搜索 `conf/` 和 `User/app/app_main.c`，定位项目根目录。

### 2. 在 C 文件中添加标记

在需要注入配置的 C 文件中插入标记对：

```c
void App_Init(void) {
    ConfigParam param = {
        /* CONFIG BEGIN */
        /* CONFIG END */
    };
}
```

> **重要**：标记对必须成对出现，且只能出现一对。工具会替换两个标记之间的**全部内容**。

### 3. 编写 YAML 配置文件

在 `conf/` 目录下创建 `.yaml` 文件：

```yaml
config_id: device_a              # 必填：配置标识符（会和文件名一同显示在列表中）
description: 设备 A 的标定参数    # 可选：配置描述

config:                          # 必填：配置数据（任意嵌套）
  sampler:
    vaside:
      adc_channel: BSP_ADC_VA   # 枚举/宏标识符
      k: 0.0072475795            # 浮点数
      b: 0.0216226430
      cutoff_freq: 250.0
    vbside:
      adc_channel: BSP_ADC_VB
      k: 0.0071909501
      b: 0.0158060932
      cutoff_freq: 250.0
  power:
    max_voltage: 28.8
    min_voltage: 6.3
    pid_gains:
      kp: 0.12
      ki: 3.9
      kd: 0.0
```

### 4. 运行工具

#### Linux（Debian / Ubuntu）

**初次使用（只需做一次）：**

```bash
# 1. 创建虚拟环境（项目根目录下）
python3 -m venv .venv

# 2. 安装依赖
.venv/bin/pip install PyQt6 pyyaml darkdetect
```

> 如果 PyPI 官方源下载慢，可以加 `-i https://pypi.tuna.tsinghua.edu.cn/simple` 换清华镜像。

**日常使用：**

```bash
cd <项目根目录>
.venv/bin/python YmaC/yaml_config_builder.py
```

#### Windows

**初次使用（只需做一次）：**

```powershell
pip install PyQt6 pyyaml darkdetect
```

**日常使用：**

```powershell
cd <项目根目录>
python YmaC\yaml_config_builder.py
```

### 5. 操作流程

1. 启动工具 → 自动扫描 `conf/` 并显示所有配置变体
2. 从左侧列表中**选择目标设备**
3. 右侧预览 C 代码 → 确认无误
4. 点击 **"应用选中配置"** → 注入 `User/app/app_main.c`
5. 点击 **"编译"** → 自动调用 CMake 构建

---

## YAML 格式规范

### 顶层字段

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `config_id` | `str` | ✅ | 配置标识符，如 `hero`、`infantry_I` |
| `description` | `str` | 否 | 配置描述，显示在 GUI 详情区 |
| `config` | `dict` | ✅ | 配置数据树，渲染为 C 代码的核心内容 |

### YAML → C 类型映射规则

| YAML 类型 | C 输出 | 示例 |
|-----------|--------|------|
| 全大写字符串 `BSP_ADC_VA` | 裸标识符 | `BSP_ADC_VA` |
| 普通字符串 | C 字符串字面量 | `"hello"` |
| `int` | C 整数 | `42` |
| `float` | C float 字面量 | `0.0072475795f` |
| `float` (负数) | 括号包裹 | `(-0.0676202320f)` |
| `bool` (`true`/`false`) | C99 bool | `true` / `false` |
| `dict` | designated initializer | `.key = { .sub = val }` |
| `list` | C 数组初始化器 | `{ 1, 2, 3 }` |

### 嵌套 dict 渲染规则

```yaml
config:
  vaside:
    adc_channel: BSP_ADC_VA
    k: 0.0072475795
```

渲染为：

```c
.vaside = {
    .adc_channel = BSP_ADC_VA,
    .k = 0.0072475795f,
}
```

顶层 `config:` 的每个 key 展开为一个独立的 C designated initializer 条目。

### 注意事项

1. **浮点精度**：所有 float 渲染为 `.10f` 格式（小数点后 10 位）
2. **枚举/宏检测**：仅当字符串**全大写且只含字母数字下划线**时被识别为 C 标识符，否则加双引号作为字符串处理
3. **文件编码**：YAML 和目标 C 文件均使用 UTF-8
4. **标记唯一性**：每个目标文件中 `/* CONFIG BEGIN */` 和 `/* CONFIG END */` 必须只出现一次

---

## 构建系统支持

工具自动检测以下构建系统：

- **CMake**：检测 `build/*/CMakeCache.txt`

编译前如果检测到当前选中的配置与目标文件中已注入的不一致，会提示是否先应用再编译。

---

## 命令行替代

如果不想使用 GUI，也可以直接用 Python 脚本注入：

```bash
# Linux: 用 .venv/bin/python，Windows: 用 .venv\Scripts\python
.venv/bin/python -c "
from pathlib import Path
import yaml
from tools.yaml_config_builder import render_config_block, inject_config

cfg = yaml.safe_load(open('conf/hero.yaml'))
rendered = render_config_block(cfg['config'])
inject_config(Path('User/app/app_main.c'), rendered, cfg['config_id'])
print('Done')
"
```

---

## scaffold.py — 工程骨架生成工具

> 目录分组表、MANIFEST schema 与工具规格的完整设计见 [docs/debug/build-toolchain-design.md](../docs/debug/build-toolchain-design.md)。
> 依赖仅需 `pyyaml`（不需要 PyQt6）。

`scaffold.py` 与 `yaml_config_builder.py` 互补：后者把参数 YAML 注入 `app_main.c` 的 CONFIG 标记区；前者从工程 YAML 起步，自动解析依赖、生成构建文件与 App 骨架。

### 1. 从工程 YAML 起步

新项目在 `Config/projects/<project>.yaml` 声明式列出需要的子系统：

```yaml
# Config/projects/acdc_sixswitch.yaml
project: acdc_sixswitch
mcu: STM32F334R8
modules:
  - components/pwm
  - components/dsp
  - components/power
  - components/math
  - devices/pwm
  - devices/adc
  - module/power
```

### 2. 三个子命令

```bash
# 校验全部 MANIFEST（结构 + 依赖合法性）
python YmaC/scaffold.py scan

# 查看某子系统传递依赖（拓扑排序 BSP→…→App）
python YmaC/scaffold.py deps components/pwm

# 从工程配置生成骨架 → build/gen/<project>/
python YmaC/scaffold.py gen Config/projects/<project>.yaml
```

### 3. 生成产物（`build/gen/<project>/`）

| 文件 | 内容 |
|------|------|
| `CMakeLists.txt` | 传递闭包内全部 .c 源文件 + 各层 include 路径（扁平 `#include` 无需改动） |
| `<project>_deps.h` | 按层分组的依赖头文件清单 |
| `board_init_stub.c` | 每模块 `// TODO: <文件> 在此实例化` 占位，App 层填入真实初始化 |

### 4. 工作流

1. 创建 `Config/projects/<project>.yaml` 声明需要的子系统
2. 运行 `scaffold.py gen` 生成骨架到 `build/gen/<project>/`
3. 在 `board_init_stub.c` 填入真实初始化（Device init → Module init → 指针注入 → ISR 启动）
4. 参数注入仍走 `yaml_config_builder.py`：手写默认值 → `Config/params/<variant>.yaml` → 注入 → `apply_config()` 同步

---

## YmaC GUI — 使用与验收指南（拓扑选择器）

YmaC 也是**拓扑选择器**：选拓扑 → 一键生成该拓扑的工程骨架（采样 + 环路控制 + PWM 输出集成到一个控制 Module，放入 App 层）→ 离线/在线调参。选完后你只需做两件事：在 App 层接外部 I/O（采样输入 / PWM 输出 / HMI）+ 用 YmaC 调参。

### 1. 启动

```bash
# Windows（项目根目录）
python YmaC\yaml_config_builder.py

# Linux（建议 venv）
.venv/bin/python YmaC/yaml_config_builder.py
```

- **依赖**：`pip install PyQt6 pyyaml darkdetect`；运行时调参（Tab3）另需 `pip install pyserial`（未装则 Tab3 显示提示并禁用控件，不影响 Tab1/Tab2）。
- **工作目录**：从项目根目录或任意子目录启动均可 —— 工具向上搜索 `Config/` + `App/` 定位 C-OOP 工程根。
- 无 GUI 环境时可用 CLI：`python YmaC/yaml_config_builder.py --cli default`（列出 `Config/params` 变体并注入；无物化副本时提示"需先在 GUI 生成工程"）。

### 2. 界面三 Tab 总览

| Tab | 功能 | 关键点 |
|-----|------|--------|
| **Tab1 参数注入** | 既有注入流程（列表选变体 → 预览 → 应用 → 编译） | 发现逻辑**双模式**：legacy（`conf/` + `User/app/`）或 C-OOP（`Config/params/` + 物化 `build/gen/*/app_main.c`） |
| **Tab2 拓扑选择** | 选拓扑 → 生成工程 → 参数表 → 写参 → 注入 → 编译 | **主验收路径**，见下节 |
| **Tab3 运行时调参** | 串口 0xFB 帧下发（复用 `App/pid_tune.h` 协议） | 依赖 `pyserial`；槽位与 Tab2 参数表同源 |

### 3. 验收走查（Tab2 主流程）

> 当前 **buck 是唯一 `ready` 拓扑**（控制模块已实现），其余 9 个是 `planned` 占位。走查以 buck 为例。

1. **启动 GUI** → 默认停在 Tab1。
2. **切到 Tab2「拓扑选择」**。
3. **左侧列表**应显示 10 个拓扑：buck 标"状态: ready"，其余标"(待实现)"。选中 planned 条目时「生成工程」按钮**禁用**，选中 buck 时**可用** —— 这是第一个验收点。
4. **选中 buck** → 右侧显示详情（描述 / 控制模块 `module/power/mod_buck` / 模块数 / 参数数 10）。
5. **填工程名**（如 `my_buck`）+ **MCU**（可编辑下拉框，如 `STM32F334R8`）+ **变体名**（如 `default`）。
6. 点**「生成工程」**。验收点：
   - A. 日志出现 `✓ 工程 [my_buck] 生成完成`；
   - B. `Config/projects/my_buck.yaml` 已生成（由拓扑 `modules:` 合成）；
   - C. `build/gen/my_buck/` 含 `CMakeLists.txt` + `my_buck_deps.h` + `board_init_stub.c` + **物化的 `app_main.c`/`.h`**（模板拷贝，模板本身不被污染）；
   - D. Tab1 顶部"当前注入"更新为指向物化副本。
7. **参数表**：渲染出 10 个 `QDoubleSpinBox`（带 min/max/unit，悬停显示 slot 号）。改几个值（如 vref=13）。
8. 点**「写入参数」** → 生成 `Config/params/my_buck_default.yaml`（dotted key 如 `pid_v.kp` 已展开为嵌套 dict）。
9. 点**「注入 App」** → 日志 `配置 [my_buck_default] 已注入 build/gen/my_buck/app_main.c`。**验收点**：打开该文件，CONFIG 区内应是一个 **`.power = { … }` 嵌套块**，同时含 10 个槽位字段和 **非槽位字段**（`ch_drive`/`duty_min`/`duty_max`/`adc_ch_vout`/`adc_ch_iout`/`adc_ch_vin`），值与你编辑的/默认的一致。
10. 点**「编译」** → 复用现有 CMake（需已装工具链 + `cmake`，workdir 指 `build/gen/my_buck/`）。
11. **回 Tab1 复验**：左侧列表应出现 `my_buck_default` 变体 → 选中 → 右侧预览显示同样的 `.power` 块 → 「应用选中配置」再次注入物化副本。

### 4. Tab1 参数注入（双模式）

- **legacy**：发现 `conf/*.yaml` + `User/app|Application/app_main.c`，行为与旧版一致。
- **C-OOP**：发现 `Config/params/*.yaml`；注入目标**优先取物化的 `build/gen/<name>/app_main.c`**（按 mtime 取最近副本），找不到再退回 legacy 扫描。
- 两套 schema 相同（`config_id`/`description`/`config`），无需改逻辑。

### 5. Tab3 运行时调参（0xFB）

1. `pip install pyserial`（未装则本 Tab 禁用）。
2. 选**串口** + **波特率** → **连接**。
3. 参数表（与 Tab2 同源）编辑 → **「下发」**。
4. 帧格式（48 字节，复用 `App/pid_tune.h`）：HEAD=`0x00`、CMD=`0x14`、`Coef[10]`（`<f` 小端 float，对应 `params.slot` 0–9）、CHECK=`π`、末 2 字节=0。
5. 设备端链路：`App_OnUartRx` → `pid_tune_rx` → 主循环 `pid_tune_respond` → `on_pid_tune_received` → `apply_config()`。
6. **无硬件验收**：串口回环（或 com0com 虚拟串口）→ 设备端应收到合法帧并触发 apply 回调；离线/在线槽位同源，不漂移。

### 6. 验收检查清单

| 步骤 | 预期产物 | 检查点 |
|------|----------|--------|
| Tab2 选拓扑 | buck=ready 可生成；9 个 planned 按钮禁用 | 状态文案 + 按钮可用性 |
| 生成工程 | `Config/projects/<name>.yaml` + `build/gen/<name>/`（CMakeLists + deps.h + stub + 物化 app_main.c） | 4 个目录/文件 + 模板未被污染 |
| 写入参数 | `Config/params/<name>_<variant>.yaml` | dotted key 嵌套展开 |
| 注入 App | 物化 `app_main.c` 的 CONFIG 区 | **`.power = { … }` 嵌套块**含槽位+非槽位全部字段 |
| 编译 | CMake 产物 | 需要工具链，workdir=`build/gen/<name>/` |
| Tab1 复验 | 变体出现在列表；预览与 Tab2 一致；可再次注入 | 双模式发现生效 |
| Tab3 | 0xFB 帧正确拼装/下发 | 回环验收 + 槽位一致 |

### 7. 拓扑目录 `Config/topologies/<topo>.yaml`

每个拓扑一个 YAML（buck / boost / buckboost / forward / flyback / sepic / cuk / zeta / buck2 / vsi_3ph）：

| 字段 | 说明 |
|------|------|
| `status` | `ready`=控制模块已实现可生成工程；`planned`=仅目录占位 |
| `modules` | scaffold 依赖 seed 列表（与 `projects/<name>.yaml` 的 modules 一致） |
| `control_module` | 该拓扑的控制模块（如 `module/power/mod_buck`） |
| `pwm` / `adc` | 建议的 PWM 设备与 ADC 通道角色 |
| `params` | **调参唯一数据源** —— 每项含 key/type/default/min/max/unit/slot/label，同时驱动 GUI 表单、`Config/params` 写入、0xFB 槽位、C 注入四件事，`slot` 保证离线/在线不漂移 |
| `tune` | 0xFB 帧参数（frame_len/check_code/slots） |

> **注入域约定**：C-OOP 注入目标是 `ProjectConfig` 根结构体，拓扑控制域默认挂在 **`.power`** 成员下（如 `.power = { .vref = …, .duty_max = … }`），其余顶层条目会编译失败。可调槽位（`params`）与非槽位字段（`pwm.ch_drive/duty_min/duty_max`、`adc.roles.*.ch`）一并覆盖 —— 否则注入会整体替换模板手写默认值，导致 `duty_max=0` 等运行时损坏。新拓扑的控制模块若挂载到别的 ProjectConfig 成员，改 `_build_config_from_form` 的返回键即可。

### 8. 已知边界

- **仅 buck 可生成工程**；其余 9 个拓扑只建了 catalog 占位，`status: planned`，需后续实现控制模块后改为 `ready`。
- 运行时**遥测回读（0xFC 帧）未实现**，Tab3 目前只下发不回读。
- 设备端 0xFB apply 回调（`on_pid_tune_received`）目前是模板里**写死的 Buck 槽位映射**；新增拓扑时需按 `topology.params` 生成槽位→cfg 映射（后续硬化点）。
