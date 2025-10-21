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
from datetime import date, datetime
from decimal import Decimal
from enum import Enum
from pathlib import Path
from typing import Any
from functools import lru_cache

import streamlit as st

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


st.set_page_config(page_title="HL7 -> FHIR R4 Converter", page_icon="DNA", layout="wide")

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
_init_copilot_if_missing(st.session_state.get("hl7_text", ""))

# Auto-load when sample changes (no upload)
if sample and st.session_state.get("_last_sample") != sample and up is None:
    sample_path = Path("samples", sample)
    try:
        st.session_state.hl7_text = sample_path.read_text(encoding="utf-8")
        st.session_state.input_label = sample
        st.session_state._last_sample = sample
        _reset_copilot(st.session_state.hl7_text)
    except OSError as exc:
        st.warning(f"Unable to read sample {sample}: {exc}")

# Uploaded file overrides
if up is not None:
    uploaded_text = up.read().decode("utf-8", errors="ignore")
    st.session_state.hl7_text = uploaded_text
    st.session_state.input_label = up.name or "uploaded_message"
    st.session_state._last_sample = None
    _reset_copilot(st.session_state.hl7_text)

st.markdown(
    """
    <div class="page-hero">
        <span class="hero-icon">DNA</span>
        <div>
            <h1>HL7 -> FHIR R4 Converter</h1>
            <p>Smarter demo data with instant bundle insights. Test data only -- no real PHI.</p>
        </div>
    </div>
    """,
    unsafe_allow_html=True,
)

primary_col, copilot_col = st.columns([3, 2])
with primary_col:
    st.markdown('<div class="card card-editor">', unsafe_allow_html=True)
    st.markdown("<span class='card-eyebrow'>Message</span>", unsafe_allow_html=True)
    st.markdown("<h3>HL7 payload</h3>", unsafe_allow_html=True)
    st.markdown(
        "<p>Paste or tweak an HL7 v2 message and convert it into a rich FHIR bundle in seconds.</p>",
        unsafe_allow_html=True,
    )
    hl7_text = st.text_area(
        "HL7 message",
        value=st.session_state.hl7_text,
        height=260,
        key="editor",
        label_visibility="collapsed",
        placeholder="MSH|^~\\&|ADT|HORIZON|EHR|HORIZON|202510131005||ADT^A01|A01-10001|P|2.5.1",
    )
    st.session_state.hl7_text = hl7_text

    st.markdown('<div class="action-row">', unsafe_allow_html=True)
    c1, c2 = st.columns([1, 1])
    convert = _button(c1, "Convert to FHIR", type="primary")
    reset = _button(c2, "Reset editor")
    st.markdown("</div>", unsafe_allow_html=True)

    current_label = html.escape(st.session_state.get("input_label", "hl7_message"))
    st.markdown(
        f"<div class='card-footnote'>Source: <strong>{current_label}</strong></div>",
        unsafe_allow_html=True,
    )
    st.markdown("</div>", unsafe_allow_html=True)
with copilot_col:
    copilot_panel = st.container()
result_container = st.container()
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
            with result_container:
                st.markdown('<div class="card card-results">', unsafe_allow_html=True)
                st.markdown("<span class='card-eyebrow'>Conversion</span>", unsafe_allow_html=True)
                st.markdown("<h3>FHIR bundle preview</h3>", unsafe_allow_html=True)

                if not pairs:
                    st.markdown(
                        "<div class='empty-state'>No FHIR resources were produced from this message.</div>",
                        unsafe_allow_html=True,
                    )
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
                        st.markdown(f"<div class='conversion-summary'>{summary_message}</div>", unsafe_allow_html=True)

                    quality_rows: list[dict[str, Any]] = []
                    anomalies_present: dict[str, list[str]] = {}
                    completeness_values: list[float] = []
                    for resource_type, resource in pairs:
                        completeness = completeness_score(resource_type, resource)
                        if completeness is not None:
                            completeness_values.append(float(completeness))
                        anomalies = detect_anomalies(resource_type, resource)
                        if anomalies:
                            key = resource.get("id") or resource_type
                            anomalies_present[key] = anomalies
                        quality_rows.append(
                            {
                                "resourceType": resource_type,
                                "id": resource.get("id", ""),
                                "completeness": completeness,
                                "anomalies": "; ".join(anomalies),
                            }
                        )
                    avg_completeness = (
                        sum(completeness_values) / len(completeness_values) if completeness_values else None
                    )

                    metrics = [
                        ("FHIR resources", str(total)),
                        ("Resource types", str(len(by_type))),
                        (
                            "Avg completeness",
                            f"{avg_completeness:.0%}" if avg_completeness is not None else "--",
                        ),
                        ("Elapsed", f"{elapsed:.2f}s" if elapsed else "--"),
                    ]
                    metric_cols = st.columns(len(metrics))
                    for col, (label, value) in zip(metric_cols, metrics):
                        col.markdown(
                            f"<div class='metric-card'><h4>{html.escape(label)}</h4><span>{html.escape(value)}</span></div>",
                            unsafe_allow_html=True,
                        )

                    chips_html = "".join(
                        f"<span class='pill'><span>{html.escape(rtype)}</span><strong>{count}</strong></span>"
                        for rtype, count in sorted(by_type.items())
                    )
                    if chips_html:
                        st.markdown(f"<div class='pill-row'>{chips_html}</div>", unsafe_allow_html=True)

                    resources_only = [resource for _, resource in pairs]
                    st.markdown("<hr class='section-divider' />", unsafe_allow_html=True)
                    tab_json, tab_res, tab_quality = st.tabs(["FHIR JSON", "Resources", "Quality"])

                    with tab_json:
                        if resources_only:
                            st.json(resources_only)
                            json_payload = json.dumps(
                                resources_only, default=_json_ready, ensure_ascii=False, indent=2
                            )
                            base_name = Path(st.session_state.get("input_label", "hl7_message")).stem
                            st.download_button(
                                "Download JSON",
                                json_payload,
                                f"{base_name}_bundle.json",
                                "application/json",
                                use_container_width=True,
                            )
                            st.download_button(
                                "Download NDJSON",
                                _ndjson(pairs),
                                f"{base_name}_bundle.ndjson",
                                "application/x-ndjson",
                                use_container_width=True,
                            )
                        else:
                            st.markdown(
                                "<div class='empty-state'>No FHIR resources available.</div>",
                                unsafe_allow_html=True,
                            )

                    with tab_res:
                        if not resources_only:
                            st.markdown(
                                "<div class='empty-state'>No FHIR resources available.</div>",
                                unsafe_allow_html=True,
                            )
                        else:
                            grouped: dict[str, list[dict]] = {}
                            for rtype, resource in pairs:
                                grouped.setdefault(rtype, []).append(resource)
                            for rtype, resources in sorted(grouped.items()):
                                st.markdown(f"**{html.escape(rtype)}** ({len(resources)})")
                                for resource in resources:
                                    rid = resource.get("id") or "no-id"
                                    label = f"{rtype} -- {rid}"
                                    with st.expander(label, expanded=False):
                                        st.json(resource)

                    with tab_quality:
                        if not resources_only:
                            st.markdown(
                                "<div class='empty-state'>No FHIR resources available.</div>",
                                unsafe_allow_html=True,
                            )
                        else:
                            _dataframe(quality_rows, use_container_width=True)
                            if anomalies_present:
                                for key, messages in anomalies_present.items():
                                    with st.expander(f"Anomalies -- {key}", expanded=False):
                                        for message in messages:
                                            st.write(f"- {message}")

                    st.markdown("<hr class='section-divider' />", unsafe_allow_html=True)
                    toggle_callable = getattr(st, "toggle", None)
                    if callable(toggle_callable):
                        redact = toggle_callable("De-identify PHI (mask names/IDs)")
                    else:
                        redact = st.checkbox("De-identify PHI (mask names/IDs)", value=False)
                    safe_pairs = [
                        (resource_type, mask_patient(resource) if redact else resource)
                        for resource_type, resource in pairs
                    ]

                    st.markdown("<span class='card-eyebrow'>Share</span>", unsafe_allow_html=True)
                    st.markdown("<h4>Send resources to a FHIR server</h4>", unsafe_allow_html=True)
                    default_base = os.getenv("FHIR_BASE_URL", "http://localhost:8080/fhir")
                    token_default = os.getenv("AUTH_TOKEN", "")
                    col_base, col_token = st.columns([2, 1])
                    base = col_base.text_input("FHIR base URL", value=default_base)
                    token = col_token.text_input("Bearer token (optional)", type="password", value=token_default)
                    if st.button("POST all resources", use_container_width=True):
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

                    st.markdown("<span class='card-eyebrow'>Automation</span>", unsafe_allow_html=True)
                    st.markdown("<h4>Referral intake workflow</h4>", unsafe_allow_html=True)
                    if st.button("Simulate referral intake workflow", use_container_width=True):
                        patient_resource = next((res for rtype, res in pairs if rtype == "Patient"), None)
                        if not patient_resource:
                            st.info("A Patient resource is required to simulate this workflow.")
                        else:
                            workflow = build_referral_intake_workflow(None, patient_resource, {"status": "planned"})
                            context = workflow.run({})
                            audit_event("workflow", "Patient", patient_resource.get("id"), "streamlit-ui")
                            st.json(context)

                st.markdown("</div>", unsafe_allow_html=True)

copilot_agent = st.session_state.get("copilot_agent")
if copilot_agent:
    with copilot_panel:
        _render_copilot(copilot_agent)
