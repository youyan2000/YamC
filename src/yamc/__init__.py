"""yamc — HardC 的配置与接入工具链（YAML → C 注入 + 拓扑生成 + 外部工程接入 + 双固件/OTA 编排）。

包结构（src 内扁平，本目录即包根，见 pyproject.toml 的 package-dir 映射）：

  cli.py             伞命令 `yamc <tool> <action>` 与全部 `yamc_*` 入口
  engine.py          六步接入流水线编排（cfg_run）
  gen_app.py         拓扑 + 外设 → app_main.c/h 生成
  ioc_parse.py       STM32 .ioc → 外设 YAML
  c2000_syscfg.py    TI main.syscfg → C2000 外设 YAML
  cmake_integrate.py CMakeLists.txt 幂等注入 HardC 块
  project_probe.py   平台/工程根探测
  params.py          静态调参纯逻辑（变体发现/拍平/渲染/注入/状态检测）
  build.py           构建系统检测与编译命令
  serial_tune.py     动态调参 0xFB 帧构造与串口下发
  topo.py            拓扑选择（list/show）
  version.py         版本自检（对标 PackageInfo）
  cubemx_generate.py stm32 工程自动生成（对标 xr_cubemx_generate）
  ccs_generate.py    c2000 工程自动生成
"""

__version__ = "0.2.0"