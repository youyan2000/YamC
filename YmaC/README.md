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