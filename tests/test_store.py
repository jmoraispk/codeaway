import press_store


def test_config_roundtrip(tmp_path, monkeypatch):
    templates_dir = tmp_path / "templates"
    config_path = templates_dir / "config.json"
    monkeypatch.setattr(press_store, "TEMPLATES_DIR", templates_dir)
    monkeypatch.setattr(press_store, "CONFIG_PATH", config_path)

    cfg = press_store.default_config()
    cfg["interval_seconds"] = 15
    rule = press_store.default_rule("ContinueButton")
    rule["template_path"] = "rule_a.png"
    rule["search_region"] = [10, 20, 300, 200]
    rule["action"] = press_store.ACTION_CLICK_TYPE_ENTER
    rule["text"] = "continue"
    cfg["rules"].append(rule)

    press_store.save_config(cfg)
    loaded = press_store.load_config()

    assert loaded["interval_seconds"] == 15.0
    assert len(loaded["rules"]) == 1
    assert loaded["rules"][0]["name"] == "ContinueButton"
    assert loaded["rules"][0]["template_path"] == "rule_a.png"
    assert loaded["rules"][0]["search_region"] == [10, 20, 300, 200]
    assert loaded["rules"][0]["action"] == press_store.ACTION_CLICK_TYPE_ENTER


def test_relativize_template_path_prefers_templates_relative(tmp_path, monkeypatch):
    templates_dir = tmp_path / "templates"
    monkeypatch.setattr(press_store, "TEMPLATES_DIR", templates_dir)
    path = templates_dir / "rule_x.png"
    assert press_store.relativize_template_path(path) == "rule_x.png"


def test_normalize_config_reassigns_priorities():
    cfg = {
        "interval_seconds": 5,
        "rules": [
            {"name": "A", "priority": 9},
            {"name": "B", "priority": 99},
        ],
    }
    normalized = press_store.normalize_config(cfg)
    assert [rule["priority"] for rule in normalized["rules"]] == [1, 2]


def test_serialize_template_path_keeps_absolute_outside_templates(tmp_path, monkeypatch):
    templates_dir = tmp_path / "templates"
    outside = tmp_path / "elsewhere" / "sample.png"
    outside.parent.mkdir(parents=True)
    outside.write_bytes(b"fake")
    monkeypatch.setattr(press_store, "TEMPLATES_DIR", templates_dir)
    stored = press_store.serialize_template_path(outside)
    assert stored == str(outside.resolve())


def test_list_template_files_only_returns_images(tmp_path, monkeypatch):
    templates_dir = tmp_path / "templates"
    templates_dir.mkdir(parents=True)
    (templates_dir / "one.png").write_bytes(b"fake")
    (templates_dir / "two.jpg").write_bytes(b"fake")
    (templates_dir / "notes.txt").write_text("ignore", encoding="utf-8")
    monkeypatch.setattr(press_store, "TEMPLATES_DIR", templates_dir)
    assert press_store.list_template_files() == ["one.png", "two.jpg"]


# ---- DPI metadata + variant generation ------------------------------------


def test_normalize_dpi_snaps_to_supported_preset():
    """A measured scale near a Windows preset snaps onto it so the
    variant lookup doesn't miss by a rounding hair."""
    assert press_store._normalize_dpi(1.501) == 1.5
    assert press_store._normalize_dpi(1.0) == 1.0
    assert press_store._normalize_dpi(2.0) == 2.0
    # Wildly custom scale survives unchanged.
    assert press_store._normalize_dpi(3.0) == 3.0
    # Garbage → None.
    assert press_store._normalize_dpi("abc") is None
    assert press_store._normalize_dpi(None) is None
    assert press_store._normalize_dpi(-1.0) is None


def test_dpi_variant_path_naming():
    from pathlib import Path

    base = Path("templates/cursor_idle.png")
    assert press_store.dpi_variant_path(base, 1.0).name == "cursor_idle.dpi100.png"
    assert press_store.dpi_variant_path(base, 1.5).name == "cursor_idle.dpi150.png"
    assert press_store.dpi_variant_path(base, 2.0).name == "cursor_idle.dpi200.png"


def test_bridge_config_round_trips_source_dpi(tmp_path, monkeypatch):
    """Schema normalizer must preserve idle/askuser source_dpi
    fields across save/load."""
    monkeypatch.setattr(press_store, "TEMPLATES_DIR", tmp_path)
    monkeypatch.setattr(press_store, "CONFIG_PATH", tmp_path / "config.json")

    cfg = press_store.default_config()
    cfg["bridge"]["idle_template_path"] = "x.png"
    cfg["bridge"]["idle_template_source_dpi"] = 1.5
    cfg["bridge"]["askuser_template_source_dpi"] = 2.0
    press_store.save_config(cfg)
    loaded = press_store.load_config()
    assert loaded["bridge"]["idle_template_source_dpi"] == 1.5
    assert loaded["bridge"]["askuser_template_source_dpi"] == 2.0


def test_write_template_with_dpi_variants_creates_all_files(tmp_path):
    """Capturing at 1.5x should produce the base PNG + one scaled
    variant per supported preset (100/125/150/175/200)."""
    import numpy as np

    base = tmp_path / "icon.png"
    # 32x32 source image at 150% source scale.
    rgb = np.full((32, 32, 3), 200, dtype=np.uint8)
    written = press_store.write_template_with_dpi_variants(base, rgb, 1.5)
    assert base.exists()
    expected_variants = {
        tmp_path / "icon.dpi100.png",
        tmp_path / "icon.dpi125.png",
        tmp_path / "icon.dpi150.png",
        tmp_path / "icon.dpi175.png",
        tmp_path / "icon.dpi200.png",
    }
    assert {p for p in written if p != base} == expected_variants
    for p in expected_variants:
        assert p.exists(), f"missing variant: {p.name}"

    # The 150% variant should match source dimensions; 100% should be ⅔
    # and 200% should be 4/3.
    from PIL import Image

    assert Image.open(tmp_path / "icon.dpi150.png").size == (32, 32)
    w100, h100 = Image.open(tmp_path / "icon.dpi100.png").size
    assert (w100, h100) == (round(32 * 1.0 / 1.5), round(32 * 1.0 / 1.5))
    w200, h200 = Image.open(tmp_path / "icon.dpi200.png").size
    assert (w200, h200) == (round(32 * 2.0 / 1.5), round(32 * 2.0 / 1.5))
