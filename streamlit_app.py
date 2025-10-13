"""
Streamlit interface for the HL7 → FHIR mini-converter.

Run with:
    streamlit run streamlit_app.py
"""

from __future__ import annotations

import json
from pathlib import Path

import streamlit as st

from hl7_to_fhir_miniconverter import convert_hl7_to_fhir_bundle

SAMPLES_DIR = Path("samples")


def _load_sample(name: str) -> str:
    path = SAMPLES_DIR / name
    return path.read_text(encoding="utf-8")


def _list_samples() -> list[str]:
    return sorted(p.name for p in SAMPLES_DIR.glob("*.hl7"))


def _as_json(data: dict) -> str:
    return json.dumps(data, indent=2)


st.set_page_config(page_title="HL7 → FHIR Demo", page_icon="🩺", layout="wide")

st.title("HL7 v2 → FHIR R4 Converter Demo")
st.markdown(
    """
    Upload an HL7 v2 message or start from one of the bundled samples, then convert it into
    a deterministic FHIR R4 bundle. Each run produces typed references (e.g., `Patient/<id>`)
    so downstream systems can ingest or persist the data without additional reconciliation.
    """
)

samples = _list_samples()
with st.sidebar:
    st.header("Message Input")
    chosen_sample = None
    if samples:
        default_sample = samples[0]
        chosen_sample = st.selectbox(
            "Sample HL7 message", samples, index=samples.index(default_sample)
        )
    uploaded = st.file_uploader("Or upload your own", type=["hl7", "txt"])
    st.sidebar.info(
        "Need inspiration? Pick a sample to pre-fill the editor, or upload a custom "
        "HL7 v2 message captured from another system."
    )

hl7_content = ""
input_label = ""
if uploaded is not None:
    hl7_content = uploaded.getvalue().decode("utf-8")
    input_label = uploaded.name
elif chosen_sample:
    hl7_content = _load_sample(chosen_sample)
    input_label = chosen_sample

hl7_content = st.text_area(
    "HL7 message",
    value=hl7_content,
    height=260,
    placeholder="Paste or upload an HL7 v2 message here to convert it to FHIR...",
)

col_convert, col_reset = st.columns([1, 1], gap="small")
convert_clicked = col_convert.button("Convert to FHIR", type="primary")
if col_reset.button("Reset editor"):
    st.experimental_rerun()

if convert_clicked:
    if not hl7_content.strip():
        st.warning("Please provide HL7 content before converting.")
    else:
        with st.spinner("Parsing HL7 message and building FHIR bundle..."):
            try:
                bundle = convert_hl7_to_fhir_bundle(hl7_content)
                bundle_dict = bundle.dict(exclude_none=True)
            except Exception as exc:  # noqa: BLE001
                st.error(f"Unable to convert HL7 message: {exc}")
            else:
                entries = bundle_dict.get("entry", [])
                resources = [entry["resource"] for entry in entries if "resource" in entry]

                resource_groups: dict[str, list[dict]] = {}
                for res in resources:
                    resource_groups.setdefault(res.get("resourceType", "Unknown"), []).append(res)

                st.success(
                    f"Generated {len(resources)} FHIR resources across "
                    f"{len(resource_groups)} resource type(s)."
                )

                download_name = Path(input_label or "hl7_message").stem + "_bundle.json"
                st.download_button(
                    "Download FHIR Bundle",
                    data=_as_json(bundle_dict),
                    file_name=download_name,
                    mime="application/fhir+json",
                )

                tabs = st.tabs(["FHIR JSON", "Resources by Type"])

                with tabs[0]:
                    st.json(bundle_dict)

                with tabs[1]:
                    if not resource_groups:
                        st.info("No FHIR resources were produced from this message.")
                    else:
                        for rtype, items in resource_groups.items():
                            with st.expander(f"{rtype} ({len(items)})", expanded=True):
                                for idx, resource in enumerate(items, start=1):
                                    resource_id = resource.get("id", f"{rtype}-{idx}")
                                    st.markdown(f"**{resource_id}**")
                                    st.json(resource)
