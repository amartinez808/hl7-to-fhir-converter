"""
Streamlit interface for the HL7 → FHIR mini-converter.

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
from datetime import date, datetime
from decimal import Decimal
from enum import Enum
from pathlib import Path
from typing import Any
from functools import lru_cache

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


def _copilot_messages_html(messages: list) -> str:
    if not messages:
        return "<div class='copilot-empty'>Ask me anything about this bundle!</div>"
    bubbles: list[str] = []
    for message in messages:
        role = "assistant" if message.role != "user" else "user"
        role_label = "ClipFHIR" if role == "assistant" else "You"
        safe_lines = "<br/>".join(html.escape(line) for line in message.content.splitlines())
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
    popover_fn = getattr(st, "popover", None)
    if callable(popover_fn):
        with popover_fn("✨ ClipFHIR Copilot", help="A playful helper that explains each conversion step"):
            st.markdown(
                f"""
                <div class="copilot-mascot">
                    <img src="{_copilot_avatar_uri()}" alt="ClipFHIR mascot" />
                    <div class="copilot-mascot-text">
                        <strong>ClipFHIR</strong>
                        <span>It looks like you're transforming HL7!</span>
                    </div>
                </div>
                """,
                unsafe_allow_html=True,
            )
            status_label = "GPT-powered responses enabled" if getattr(agent, "llm", None) else "Offline rule-based responses"
            st.markdown(f"<div class='copilot-status'>{status_label}</div>", unsafe_allow_html=True)
            st.markdown(f"<div class='copilot-scroll'>{_copilot_messages_html(chat_messages)}</div>", unsafe_allow_html=True)
            with st.form("copilot_popover_form", clear_on_submit=False):
                prompt_value = st.text_input(
                    "Ask ClipFHIR something",
                    value=st.session_state.get("copilot_popover_input", ""),
                    key="copilot_popover_input",
                    placeholder="e.g. Where did the OBX go?",
                )
                submitted = st.form_submit_button("Send", use_container_width=True)
            if submitted and prompt_value.strip():
                agent.chat(prompt_value)
                st.session_state["copilot_agent"] = agent
                st.session_state["copilot_popover_input"] = ""
                _rerun_app()
    elif hasattr(st, "chat_message"):
        st.divider()
        st.subheader("ClipFHIR Copilot")
        status_label = "GPT-powered responses enabled" if getattr(agent, "llm", None) else "Offline rule-based responses"
        st.caption(status_label)
        for message in chat_messages:
            role = message.role if message.role in {"assistant", "user"} else "assistant"
            avatar = _copilot_avatar_uri() if role == "assistant" else "👩‍💻"
            with st.chat_message(role, avatar=avatar):
                st.markdown(message.content)
        chat_input_fn = getattr(st, "chat_input", None)
        prompt = chat_input_fn("Ask ClipFHIR about this conversion") if callable(chat_input_fn) else None
        if prompt:
            agent.chat(prompt)
            st.session_state["copilot_agent"] = agent
            _rerun_app()
    else:
        st.divider()
        st.subheader("ClipFHIR Copilot")
        status_label = "GPT-powered responses enabled" if getattr(agent, "llm", None) else "Offline rule-based responses"
        st.caption(status_label)
        for message in chat_messages:
            st.markdown(f"**{message.role.title()}:** {message.content}")
        with st.form("copilot_fallback"):
            prompt = st.text_area("Ask ClipFHIR about this conversion", key="copilot_prompt")
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
