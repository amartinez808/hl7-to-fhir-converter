"""Probabilistic patient linkage helpers."""

from __future__ import annotations

from difflib import SequenceMatcher
from typing import Mapping, Sequence

try:
    import jellyfish
except ImportError:  # pragma: no cover - jellyfish required via requirements.txt
    jellyfish = None  # type: ignore

__all__ = ["match", "is_duplicate"]


def _jw(a: str | None, b: str | None) -> float:
    if not a and not b:
        return 0.0
    if not a or not b:
        return 0.0
    normalized_a = a.strip().lower()
    normalized_b = b.strip().lower()
    if jellyfish is None:
        return SequenceMatcher(None, normalized_a, normalized_b).ratio()
    if hasattr(jellyfish, "jaro_winkler"):
        return jellyfish.jaro_winkler(normalized_a, normalized_b)
    if hasattr(jellyfish, "jaro_winkler_similarity"):
        return jellyfish.jaro_winkler_similarity(normalized_a, normalized_b)
    return SequenceMatcher(None, normalized_a, normalized_b).ratio()


def _pair_score(weight: float, values: Sequence[str | None]) -> tuple[float, float]:
    a, b = values
    score = _jw(a, b)
    return score * weight, weight if (a or b) else 0.0


def match(
    name: Sequence[str | None],
    dob: Sequence[str | None],
    mrn: Sequence[str | None],
    address: Sequence[str | None],
) -> float:
    """Return a 0..1 linkage score for the provided field pairs."""
    weights = {
        "name": 0.4,
        "dob": 0.2,
        "mrn": 0.25,
        "address": 0.15,
    }
    components = [
        _pair_score(weights["name"], name),
        _pair_score(weights["dob"], dob),
        _pair_score(weights["mrn"], mrn),
        _pair_score(weights["address"], address),
    ]
    total_weight = sum(weight for _, weight in components if weight)
    if total_weight == 0:
        return 0.0
    score_sum = sum(score for score, weight in components if weight)
    return round(score_sum / total_weight, 3)


def _name_string(resource: Mapping[str, object]) -> str | None:
    names = resource.get("name")
    if isinstance(names, list) and names:
        name = names[0]
        if isinstance(name, Mapping):
            family = str(name.get("family", "")).strip()
            given = name.get("given")
            given_str = ""
            if isinstance(given, list):
                given_str = " ".join(str(g).strip() for g in given if g)
            elif isinstance(given, str):
                given_str = given.strip()
            full = " ".join(part for part in [given_str, family] if part)
            return full or name.get("text")
    return None


def _mrn(resource: Mapping[str, object]) -> str | None:
    identifiers = resource.get("identifier")
    if isinstance(identifiers, list):
        for ident in identifiers:
            if not isinstance(ident, Mapping):
                continue
            value = ident.get("value")
            if value:
                return str(value)
    return None


def _address_string(resource: Mapping[str, object]) -> str | None:
    addresses = resource.get("address")
    if isinstance(addresses, list) and addresses:
        addr = addresses[0]
        if isinstance(addr, Mapping):
            parts = []
            line = addr.get("line")
            if isinstance(line, list):
                parts.extend(str(v).strip() for v in line if v)
            elif isinstance(line, str):
                parts.append(line.strip())
            for field in ("city", "state", "postalCode"):
                value = addr.get(field)
                if value:
                    parts.append(str(value).strip())
            return ", ".join(p for p in parts if p)
    return None


def is_duplicate(
    a: Mapping[str, object],
    b: Mapping[str, object],
    threshold: float = 0.85,
) -> bool:
    """Return True when two Patient-like dicts look like the same person."""
    name_score = (_name_string(a), _name_string(b))
    dob_score = (str(a.get("birthDate")) if a.get("birthDate") else None, str(b.get("birthDate")) if b.get("birthDate") else None)
    mrn_score = (_mrn(a), _mrn(b))
    address_score = (_address_string(a), _address_string(b))
    score = match(name_score, dob_score, mrn_score, address_score)
    return score >= threshold
