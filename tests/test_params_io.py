"""静态调参闭环 golden: scan/flatten/unflatten/render/inject/detect 往返一致。"""

from __future__ import annotations

from pathlib import Path

from yamc import params as P


def make_workspace(tmp: Path) -> Path:
    """构造 HardC 布局临时工程（Config/ + App/ + 物化 build/gen/demo/app_main.c）。"""
    root = tmp / "proj"
    (root / "Config" / "params").mkdir(parents=True)
    (root / "App").mkdir(exist_ok=True)
    root.joinpath("Config/params/default.yaml").write_text(
        "config_id: default\ndescription: 默认变体\nconfig:\n"
        "  vref: 12.0\n  pid_v:\n    kp: 2.0\n    ki: 0.1\n  mode: BUCK\n",
        encoding="utf-8",
    )
    gen = root / "build" / "gen" / "demo"
    gen.mkdir(parents=True)
    (gen / "app_main.c").write_text(
        "/* CONFIG BEGIN */\n/* config: none */\n/* CONFIG END */\n",
        encoding="utf-8",
    )
    return root


def test_discover_params() -> None:
    import tempfile

    with tempfile.TemporaryDirectory() as td:
        root = make_workspace(Path(td))
        ws = P.discover_params(root)
        assert ws["ok"] is True
        assert ws["layout"] == "hardc"
        assert len(ws["configs"]) == 1
        assert ws["configs"][0]["config_id"] == "default"
        assert ws["target_file"] is not None
        assert ws["current_id"] == "none"


def test_flatten_unflatten_roundtrip() -> None:
    cfg = {"vref": 12.0, "pid_v": {"kp": 2.0, "ki": 0.1}, "mode": "BUCK"}
    flat = P.flatten_config_tree(cfg)
    assert ("pid_v.kp", 2.0) in flat
    restored = P.unflatten_config_tree(flat)
    assert restored == cfg


def test_render_config_block() -> None:
    cfg = {"vref": 12.0, "mode": "BUCK", "enabled": True}
    block = P.render_config_block(cfg)
    assert ".vref = 12.0000000000f" in block
    assert ".mode = BUCK" in block
    assert ".enabled = true" in block


def test_inject_detect_roundtrip() -> None:
    import tempfile

    with tempfile.TemporaryDirectory() as td:
        root = make_workspace(Path(td))
        target = P.find_target_file(root)
        assert target is not None
        ok = P.inject_config(target, ".vref = 12.0000000000f", "default")
        assert ok is True
        assert P.detect_current_config(target) == "default"
        # 幂等: CONFIG 块仍唯一
        text = target.read_text(encoding="utf-8")
        assert text.count(P.BEGIN_MARKER) == 1