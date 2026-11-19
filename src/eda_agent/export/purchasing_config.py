# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 George Saliba <george.saliba@salitronic.com>
"""Purchasing preferences: ``config.toml`` in the export folder.

The file is created, once, the first time a purchasing tool needs it, in the
folder ``export_dir()`` returns (``EDA_AGENT_EXPORT_DIR``, with its guard
against the public repository). It is created exclusively and is never
rewritten or reformatted afterwards: it belongs to the user.

Precedence for each setting on its own: an explicit tool argument, then the
file, then the built-in default. Credentials stay in environment variables;
the file holds preferences only. A key that looks like a credential is
refused and said so, and no part number, designator or price is accepted.

A broken file never stops a tool. Each bad value falls back to its default
and the reply names the file, the key and the reason. A folder that cannot
be written does not stop a read-only tool either: the defaults are used and
a warning says why.

Numbers are read through ``Decimal(str(value))``, so ``1.1`` is exactly 1.1
whether the file or the caller wrote it as a number or as a string.
"""

from __future__ import annotations

import re
import tomllib
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path
from typing import Any, Mapping, Optional

from .purchasing_model import QuantityPolicy, Severity, to_decimal, to_jsonable
from .purchasing_review import validate_override

__all__ = [
    "CONFIG_NAME",
    "DEFAULTS",
    "SETTING_NAMES",
    "SettingError",
    "Settings",
    "TEMPLATE",
    "ensure_config",
    "load_config",
    "resolve_settings",
    "validate_arguments",
]

CONFIG_NAME = "config.toml"
SCHEMA_VERSION = 1
DISTRIBUTORS = ("digikey", "mouser")
PREFERENCES = ("lowest", "digikey", "mouser")

#: Settings a tool may override by an argument of the same name.
SETTING_NAMES = (
    "assemblies", "overage_factor", "overage_max_unit_price",
    "overage_currency", "extra_spend_percent", "extra_spend_flat",
    "extra_spend_currency", "price_break_window", "large_surplus_ratio",
    "prefer",
)

DEFAULTS: dict[str, Any] = {
    "assemblies": 2,
    "overage_factor": Decimal("1.10"),
    "overage_max_unit_price": Decimal("5.00"),
    "overage_currency": "USD",
    "extra_spend_percent": Decimal("10"),
    "extra_spend_flat": Decimal("5.00"),
    "extra_spend_currency": "USD",
    "price_break_window": 2,
    "large_surplus_ratio": None,
    "prefer": "digikey",
}

#: (table, key) in the file -> the setting it sets.
_FILE_KEYS = {
    ("assemblies", "count"): "assemblies",
    ("overage", "factor"): "overage_factor",
    ("overage", "max_unit_price"): "overage_max_unit_price",
    ("overage", "currency"): "overage_currency",
    ("upgrade", "extra_spend_percent"): "extra_spend_percent",
    ("upgrade", "extra_spend_flat"): "extra_spend_flat",
    ("upgrade", "extra_spend_currency"): "extra_spend_currency",
    ("upgrade", "price_break_window"): "price_break_window",
    ("distributors", "prefer"): "prefer",
    ("review", "large_surplus_ratio"): "large_surplus_ratio",
}
_OTHER_KEYS = {
    ("distributors", "enabled"), ("review", "stale_pricing_hours"),
    ("export", "purchasing_columns"), ("matching", "auto_approve"),
    ("matching", "auto_approve_verified_aliases"),
}
_TABLES = {"assemblies", "overage", "upgrade", "distributors", "review",
           "export", "setup", "matching"}
_SECRET = re.compile(
    r"(api[_-]?key|secret|token|passw|credential|client[_-]?id)",
    re.IGNORECASE)

TEMPLATE = '''\
# EDA Agent purchasing preferences. Created automatically and never
# rewritten by the tool: edit it freely.
# Do not put API keys, passwords or design data here. Credentials stay in
# environment variables.
# Every value below can be overridden for one call by a tool argument of
# the same name as its key.
schema_version = 1

[assemblies]
count = 2

[overage]                      # for inexpensive parts only
factor = "1.10"
max_unit_price = "5.00"        # overage applies at or below this unit price
currency = "USD"

[upgrade]
# A price-break upgrade that costs more than the baseline is allowed when its
# cost is at most:   baseline_cost * (1 + extra_spend_percent / 100) + extra_spend_flat
# The three values are independent. Zero is allowed. Unit price and the
# size of the quantity increase do not matter, only this cost and the
# look-ahead. An upgrade that costs LESS is always taken, anywhere on the
# price ladder.
extra_spend_percent = "10"
extra_spend_flat = "5.00"
extra_spend_currency = "USD"   # the flat amount applies to offers in this currency
price_break_window = 2         # how many feasible breaks above the current one a paid upgrade may reach; 0 turns paid upgrades off

[distributors]
prefer = "digikey"             # digikey or mouser (the other is the fallback), or lowest
enabled = ["digikey", "mouser"]

[review]
stale_pricing_hours = 4
# large_surplus_ratio = "10"   # unset: no warning. Extra parts are reported in
                               # every row either way; this only adds a note
                               # when purchase_qty / required_qty is above it.

[review.severity]              # only codes that are not fixed may be changed
# LIFECYCLE_NRND = "review"
# LARGE_SURPLUS = "review"     # "info" or "review"; never "blocking"

# [setup]                      # FUTURE. Not read yet.
# min_quantity = { R = 100, C = 100 }   # per designator prefix, for feeder setup

[matching]
# Close part number matches that count as approved in EVERY project, with no
# manual approval. Only "formatting" (punctuation differs) and "likely" (a
# confirmed packaging suffix) can be named, and only for a candidate whose
# manufacturer name is identical. Manual approvals and rejections are never
# written here: they are kept per project, outside this file.
auto_approve = []

# Accept a VERIFIED MANUFACTURER ALIAS without asking: two ordering numbers
# that the manufacturer's own document says are the same part (for example a
# KEMET packaging code, TU and 7411). Never a guessed suffix, never another
# maker's part. A project's rejection still wins. false: such an alias is
# listed for the usual per-project approval instead. Absent means false.
auto_approve_verified_aliases = false

[export]
purchasing_columns = ["required_qty", "purchase_qty", "surplus",
                      "part_number", "matched_mpn", "mpn_approval",
                      "mpn_audit", "manufacturer", "description",
                      "reference_designators", "distributor",
                      "distributor_sku", "unit_price", "extended_cost",
                      "stock", "restock_date", "restock_qty",
                      "restock_covered", "lifecycle", "purchasing_status",
                      "review_reasons", "quantity_rule"]
'''


class SettingError(ValueError):
    """A setting that cannot be used. Nothing was read or written."""

    def __init__(self, problems: list[str]):
        super().__init__("; ".join(problems))
        self.problems = problems


# ---- one setting -------------------------------------------------------------

def _whole(name: str, value: Any, minimum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) \
            or value < minimum:
        raise SettingError([
            f"{name} {value!r} is not a whole number, {minimum} or more"])
    return value


def _number(name: str, value: Any, minimum: Decimal) -> Decimal:
    number = to_decimal(value)
    if number is None or number < minimum:
        raise SettingError([
            f"{name} {value!r} is not a number of {minimum} or more"])
    return number


def parse_setting(name: str, value: Any) -> Any:
    """``value`` as the setting ``name``, or ``SettingError``."""
    if name == "assemblies":
        return _whole(name, value, 1)
    if name == "price_break_window":
        return _whole(name, value, 0)
    if name in ("overage_factor",):
        return _number(name, value, Decimal(1))
    if name in ("overage_max_unit_price", "extra_spend_percent",
                "extra_spend_flat"):
        return _number(name, value, Decimal(0))
    if name == "large_surplus_ratio":
        return _number(name, value, Decimal(1))
    if name in ("overage_currency", "extra_spend_currency"):
        if not isinstance(value, str) or not re.fullmatch(
                r"[A-Za-z]{3}", value.strip()):
            raise SettingError([
                f"{name} {value!r} is not a 3 letter currency code"])
        return value.strip().upper()
    if name == "prefer":
        text = str(value or "").strip().lower() \
            if isinstance(value, str) else ""
        if text not in PREFERENCES:
            raise SettingError([
                f"prefer {value!r} is not one of " + ", ".join(PREFERENCES)])
        return text
    raise SettingError([f"unknown setting {name!r}"])


def validate_arguments(arguments: Optional[Mapping[str, Any]]
                       ) -> dict[str, Any]:
    """The explicit arguments, validated, with unset ones left out. Every
    problem is reported at once. Pure: nothing is read or written."""
    parsed: dict[str, Any] = {}
    problems: list[str] = []
    for name, value in (arguments or {}).items():
        if value is None:
            continue
        try:
            parsed[name] = parse_setting(name, value)
        except SettingError as exc:
            problems.extend(exc.problems)
    if problems:
        raise SettingError(problems)
    return parsed


# ---- the file --------------------------------------------------------------------

def ensure_config(create: bool = True
                  ) -> tuple[Optional[Path], bool, list[str]]:
    """The path of ``config.toml``, whether this call created it, and
    warnings. Created exclusively with the template, never overwritten.
    Returns no path when the export folder cannot be used."""
    from .destination import ExportPathError, export_dir

    try:
        folder = export_dir()
    except ExportPathError as exc:
        return None, False, [f"{CONFIG_NAME} not used, defaults apply: {exc}"]
    path = folder / CONFIG_NAME
    if path.exists() or not create:
        return path, False, []
    try:
        folder.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        return None, False, [
            f"{CONFIG_NAME} could not be created in {folder} ({exc}); "
            "defaults apply"]
    try:
        with open(path, "x", encoding="utf-8", newline="\n") as handle:
            handle.write(TEMPLATE)
    except FileExistsError:
        return path, False, []
    except OSError as exc:
        return None, False, [
            f"{CONFIG_NAME} could not be created in {folder} ({exc}); "
            "defaults apply"]
    return path, True, []


@dataclass
class _File:
    values: dict[str, Any] = field(default_factory=dict)
    enabled: Optional[tuple] = None
    stale_hours: Optional[Decimal] = None
    auto_approve: Optional[tuple] = None
    auto_aliases: Optional[bool] = None
    columns: Optional[tuple] = None
    severity: dict[str, Severity] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)


def _walk_secrets(node: Any, trail: str, found: list[str]) -> None:
    if isinstance(node, dict):
        for key, value in node.items():
            here = f"{trail}.{key}" if trail else str(key)
            if _SECRET.search(str(key)):
                found.append(here)
            else:
                _walk_secrets(value, here, found)


def load_config(path: Path) -> _File:
    """What the file says, validated. Never raises for the file's content."""
    out = _File()
    name = path.name
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except tomllib.TOMLDecodeError as exc:
        out.warnings.append(
            f"{name} is not valid TOML ({exc}); every default applies")
        return out
    except (OSError, UnicodeDecodeError) as exc:
        out.warnings.append(
            f"{name} could not be read ({exc}); every default applies")
        return out

    secrets: list[str] = []
    _walk_secrets(data, "", secrets)
    for where in secrets:
        out.warnings.append(
            f"{name}: {where} looks like a credential and is refused. "
            "Credentials belong in environment variables, never in this file")
    refused = {s.split(".")[0] for s in secrets}

    version = data.get("schema_version")
    if version is not None and version != SCHEMA_VERSION:
        out.warnings.append(
            f"{name}: schema_version {version!r}, this version reads "
            f"{SCHEMA_VERSION}; known keys are still read")
    for table, body in data.items():
        if table == "schema_version" or table in refused:
            continue
        if table not in _TABLES:
            out.warnings.append(
                f"{name}: unknown key or table {table!r} is kept and ignored")
            continue
        if table == "setup":
            out.warnings.append(
                f"{name}: [setup] (feeder and setup quantities) is not "
                "supported yet and is ignored")
            continue
        if not isinstance(body, dict):
            out.warnings.append(f"{name}: {table!r} must be a table")
            continue
        for key, value in body.items():
            where = f"{table}.{key}"
            if _SECRET.search(key):
                continue                       # already reported above
            if (table, key) == ("review", "severity") or where == "review.severity":
                continue
            if (table, key) in _FILE_KEYS:
                setting = _FILE_KEYS[(table, key)]
                try:
                    out.values[setting] = parse_setting(setting, value)
                except SettingError as exc:
                    out.warnings.append(
                        f"{name}: {where}: {exc}; the default applies")
            elif (table, key) in _OTHER_KEYS:
                _read_other(out, name, table, key, value)
            elif where != "review.severity":
                out.warnings.append(
                    f"{name}: unknown key {where!r} is kept and ignored")
    severity = (data.get("review") or {}).get("severity") \
        if isinstance(data.get("review"), dict) else None
    if isinstance(severity, dict):
        for code, level in severity.items():
            if _SECRET.search(str(code)):
                continue
            chosen, why = validate_override(str(code), level)
            if chosen is None:
                out.warnings.append(f"{name}: [review.severity] {why}")
            else:
                out.severity[str(code)] = chosen
    elif severity is not None:
        out.warnings.append(f"{name}: review.severity must be a table")
    return out


def _read_other(out: _File, name: str, table: str, key: str,
                value: Any) -> None:
    where = f"{table}.{key}"
    if where == "distributors.enabled":
        if isinstance(value, list) and value and all(
                isinstance(v, str) and v.strip().lower() in DISTRIBUTORS
                for v in value):
            out.enabled = tuple(dict.fromkeys(
                v.strip().lower() for v in value))
        else:
            out.warnings.append(
                f"{name}: {where} must list digikey and/or mouser; the "
                "default applies")
    elif where == "review.stale_pricing_hours":
        number = to_decimal(value)
        if number is None or number < 0:
            out.warnings.append(
                f"{name}: {where} {value!r} is not a number of 0 or more; "
                "the default applies")
        else:
            out.stale_hours = number
    elif where == "matching.auto_approve":
        from .purchasing_decisions import GLOBAL_RULES

        if not isinstance(value, list) or not all(
                isinstance(v, str) for v in value):
            out.warnings.append(
                f"{name}: {where} must be a list of rule names; none applies")
            return
        chosen = []
        for rule in value:
            key = rule.strip().lower()
            if key in GLOBAL_RULES:
                chosen.append(key)
            else:
                out.warnings.append(
                    f"{name}: {where} names {rule!r}, which is refused: only "
                    + " and ".join(sorted(GLOBAL_RULES)) + " can be "
                    "authorized for every project. A possible or uncertain "
                    "match needs a person")
        out.auto_approve = tuple(dict.fromkeys(chosen))
    elif where == "matching.auto_approve_verified_aliases":
        if isinstance(value, bool):
            out.auto_aliases = value
        else:
            out.warnings.append(
                f"{name}: {where} must be true or false, not {value!r}; "
                "false applies (verified aliases need a person's approval)")
    elif where == "export.purchasing_columns":
        if isinstance(value, list) and value and all(
                isinstance(v, str) for v in value):
            out.columns = tuple(value)
        else:
            out.warnings.append(
                f"{name}: {where} must be a non-empty list of column "
                "identifiers; the default applies")


# ---- the settings a call uses ---------------------------------------------------------

@dataclass(frozen=True)
class Settings:
    assemblies: int
    overage_factor: Decimal
    overage_max_unit_price: Decimal
    overage_currency: str
    extra_spend_percent: Decimal
    extra_spend_flat: Decimal
    extra_spend_currency: str
    price_break_window: int
    large_surplus_ratio: Optional[Decimal]
    prefer: str
    enabled: tuple = DISTRIBUTORS
    stale_pricing_hours: Decimal = Decimal(4)
    purchasing_columns: Optional[tuple] = None
    matching_auto_approve: tuple = ()
    matching_auto_approve_verified_aliases: bool = False
    severity: Mapping[str, Severity] = field(default_factory=dict)
    sources: Mapping[str, str] = field(default_factory=dict)
    warnings: tuple = ()
    config_path: Optional[str] = None
    config_created: bool = False

    def policy(self) -> QuantityPolicy:
        return QuantityPolicy(
            assemblies=self.assemblies,
            overage_factor=self.overage_factor,
            overage_max_unit_price=self.overage_max_unit_price,
            overage_currency=self.overage_currency,
            extra_spend_percent=self.extra_spend_percent,
            extra_spend_flat=self.extra_spend_flat,
            extra_spend_currency=self.extra_spend_currency,
            price_break_window=self.price_break_window,
            large_surplus_ratio=self.large_surplus_ratio)

    def report(self) -> dict[str, Any]:
        """Each setting's effective value and where it came from."""
        out = {name: {"value": to_jsonable(getattr(self, name)),
                      "source": self.sources.get(name, "default")}
               for name in SETTING_NAMES}
        out["auto_approve_verified_aliases"] = {
            "value": self.matching_auto_approve_verified_aliases,
            "source": self.sources.get(
                "matching_auto_approve_verified_aliases", "default")}
        out["config_file"] = {"path": self.config_path,
                              "created_now": self.config_created}
        return out


def resolve_settings(arguments: Optional[Mapping[str, Any]] = None,
                     create: bool = True) -> Settings:
    """The settings for one call: arguments, then the file, then defaults.

    Raises ``SettingError`` for a bad argument, before the file is touched.
    """
    given = validate_arguments(arguments)
    path, created, warnings = ensure_config(create)
    file = load_config(path) if path is not None and path.exists() \
        else _File()
    warnings = [*warnings, *file.warnings]

    values: dict[str, Any] = {}
    sources: dict[str, str] = {}
    for name in SETTING_NAMES:
        if name in given:
            values[name], sources[name] = given[name], "argument"
        elif name in file.values:
            values[name], sources[name] = file.values[name], "config"
        else:
            values[name], sources[name] = DEFAULTS[name], "default"
    sources["matching_auto_approve_verified_aliases"] = (
        "config" if file.auto_aliases is not None else "default")
    return Settings(
        **values,
        enabled=file.enabled or DISTRIBUTORS,
        stale_pricing_hours=file.stale_hours
        if file.stale_hours is not None else Decimal(4),
        purchasing_columns=file.columns,
        matching_auto_approve=file.auto_approve or (),
        matching_auto_approve_verified_aliases=bool(file.auto_aliases),
        severity=dict(file.severity),
        sources=sources,
        warnings=tuple(warnings),
        config_path=str(path) if path is not None else None,
        config_created=created)
