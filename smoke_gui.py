"""offscreen smoke test — 驱动真实 GUI 外部工程接入全链 (Area B 走查).

不点击, 用 QTimer 在事件循环内驱动 _ConfigBuilderWindow:
  设置工程根 → 探测 → 选拓扑 → 运行完整接入 (no_build) → 等 done → 断言.
验证: 窗口可构造 (Tab1/Tab2/Tab3 无回归) + 外部接入 end-to-end ok.
"""
import os
import sys
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, str(Path(__file__).parent))

from PyQt6.QtCore import QTimer
from PyQt6.QtWidgets import QApplication

import yaml_config_builder as ycb

results: dict = {}


def _driver(win) -> None:
    try:
        # 1. 工程根 + 探测
        win._edit_ext_root.setText(r"f:/My_Projects/_ymac_test/software_clean")
        win._on_ext_probe()
        results["probe"] = win._lbl_ext_probe.text()
        assert "平台" in results["probe"], f"探测失败: {results['probe']}"

        # 2. 选拓扑 (load + 选中 buck)
        win._load_topologies()
        assert win._topologies, "无拓扑"
        for i, t in enumerate(win._topologies):
            if str(t.get("name")) == "buck":
                win._on_topo_selected(i)
                break
        assert str(win._current_topo.get("name")) == "buck", "未选中 buck"
        results["topo"] = str(win._current_topo.get("name"))
        results["param_rows"] = win._param_form.rowCount()
        assert results["param_rows"] > 0, "参数表单为空"

        # 3. 拦截 done → 记录并退出
        def _fake_done(ok: bool, reason: str) -> None:
            results["done"] = (ok, reason)
            QApplication.instance().quit()

        win._on_ext_done = _fake_done
        win._chk_ext_no_build.setChecked(True)
        win._chk_ext_no_sub.setChecked(True)
        win._btn_ext_run.click()  # 走真实 click 路径
    except Exception as exc:  # noqa: BLE001
        results["error"] = f"{type(exc).__name__}: {exc}"
        QApplication.instance().quit()


_ORIG_EXEC = QApplication.exec


def _patched_exec(self) -> int:
    QTimer.singleShot(600, lambda: _seed())
    QTimer.singleShot(90_000, lambda: (results.update(error="timeout"), QApplication.instance().quit()))
    return _ORIG_EXEC()


QApplication.exec = _patched_exec

_seeded = {}


def _seed() -> None:
    app = QApplication.instance()
    for w in app.topLevelWidgets():
        if hasattr(w, "_edit_ext_root"):
            _driver(w)
            return
    results["error"] = "未找到主窗口"
    QApplication.instance().quit()


ycb._build_gui()

if "error" in results:
    print("SMOKE FAIL:", results)
    sys.exit(1)
if "done" not in results:
    print("SMOKE FAIL: 无 done", results)
    sys.exit(1)
ok, reason = results["done"]
print(f"SMOKE {'PASS' if ok else 'FAIL'}")
for k, v in results.items():
    print(f"  {k}: {v}")
if not ok:
    sys.exit(1)
