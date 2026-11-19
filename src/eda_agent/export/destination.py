# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 George Saliba <george.saliba@salitronic.com>
"""Where proprietary exports are written, and which paths may receive them.

BOMs, purchasing plans, CSV and Excel exports describe a company's designs
and suppliers, so they must never land inside the public repository.

``EDA_AGENT_EXPORT_DIR`` names the folder that receives them by default.
When it is unset they go to ``EDA Agent/exports`` under the user's home
folder. Either way the folder must be absolute and outside the repository,
and any explicit output path that resolves inside the repository is refused.

An export is never overwritten. A free file name is used unchanged; one that
exists gets the local time before its extension, ``name_20261117_153000.csv``,
and then ``_2``, ``_3`` and so on. Files are created exclusively, so a file
that appears between choosing the name and writing is not replaced either.
"""

from __future__ import annotations

import os
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Iterator, Optional

__all__ = [
    "EXPORT_DIR_ENV",
    "ExportPathError",
    "claimed",
    "export_dir",
    "export_file",
    "export_files",
    "repo_root",
    "validated_path",
    "working_file",
]

EXPORT_DIR_ENV = "EDA_AGENT_EXPORT_DIR"
_DEFAULT_PARTS = ("EDA Agent", "exports")


class ExportPathError(ValueError):
    """An export destination that must not be written."""


def repo_root() -> Optional[Path]:
    """The public repository this package is checked out in, if any."""
    here = Path(__file__).resolve()
    for folder in here.parents:
        if (folder / ".git").exists() or (folder / "pyproject.toml").is_file():
            return folder
    return None


def validated_path(path: "str | os.PathLike[str]") -> Path:
    """Return ``path`` unchanged when it may hold an export, else raise.

    The check resolves ``..`` and symlinks first, so a traversal such as
    ``repo/sub/../x.csv`` is judged by where it really lands.
    """
    if not str(path).strip():
        raise ExportPathError("the export path is empty")
    resolved = Path(path).expanduser().resolve()
    root = repo_root()
    if root is not None and (resolved == root or root in resolved.parents):
        raise ExportPathError(
            f"{resolved} is inside the public repository {root}. Proprietary "
            f"exports go to {EXPORT_DIR_ENV} (or another folder outside the "
            "repository); nothing was written.")
    return Path(path)


def export_dir() -> Path:
    """The folder for proprietary exports, validated as outside the repo."""
    raw = os.environ.get(EXPORT_DIR_ENV, "").strip()
    if not raw:
        return validated_path(Path.home().joinpath(*_DEFAULT_PARTS))
    configured = Path(raw)
    if not configured.is_absolute():
        raise ExportPathError(
            f"{EXPORT_DIR_ENV} must be an absolute path, got {raw!r}")
    return validated_path(configured)


def _now() -> datetime:
    """The local time a collision is stamped with (tests replace this)."""
    return datetime.now()


def _suffixed(name: str, tag: str) -> str:
    stem, ext = os.path.splitext(name)
    return f"{stem}_{tag}{ext}"


def _attempts(names: list[str], stamp: str) -> Iterator[list[str]]:
    """The names to try, in order: as asked, then stamped, then numbered.

    Every file of a set gets the same suffix, so a BOM and its CPL always
    carry the same stamp and number.
    """
    yield list(names)
    yield [_suffixed(name, stamp) for name in names]
    for n in range(2, 10_000):
        yield [_suffixed(name, f"{stamp}_{n}") for name in names]


def _claim(folder: Path, names: list[str]) -> list[Path]:
    """Create ``names`` in ``folder`` exclusively, under the first free set.

    The files are created empty with mode ``x``, which fails rather than
    replace a file that exists, even one that appeared a moment ago, so
    the caller then owns them and may write into them.
    """
    stamp = _now().strftime("%Y%m%d_%H%M%S")
    for attempt in _attempts(names, stamp):
        paths = [validated_path(folder / name) for name in attempt]
        made: list[Path] = []
        try:
            for path in paths:
                with open(path, "xb"):
                    pass
                made.append(path)
        except FileExistsError:
            for path in made:
                path.unlink(missing_ok=True)
            continue
        return made
    raise ExportPathError(f"no free file name was found in {folder}")


def export_files(explicit_folder: str, category: str,
                 names: list[str]) -> list[Path]:
    """Claim destinations for files written together, e.g. BOM and CPL.

    The folder is ``explicit_folder`` as given, or ``export_dir()/category``.
    It is checked against the repository before anything is made, then
    created when missing. A name that is free is used unchanged. If any of
    the set exists, all of it gets ``_YYYYMMDD_HHMMSS`` (local time) before
    the extension, and ``_2``, ``_3`` and so on after that while those
    exist too. Nothing is ever overwritten.

    The returned files exist and are empty: write into them (see
    ``claimed``).
    """
    folder = validated_path(explicit_folder) if explicit_folder         else export_dir() / category
    validated_path(folder / names[0])
    folder.mkdir(parents=True, exist_ok=True)
    return _claim(folder, list(names))


def export_file(explicit_file: str, category: str, name: str) -> Path:
    """Claim the destination of one file: the explicit path, or the
    configured folder. Collisions are handled as in ``export_files``."""
    if not explicit_file:
        return export_files("", category, [name])[0]
    target = validated_path(explicit_file)
    if target.is_dir():
        raise ExportPathError(
            f"{target} is a folder; name a file, or leave the path empty. "
            "Nothing was written.")
    return export_files(str(target.parent), category, [target.name])[0]


@contextmanager
def claimed(paths: list[Path]) -> Iterator[list[Path]]:
    """Write into claimed files; if the write fails, remove them.

    Without this a failed write would leave an empty or partial export
    that later looks like a real one.
    """
    try:
        yield paths
    except BaseException:
        for path in paths:
            path.unlink(missing_ok=True)
        raise


def working_file(category: str, name: str, create: bool = True) -> Path:
    """A file the workflow keeps and rewrites in place, such as a journal.

    It lives under ``category`` in the export folder and passes the same
    guard against the public repository. It is not an export: the
    never-overwrite rule does not apply, and the caller replaces it
    atomically. ``category`` and ``name`` are plain names, never paths.
    The folder is created unless ``create`` is false (a read).
    """
    for part in (*str(category).replace("\\", "/").split("/"), name):
        if not part or part in (".", "..") or any(
                c not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ"
                         "0123456789._-" for c in part):
            raise ExportPathError(
                f"{part!r} is not a plain folder or file name")
    folder = export_dir().joinpath(*str(category).replace("\\", "/").split("/"))
    target = validated_path(folder / name)
    if create:
        target.parent.mkdir(parents=True, exist_ok=True)
    return target
