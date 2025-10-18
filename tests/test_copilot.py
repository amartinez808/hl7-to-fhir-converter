from __future__ import annotations

from pathlib import Path

import pytest

from app.copilot import ConversionCopilot, build_context
from hl7_to_fhir_miniconverter import convert_hl7_to_fhir_bundle

SAMPLES = Path("samples")


def load_sample(name: str) -> str:
    return (SAMPLES / name).read_text(encoding="utf-8")


def test_build_context_for_oru_message():
    hl7_text = load_sample("lab_oru_r01.hl7")
    bundle = convert_hl7_to_fhir_bundle(hl7_text)
    context = build_context(hl7_text, bundle, duration_seconds=0.25)

    assert context.success is True
    assert context.message_type == "ORU"
    assert context.segment_counts.get("OBX") == 1
    assert any(insight.resource_type == "Observation" for insight in context.resources)
    assert context.duration_seconds == pytest.approx(0.25)


def test_context_warns_when_segment_missing():
    hl7_missing = "\r".join(
        [
            "MSH|^~\\&|ADT|HOSP|EHR|HOSP|202510131000||ADT^A01|10001|P|2.3",
            "PID|1||123456^^^HOSP||Doe^John",
            "",  # Ensures trailing delimiter handling
        ]
    )
    bundle = convert_hl7_to_fhir_bundle(hl7_missing)
    context = build_context(hl7_missing, bundle)

    assert "PV1" in ",".join(context.missing_segments)
    assert any("Missing recommended segments" in warning for warning in context.warnings)


def test_copilot_answers_segment_questions():
    hl7_text = load_sample("lab_oru_r01.hl7")
    bundle = convert_hl7_to_fhir_bundle(hl7_text)
    context = build_context(hl7_text, bundle)
    copilot = ConversionCopilot(context)

    initial_messages = list(copilot.conversation())
    assert initial_messages, "Copilot should seed an introductory summary."

    reply = copilot.chat("Where did the OBX segment end up?")
    assert "OBX" in reply or "segment" in reply

    warning_reply = copilot.chat("Any warnings I should know about?")
    assert warning_reply  # Should return a helpful message even if empty
