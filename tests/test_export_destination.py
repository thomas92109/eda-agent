# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 George Saliba <george.saliba@salitronic.com>
"""Proprietary exports go to EDA_AGENT_EXPORT_DIR, never into the repository.

The public repository must not receive a BOM, a CPL or a netlist, whether
the path came from the environment or was typed in explicitly. Nothing
here writes outside pytest's tmp_path.
"""

from __future__ import annotations

import asyncio
from datetime import datetime
from pathlib import Path

import pytest

from eda_agent.export import destination as dest
from eda_agent.export.destination import (
    EXPORT_DIR_ENV,
    ExportPathError,
    claimed,
    export_dir,
    export_file,
    export_files,
    repo_root,
    validated_path,
)
from eda_agent.tools import project


@pytest.fixture
def repo() -> Path:
    root = repo_root()
    assert root is not None, "tests run from a checkout with a .git or pyproject.toml"
    return root


@pytest.fixture
def home(tmp_path, monkeypatch) -> Path:
    """A fake home folder, so the default location is predictable."""
    fake = tmp_path / "home"
    fake.mkdir()
    monkeypatch.setenv("HOME", str(fake))
    monkeypatch.setenv("USERPROFILE", str(fake))
    return fake


def test_unset_default_is_under_the_home_folder_not_the_repo(home, repo, monkeypatch):
    monkeypatch.delenv(EXPORT_DIR_ENV, raising=False)
    assert export_dir() == home / "EDA Agent" / "exports"
    assert repo not in export_dir().parents and export_dir() != repo


def test_configured_folder_is_the_destination(tmp_path, monkeypatch):
    folder = tmp_path / "exports"
    monkeypatch.setenv(EXPORT_DIR_ENV, str(folder))
    assert export_dir() == folder
    assert export_file("", "bom", "board_BOM.csv") == folder / "bom" / "board_BOM.csv"


def test_relative_configured_folder_is_refused(monkeypatch):
    monkeypatch.setenv(EXPORT_DIR_ENV, "exports")
    with pytest.raises(ExportPathError, match="absolute"):
        export_dir()


def test_configured_folder_inside_the_repo_is_refused(repo, monkeypatch):
    monkeypatch.setenv(EXPORT_DIR_ENV, str(repo / "exports"))
    with pytest.raises(ExportPathError, match="inside the public repository"):
        export_dir()


def test_explicit_path_inside_the_repo_is_refused(repo):
    with pytest.raises(ExportPathError):
        validated_path(repo / "bom.csv")
    with pytest.raises(ExportPathError):
        validated_path(repo / "docs" / "bom.csv")


def test_traversal_into_the_repo_is_refused(repo):
    traversal = repo / "docs" / ".." / "bom.csv"
    with pytest.raises(ExportPathError):
        validated_path(traversal)
    with pytest.raises(ExportPathError):
        export_file(str(traversal), "bom", "unused.csv")


def test_repo_root_itself_is_refused(repo):
    with pytest.raises(ExportPathError):
        validated_path(repo)


def test_explicit_path_outside_the_repo_is_kept_as_given(tmp_path, repo):
    given = tmp_path / "custom" / "bom.csv"
    assert validated_path(given) == given
    assert export_file(str(given), "bom", "unused.csv") == given


def test_sibling_with_the_repo_name_as_prefix_is_outside(repo):
    sibling = repo.parent / (repo.name + "-exports") / "bom.csv"
    assert validated_path(sibling) == sibling


STAMP = "20261117_153000"


@pytest.fixture
def clock(monkeypatch):
    """A fixed local time, so a collision's name can be asserted."""
    monkeypatch.setattr(dest, "_now", lambda: datetime(2026, 11, 17, 15, 30, 0))


def _write(path: Path, text: str = "old") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def test_a_free_name_is_used_unchanged_and_created(tmp_path, monkeypatch, clock):
    folder = tmp_path / "exports"
    monkeypatch.setenv(EXPORT_DIR_ENV, str(folder))
    out = export_file("", "bom", "board.csv")
    assert out == folder / "bom" / "board.csv"
    assert out.is_file() and out.read_bytes() == b""


def test_a_taken_name_gets_the_local_time_before_the_extension(
        tmp_path, monkeypatch, clock):
    folder = tmp_path / "exports"
    monkeypatch.setenv(EXPORT_DIR_ENV, str(folder))
    _write(folder / "bom" / "board.csv")
    out = export_file("", "bom", "board.csv")
    assert out.name == f"board_{STAMP}.csv"
    assert (folder / "bom" / "board.csv").read_text(encoding="utf-8") == "old"


def test_a_taken_timestamped_name_is_numbered_from_2(tmp_path, monkeypatch, clock):
    folder = tmp_path / "exports"
    monkeypatch.setenv(EXPORT_DIR_ENV, str(folder))
    _write(folder / "bom" / "board.csv")
    _write(folder / "bom" / f"board_{STAMP}.csv")
    assert export_file("", "bom", "board.csv").name == f"board_{STAMP}_2.csv"
    assert export_file("", "bom", "board.csv").name == f"board_{STAMP}_3.csv"
    assert export_file("", "bom", "board.csv").name == f"board_{STAMP}_4.csv"


def test_the_stamp_is_year_month_day_underscore_hour_minute_second(
        tmp_path, monkeypatch):
    monkeypatch.setattr(dest, "_now", lambda: datetime(2027, 1, 2, 3, 4, 5))
    _write(tmp_path / "a.csv")
    assert export_file(str(tmp_path / "a.csv"), "bom", "x").name         == "a_20270102_030405.csv"


def test_an_explicit_file_that_exists_is_never_replaced(tmp_path, clock):
    target = _write(tmp_path / "mine" / "bom.csv", "keep me")
    out = export_file(str(target), "bom", "unused.csv")
    assert out == tmp_path / "mine" / f"bom_{STAMP}.csv"
    assert target.read_text(encoding="utf-8") == "keep me"
    again = export_file(str(target), "bom", "unused.csv")
    assert again.name == f"bom_{STAMP}_2.csv"


def test_an_explicit_file_that_is_free_is_used_as_given(tmp_path, clock):
    target = tmp_path / "new" / "deeper" / "bom.csv"
    assert export_file(str(target), "bom", "unused.csv") == target
    assert target.is_file()


def test_a_folder_is_not_a_file_name(tmp_path):
    with pytest.raises(ExportPathError, match="is a folder"):
        export_file(str(tmp_path), "bom", "unused.csv")


def test_a_name_with_no_extension_or_dots_is_stamped_at_the_end(tmp_path, clock):
    _write(tmp_path / "netlist")
    _write(tmp_path / "v1.2.csv")
    assert export_file(str(tmp_path / "netlist"), "n", "x").name         == f"netlist_{STAMP}"
    assert export_file(str(tmp_path / "v1.2.csv"), "n", "x").name         == f"v1.2_{STAMP}.csv"


def test_a_pair_shares_one_suffix_when_only_one_of_them_exists(
        tmp_path, monkeypatch, clock):
    folder = tmp_path / "exports"
    monkeypatch.setenv(EXPORT_DIR_ENV, str(folder))
    _write(folder / "bom" / "b_CPL.csv")
    bom, cpl = export_files("", "bom", ["b_BOM.csv", "b_CPL.csv"])
    assert bom.name == f"b_BOM_{STAMP}.csv" and cpl.name == f"b_CPL_{STAMP}.csv"
    assert not (folder / "bom" / "b_BOM.csv").exists(), "no half-pair left behind"


def test_a_pair_is_numbered_together_too(tmp_path, clock):
    _write(tmp_path / "b_BOM.csv")
    _write(tmp_path / f"b_CPL_{STAMP}.csv")
    bom, cpl = export_files(str(tmp_path), "bom", ["b_BOM.csv", "b_CPL.csv"])
    assert (bom.name, cpl.name) == (f"b_BOM_{STAMP}_2.csv", f"b_CPL_{STAMP}_2.csv")


def test_a_file_that_appears_while_choosing_is_not_replaced(
        tmp_path, clock, monkeypatch):
    """Another process creates the free-looking name just before this one
    does. Exclusive creation notices, and takes the next name."""
    racer = tmp_path / "bom.csv"
    real = dest.validated_path
    seen = {"racer": 0}

    def validating(path):
        checked = real(path)
        if Path(path) == racer:
            seen["racer"] += 1
            # The third look at this name is the claim's own, just before
            # it creates the file.
            if seen["racer"] == 3:
                _write(racer, "the other process")
        return checked

    monkeypatch.setattr(dest, "validated_path", validating)
    out = export_file(str(racer), "bom", "unused.csv")
    assert out.name == f"bom_{STAMP}.csv"
    assert racer.read_text(encoding="utf-8") == "the other process"


def test_claimed_removes_the_files_when_the_write_fails(tmp_path):
    made = export_file(str(tmp_path / "x.csv"), "bom", "unused.csv")
    assert made.exists()
    with pytest.raises(RuntimeError):
        with claimed([made]):
            made.write_text("partial", encoding="utf-8")
            raise RuntimeError("disk full")
    assert not made.exists()


def test_the_repo_is_checked_before_anything_is_created(repo):
    folder = repo / "exports"
    for call in (lambda: export_file(str(folder / "a.csv"), "bom", "x"),
                 lambda: export_files(str(folder), "bom", ["a.csv", "b.csv"])):
        with pytest.raises(ExportPathError):
            call()
    assert not folder.exists()


class _Bridge:
    """Just enough of the Altium bridge for the JLC assembly export."""

    async def send_command_async(self, command, params=None, timeout=None):
        if command == "application.get_open_documents":
            return {"documents": [{"file_path": r"C:\work\Lamp.PcbDoc"}]}
        if command == "pcb.focus_board":
            return {"success": True}
        if command == "pcb.export_coordinates":
            return {"board_name": "Lamp.PcbDoc", "placements": [
                {"designator": "R1", "footprint": "R0402", "comment": "10k",
                 "x": 5100, "y": 5000, "rotation": 0.0, "side": "Top"}]}
        if command == "project.get_board_info":
            return {"origin_x": 5000, "origin_y": 5000}
        if command == "project.get_component_info_batch":
            return {"components": [
                {"designator": "R1", "parameters": {"LCSC": "C25804"}}]}
        raise AssertionError(command)


def _jlc_tool(monkeypatch):
    monkeypatch.setattr(project, "get_bridge", lambda: _Bridge())
    tools = {}

    class _Mcp:
        def tool(self, *a, **k):
            def deco(fn):
                tools[fn.__name__] = fn
                return fn
            return deco

    project.register_project_tools(_Mcp())
    return tools["proj_export_jlc_assembly"]


def test_jlc_default_writes_to_the_configured_folder(
        tmp_path, monkeypatch, clock):
    folder = tmp_path / "exports"
    monkeypatch.setenv(EXPORT_DIR_ENV, str(folder))
    run = _jlc_tool(monkeypatch)
    first = asyncio.run(run())
    assert first["ok"], first
    assert Path(first["bom_path"]).parent == folder / "bom"
    assert Path(first["bom_path"]).is_file() and Path(first["cpl_path"]).is_file()
    assert Path(first["bom_path"]).name == "Lamp_BOM_JLCPCB.csv"
    assert Path(first["cpl_path"]).name == "Lamp_CPL_JLCPCB.csv"
    first_bytes = Path(first["bom_path"]).read_bytes()
    assert first_bytes, "the first export was written, not left empty"
    second = asyncio.run(run())
    assert second["ok"], second
    assert Path(second["bom_path"]).name == f"Lamp_BOM_JLCPCB_{STAMP}.csv"
    assert Path(second["cpl_path"]).name == f"Lamp_CPL_JLCPCB_{STAMP}.csv"
    assert Path(first["bom_path"]).read_bytes() == first_bytes, "first survives"
    third = asyncio.run(run())
    assert Path(third["bom_path"]).name == f"Lamp_BOM_JLCPCB_{STAMP}_2.csv"
    assert Path(third["cpl_path"]).name == f"Lamp_CPL_JLCPCB_{STAMP}_2.csv"


def test_jlc_explicit_folder_gets_one_suffix_when_only_one_file_exists(
        tmp_path, monkeypatch, clock):
    _write(tmp_path / "Lamp_CPL_JLCPCB.csv", "someone's CPL")
    out = asyncio.run(_jlc_tool(monkeypatch)(str(tmp_path)))
    assert out["ok"], out
    assert Path(out["bom_path"]).name == f"Lamp_BOM_JLCPCB_{STAMP}.csv"
    assert Path(out["cpl_path"]).name == f"Lamp_CPL_JLCPCB_{STAMP}.csv"
    assert not (tmp_path / "Lamp_BOM_JLCPCB.csv").exists()
    assert (tmp_path / "Lamp_CPL_JLCPCB.csv").read_text(encoding="utf-8")         == "someone's CPL"


def test_jlc_explicit_folder_in_the_repo_is_refused_and_nothing_written(
        repo, monkeypatch):
    run = _jlc_tool(monkeypatch)
    out = asyncio.run(run(str(repo / "exports")))
    assert not out["ok"] and "inside the public repository" in out["reason"]
    assert not (repo / "exports").exists()


class _OtherBridge:
    """The two reads the netlist and variant matrix exports make."""

    async def send_command_async(self, command, params=None, **kwargs):
        if command == "project.get_nets":
            return {"pins": [
                {"component": "R1", "pin": "1", "pin_name": "1", "net": "N1"},
                {"component": "R2", "pin": "1", "pin_name": "1", "net": "N1"}]}
        if command == "project.get_variant_matrix":
            return {"variants": ["Base"], "component_count": 1,
                    "rows": [{"designator": "R1", "cells": ["Fitted"]}]}
        raise AssertionError(command)


def _tools(monkeypatch):
    monkeypatch.setattr(project, "get_bridge", lambda: _OtherBridge())
    tools = {}

    class _Mcp:
        def tool(self, *a, **k):
            def deco(fn):
                tools[fn.__name__] = fn
                return fn
            return deco

    project.register_project_tools(_Mcp())
    return tools


def test_netlist_and_variant_matrix_use_the_same_naming(
        tmp_path, monkeypatch, clock):
    monkeypatch.setenv(EXPORT_DIR_ENV, str(tmp_path / "exports"))
    tools = _tools(monkeypatch)
    for tool, kwargs, folder, name in (
            ("proj_export_netlist", {"net_format": "tabular"}, "netlist",
             "netlist"),
            ("proj_export_variant_matrix_csv", {}, "variant-matrix",
             "variant_matrix")):
        run = tools[tool]
        first = asyncio.run(run(**kwargs))
        assert first["success"], first
        assert Path(first["output_path"])             == tmp_path / "exports" / folder / f"{name}.csv"
        old = Path(first["output_path"]).read_bytes()
        assert old, "written, not left empty"
        second = asyncio.run(run(**kwargs))
        assert Path(second["output_path"]).name == f"{name}_{STAMP}.csv"
        third = asyncio.run(run(**kwargs))
        assert Path(third["output_path"]).name == f"{name}_{STAMP}_2.csv"
        assert Path(first["output_path"]).read_bytes() == old


def test_an_explicit_existing_netlist_or_matrix_path_is_not_replaced(
        tmp_path, monkeypatch, clock):
    tools = _tools(monkeypatch)
    for tool, kwargs in (("proj_export_netlist", {"net_format": "tabular"}),
                         ("proj_export_variant_matrix_csv", {})):
        target = _write(tmp_path / tool / "out.csv", "keep me")
        out = asyncio.run(tools[tool](output_path=str(target), **kwargs))
        assert out["success"], out
        assert Path(out["output_path"]).name == f"out_{STAMP}.csv"
        assert target.read_text(encoding="utf-8") == "keep me"


def test_a_repo_path_is_still_refused_by_every_export(repo, monkeypatch):
    tools = _tools(monkeypatch)
    for tool, kwargs in (("proj_export_netlist", {"net_format": "tabular"}),
                         ("proj_export_variant_matrix_csv", {})):
        out = asyncio.run(tools[tool](
            output_path=str(repo / "docs" / ".." / "x.csv"), **kwargs))
        assert not out["success"] and "public repository" in out["error"]
    assert not (repo / "x.csv").exists()


def test_module_has_no_hardcoded_machine_path():
    text = Path(dest.__file__).read_text(encoding="utf-8")
    assert "C:\\" not in text and "C:/" not in text
