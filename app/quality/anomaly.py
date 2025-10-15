"""Lightweight anomaly detection for converted FHIR resources."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Mapping

__all__ = ["detect_anomalies"]


def _as_datetime(value) -> datetime | None:
    if isinstance(value, datetime):
        return value
    if isinstance(value, str):
        candidate = value.strip()
        if not candidate:
            return None
        if candidate.endswith("Z"):
            candidate = candidate[:-1] + "+00:00"
        try:
            return datetime.fromisoformat(candidate)
        except ValueError:
            return None
    return None


def _is_future(dt: datetime | None) -> bool:
    if not dt:
        return False
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt > datetime.now(timezone.utc)


def detect_anomalies(resource_type: str, resource: Mapping[str, object] | None) -> list[str]:
    """Return a list of human-readable anomaly descriptions."""
    if not resource:
        return []

    findings: list[str] = []
    if resource_type == "Encounter":
        period = resource.get("period") or resource.get("actualPeriod") or {}
        start = _as_datetime(period.get("start"))
        end = _as_datetime(period.get("end"))
        if start and end and start > end:
            findings.append("Encounter period start occurs after end.")
        if _is_future(start):
            findings.append("Encounter period start is in the future.")
        if _is_future(end):
            findings.append("Encounter period end is in the future.")
    elif resource_type == "Observation":
        if not resource.get("code"):
            findings.append("Observation is missing a code.")
        if not resource.get("subject"):
            findings.append("Observation is missing a subject reference.")
        if _is_future(_as_datetime(resource.get("effectiveDateTime"))):
            findings.append("Observation effectiveDateTime is in the future.")
    elif resource_type == "Patient":
        birth = resource.get("birthDate")
        birth_dt = _as_datetime(birth)
        if _is_future(birth_dt):
            findings.append("Patient birthDate is in the future.")
    return findings
