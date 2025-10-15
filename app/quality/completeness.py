"""Simple completeness scoring for FHIR resources."""

from __future__ import annotations

from typing import Iterable, Mapping


CompletenessRules = Mapping[str, Iterable[Iterable[str]]]

_RULES: CompletenessRules = {
    "Patient": [["identifier"], ["name"], ["gender"], ["birthDate"]],
    "Encounter": [["status"], ["class", "class_fhir"], ["subject"], ["actualPeriod", "period"]],
    "Observation": [
        ["status"],
        ["code"],
        ["subject"],
        ["effectiveDateTime", "effectivePeriod"],
        ["valueQuantity", "valueCodeableConcept", "valueString"],
    ],
    "DiagnosticReport": [["status"], ["code"], ["result"], ["subject"]],
    "MedicationRequest": [["status"], ["intent"], ["medication", "medicationCodeableConcept"], ["subject"]],
}


def _has_value(value) -> bool:
    if value is None:
        return False
    if isinstance(value, (list, dict, set, tuple)):
        return bool(value)
    if isinstance(value, str):
        return value.strip() != ""
    return True


def completeness_score(resource_type: str, resource: Mapping[str, object] | None) -> float:
    """Return a 0..1 completeness score based on key presence."""
    if not resource:
        return 0.0
    groups = _RULES.get(resource_type, [[key] for key in resource.keys()])
    if not groups:
        return 1.0
    hits = 0
    total = len(groups)
    for group in groups:
        if any(_has_value(resource.get(field)) for field in group):
            hits += 1
    return round(hits / total, 3)


__all__ = ["completeness_score"]
