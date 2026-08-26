import os, sys
from pathlib import Path
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
from PyQt6.QtCore import QTimer
from PyQt6.QtWidgets import QApplication
from yamc import yaml_config_builder as ycb

results = {}

def main_window():
    for w in QApplication.topLevelWidgets():
        if type(w).__name__ == "_ConfigBuilderWindow":
            return w
    return None

def _check():
    win = main_window()
    if win is None:
        results["error"] = "no main window"
        QApplication.quit()
        return
    try:
        tabs = [win._tabs.tabText(i) for i in range(win._tabs.count())]
        assert len(tabs) >= 4, tabs
        assert hasattr(win, "_sp_tab_proj"), "missing project QSplitter"
        assert hasattr(win, "_btn_gen") and not hasattr(win, "_btn_proj_gen"), "gen-button placement wrong"
        assert win._radio_ext_auto.isChecked(), "auto mode should be default"
        from yamc import engine as E
        assert win._edit_ext_hardc.text() == E.DEFAULT_HARDC_GIT_SOURCE, "default URL"
        results["ok"] = True
    except Exception as exc:
        results["error"] = f"{type(exc).__name__}: {exc}"
    finally:
        QApplication.quit()

_orig_exec = QApplication.exec
def _patched_exec(self):
    QTimer.singleShot(1500, _check)
    QTimer.singleShot(30000, lambda: (results.update(error="timeout"), QApplication.quit()))
    return _orig_exec()
QApplication.exec = _patched_exec

ycb._build_gui()

if results.get("ok"):
    print("GUI STRUCTURE OK")
    sys.exit(0)
print("GUI STRUCTURE FAIL:", results)
sys.exit(1)