"""Terminology helpers backed by lightweight YAML mapping tables."""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml

RULES_DIR = Path(__file__).resolve().parent.parent / "mapping" / "rules"


@lru_cache(maxsize=None)
def _load_table(name: str) -> dict[str, Any]:
    path = RULES_DIR / f"{name}.yaml"
    if not path.exists():
        return {}
    with path.open(encoding="utf-8") as handle:
        data = yaml.safe_load(handle) or {}
        if not isinstance(data, dict):
            return {}
        # normalise keys to strings for consistent lookups
        return {str(k): v for k, v in data.items()}


def map_code(table: str, code: str | None) -> dict[str, Any] | None:
    """Return a terminology mapping dict for the given table and code."""
    if not code:
        return None
    code_str = str(code).strip()
    table_data = _load_table(table)
    # Case-sensitive lookup first, then uppercase fallback
    value = table_data.get(code_str) or table_data.get(code_str.upper())
    if value is None:
        return None
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        if table.startswith("local_to_"):
            target_table = table.removeprefix("local_to_")
            return map_code(target_table, value)
        return map_code(table, value) if value != code_str else None
    return None


__all__ = ["map_code"]
