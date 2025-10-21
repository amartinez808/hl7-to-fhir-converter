# -*- coding: utf-8 -*-
"""
Streamlit interface for the HL7 -> FHIR mini-converter.

Run with:
    streamlit run streamlit_app.py
"""

from __future__ import annotations

import base64
import html
import json
import logging
import os
import sys
import time
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal
from enum import Enum
from pathlib import Path
from typing import Any, Iterable, Optional
from functools import lru_cache

import streamlit as st

from hl7apy.core import Segment
from hl7apy.parser import parse_message

from app.copilot import ConversionCopilot, ConversionContext
from app.adapters.fhir_client import FHIRClient
from app.orchestration.workflows.referral_intake import build_referral_intake_workflow
from app.quality.anomaly import detect_anomalies
from app.quality.completeness import completeness_score
from app.security.audit import audit_event
from app.security.deidentify import mask_patient

logging.basicConfig(
    level=logging.INFO,
    handlers=[logging.StreamHandler(sys.stdout)],
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)

try:
    import hl7_to_fhir_miniconverter as _converter_module
except Exception as converter_exc:  # noqa: BLE001
    raise RuntimeError("Unable to import hl7_to_fhir_miniconverter module") from converter_exc

try:
    _json_ready = getattr(_converter_module, "_json_ready")
except AttributeError:
    def _json_ready(obj: Any):  # type: ignore[override]
        if hasattr(obj, "dict"):
            return obj.dict()
        if isinstance(obj, (datetime, date)):
            return obj.isoformat()
        if isinstance(obj, Decimal):
            return float(obj)
        if isinstance(obj, Enum):
            return obj.value
        if isinstance(obj, (bytes, bytearray)):
            return obj.decode("utf-8", errors="ignore")
        if isinstance(obj, list):
            return [_json_ready(item) for item in obj]
        if isinstance(obj, dict):
            return {k: _json_ready(v) for k, v in obj.items()}
        return str(obj)

_CONVERT_FN = None
for name in ("convert_hl7_to_fhir", "convert_hl7_to_fhir_bundle", "convert_to_fhir_bundle"):
    candidate = getattr(_converter_module, name, None)
    if callable(candidate):
        _CONVERT_FN = candidate
        break
if _CONVERT_FN is None:
    fallback_candidate = getattr(_converter_module, "convert", None)
    if callable(fallback_candidate):
        _CONVERT_FN = fallback_candidate
if _CONVERT_FN is None:
    raise RuntimeError("No known converter function found in hl7_to_fhir_miniconverter")

_NORMALIZE_HL7 = getattr(
    _converter_module,
    "normalize_hl7",
    lambda text: text.replace("\r\n", "\n").replace("\n", "\r").strip("\r\n"),
)


def _normalize_result(obj: Any) -> Any:
    if hasattr(obj, "dict"):
        try:
            return obj.dict(exclude_none=True)
        except TypeError:
            return obj.dict()
    if hasattr(obj, "model_dump"):
        try:
            return obj.model_dump(exclude_none=True)
        except TypeError:
            return obj.model_dump()
    if isinstance(obj, (bytes, bytearray)):
        obj = obj.decode("utf-8", errors="ignore")
    if isinstance(obj, str):
        try:
            return json.loads(obj)
        except json.JSONDecodeError:
            return obj
    return obj


def _pairs_from_result(obj: Any) -> list[tuple[str, dict]]:
    data = _normalize_result(obj)
    if isinstance(data, dict):
        if "entry" in data:
            pairs: list[tuple[str, dict]] = []
            for entry in data.get("entry", []):
                resource = entry.get("resource")
                if isinstance(resource, dict):
                    pairs.append((resource.get("resourceType", "Unknown"), resource))
            return pairs
        if "resourceType" in data:
            return [(data.get("resourceType", "Unknown"), data)]
    if isinstance(data, list):
        if data and isinstance(data[0], tuple):
            return [(rtype, res) for rtype, res in data if isinstance(res, dict)]
        if data and isinstance(data[0], dict):
            return [(item.get("resourceType", "Unknown"), item) for item in data]
    return []


def _ndjson(pairs: list[tuple[str, dict]]) -> str:
    return "\n".join(json.dumps(resource, default=_json_ready, ensure_ascii=False) for _, resource in pairs)


def _rerun_app():
    if hasattr(st, "rerun"):
        st.rerun()
    elif hasattr(st, "experimental_rerun"):
        st.experimental_rerun()


def _button(container, label: str, **kwargs) -> bool:
    try:
        return container.button(label, **kwargs)
    except TypeError:
        kwargs.pop("type", None)
        return container.button(label, **kwargs)


def _dataframe(data, **kwargs):
    try:
        return st.dataframe(data, **kwargs)
    except TypeError:
        kwargs.pop("use_container_width", None)
        return st.dataframe(data, **kwargs)


def _new_copilot_agent(seed_text: str | None) -> ConversionCopilot:
    text = seed_text or ""
    if text.strip():
        return ConversionCopilot.from_payload(text, None)
    placeholder = ConversionContext(
        success=True,
        message_type=None,
        trigger_event=None,
        timestamp=datetime.utcnow(),
        duration_seconds=None,
        segment_counts={},
        missing_segments=[],
        warnings=["Load an HL7 message to analyze and I'll break it down."],
        errors=[],
        resources=[],
        normalized_hl7="",
    )
    return ConversionCopilot(placeholder)


def _init_copilot_if_missing(seed_text: str | None) -> None:
    if "copilot_agent" not in st.session_state:
        st.session_state["copilot_agent"] = _new_copilot_agent(seed_text)


def _reset_copilot(seed_text: str | None) -> None:
    st.session_state["copilot_agent"] = _new_copilot_agent(seed_text)
    st.session_state.pop("copilot_prompt", None)


@dataclass(frozen=True)
class SampleDefinition:
    label: str
    hl7_path: Path
    description: str
    message_type: str
    trigger_event: str
    fhir_path: Optional[Path] = None


SUPPORTED_MESSAGES: dict[str, dict[str, Any]] = {
    "ADT^A01": {
        "summary": "Inpatient admissions with demographics, allergies, and encounter context.",
        "segments": ["MSH", "EVN", "PID", "PD1", "NK1", "AL1", "PV1", "DG1"],
        "tips": [
            "Ensure PID-3 has a medical record number.",
            "PV1-2 (patient class) is required to determine encounter type.",
            "Include AL1 segments to carry forward allergies."
        ],
    },
    "ORU^R01": {
        "summary": "Laboratory observation results mapped to Observations and DiagnosticReport.",
        "segments": ["MSH", "PID", "PV1", "ORC", "OBR", "OBX", "NTE"],
        "tips": [
            "OBR-4 and OBX-3 should include LOINC codes when possible.",
            "OBX-11 conveys result status and should be populated.",
            "Use NTE segments for interpretive comments."
        ],
    },
    "ORM^O01": {
        "summary": "Medication or procedural orders mapped to MedicationRequest resources.",
        "segments": ["MSH", "PID", "PV1", "ORC", "RXO", "RXR", "OBX"],
        "tips": [
            "Populate RXO-1 with RXNORM codes for interoperability.",
            "Provide RXR route/site information for clearer administration instructions.",
            "Include OBX segments for indications or ancillary order details."
        ],
    },
    "VXU^V04": {
        "summary": "Immunization updates producing Immunization resources.",
        "segments": ["MSH", "PID", "PD1", "NK1", "ORC", "RXA", "RXR", "OBX"],
        "tips": [
            "RXA-5 should leverage CVX codes for vaccine identification.",
            "Include administering provider identifiers in ORC-12 and RXA-11.",
            "OBX segments can communicate funding program or lot-specific details."
        ],
    },
}


def _load_sample_definitions() -> dict[str, SampleDefinition]:
    samples_root = Path("samples")
    fhir_root = samples_root / "fhir"
    definitions = [
        SampleDefinition(
            label="ADT^A01 — GoodHealth admission",
            hl7_path=samples_root / "adt_goodhealth_a01.hl7",
            fhir_path=fhir_root / "adt_goodhealth_a01.json",
            description="Inpatient admission with allergy and diagnosis details.",
            message_type="ADT",
            trigger_event="A01",
        ),
        SampleDefinition(
            label="ORU^R01 — Complete blood count",
            hl7_path=samples_root / "oru_goodhealth_r01.hl7",
            fhir_path=fhir_root / "oru_goodhealth_r01.json",
            description="Outpatient laboratory results for a CBC panel.",
            message_type="ORU",
            trigger_event="R01",
        ),
        SampleDefinition(
            label="ORM^O01 — Amoxicillin order",
            hl7_path=samples_root / "orm_goodhealth_o01.hl7",
            fhir_path=fhir_root / "orm_goodhealth_o01.json",
            description="Outpatient medication order for amoxicillin.",
            message_type="ORM",
            trigger_event="O01",
        ),
        SampleDefinition(
            label="VXU^V04 — COVID-19 immunization",
            hl7_path=samples_root / "vxu_goodhealth_v04.hl7",
            fhir_path=fhir_root / "vxu_goodhealth_v04.json",
            description="Immunization update for a pediatric patient.",
            message_type="VXU",
            trigger_event="V04",
        ),
    ]
    return {definition.label: definition for definition in definitions if definition.hl7_path.exists()}


def _init_analytics() -> None:
    if "analytics" not in st.session_state:
        st.session_state["analytics"] = {
            "total_runs": 0,
            "successful_runs": 0,
            "partial_runs": 0,
            "resources_created": 0,
            "segments_observed": 0,
            "segments_expected": 0,
        }


def _update_analytics(context: ConversionContext | None, pairs: list[tuple[str, dict]], partial: bool) -> None:
    _init_analytics()
    analytics = st.session_state["analytics"]
    analytics["total_runs"] += 1
    if partial:
        analytics["partial_runs"] += 1
    else:
        analytics["successful_runs"] += 1
    analytics["resources_created"] += len(pairs)
    if context:
        analytics["segments_observed"] += sum(context.segment_counts.values())
        message_key = f"{context.message_type or ''}^{context.trigger_event or ''}".strip("^")
        supported = SUPPORTED_MESSAGES.get(message_key, {})
        analytics["segments_expected"] += len(supported.get("segments", []))
    st.session_state["analytics"] = analytics


def _analytics_snapshot() -> dict[str, Any]:
    _init_analytics()
    data = st.session_state["analytics"]
    success_rate = 0.0
    if data["total_runs"]:
        success_rate = data["successful_runs"] / data["total_runs"]
    segment_rate = 0.0
    if data["segments_expected"]:
        segment_rate = min(1.0, data["segments_observed"] / data["segments_expected"])
    return {
        "total_runs": data["total_runs"],
        "success_rate": success_rate,
        "segment_rate": segment_rate,
        "resources_created": data["resources_created"],
        "partial_runs": data["partial_runs"],
    }


def _component(field: str | None, position: int) -> Optional[str]:
    if not field:
        return None
    parts = str(field).split("^")
    if 1 <= position <= len(parts):
        value = parts[position - 1].strip()
        return value or None
    return None


def _segment_field(segment: Segment, index: int) -> Optional[str]:
    fields = segment.to_er7().split("|")
    if segment.name == "MSH":
        real_index = index - 1
    else:
        real_index = index
    if real_index < 0:
        return None
    if real_index < len(fields):
        value = fields[real_index]
        return value or None
    if real_index == len(fields):
        return fields[-1] or None
    return None


def _parse_segments(hl7_text: str) -> list[Segment]:
    try:
        message = parse_message(_NORMALIZE_HL7(hl7_text), validation_level=1)
    except Exception:
        return []
    segments: list[Segment] = []

    def _collect(node: Any) -> None:
        for child in getattr(node, "children", []):
            if isinstance(child, Segment):
                segments.append(child)
            else:
                _collect(child)

    _collect(message)
    return segments


def _partial_conversion(hl7_text: str) -> tuple[list[tuple[str, dict]], list[str]]:
    """
    Attempt a best-effort conversion when the primary converter fails.
    Returns (pairs, issues).
    """
    segments = _parse_segments(hl7_text)
    if not segments:
        return [], ["Unable to read HL7 structure. Verify segment delimiters are carriage returns (\\r)."]

    segment_lookup: dict[str, Segment] = {}
    for segment in segments:
        segment_lookup.setdefault(segment.name, segment)

    pairs: list[tuple[str, dict]] = []
    issues: list[str] = []

    pid = segment_lookup.get("PID")
    patient_id: Optional[str] = None
    if pid:
        mrn_field = _segment_field(pid, 3)
        patient_id = _component(mrn_field, 1) or "unknown"
        patient = {
            "resourceType": "Patient",
            "id": f"pat-{patient_id}",
        }
        name_field = _segment_field(pid, 5)
        family = _component(name_field, 1)
        given = [value for value in (_component(name_field, 2), _component(name_field, 3)) if value]
        if family or given:
            patient["name"] = [{"family": family, "given": given}]  # type: ignore[index]
        birth_raw = _segment_field(pid, 7)
        if birth_raw and birth_raw.isdigit() and len(birth_raw) == 8:
            patient["birthDate"] = f"{birth_raw[:4]}-{birth_raw[4:6]}-{birth_raw[6:]}"
        gender = _segment_field(pid, 8)
        if gender:
            mapping = {"F": "female", "M": "male", "O": "other", "U": "unknown"}
            patient["gender"] = mapping.get(gender.upper(), "unknown")
        addr_field = _segment_field(pid, 11)
        street = _component(addr_field, 1)
        city = _component(addr_field, 3)
        state = _component(addr_field, 4)
        postal = _component(addr_field, 5)
        country = _component(addr_field, 6)
        address = {}
        if street:
            address["line"] = [street]
        if city:
            address["city"] = city
        if state:
            address["state"] = state
        if postal:
            address["postalCode"] = postal
        if country:
            address["country"] = country
        if address:
            patient["address"] = [address]  # type: ignore[index]
        pairs.append(("Patient", patient))
    else:
        issues.append("PID segment missing; generated partial bundle without demographics.")

    pv1 = segment_lookup.get("PV1")
    if pv1 and patient_id:
        encounter_id = f"enc-{_segment_field(pv1, 19) or 'partial'}"
        cls = (_segment_field(pv1, 2) or "U").upper()
        class_map = {"I": "IMP", "O": "AMB", "E": "EMER", "P": "PRENC"}
        class_code = class_map.get(cls, "UNK")
        class_display_map = {
            "IMP": "inpatient encounter",
            "AMB": "ambulatory",
            "EMER": "emergency",
            "PRENC": "pre-admission",
            "UNK": "unknown",
        }
        encounter = {
            "resourceType": "Encounter",
            "id": encounter_id,
            "status": "in-progress",
            "class": {
                "system": "http://terminology.hl7.org/CodeSystem/v3-ActCode",
                "code": class_code,
                "display": class_display_map.get(class_code, "unknown"),
            },
            "subject": {"reference": f"Patient/pat-{patient_id}"},
        }
        location_raw = _segment_field(pv1, 3)
        location_display = " / ".join(filter(None, (_component(location_raw, i) for i in (1, 2, 3, 4))))
        if location_display:
            encounter["location"] = [{"location": {"display": location_display}}]  # type: ignore[index]
        pairs.append(("Encounter", encounter))

    return pairs, issues


def _supported_key(message_type: Optional[str], trigger_event: Optional[str]) -> Optional[str]:
    if not message_type:
        return None
    key = f"{message_type}"
    if trigger_event:
        key = f"{message_type}^{trigger_event}"
    return key if key in SUPPORTED_MESSAGES else None


def _segment_guidance(context: ConversionContext | None) -> tuple[list[str], list[str]]:
    if not context:
        return [], []
    key = _supported_key(context.message_type, context.trigger_event)
    if not key:
        return [], []
    supported = SUPPORTED_MESSAGES.get(key, {})
    expected = supported.get("segments", [])
    missing = [segment for segment in expected if context.segment_counts.get(segment, 0) == 0]
    return expected, missing


def _build_issue_list(
    context: ConversionContext | None,
    error: Exception | None,
    partial_issues: list[str],
) -> list[str]:
    issues: list[str] = []
    if error:
        issues.append(str(error))
    issues.extend(partial_issues)
    _, missing = _segment_guidance(context)
    if missing:
        links = ", ".join(
            f"[{segment}](https://hl7-definition.caristix.com/v2/HL7v2.5.1/Segments/{segment})"
            for segment in missing
        )
        issues.append(f"Missing recommended segments: {links}")
    return issues


def _mapping_summary_rows(context: ConversionContext | None, pairs: list[tuple[str, dict]]) -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    if context and context.resources:
        for insight in context.resources:
            if not insight.resource_type:
                continue
            summary_bits = []
            for key, value in insight.summary.items():
                summary_bits.append(f"{key}={value}")
            if insight.anomalies:
                summary_bits.extend(insight.anomalies)
            rows.append(
                {
                    "Resource": insight.resource_type,
                    "Highlights": "; ".join(summary_bits) or "Mapped without summary details.",
                }
            )
    else:
        for resource_type, resource in pairs:
            highlight_keys = []
            if "id" in resource:
                highlight_keys.append(f"id={resource['id']}")
            if "code" in resource and isinstance(resource["code"], dict):
                display = resource["code"].get("text") or ""
                if display:
                    highlight_keys.append(f"code={display}")
            rows.append(
                {
                    "Resource": resource_type,
                    "Highlights": "; ".join(highlight_keys) or "Resource created.",
                }
            )
    return rows


def run_conversion(label: str, hl7_text: str, *, update_copilot: bool = True) -> dict[str, Any]:
    start_time = time.time()
    result_payload: Any | None = None
    error: Exception | None = None
    pairs: list[tuple[str, dict]] = []
    partial = False
    partial_issues: list[str] = []

    try:
        result_payload = _CONVERT_FN(hl7_text)
        normalized = _normalize_result(result_payload)
        pairs = _pairs_from_result(normalized)
    except Exception as exc:  # noqa: BLE001
        error = exc
        pairs, partial_issues = _partial_conversion(hl7_text)
        partial = bool(pairs)
        if partial:
            result_payload = {
                "resourceType": "Bundle",
                "type": "collection",
                "entry": [{"resource": resource} for _, resource in pairs],
            }
    elapsed = time.time() - start_time

    agent = ConversionCopilot.from_payload(
        hl7_text,
        result_payload,
        duration_seconds=elapsed,
        error=error if not partial else None,
    )
    if update_copilot:
        st.session_state["copilot_agent"] = agent

    context = agent.context
    _update_analytics(context, pairs, partial)

    issues = _build_issue_list(context, error, partial_issues)

    return {
        "label": label,
        "elapsed": elapsed,
        "pairs": pairs,
        "context": context,
        "agent": agent,
        "error": error,
        "partial": partial,
        "issues": issues,
        "hl7_text": hl7_text,
    }


def _render_conversion_outcome(outcome: dict[str, Any], *, show_actions: bool = True) -> None:
    context: ConversionContext | None = outcome["context"]
    pairs: list[tuple[str, dict]] = outcome["pairs"]
    partial: bool = outcome["partial"]
    issues: list[str] = outcome["issues"]
    elapsed: float = outcome["elapsed"]
    label: str = outcome["label"]
    error: Exception | None = outcome["error"]

    message_type = context.message_type if context else None
    trigger = context.trigger_event if context else None
    message_key = _supported_key(message_type, trigger)
    supported = SUPPORTED_MESSAGES.get(message_key or "", {})

    expected_segments, missing_segments = _segment_guidance(context)

    if pairs:
        message = f"Converted {len(pairs)} FHIR resources in {elapsed:.2f} seconds."
        if partial:
            st.warning(f"Partial conversion complete — {message}", icon="⚠️")
        else:
            st.success(message, icon="✅")
    else:
        if error:
            st.error(f"Conversion failed: {error}", icon="🚫")
        else:
            st.info("No FHIR resources produced; review the input message for required segments.", icon="ℹ️")

    badge = ""
    if message_type and trigger:
        badge = f"`{message_type}^{trigger}`"
    elif message_type:
        badge = f"`{message_type}`"
    if badge:
        st.caption(f"Message profile detected: {badge}")

    if supported:
        st.markdown(
            f"<div class='conversion-summary'>Supported segments: {', '.join(f'`{seg}`' for seg in expected_segments)}</div>",
            unsafe_allow_html=True,
        )

    by_type: dict[str, int] = {}
    for resource_type, _ in pairs:
        by_type[resource_type] = by_type.get(resource_type, 0) + 1

    if by_type:
        chips_html = "".join(
            f"<span class='pill'><span>{html.escape(resource)}</span><strong>{count}</strong></span>"
            for resource, count in sorted(by_type.items())
        )
        st.markdown(f"<div class='pill-row'>{chips_html}</div>", unsafe_allow_html=True)

    mapping_rows = _mapping_summary_rows(context, pairs)
    if mapping_rows:
        st.markdown("#### Transformation summary")
        _dataframe(mapping_rows, use_container_width=True)

    if issues:
        st.markdown("#### Conversion notes")
        for note in issues:
            st.warning(note)

    if missing_segments:
        st.markdown("#### Suggested fixes")
        st.write(
            "Add or correct the following HL7 segments to improve fidelity:"
        )
        for segment in missing_segments:
            st.write(
                f"- [{segment} reference](https://hl7-definition.caristix.com/v2/HL7v2.5.1/Segments/{segment})"
            )

    resources_only = [resource for _, resource in pairs]
    st.markdown("<hr class='section-divider' />", unsafe_allow_html=True)

    tab_json, tab_res, tab_quality, tab_samples = st.tabs(
        ["FHIR JSON", "Resources", "Quality", "Examples & schemas"]
    )

    base_name = Path(label).stem or "hl7_message"
    if resources_only:
        json_payload = json.dumps(resources_only, default=_json_ready, ensure_ascii=False, indent=2)
        ndjson_payload = _ndjson(pairs)
    else:
        json_payload = "[]"
        ndjson_payload = ""

    with tab_json:
        if resources_only:
            st.json(resources_only)
            st.download_button(
                "Download FHIR bundle (JSON)",
                json_payload,
                f"{base_name}_bundle.json",
                "application/json",
                use_container_width=True,
            )
            st.download_button(
                "Download NDJSON",
                ndjson_payload,
                f"{base_name}_bundle.ndjson",
                "application/x-ndjson",
                use_container_width=True,
            )
        else:
            st.info("No FHIR resources available yet.")

    with tab_res:
        if not resources_only:
            st.info("No FHIR resources available.")
        else:
            grouped: dict[str, list[dict]] = {}
            for resource_type, resource in pairs:
                grouped.setdefault(resource_type, []).append(resource)
            for resource_type, resources in sorted(grouped.items()):
                st.markdown(f"**{resource_type}** ({len(resources)})")
                for resource in resources:
                    rid = resource.get("id") or "auto-generated"
                    with st.expander(f"{resource_type} — {rid}", expanded=False):
                        st.json(resource)

    with tab_quality:
        if not resources_only:
            st.info("Quality metrics unavailable until conversion completes.")
        else:
            rows = []
            anomalies_present: dict[str, list[str]] = {}
            completeness_values: list[float] = []
            for resource_type, resource in pairs:
                anomalies = detect_anomalies(resource_type, resource)
                if anomalies:
                    key = resource.get("id") or resource_type
                    anomalies_present[key] = anomalies
                score = completeness_score(resource_type, resource)
                if score is not None:
                    completeness_values.append(float(score))
                rows.append(
                    {
                        "resourceType": resource_type,
                        "id": resource.get("id", ""),
                        "completeness": score,
                        "anomalies": "; ".join(anomalies),
                    }
                )
            _dataframe(rows, use_container_width=True)
            if anomalies_present:
                for key, messages in anomalies_present.items():
                    with st.expander(f"Anomalies — {key}", expanded=False):
                        for message in messages:
                            st.write(f"- {message}")

    with tab_samples:
        if message_key and message_key in SUPPORTED_MESSAGES:
            st.markdown(f"**{message_key} schema guidance**")
            st.write(supported.get("summary"))
            st.markdown("**Common segments**")
            st.write(", ".join(supported.get("segments", [])))
            st.markdown("**Implementation tips**")
            for tip in supported.get("tips", []):
                st.write(f"- {tip}")
        sample_defs = _load_sample_definitions()
        for definition in sample_defs.values():
            with st.expander(definition.label, expanded=False):
                st.caption(definition.description)
                hl7_text = definition.hl7_path.read_text(encoding="utf-8")
                st.code(hl7_text, language="hl7")
                if definition.fhir_path and definition.fhir_path.exists():
                    fhir_text = definition.fhir_path.read_text(encoding="utf-8")
                    st.code(fhir_text, language="json")

    if not show_actions:
        return

    st.markdown("<hr class='section-divider' />", unsafe_allow_html=True)

    safe_pairs = pairs
    redact_toggle = st.toggle("De-identify PHI (mask patient identifiers)", value=False)
    if redact_toggle:
        safe_pairs = [
            (resource_type, mask_patient(resource) if resource_type == "Patient" else resource)
            for resource_type, resource in pairs
        ]

    st.markdown("#### Send to FHIR (optional)")
    default_base = os.getenv("FHIR_BASE_URL", "http://localhost:8080/fhir")
    token_default = os.getenv("AUTH_TOKEN", "")
    col_base, col_token = st.columns([2, 1])
    base_url = col_base.text_input("FHIR base URL", value=default_base)
    bearer_token = col_token.text_input("Bearer token (optional)", type="password", value=token_default)

    if st.button("POST all resources", use_container_width=True, disabled=not safe_pairs):
        if not safe_pairs:
            st.info("No resources to send.")
        else:
            client = FHIRClient(base_url, bearer_token or None)
            posted: list[dict[str, Any]] = []
            errors: list[str] = []
            for resource_type, resource in safe_pairs:
                try:
                    response = client.create(resource_type, resource)
                    entry: dict[str, Any] = {"resourceType": resource_type, "status": response.status_code}
                    try:
                        payload = response.json()
                    except ValueError:
                        payload = {}
                    resource_id = payload.get("id")
                    if resource_id:
                        entry["id"] = resource_id
                    audit_event("create", resource_type, resource_id, "streamlit-ui")
                    posted.append(entry)
                except Exception as exc:  # noqa: BLE001
                    errors.append(f"{resource_type}: {exc}")
                    posted.append({"resourceType": resource_type, "status": "error"})
            if errors:
                for msg in errors:
                    st.warning(msg)
            st.success("POST complete" if not errors else "POST attempted with warnings")
            if posted:
                st.table(posted)

    st.markdown("#### Referral intake workflow")
    if st.button("Simulate referral intake workflow", use_container_width=True):
        patient_resource = next((res for rtype, res in pairs if rtype == "Patient"), None)
        if not patient_resource:
            st.info("A Patient resource is required to trigger this workflow.")
        else:
            workflow = build_referral_intake_workflow(None, patient_resource, {"status": "planned"})
            workflow_context = workflow.run({})
            audit_event("workflow", "Patient", patient_resource.get("id"), "streamlit-ui")
            st.json(workflow_context)




_COPILOT_SVG = """
<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 140 140">
  <defs>
    <linearGradient id="clipFHIRGradient" x1="0%" y1="0%" x2="100%" y2="100%">
      <stop offset="0%" stop-color="#6D8CFB"/>
      <stop offset="100%" stop-color="#8FE3FF"/>
    </linearGradient>
  </defs>
  <g fill="none" stroke-linecap="round" stroke-linejoin="round">
    <path d="M50 30 C40 10 70 5 85 30 L105 70 C115 90 105 120 75 120 C45 120 35 90 45 70 L65 30" stroke="url(#clipFHIRGradient)" stroke-width="12"/>
    <path d="M60 44 C55 60 72 68 78 56" stroke="#FFFFFF" stroke-width="10"/>
    <circle cx="62" cy="64" r="6" fill="#22356F"/>
    <circle cx="86" cy="72" r="8" fill="#22356F"/>
    <circle cx="86" cy="70" r="3" fill="#FFFFFF"/>
    <circle cx="62" cy="62" r="2.5" fill="#FFFFFF"/>
    <path d="M70 92 C80 104 92 100 98 92" stroke="#22356F" stroke-width="6" />
  </g>
  <ellipse cx="80" cy="126" rx="34" ry="8" fill="rgba(34,53,111,0.15)"/>
</svg>
""".strip()

_COPILOT_STYLE = """
<style>
.copilot-card {
    background: linear-gradient(145deg, rgba(245, 247, 255, 0.95), rgba(255, 255, 255, 0.96));
    border-radius: 18px;
    padding: 0.9rem 1rem;
    box-shadow: 0 14px 34px rgba(37, 55, 113, 0.15);
    border: 1px solid rgba(109, 140, 251, 0.15);
    margin-bottom: 0.85rem;
}
.copilot-mascot {
    display: flex;
    align-items: center;
    gap: 0.75rem;
    margin-bottom: 0.5rem;
}
.copilot-mascot img {
    width: 52px;
    height: 52px;
    filter: drop-shadow(0 6px 12px rgba(88, 131, 255, 0.25));
}
.copilot-mascot-text strong {
    display: block;
    font-weight: 700;
}
.copilot-mascot-text span {
    font-size: 0.9rem;
    color: rgba(44, 56, 92, 0.82);
}
.copilot-status {
    font-size: 0.72rem;
    letter-spacing: 0.08em;
    text-transform: uppercase;
    color: rgba(44, 56, 92, 0.65);
    margin-bottom: 0.5rem;
}
.copilot-scroll {
    max-height: 16rem;
    overflow-y: auto;
    padding-right: 0.25rem;
    margin-bottom: 0.75rem;
}
.copilot-scroll::-webkit-scrollbar {
    width: 6px;
}
.copilot-scroll::-webkit-scrollbar-thumb {
    background: rgba(93, 113, 255, 0.35);
    border-radius: 3px;
}
.copilot-bubble {
    border-radius: 12px;
    padding: 0.65rem 0.8rem;
    margin-bottom: 0.6rem;
    background: rgba(255, 255, 255, 0.92);
    box-shadow: 0 6px 20px rgba(17, 27, 71, 0.12);
}
.copilot-bubble--assistant {
    background: linear-gradient(135deg, rgba(109, 140, 251, 0.18), rgba(143, 227, 255, 0.14));
    border: 1px solid rgba(109, 140, 251, 0.35);
}
.copilot-bubble--user {
    background: rgba(255, 255, 255, 0.96);
    border: 1px solid rgba(44, 80, 160, 0.08);
}
.copilot-bubble__role {
    font-size: 0.75rem;
    text-transform: uppercase;
    letter-spacing: 0.08em;
    margin-bottom: 0.25rem;
    color: rgba(44, 56, 92, 0.62);
}
.copilot-bubble__content {
    font-size: 0.94rem;
    line-height: 1.35rem;
}
.copilot-bubble__content p {
    margin: 0;
}
.copilot-bubble__content p + p {
    margin-top: 0.45rem;
}
.copilot-bubble__content ul {
    margin: 0.45rem 0 0 1.05rem;
    padding: 0;
    list-style: disc;
    color: inherit;
}
.copilot-bubble__content li {
    margin-bottom: 0.2rem;
}
.copilot-empty {
    padding: 0.75rem;
    color: rgba(44, 56, 92, 0.7);
    font-style: italic;
}
</style>
""".strip()


@lru_cache(maxsize=1)
def _copilot_avatar_uri() -> str:
    encoded = base64.b64encode(_COPILOT_SVG.encode("utf-8")).decode("ascii")
    return f"data:image/svg+xml;base64,{encoded}"


def _format_message_html(text: str) -> str:
    lines = [line.strip() for line in text.splitlines()]
    blocks: list[str] = []
    list_items: list[str] = []

    def flush_list() -> None:
        nonlocal list_items
        if list_items:
            blocks.append("<ul>" + "".join(list_items) + "</ul>")
            list_items = []

    for line in lines:
        if not line:
            flush_list()
            continue

        bullet_prefixes = ("•", "-", "*")
        if line.startswith(bullet_prefixes):
            stripped = line.lstrip("•-* ").strip()
            list_items.append(f"<li>{html.escape(stripped)}</li>")
            continue

        if len(line) > 2 and line[0].isdigit() and line[1] in {".", ")"}:
            remainder = line.split(" ", 1)
            stripped = remainder[1].strip() if len(remainder) > 1 else line[2:].strip()
            list_items.append(f"<li>{html.escape(stripped)}</li>")
            continue

        flush_list()
        blocks.append(f"<p>{html.escape(line)}</p>")

    flush_list()
    return "".join(blocks) or "<p></p>"


def _copilot_messages_html(messages: list) -> str:
    if not messages:
        return "<div class='copilot-empty'>Ask me anything about this bundle!</div>"
    bubbles: list[str] = []
    for message in messages:
        role = "assistant" if message.role != "user" else "user"
        role_label = "ClipFHIR" if role == "assistant" else "You"
        safe_lines = _format_message_html(message.content)
        bubbles.append(
            f"""
            <div class="copilot-bubble copilot-bubble--{role}">
                <div class="copilot-bubble__role">{role_label}</div>
                <div class="copilot-bubble__content">{safe_lines}</div>
            </div>
            """
        )
    return "\n".join(bubbles)


def _render_copilot(agent: ConversionCopilot):
    st.markdown(_COPILOT_STYLE, unsafe_allow_html=True)
    chat_messages = list(agent.conversation())
    status_label = "GPT-powered responses enabled" if getattr(agent, "llm", None) else "Offline rule-based responses"
    st.markdown(
        f"""
        <div class="copilot-card">
            <div class="copilot-mascot">
                <img src="{_copilot_avatar_uri()}" alt="ClipFHIR mascot" />
                <div class="copilot-mascot-text">
                    <strong>ClipFHIR</strong>
                    <span>It looks like you're transforming HL7!</span>
                </div>
            </div>
            <div class="copilot-status">{status_label}</div>
            <div class="copilot-scroll">{_copilot_messages_html(chat_messages)}</div>
        </div>
        """,
        unsafe_allow_html=True,
    )
    submitted = False
    prompt_value = ""
    with st.form("copilot_panel_form", clear_on_submit=True):
        input_col, button_col = st.columns([4, 1])
        with input_col:
            prompt_value = st.text_input(
                "Ask ClipFHIR something",
                key="copilot_prompt",
                placeholder="e.g. Where did the OBX go?",
                label_visibility="collapsed",
            )
        with button_col:
            submitted = st.form_submit_button("Send", use_container_width=True)
    if submitted and prompt_value.strip():
        with st.spinner("ClipFHIR is thinking…"):
            agent.chat(prompt_value)
        st.session_state["copilot_agent"] = agent


st.set_page_config(page_title="HL7 → FHIR R4 Converter", page_icon="🧬", layout="wide")

# Inject modern font styling (fallback keeps Streamlit defaults if loading fails)
st.markdown(
    """
    <style>
    @import url('https://fonts.googleapis.com/css2?family=Manrope:wght@400;600;700&family=JetBrains+Mono:wght@400;600&display=swap');
    :root, [data-testid="stAppViewContainer"], [data-testid="stSidebar"] * {
        font-family: 'Manrope', 'Inter', -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
    }
    body, :root {
        color-scheme: light dark;
    }
    .app-surface {
        --surface-bg: radial-gradient(circle at 0% 0%, rgba(139, 163, 255, 0.14), transparent 35%),
                       radial-gradient(circle at 100% 0%, rgba(143, 227, 255, 0.16), transparent 40%),
                       linear-gradient(180deg, #f5f7ff 0%, #f9fbff 100%);
        --surface-color: #172140;
        --surface-muted: rgba(16, 26, 57, 0.62);
        --glass-bg: rgba(255, 255, 255, 0.92);
        --glass-border: rgba(109, 140, 251, 0.18);
        --glass-shadow: 0 26px 48px rgba(30, 54, 110, 0.12);
        --hero-gradient: linear-gradient(135deg, rgba(109, 140, 251, 0.14), rgba(143, 227, 255, 0.18));
        --hero-text: #101a39;
        --hero-subtext: rgba(16, 26, 57, 0.72);
        --pill-bg: rgba(109, 140, 251, 0.12);
        --pill-border: rgba(109, 140, 251, 0.2);
        --pill-text: #1a2f66;
        --textarea-bg: rgba(18, 35, 80, 0.05);
        --textarea-border: rgba(109, 140, 251, 0.28);
        --textarea-color: #0f1a37;
        --metric-bg: rgba(255, 255, 255, 0.82);
        --metric-border: rgba(109, 140, 251, 0.18);
        --metric-text: #101a39;
        --divider-color: rgba(16, 26, 57, 0.08);
        --empty-bg: rgba(109, 140, 251, 0.1);
        --empty-border: rgba(109, 140, 251, 0.28);
        --empty-text: rgba(16, 26, 57, 0.68);
        color: var(--surface-color);
    }
    [data-testid="stAppViewContainer"].app-surface-dark {
        --surface-bg: radial-gradient(circle at 0% 0%, rgba(88, 102, 190, 0.26), transparent 35%),
                      radial-gradient(circle at 100% 0%, rgba(60, 126, 176, 0.24), transparent 40%),
                      linear-gradient(180deg, #101732 0%, #0b0e1a 100%);
        --surface-color: rgba(240, 244, 255, 0.94);
        --surface-muted: rgba(205, 213, 241, 0.76);
        --glass-bg: rgba(18, 22, 41, 0.82);
        --glass-border: rgba(109, 140, 251, 0.32);
        --glass-shadow: 0 26px 48px rgba(6, 10, 26, 0.45);
        --hero-gradient: linear-gradient(135deg, rgba(109, 140, 251, 0.18), rgba(143, 227, 255, 0.28));
        --hero-text: #f4f6ff;
        --hero-subtext: rgba(220, 230, 255, 0.76);
        --pill-bg: rgba(109, 140, 251, 0.2);
        --pill-border: rgba(109, 140, 251, 0.45);
        --pill-text: rgba(226, 234, 255, 0.94);
        --textarea-bg: rgba(15, 24, 48, 0.65);
        --textarea-border: rgba(109, 140, 251, 0.45);
        --textarea-color: rgba(232, 237, 255, 0.94);
        --metric-bg: rgba(20, 27, 52, 0.92);
        --metric-border: rgba(109, 140, 251, 0.32);
        --metric-text: rgba(234, 239, 255, 0.98);
        --divider-color: rgba(132, 144, 198, 0.18);
        --empty-bg: rgba(109, 140, 251, 0.18);
        --empty-border: rgba(109, 140, 251, 0.45);
        --empty-text: rgba(215, 224, 255, 0.82);
    }
    [data-testid="stAppViewContainer"] {
        background: var(--surface-bg);
        padding-top: 1.5rem;
        color: var(--surface-color);
    }
    [data-testid="stAppViewContainer"].app-surface-dark {
        color: var(--surface-color);
    }
    [data-testid="stSidebar"] {
        background: var(--glass-bg);
        backdrop-filter: blur(16px);
        border-right: 1px solid var(--glass-border);
        color: var(--surface-color);
    }
    .page-hero {
        display: flex;
        align-items: center;
        gap: 1rem;
        padding: 1.1rem 1.35rem;
        margin-bottom: 1.25rem;
        border-radius: 22px;
        background: var(--hero-gradient);
        border: 1px solid var(--glass-border);
        box-shadow: var(--glass-shadow);
        color: var(--surface-color);
    }
    .page-hero .hero-icon {
        font-size: 2.2rem;
    }
    .page-hero h1 {
        font-size: 1.9rem;
        font-weight: 700;
        margin: 0;
        color: var(--hero-text);
    }
    .page-hero p {
        margin: 0.25rem 0 0;
        color: var(--hero-subtext);
        font-size: 0.95rem;
    }
    .card {
        background: var(--glass-bg);
        border-radius: 20px;
        padding: 1.35rem 1.5rem;
        border: 1px solid var(--glass-border);
        box-shadow: var(--glass-shadow);
        backdrop-filter: blur(14px);
        margin-bottom: 1.35rem;
        color: var(--surface-color);
    }
    .card h3 {
        margin-top: 0;
        margin-bottom: 0.35rem;
        font-weight: 700;
        color: var(--hero-text);
    }
    .card p {
        color: var(--surface-muted);
        margin-bottom: 0.9rem;
        font-size: 0.92rem;
    }
    .card-eyebrow {
        font-size: 0.78rem;
        letter-spacing: 0.14em;
        text-transform: uppercase;
        color: var(--surface-muted);
        opacity: 0.72;
        font-weight: 600;
        display: inline-block;
        margin-bottom: 0.35rem;
    }
    .stTextArea textarea {
        font-family: 'JetBrains Mono', 'Menlo', 'SFMono-Regular', monospace;
        font-size: 0.92rem;
        line-height: 1.45rem;
        border-radius: 14px;
        border: 1px solid var(--textarea-border);
        background: var(--textarea-bg);
        color: var(--textarea-color);
        padding: 1rem;
        min-height: 220px;
    }
    .stTextArea textarea:focus {
        border-color: rgba(109, 140, 251, 0.65);
        box-shadow: 0 0 0 3px rgba(109, 140, 251, 0.28);
    }
    .action-row {
        margin-top: 0.85rem;
        display: flex;
        gap: 0.75rem;
    }
    .action-row button[kind="primary"] {
        background: linear-gradient(135deg, #6d8cfb, #8fe3ff);
        border: none;
        color: #0d1330;
        font-weight: 600;
        box-shadow: 0 16px 28px rgba(109, 140, 251, 0.32);
    }
    [data-testid="stAppViewContainer"].app-surface-dark .action-row button[kind="primary"] {
        color: #081122;
    }
    .action-row button:not([kind="primary"]) {
        background: rgba(17, 26, 57, 0.06);
        color: var(--surface-color);
        border: 1px solid rgba(17, 26, 57, 0.12);
    }
    [data-testid="stAppViewContainer"].app-surface-dark .action-row button:not([kind="primary"]) {
        background: rgba(226, 236, 255, 0.04);
        border-color: rgba(226, 236, 255, 0.14);
        color: var(--surface-color);
    }
    .pill-row {
        display: flex;
        flex-wrap: wrap;
        gap: 0.6rem;
        margin: 1rem 0 0.8rem;
    }
    .pill {
        display: inline-flex;
        align-items: center;
        gap: 0.45rem;
        padding: 0.44rem 0.75rem;
        border-radius: 999px;
        background: var(--pill-bg);
        border: 1px solid var(--pill-border);
        color: var(--pill-text);
        font-size: 0.85rem;
        font-weight: 600;
    }
    .pill strong {
        font-size: 0.88rem;
        color: inherit;
    }
    div[data-baseweb="tab-list"] button {
        border-radius: 999px !important;
        padding: 0.45rem 1.1rem !important;
        margin-right: 0.5rem !important;
        color: var(--surface-muted) !important;
        font-weight: 600 !important;
    }
    div[data-baseweb="tab-list"] button[aria-selected="true"] {
        background: linear-gradient(135deg, rgba(109, 140, 251, 0.24), rgba(143, 227, 255, 0.32)) !important;
        color: var(--surface-color) !important;
    }
    [data-testid="stAppViewContainer"].app-surface-dark div[data-baseweb="tab-list"] button[aria-selected="true"] {
        background: linear-gradient(135deg, rgba(109, 140, 251, 0.32), rgba(143, 227, 255, 0.42)) !important;
    }
    .metric-card {
        border-radius: 16px;
        padding: 0.95rem 1.05rem;
        border: 1px solid var(--metric-border);
        background: var(--metric-bg);
        box-shadow: inset 0 1px 0 rgba(255, 255, 255, 0.12);
    }
    .metric-card h4 {
        margin: 0;
        font-size: 0.78rem;
        letter-spacing: 0.08em;
        text-transform: uppercase;
        color: var(--surface-muted);
        opacity: 0.78;
    }
    .metric-card span {
        display: block;
        font-size: 1.4rem;
        font-weight: 700;
        color: var(--metric-text);
        margin-top: 0.3rem;
    }
    .card-footnote {
        margin-top: 0.9rem;
        font-size: 0.82rem;
        color: var(--surface-muted);
    }
    .card-footnote strong {
        color: var(--hero-text);
    }
    .section-divider {
        border: none;
        border-top: 1px solid var(--divider-color);
        margin: 1.35rem 0;
    }
    .stDownloadButton button {
        width: 100%;
        border-radius: 12px;
        font-weight: 600;
        background: rgba(109, 140, 251, 0.14);
        color: var(--surface-color);
        border: 1px solid rgba(109, 140, 251, 0.24);
    }
    [data-testid="stAppViewContainer"].app-surface-dark .stDownloadButton button {
        background: rgba(109, 140, 251, 0.24);
        color: rgba(15, 20, 38, 0.92);
        border-color: rgba(109, 140, 251, 0.45);
    }
    .stTable {
        border-radius: 14px;
        overflow: hidden;
    }
    .stTable [data-testid="stTable"] {
        background: transparent;
        color: var(--surface-color);
    }
    .empty-state {
        padding: 1.05rem 1.2rem;
        border-radius: 16px;
        border: 1px dashed var(--empty-border);
        background: var(--empty-bg);
        color: var(--empty-text);
        font-size: 0.95rem;
    }
    .conversion-summary {
        margin: 0.35rem 0 1rem;
        color: var(--surface-muted);
        font-size: 0.95rem;
        font-weight: 500;
    }
    @media (max-width: 900px) {
        .page-hero {
            flex-direction: column;
            align-items: flex-start;
        }
        .card {
            padding: 1rem;
            margin-bottom: 1rem;
        }
        .action-row {
            flex-direction: column;
            gap: 0.5rem;
        }
        .action-row button {
            width: 100%;
        }
    }
    </style>
    """,
    unsafe_allow_html=True,
)

st.markdown(
    """
    <script>
    const applySurfaceTheme = () => {
        const parentDoc = window.parent.document;
        const root = parentDoc.querySelector('[data-testid="stAppViewContainer"]');
        if (!root) {
            return;
        }
        root.classList.add('app-surface');
        const theme = parentDoc.body.getAttribute('data-theme');
        if (theme === 'dark') {
            root.classList.add('app-surface-dark');
        } else {
            root.classList.remove('app-surface-dark');
        }
    };
    applySurfaceTheme();
    const themeObserver = new MutationObserver(applySurfaceTheme);
    themeObserver.observe(window.parent.document.body, { attributes: true, attributeFilter: ['data-theme'] });
    </script>
    """,
    unsafe_allow_html=True,
)


# --- Sidebar inputs ---
sample_definitions = _load_sample_definitions()
sample_labels = list(sample_definitions.keys())

with st.sidebar:
    st.subheader("Message input")
    sample_choice: Optional[str] = None
    if sample_labels:
        default_index = 0
        if st.session_state.get("selected_sample") in sample_labels:
            default_index = sample_labels.index(st.session_state["selected_sample"])
        sample_choice = st.selectbox(
            "Sample HL7 message",
            sample_labels,
            index=default_index,
        )
        st.session_state["selected_sample"] = sample_choice
        sample_meta = sample_definitions[sample_choice]
        st.caption(sample_meta.description)
        if sample_meta.fhir_path and sample_meta.fhir_path.exists():
            st.download_button(
                "Download sample FHIR bundle",
                sample_meta.fhir_path.read_text(encoding="utf-8"),
                file_name=sample_meta.fhir_path.name,
                mime="application/json",
                use_container_width=True,
            )
    else:
        sample_meta = None
        st.info("Add HL7 examples under `samples/` to enable quick-start testing.")

    st.divider()
    uploaded_files = st.file_uploader(
        "Upload HL7 file(s) for batch conversion",
        type=["hl7", "txt"],
        accept_multiple_files=True,
        help="Drop one or more HL7 v2 messages to convert them together.",
    )
    if uploaded_files:
        st.success(f"{len(uploaded_files)} file(s) queued for conversion.")
        if st.button("Load first uploaded file into editor", use_container_width=True):
            first_file = uploaded_files[0]
            file_text = first_file.getvalue().decode("utf-8", errors="ignore")
            st.session_state.hl7_text = file_text
            st.session_state.input_label = first_file.name
            st.session_state._last_sample = None
            _reset_copilot(file_text)
            st.success(f"{first_file.name} loaded into the editor.")

    st.divider()
    st.markdown(
        "Supported message profiles: "
        + ", ".join(f"`{key}`" for key in SUPPORTED_MESSAGES.keys())
    )
    st.markdown(
        "Need formatting help? Review the "
        "[HL7 v2.5.1 specification](https://www.hl7.org/documentcenter/public_temp_C2C6B76B-1C23-BA17-0CE3F0B2DB9A0C1C/standards/v2/v251/infrastructure.html)."
    )

# Editor state
if "hl7_text" not in st.session_state:
    if sample_choice:
        selected = sample_definitions[sample_choice]
        st.session_state.hl7_text = selected.hl7_path.read_text(encoding="utf-8")
        st.session_state.input_label = selected.hl7_path.name
        st.session_state._last_sample = sample_choice
    else:
        st.session_state.hl7_text = ""
        st.session_state.input_label = "manual_entry.hl7"
        st.session_state._last_sample = None
    _init_copilot_if_missing(st.session_state.hl7_text)
else:
    _init_copilot_if_missing(st.session_state.get("hl7_text", ""))

# Auto-load when sample changes
if sample_choice and st.session_state.get("_last_sample") != sample_choice:
    selected_definition = sample_definitions[sample_choice]
    try:
        new_text = selected_definition.hl7_path.read_text(encoding="utf-8")
        st.session_state.hl7_text = new_text
        st.session_state.input_label = selected_definition.hl7_path.name
        st.session_state._last_sample = sample_choice
        _reset_copilot(new_text)
        st.toast(f"Loaded {sample_choice}")
    except OSError as exc:
        st.sidebar.warning(f"Unable to read sample: {exc}")

st.markdown(
    """
    <div class="page-hero">
        <span class="hero-icon">🧬</span>
        <div>
            <h1>HL7 → FHIR R4 Converter</h1>
            <p>Smarter demo data with instant bundle insights. Test data only -- no real PHI.</p>
        </div>
    </div>
    """,
    unsafe_allow_html=True,
)

analytics = _analytics_snapshot()
metric_cols = st.columns(4)
metric_cols[0].metric("Conversions", analytics["total_runs"])
metric_cols[1].metric("Success rate", f"{analytics['success_rate'] * 100:.0f}%")
metric_cols[2].metric("Segment coverage", f"{analytics['segment_rate'] * 100:.0f}%")
metric_cols[3].metric("FHIR resources", analytics["resources_created"])

if analytics["partial_runs"]:
    st.caption(f"Partial conversions recorded: {analytics['partial_runs']}")

primary_col, aside_col = st.columns([3, 2])

with primary_col:
    st.markdown('<div class="card card-editor">', unsafe_allow_html=True)
    st.markdown("<span class='card-eyebrow'>Message</span>", unsafe_allow_html=True)
    st.markdown("<h3>HL7 payload</h3>", unsafe_allow_html=True)
    st.markdown(
        "<p>Paste or tweak an HL7 v2 message and convert it into a rich FHIR bundle in seconds. "
        "The editor supports keyboard navigation and resizes for smaller screens.</p>",
        unsafe_allow_html=True,
    )
    hl7_text = st.text_area(
        "HL7 message",
        value=st.session_state.hl7_text,
        height=260,
        key="editor",
        label_visibility="collapsed",
        placeholder="MSH|^~\&|ADT|GOODHEALTH|EHR|GOODHEALTH|202510131005||ADT^A01|A01-99999|P|2.5.1",
    )
    st.session_state.hl7_text = hl7_text

    st.markdown('<div class="action-row">', unsafe_allow_html=True)
    c1, c2 = st.columns([1, 1])
    convert = _button(c1, "Convert current message", type="primary")
    reset = _button(c2, "Reset editor")
    st.markdown("</div>", unsafe_allow_html=True)

    current_label = html.escape(st.session_state.get("input_label", "manual_entry.hl7"))
    st.markdown(
        f"<div class='card-footnote'>Source: <strong>{current_label}</strong></div>",
        unsafe_allow_html=True,
    )
    st.markdown("</div>", unsafe_allow_html=True)

    st.markdown('<div class="card card-batch">', unsafe_allow_html=True)
    st.markdown("<span class='card-eyebrow'>Batch</span>", unsafe_allow_html=True)
    st.markdown("<h3>Bulk conversion</h3>", unsafe_allow_html=True)
    if uploaded_files:
        st.markdown(
            "<p>The following files will be processed together. Each result includes downloads and quality feedback.</p>",
            unsafe_allow_html=True,
        )
        for uploaded in uploaded_files:
            st.markdown(f"- `{uploaded.name}` ({uploaded.size} bytes)")
    else:
        st.markdown(
            "<p>Upload one or more `.hl7` files in the sidebar to enable batch conversion.</p>",
            unsafe_allow_html=True,
        )
    batch_convert = st.button(
        "Batch convert uploaded files",
        use_container_width=True,
        disabled=not uploaded_files,
    )
    st.markdown("</div>", unsafe_allow_html=True)

with aside_col:
    st.markdown('<div class="card card-supported">', unsafe_allow_html=True)
    st.markdown("<span class='card-eyebrow'>Profiles</span>", unsafe_allow_html=True)
    st.markdown("<h3>Supported message types</h3>", unsafe_allow_html=True)
    for key, info in SUPPORTED_MESSAGES.items():
        st.markdown(f"**{key}** — {info['summary']}")
    st.markdown("</div>", unsafe_allow_html=True)
    copilot_panel = st.container()

if reset:
    st.session_state.clear()
    _rerun_app()

single_outcome: Optional[dict[str, Any]] = None
batch_outcomes: list[dict[str, Any]] = []

if convert:
    if not hl7_text.strip():
        st.warning("Please provide HL7 content before converting.")
    else:
        status_callable = getattr(st, "status", None)
        if callable(status_callable):
            with status_callable("Converting HL7 message…", expanded=False) as status:
                outcome = run_conversion(st.session_state.get("input_label", "manual_entry.hl7"), hl7_text)
                if outcome["error"] and not outcome["partial"]:
                    status.update(label="Conversion completed with errors", state="error")
                elif outcome["partial"]:
                    status.update(label="Partial conversion generated", state="running")
                else:
                    status.update(label="Conversion complete", state="complete")
        else:
            with st.spinner("Converting HL7 message…"):
                outcome = run_conversion(st.session_state.get("input_label", "manual_entry.hl7"), hl7_text)
        single_outcome = outcome
        if outcome["pairs"]:
            st.toast(f"Converted {len(outcome['pairs'])} FHIR resource(s).")
        else:
            st.toast("Conversion attempted. Review notes below for corrective actions.")

if batch_convert and uploaded_files:
    progress = st.progress(0, text="Batch conversion in progress…")
    for index, uploaded in enumerate(uploaded_files, start=1):
        content = uploaded.getvalue().decode("utf-8", errors="ignore")
        outcome = run_conversion(uploaded.name, content, update_copilot=False)
        batch_outcomes.append(outcome)
        progress.progress(index / len(uploaded_files))
    progress.empty()
    st.success(f"Processed {len(batch_outcomes)} message(s).")

with primary_col:
    if single_outcome:
        st.markdown('<div class="card card-results">', unsafe_allow_html=True)
        _render_conversion_outcome(single_outcome)
        st.markdown("</div>", unsafe_allow_html=True)

    if batch_outcomes:
        st.markdown('<div class="card card-results">', unsafe_allow_html=True)
        st.markdown("### Batch conversion results")
        summary_rows = []
        for item in batch_outcomes:
            status_label = "Success"
            if item["partial"]:
                status_label = "Partial"
            elif not item["pairs"]:
                status_label = "Error"
            summary_rows.append(
                {
                    "File": item["label"],
                    "Status": status_label,
                    "Resources": len(item["pairs"]),
                    "Duration (s)": f"{item['elapsed']:.2f}",
                }
            )
        _dataframe(summary_rows, use_container_width=True)
        for item in batch_outcomes:
            status_label = "Success"
            if item["partial"]:
                status_label = "Partial"
            elif not item["pairs"]:
                status_label = "Error"
            with st.expander(f"{item['label']} — {status_label}", expanded=False):
                if item["pairs"]:
                    _render_conversion_outcome(item, show_actions=False)
                    bundle_json = json.dumps(
                        [resource for _, resource in item["pairs"]],
                        default=_json_ready,
                        ensure_ascii=False,
                        indent=2,
                    )
                    st.download_button(
                        "Download bundle JSON",
                        bundle_json,
                        f"{Path(item['label']).stem}_bundle.json",
                        "application/json",
                        use_container_width=True,
                    )
                    st.download_button(
                        "Download NDJSON",
                        _ndjson(item["pairs"]),
                        f"{Path(item['label']).stem}_bundle.ndjson",
                        "application/x-ndjson",
                        use_container_width=True,
                    )
                else:
                    st.warning("No FHIR resources were generated. Review the notes above for remediation tips.")
        st.markdown("</div>", unsafe_allow_html=True)

copilot_agent = st.session_state.get("copilot_agent")
if copilot_agent:
    with copilot_panel:
        _render_copilot(copilot_agent)
