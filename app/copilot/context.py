"""Structured diagnostics for HL7 → FHIR conversions."""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Iterable, Mapping

from hl7apy.core import Segment
from hl7apy.parser import parse_message

from app.quality.anomaly import detect_anomalies
from app.quality.completeness import completeness_score

try:  # Prefer the canonical normalizer from the converter module when available
    from hl7_to_fhir_miniconverter import normalize_hl7 as _normalize_hl7
except Exception:  # pragma: no cover - fallback for environments without the module
    def _normalize_hl7(text: str) -> str:
        return text.replace("\r\n", "\n").replace("\n", "\r").strip("\r\n")


@dataclass(slots=True)
class ResourceInsight:
    """Summarised view of a single FHIR resource."""

    resource_type: str
    identifier: str | None
    summary: Mapping[str, Any]
    completeness: float | None
    anomalies: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)


@dataclass(slots=True)
class ConversionContext:
    """Diagnostics captured for a conversion attempt."""

    success: bool
    message_type: str | None
    trigger_event: str | None
    timestamp: datetime
    duration_seconds: float | None
    segment_counts: Mapping[str, int]
    missing_segments: list[str]
    warnings: list[str]
    errors: list[str]
    resources: list[ResourceInsight]
    normalized_hl7: str


_RECOMMENDED_SEGMENTS: dict[str, set[str]] = {
    "ADT": {"MSH", "PID", "PV1"},
    "ORU": {"MSH", "PID", "OBR", "OBX"},
    "RDE": {"MSH", "PID", "RXE", "RXO"},
}


def _iter_segments(node) -> Iterable[Segment]:
    for child in getattr(node, "children", []):
        if isinstance(child, Segment):
            yield child
        else:
            yield from _iter_segments(child)


def _collect_resource_insights(resources: list[Mapping[str, Any]]) -> list[ResourceInsight]:
    insights: list[ResourceInsight] = []
    for resource in resources:
        rtype = str(resource.get("resourceType", "Unknown"))
        identifier = str(resource.get("id")) if resource.get("id") else None
        summary: dict[str, Any] = {}
        notes: list[str] = []

        if rtype == "Patient":
            names = resource.get("name") or []
            if isinstance(names, list) and names:
                preferred = names[0]
                summary["name"] = " ".join(preferred.get("given", [])) + " " + preferred.get("family", "")
            identifiers = resource.get("identifier") or []
            if identifiers:
                summary["identifier_system"] = identifiers[0].get("system")
                summary["identifier_value"] = identifiers[0].get("value")
        elif rtype == "Observation":
            code = resource.get("code", {})
            coding = code.get("coding", []) if isinstance(code, Mapping) else []
            if coding:
                preferred = coding[0]
                summary["code"] = preferred.get("code")
                summary["system"] = preferred.get("system")
                summary["display"] = preferred.get("display")
                has_loinc = any((c.get("system") or "").lower() == "http://loinc.org" for c in coding if isinstance(c, Mapping))
                if not has_loinc:
                    notes.append("Observation code not mapped to LOINC; review normalization table.")
            value = resource.get("valueQuantity") or resource.get("valueString")
            if isinstance(value, Mapping):
                summary["value"] = value.get("value")
                summary["unit"] = value.get("unit")
        elif rtype == "DiagnosticReport":
            summary["category"] = (resource.get("code") or {}).get("text")
            if not resource.get("result"):
                notes.append("DiagnosticReport has no Observation references.")
        elif rtype == "Encounter":
            period = resource.get("period") or resource.get("actualPeriod") or {}
            summary["start"] = period.get("start")
            summary["end"] = period.get("end")
        elif rtype == "MedicationRequest":
            med_code = (resource.get("medicationCodeableConcept") or {}).get("coding", [])
            if med_code:
                summary["medication"] = med_code[0].get("display") or med_code[0].get("code")

        completeness: float | None = None
        try:
            completeness = completeness_score(rtype, resource)
        except Exception:
            completeness = None

        anomalies: list[str] = []
        try:
            anomalies = detect_anomalies(rtype, resource)
        except Exception:
            anomalies = []

        insights.append(
            ResourceInsight(
                resource_type=rtype,
                identifier=identifier,
                summary={k: v for k, v in summary.items() if v},
                completeness=completeness,
                anomalies=anomalies,
                notes=notes,
            )
        )
    return insights


def _extract_resources(conversion_result: Any) -> list[Mapping[str, Any]]:
    if conversion_result is None:
        return []
    if hasattr(conversion_result, "dict"):
        try:
            converted = conversion_result.dict(exclude_none=True)
        except Exception:  # pragma: no cover - fallback for pydantic v1 objects
            converted = conversion_result.dict()
    elif hasattr(conversion_result, "model_dump"):
        try:
            converted = conversion_result.model_dump(exclude_none=True)
        except Exception:
            converted = conversion_result.model_dump()
    else:
        converted = conversion_result

    if isinstance(converted, Mapping):
        entries = converted.get("entry")
        if isinstance(entries, list):
            resources: list[Mapping[str, Any]] = []
            for entry in entries:
                resource = entry.get("resource") if isinstance(entry, Mapping) else None
                if isinstance(resource, Mapping):
                    resources.append(resource)
            if resources:
                return resources
        if "resourceType" in converted:
            return [converted]
    if isinstance(converted, list):
        if converted and isinstance(converted[0], Mapping):
            return [item for item in converted if isinstance(item, Mapping)]
    return []


def build_context(
    hl7_text: str,
    conversion_result: Any | None,
    *,
    duration_seconds: float | None = None,
    error: Exception | None = None,
) -> ConversionContext:
    """Create a ConversionContext from the raw HL7 text and converter output."""

    normalized = _normalize_hl7(hl7_text)
    segment_counts: Counter[str] = Counter()
    message_type: str | None = None
    trigger_event: str | None = None
    warnings: list[str] = []
    errors: list[str] = []

    try:
        message = parse_message(normalized)
        msh_segment: Segment | None = None
        for segment in _iter_segments(message):
            segment_counts.update([segment.name])
            if segment.name == "MSH" and msh_segment is None:
                msh_segment = segment
        if msh_segment is not None:
            fields = msh_segment.to_er7().split("|")
            if len(fields) > 8:
                components = (fields[8] or "").split("^")
                message_type = components[0] or None
                trigger_event = components[1] if len(components) > 1 else None
    except Exception as exc:  # noqa: BLE001
        errors.append(f"Unable to parse HL7 message: {exc}")

    expected = _RECOMMENDED_SEGMENTS.get(message_type or "", set())
    missing = sorted(s for s in expected if segment_counts.get(s, 0) == 0)
    if missing:
        warnings.append("Missing recommended segments: " + ", ".join(missing))

    resources = _extract_resources(conversion_result)
    insights = _collect_resource_insights(resources)

    if message_type == "ORU" and segment_counts.get("OBX", 0) > len([i for i in insights if i.resource_type == "Observation"]):
        warnings.append("Some OBX segments did not map to Observations; review unsupported value types.")

    if error is not None:
        errors.append(str(error))

    return ConversionContext(
        success=error is None,
        message_type=message_type,
        trigger_event=trigger_event,
        timestamp=datetime.utcnow(),
        duration_seconds=duration_seconds,
        segment_counts=dict(segment_counts),
        missing_segments=missing,
        warnings=warnings,
        errors=errors,
        resources=insights,
        normalized_hl7=normalized,
    )
