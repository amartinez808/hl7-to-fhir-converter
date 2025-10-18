"""
Streamlit interface for the HL7 → FHIR mini-converter.

Run with:
    streamlit run streamlit_app.py
"""

from __future__ import annotations

import json
import logging
import os
import sys
import time
from datetime import date, datetime
from decimal import Decimal
from enum import Enum
from pathlib import Path
from typing import Any

import streamlit as st

from app.copilot import ConversionCopilot
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


def _render_copilot(agent: ConversionCopilot):
    st.divider()
    st.subheader("Conversion Copilot")
    chat_messages = list(agent.conversation())
    if hasattr(st, "chat_message"):
        for message in chat_messages:
            role = message.role if message.role in {"assistant", "user"} else "assistant"
            with st.chat_message(role):
                st.markdown(message.content)
        chat_input_fn = getattr(st, "chat_input", None)
        prompt = chat_input_fn("Ask the copilot about this conversion") if callable(chat_input_fn) else None
        if prompt:
            agent.chat(prompt)
            st.session_state["copilot_agent"] = agent
            _rerun_app()
    else:
        for message in chat_messages:
            st.markdown(f"**{message.role.title()}:** {message.content}")
        with st.form("copilot_fallback"):
            prompt = st.text_area("Ask the copilot about this conversion", key="copilot_prompt")
            submitted = st.form_submit_button("Send")
        if submitted and prompt.strip():
            agent.chat(prompt)
            st.session_state["copilot_agent"] = agent
            st.session_state["copilot_prompt"] = ""
            _rerun_app()


st.set_page_config(page_title="HL7 → FHIR R4 Converter", page_icon="🧬", layout="wide")

# Inject modern font styling (fallback keeps Streamlit defaults if loading fails)
st.markdown(
    """
    <style>
    @import url('https://fonts.googleapis.com/css2?family=Manrope:wght@400;600;700&display=swap');
    :root, [data-testid="stAppViewContainer"], [data-testid="stSidebar"] * {
        font-family: 'Manrope', 'Inter', -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
    }
    [data-testid="stMetricLabel"], [data-testid="stHeader"] h1 {
        font-weight: 600;
    }
    </style>
    """,
    unsafe_allow_html=True,
)

# --- Sidebar inputs ---
with st.sidebar:
    st.subheader("Message Input")
    samples_dir = Path("samples")
    sample_files = sorted(p.name for p in samples_dir.glob("*.hl7"))
    sample = None
    if sample_files:
        sample = st.selectbox("Sample HL7 message", sample_files, index=0)
    up = st.file_uploader("Or upload your own", type=["hl7", "txt"])
    st.caption("Limit 200MB per file • HL7, TXT")

# Editor state
if "hl7_text" not in st.session_state:
    if sample_files:
        default_sample = Path("samples", sample_files[0])
        st.session_state.hl7_text = default_sample.read_text(encoding="utf-8")
        st.session_state.input_label = sample_files[0]
        st.session_state._last_sample = sample_files[0]
    else:
        st.session_state.hl7_text = ""
        st.session_state.input_label = "hl7_message"
        st.session_state._last_sample = None

# Auto-load when sample changes (no upload)
if sample and st.session_state.get("_last_sample") != sample and up is None:
    sample_path = Path("samples", sample)
    try:
        st.session_state.hl7_text = sample_path.read_text(encoding="utf-8")
        st.session_state.input_label = sample
        st.session_state._last_sample = sample
    except OSError as exc:
        st.warning(f"Unable to read sample {sample}: {exc}")

# Uploaded file overrides
if up is not None:
    uploaded_text = up.read().decode("utf-8", errors="ignore")
    st.session_state.hl7_text = uploaded_text
    st.session_state.input_label = up.name or "uploaded_message"
    st.session_state._last_sample = None

st.title("HL7 v2 ➜ FHIR R4 Converter Demo")
st.caption("Test data only — no real PHI.")

hl7_text = st.text_area(
    "HL7 message",
    value=st.session_state.hl7_text,
    height=220,
    key="editor",
)
st.session_state.hl7_text = hl7_text

c1, c2 = st.columns([1, 1])
convert = _button(c1, "Convert to FHIR", type="primary")
reset = _button(c2, "Reset editor")
if reset:
    st.session_state.clear()
    _rerun_app()

if convert:
    if not hl7_text.strip():
        st.warning("Please provide HL7 content before converting.")
    else:
        result = None
        error: Exception | None = None
        elapsed = 0.0
        status_callable = getattr(st, "status", None)
        if callable(status_callable):
            with status_callable("Converting…", expanded=False) as status:
                t0 = time.time()
                try:
                    result = _CONVERT_FN(hl7_text)
                    elapsed = time.time() - t0
                    status.update(label=f"Done in {elapsed:.2f}s", state="complete")
                except Exception as exc:  # noqa: BLE001
                    error = exc
                    status.update(label="Conversion failed", state="error")
        else:
            with st.spinner("Converting…"):
                t0 = time.time()
                try:
                    result = _CONVERT_FN(hl7_text)
                    elapsed = time.time() - t0
                except Exception as exc:  # noqa: BLE001
                    error = exc
        if error is not None:
            logger.exception("Unable to convert HL7 message", exc_info=error)
            st.error(f"Unable to convert HL7 message: {error}")
            st.session_state["copilot_agent"] = ConversionCopilot.from_payload(
                hl7_text,
                result,
                duration_seconds=elapsed or None,
                error=error,
            )
        elif result is None:
            st.error("Conversion produced no result.")
            st.session_state["copilot_agent"] = ConversionCopilot.from_payload(
                hl7_text,
                result,
                duration_seconds=elapsed or None,
                error=None,
            )
        else:
            st.session_state["copilot_agent"] = ConversionCopilot.from_payload(
                hl7_text,
                result,
                duration_seconds=elapsed or None,
                error=None,
            )
            normalized = _normalize_result(result)
            pairs = _pairs_from_result(normalized)
            if not pairs:
                st.info("No FHIR resources were produced from this message.")
            else:
                total = len(pairs)
                by_type: dict[str, int] = {}
                for resource_type, _ in pairs:
                    by_type[resource_type] = by_type.get(resource_type, 0) + 1

                toast_fn = getattr(st, "toast", None)
                summary_message = f"Converted ✅ {total} resources across {len(by_type)} types"
                if callable(toast_fn):
                    toast_fn(summary_message)
                else:
                    st.success(summary_message)

                chips = " ".join(f"`{rtype}` **{count}**" for rtype, count in sorted(by_type.items()))
                st.markdown(chips or "_No resources produced_")

                resources_only = [resource for _, resource in pairs]
                tab_json, tab_res, tab_quality = st.tabs(["FHIR JSON", "Resources", "Quality"])

                with tab_json:
                    if resources_only:
                        st.json(resources_only)
                        json_payload = json.dumps(resources_only, default=_json_ready, ensure_ascii=False, indent=2)
                        st.download_button(
                            "Download JSON",
                            json_payload,
                            f"{Path(st.session_state.get('input_label', 'hl7_message')).stem}_bundle.json",
                            "application/json",
                        )
                        st.download_button(
                            "Download NDJSON",
                            _ndjson(pairs),
                            f"{Path(st.session_state.get('input_label', 'hl7_message')).stem}_bundle.ndjson",
                            "application/x-ndjson",
                        )
                    else:
                        st.info("No FHIR resources available.")

                with tab_res:
                    if not resources_only:
                        st.info("No FHIR resources available.")
                    else:
                        for resource_type, resource in pairs:
                            label = f"{resource_type} — {resource.get('id', 'no-id')}"
                            with st.expander(label, expanded=False):
                                st.json(resource)

                with tab_quality:
                    if not resources_only:
                        st.info("No FHIR resources available.")
                    else:
                        rows = []
                        anomalies_present: dict[str, list[str]] = {}
                        for resource_type, resource in pairs:
                            anomalies = detect_anomalies(resource_type, resource)
                            if anomalies:
                                key = resource.get("id") or resource_type
                                anomalies_present[key] = anomalies
                            rows.append(
                                {
                                    "resourceType": resource_type,
                                    "id": resource.get("id", ""),
                                    "completeness": completeness_score(resource_type, resource),
                                    "anomalies": "; ".join(anomalies),
                                }
                            )
                        _dataframe(rows, use_container_width=True)
                        if anomalies_present:
                            for key, messages in anomalies_present.items():
                                with st.expander(f"Anomalies for {key}", expanded=False):
                                    for message in messages:
                                        st.write(f"- {message}")

                st.divider()
                toggle_callable = getattr(st, "toggle", None)
                if callable(toggle_callable):
                    redact = toggle_callable("De-identify PHI (mask names/IDs)")
                else:
                    redact = st.checkbox("De-identify PHI (mask names/IDs)", value=False)
                safe_pairs = [
                    (resource_type, mask_patient(resource) if redact else resource)
                    for resource_type, resource in pairs
                ]

                st.subheader("Send to FHIR (optional)")
                default_base = os.getenv("FHIR_BASE_URL", "http://localhost:8080/fhir")
                base = st.text_input("FHIR base URL", value=default_base)
                token_default = os.getenv("AUTH_TOKEN", "")
                token = st.text_input("Bearer token (optional)", type="password", value=token_default)

                if st.button("POST all resources"):
                    if not safe_pairs:
                        st.info("No resources to send.")
                    else:
                        client = FHIRClient(base, token or None)
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
                                message = f"{resource_type}: {exc}"
                                errors.append(message)
                                posted.append({"resourceType": resource_type, "status": "error"})
                        if errors:
                            for msg in errors:
                                st.warning(msg)
                        st.success("POST complete" if not errors else "POST attempted with warnings")
                        if posted:
                            st.table(posted)

                st.divider()
                st.subheader("Referral Intake Workflow")
                if st.button("Simulate referral intake workflow"):
                    patient_resource = next((res for rtype, res in pairs if rtype == "Patient"), None)
                    if not patient_resource:
                        st.info("A Patient resource is required to simulate this workflow.")
                    else:
                        workflow = build_referral_intake_workflow(None, patient_resource, {"status": "planned"})
                        context = workflow.run({})
                        audit_event("workflow", "Patient", patient_resource.get("id"), "streamlit-ui")
                        st.json(context)

copilot_agent = st.session_state.get("copilot_agent")
if copilot_agent:
    _render_copilot(copilot_agent)
