# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 George Saliba <george.saliba@salitronic.com>
"""config.toml for the purchasing workflow: creation, precedence, validation.

Synthetic data only. Every test points EDA_AGENT_EXPORT_DIR at tmp_path, so
nothing is written to a real export folder.
"""

from __future__ import annotations

import tomllib
from decimal import Decimal
from pathlib import Path

import pytest

from eda_agent.export import purchasing_config as pc
from eda_agent.export.purchasing_model import Severity


@pytest.fixture
def folder(tmp_path, monkeypatch):
    monkeypatch.setenv("EDA_AGENT_EXPORT_DIR", str(tmp_path))
    return tmp_path


def _write(folder: Path, text: str) -> Path:
    path = folder / "config.toml"
    path.write_text(text, encoding="utf-8", newline="\n")
    return path


def test_the_template_is_valid_toml_and_holds_the_defaults(folder):
    parsed = tomllib.loads(pc.TEMPLATE)
    assert parsed["upgrade"]["extra_spend_percent"] == "10"
    assert parsed["upgrade"]["extra_spend_flat"] == "5.00"
    assert parsed["upgrade"]["extra_spend_currency"] == "USD"
    assert parsed["upgrade"]["price_break_window"] == 2
    assert "max_quantity_multiplier" not in pc.TEMPLATE
    settings = pc.resolve_settings()
    assert settings.warnings == ()
    for name in pc.SETTING_NAMES:
        assert getattr(settings, name) == pc.DEFAULTS[name], name
        # large_surplus_ratio is commented out in the template
        assert settings.sources[name] == (
            "default" if name == "large_surplus_ratio" else "config")


def test_defaults_are_the_documented_ones():
    d = pc.DEFAULTS
    assert d["assemblies"] == 2
    assert d["overage_factor"] == Decimal("1.10")
    assert d["overage_max_unit_price"] == Decimal("5.00")
    assert d["extra_spend_percent"] == Decimal("10")
    assert d["extra_spend_flat"] == Decimal("5.00")
    assert d["price_break_window"] == 2
    assert d["prefer"] == "digikey"
    assert d["large_surplus_ratio"] is None


# TS-F01
def test_a_missing_file_is_created_once_and_left_alone(folder):
    first = pc.resolve_settings()
    path = folder / "config.toml"
    assert first.config_created is True and path.is_file()
    before = path.read_bytes()
    assert b"\r" not in before
    second = pc.resolve_settings()
    assert second.config_created is False
    assert path.read_bytes() == before


# TS-F02
def test_an_existing_file_is_never_rewritten(folder):
    path = _write(folder, "[assemblies]\ncount = 5\n")
    before = path.read_bytes()
    settings = pc.resolve_settings()
    assert settings.assemblies == 5 and settings.sources["assemblies"] == "config"
    assert path.read_bytes() == before


# TS-F03
def test_the_folder_follows_the_export_variable_and_the_repo_is_refused(
        monkeypatch):
    from eda_agent.export.destination import repo_root
    monkeypatch.setenv("EDA_AGENT_EXPORT_DIR", str(repo_root() / "docs"))
    settings = pc.resolve_settings()
    assert settings.config_path is None
    assert any("not used" in w for w in settings.warnings)
    assert settings.assemblies == 2
    assert not (repo_root() / "docs" / "config.toml").exists()


# TS-F04
def test_argument_beats_file_beats_default_for_each_setting(folder):
    _write(folder, '[assemblies]\ncount = 4\n[upgrade]\nextra_spend_flat = "9"\n')
    s = pc.resolve_settings({"assemblies": 3})
    assert (s.assemblies, s.sources["assemblies"]) == (3, "argument")
    assert (s.extra_spend_flat, s.sources["extra_spend_flat"]) == (
        Decimal("9"), "config")
    assert (s.extra_spend_percent, s.sources["extra_spend_percent"]) == (
        Decimal("10"), "default")


# TS-F12
@pytest.mark.parametrize("name,value,other", [
    ("extra_spend_percent", "25", "extra_spend_flat"),
    ("extra_spend_flat", "0", "extra_spend_percent"),
    ("price_break_window", 0, "extra_spend_percent"),
])
def test_the_allowance_settings_are_independent(folder, name, value, other):
    _write(folder, '[upgrade]\nextra_spend_percent = "7"\nextra_spend_flat = "3"\n'
                   'price_break_window = 4\n')
    base = pc.resolve_settings()
    changed = pc.resolve_settings({name: value})
    assert getattr(changed, name) == pc.parse_setting(name, value)
    assert changed.sources[name] == "argument"
    assert getattr(changed, other) == getattr(base, other)
    assert changed.sources[other] == "config"


def test_zero_is_a_valid_allowance(folder):
    s = pc.resolve_settings({"extra_spend_percent": 0, "extra_spend_flat": "0.00",
                             "price_break_window": 0})
    assert s.extra_spend_percent == 0 and s.extra_spend_flat == 0
    assert s.price_break_window == 0


# TS-Q31 (settings side)
@pytest.mark.parametrize("name,value", [
    ("assemblies", 0), ("assemblies", True), ("assemblies", "3"),
    ("assemblies", 2.5), ("extra_spend_percent", -1),
    ("extra_spend_percent", "ten"), ("extra_spend_flat", -0.01),
    ("extra_spend_flat", True), ("price_break_window", -1),
    ("price_break_window", 1.5), ("price_break_window", True),
    ("overage_factor", "0.9"), ("overage_currency", "DOLLARS"),
    ("prefer", "farnell"), ("large_surplus_ratio", 0),
])
def test_bad_settings_are_refused_before_the_file_is_touched(
        folder, name, value):
    with pytest.raises(pc.SettingError):
        pc.resolve_settings({name: value})
    assert not (folder / "config.toml").exists()


def test_every_problem_is_reported_at_once(folder):
    with pytest.raises(pc.SettingError) as info:
        pc.resolve_settings({"assemblies": 0, "extra_spend_flat": -1})
    assert len(info.value.problems) == 2


# TS-F11
@pytest.mark.parametrize("raw", ['"1.1"', "1.1"])
def test_a_number_and_a_string_read_the_same_exact_value(folder, raw):
    _write(folder, f"[overage]\nfactor = {raw}\n")
    assert pc.resolve_settings().overage_factor == Decimal("1.1")


def test_arguments_read_a_float_by_its_shortest_form():
    assert pc.parse_setting("overage_factor", 1.1) == Decimal("1.1")
    assert pc.parse_setting("extra_spend_percent", 10.0) == Decimal("10.0")
    assert pc.parse_setting("extra_spend_percent", 10) == Decimal("10")


# TS-F05
def test_a_credential_in_the_file_is_refused(folder):
    _write(folder, '[credentials]\napi_key = "abc"\n[upgrade]\n'
                   'extra_spend_flat = "6"\n[distributors]\nmouser_api_key = "x"\n')
    s = pc.resolve_settings()
    assert s.extra_spend_flat == Decimal("6")
    text = " ".join(s.warnings)
    assert "credentials" in text and "mouser_api_key" in text
    assert "abc" not in text


# TS-F06
def test_unknown_keys_are_kept_reported_and_ignored(folder):
    path = _write(folder, '[upgrade]\nfavourite_colour = "red"\n'
                          '[mystery]\nx = 1\nextra_spend_flat = "6"\n'
                          '[assemblies]\ncount = 3\n')
    before = path.read_bytes()
    s = pc.resolve_settings()
    assert s.assemblies == 3
    assert any("favourite_colour" in w for w in s.warnings)
    assert any("mystery" in w for w in s.warnings)
    assert path.read_bytes() == before


# TS-F13
def test_a_removed_multiplier_key_is_unknown_and_ignored(folder):
    _write(folder, '[upgrade]\nmax_quantity_multiplier = "2"\n')
    s = pc.resolve_settings()
    assert any("max_quantity_multiplier" in w for w in s.warnings)
    assert not hasattr(s, "max_quantity_multiplier")


# TS-F07 and TS-F15
def test_severity_overrides_move_only_what_may_move(folder):
    _write(folder, '[review.severity]\nUNRESOLVED_COMPONENT = "info"\n'
                   'LIFECYCLE_EOL = "info"\nLIFECYCLE_OBSOLETE = "review"\n'
                   'DISTRIBUTOR_DISCONTINUED = "info"\nOUT_OF_STOCK = "info"\n'
                   'LIFECYCLE_NRND = "blocking"\nLARGE_SURPLUS = "blocking"\n'
                   'NO_SUCH_CODE = "info"\nMOQ_ADJUSTED = "loud"\n')
    s = pc.resolve_settings()
    assert dict(s.severity) == {"LIFECYCLE_NRND": Severity.BLOCKING}
    joined = " ".join(s.warnings)
    for code in ("UNRESOLVED_COMPONENT", "LIFECYCLE_EOL", "LIFECYCLE_OBSOLETE",
                 "DISTRIBUTOR_DISCONTINUED", "OUT_OF_STOCK", "LARGE_SURPLUS",
                 "NO_SUCH_CODE", "MOQ_ADJUSTED"):
        assert code in joined, code


def test_large_surplus_may_be_raised_to_review(folder):
    _write(folder, '[review.severity]\nLARGE_SURPLUS = "review"\n')
    assert pc.resolve_settings().severity["LARGE_SURPLUS"] is Severity.REVIEW


# TS-F08
def test_invalid_toml_gives_defaults_and_one_warning(folder):
    _write(folder, "[upgrade\nextra_spend_flat = \n")
    s = pc.resolve_settings()
    assert s.extra_spend_flat == Decimal("5.00")
    assert len(s.warnings) == 1 and "not valid TOML" in s.warnings[0]


def test_one_bad_value_falls_back_alone(folder):
    _write(folder, '[upgrade]\nextra_spend_percent = "-5"\nextra_spend_flat = "8"\n')
    s = pc.resolve_settings()
    assert s.extra_spend_percent == Decimal("10")
    assert s.extra_spend_flat == Decimal("8")
    assert any("upgrade.extra_spend_percent" in w for w in s.warnings)


# TS-F09
def test_an_unwritable_folder_does_not_stop_a_read(tmp_path, monkeypatch):
    blocker = tmp_path / "afile"
    blocker.write_text("x", encoding="utf-8")
    monkeypatch.setenv("EDA_AGENT_EXPORT_DIR", str(blocker / "sub"))
    s = pc.resolve_settings()
    assert s.assemblies == 2 and s.config_path is None
    assert any("could not be created" in w for w in s.warnings)


# TS-F10
def test_the_report_gives_value_and_source(folder):
    s = pc.resolve_settings({"price_break_window": 3})
    rep = s.report()
    assert rep["price_break_window"] == {"value": 3, "source": "argument"}
    assert rep["extra_spend_flat"]["value"] == "5.00"
    assert rep["config_file"]["created_now"] is True


# TS-F14
def test_the_setup_block_is_ignored_and_reported_when_live(folder):
    _write(folder, "[setup]\nmin_quantity = { R = 100 }\n")
    s = pc.resolve_settings()
    assert any("[setup]" in w and "not supported" in w for w in s.warnings)
    _write(folder, "# [setup]\n# min_quantity = { R = 100 }\n")
    assert pc.resolve_settings().warnings == ()


def test_the_other_keys_are_validated(folder):
    _write(folder, '[distributors]\nenabled = ["mouser"]\n'
                   '[review]\nstale_pricing_hours = 1\n'
                   '[export]\npurchasing_columns = ["purchase_qty"]\n')
    s = pc.resolve_settings()
    assert s.enabled == ("mouser",)
    assert s.stale_pricing_hours == Decimal(1)
    assert s.purchasing_columns == ("purchase_qty",)
    _write(folder, '[distributors]\nenabled = ["farnell"]\n')
    s = pc.resolve_settings()
    assert s.enabled == ("digikey", "mouser")
    assert any("distributors.enabled" in w for w in s.warnings)


def test_the_policy_carries_the_settings(folder):
    s = pc.resolve_settings({"extra_spend_percent": 25, "price_break_window": 3,
                             "large_surplus_ratio": 10, "assemblies": 7})
    p = s.policy()
    assert (p.assemblies, p.extra_spend_percent, p.price_break_window,
            p.large_surplus_ratio) == (7, Decimal(25), 3, Decimal(10))
    assert p.extra_spend_flat == Decimal("5.00")
