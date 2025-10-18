"""Rule-backed conversational helper for the conversion demo."""

from __future__ import annotations

import random

from dataclasses import dataclass
from typing import Any, Iterable

from .context import ConversionContext, ResourceInsight
from .llm import GPTResponder

DEFAULT_FALLBACK = (
    "I'm still mulling that over. Try asking about specific segments, warnings, or say 'help' for inspiration."
)


@dataclass(slots=True)
class ChatMessage:
    """Represents a single chat message for the Streamlit UI."""

    role: str
    content: str


class ConversionCopilot:
    """Light-weight conversational layer over conversion diagnostics."""

    def __init__(self, context: ConversionContext, llm: GPTResponder | None = None):
        self.context = context
        self.history: list[ChatMessage] = []
        self.persona_name = "ClipFHIR"
        self.llm = llm
        self._bootstrap()

    def _bootstrap(self) -> None:
        summary = self._intro_message()
        self._append("assistant", summary)
        if self.context.warnings:
            warning_lines = "\n".join(f"• {warning}" for warning in self.context.warnings)
            self._append(
                "assistant",
                "Heads-up on the current message:\n" + warning_lines,
            )
        if self.context.errors:
            for err in self.context.errors:
                self._append("assistant", f"Conversion raised an error: {err}")

    def _append(self, role: str, content: str) -> None:
        self.history.append(ChatMessage(role=role, content=content.strip()))

    def _intro_message(self) -> str:
        opening = random.choice(
            [
                "Hey there! ClipFHIR the conversion companion sliding onto your screen.",
                "Hi! ClipFHIR here—your HL7-to-FHIR wingmate.",
                "👋 ClipFHIR checking in. Looks like a fresh bundle is brewing!",
            ]
        )
        return f"{opening}\n\n{self._initial_summary()}"

    def _initial_summary(self) -> str:
        ctx = self.context
        msg_bits: list[str] = []
        if ctx.message_type:
            mt = ctx.message_type
            if ctx.trigger_event:
                mt = f"{mt}^{ctx.trigger_event}"
            msg_bits.append(f"Message type: {mt}")
        segment_total = sum(ctx.segment_counts.values())
        msg_bits.append(f"Parsed {segment_total} segments")
        top_segments = sorted(ctx.segment_counts.items(), key=lambda item: item[1], reverse=True)
        if top_segments:
            top_preview = ", ".join(f"{name}×{count}" for name, count in top_segments[:5])
            msg_bits.append(f"Key segments: {top_preview}")
        resource_lines: list[str] = []
        for insight in ctx.resources:
            summary_parts = [insight.resource_type]
            if insight.identifier:
                summary_parts.append(f"id={insight.identifier}")
            if insight.summary:
                summary_parts.append(
                    ", ".join(f"{k}={v}" for k, v in insight.summary.items())
                )
            if insight.completeness is not None:
                summary_parts.append(f"completeness {insight.completeness:.0%}")
            if insight.anomalies:
                summary_parts.append("anomalies: " + "; ".join(insight.anomalies))
            resource_lines.append("; ".join(summary_parts))
        if not resource_lines:
            resource_lines.append("No FHIR resources produced.")
        summary_body = "\n".join(resource_lines)
        return "\n".join(msg_bits + ["", "FHIR bundle summary:", summary_body])

    def _segment_fact(self, segment_name: str) -> str | None:
        count = self.context.segment_counts.get(segment_name)
        if count:
            return f"There are {count} {segment_name} segment(s) in this message."
        if segment_name in self.context.missing_segments:
            return f"The message is missing a {segment_name} segment, which we normally expect for this trigger event."
        if segment_name in self.context.segment_counts:
            return f"{segment_name} appears zero times after parsing (possible parsing issue)."
        return None

    def _resource_fact(self, resource_type: str) -> str | None:
        matches = [insight for insight in self.context.resources if insight.resource_type.lower() == resource_type.lower()]
        if not matches:
            return f"No {resource_type} resources were generated."
        lines: list[str] = []
        for insight in matches:
            line_bits: list[str] = []
            if insight.identifier:
                line_bits.append(f"id {insight.identifier}")
            if insight.summary:
                line_bits.append(
                    ", ".join(f"{k}={v}" for k, v in insight.summary.items())
                )
            if insight.completeness is not None:
                line_bits.append(f"completeness {insight.completeness:.0%}")
            if insight.anomalies:
                line_bits.append("anomalies: " + "; ".join(insight.anomalies))
            if insight.notes:
                line_bits.extend(insight.notes)
            lines.append("; ".join(line_bits) or "Resource created with no additional details available.")
        return "\n".join(lines)

    def chat(self, question: str) -> str:
        """Generate a reply, appending both the user prompt and assistant response to history."""
        question = question.strip()
        if not question:
            return ""
        self._append("user", question)

        response = self._respond(question)
        self._append("assistant", response)
        return response

    def _respond(self, question: str) -> str:
        rule_reply = self._rule_based_response(question)
        if rule_reply is not None:
            return rule_reply
        if self.llm:
            llm_history = [
                {"role": msg.role if msg.role in {"assistant", "user"} else "assistant", "content": msg.content}
                for msg in self.history[:-1]
                if msg.content
            ]
            llm_reply = self.llm.reply(question, self.context, llm_history)
            if llm_reply:
                return llm_reply
        return DEFAULT_FALLBACK

    def _rule_based_response(self, question: str) -> str | None:
        lowered = question.lower()

        if "warning" in lowered or "issue" in lowered or "problem" in lowered:
            if self.context.warnings:
                return "\n".join(f"• {w}" for w in self.context.warnings)
            if self.context.errors:
                return "\n".join(self.context.errors)
            return "No conversion issues detected."

        if any(greeting in lowered for greeting in ("hi", "hello", "hey", "hiya")):
            return (
                "Hello! ClipFHIR here with a magnifying glass on your bundle.\n"
                "Ask me about segments, resources, or what to double-check before sending downstream."
            )

        if "thanks" in lowered or "thank" in lowered:
            return "Happy to help! I live to make HL7 a little less mysterious. Anything else?"

        if "clippy" in lowered:
            return "Close! I'm ClipFHIR, a distant cousin of Clippy with far better taste in interoperability."

        for segment in ("PID", "PV1", "OBR", "OBX", "MSH", "RXE", "RXO"):
            if segment.lower() in lowered:
                fact = self._segment_fact(segment)
                if fact:
                    return fact

        for resource_name in ("patient", "encounter", "observation", "diagnosticreport", "medicationrequest"):
            if resource_name in lowered:
                fact = self._resource_fact(resource_name)
                if fact:
                    return fact

        if "code" in lowered or "loinc" in lowered or "snomed" in lowered:
            flagged = [insight for insight in self.context.resources if insight.notes]
            if flagged:
                lines: list[str] = []
                for insight in flagged:
                    lines.append(f"{insight.resource_type}: " + "; ".join(insight.notes))
                return "\n".join(lines)
            return "All mapped resources already include preferred terminology codes."

        if "what next" in lowered or "next step" in lowered:
            suggestions = [
                "Review the normalization table if any lab codes are unmapped.",
                "Use the `POST all resources` action to push the bundle to your FHIR test server.",
                "Capture a copy of the anomalies tab for QA records.",
            ]
            return "\n".join(f"• {item}" for item in suggestions)

        if "duration" in lowered or "time" in lowered:
            if self.context.duration_seconds is not None:
                return f"Conversion completed in {self.context.duration_seconds:.2f} seconds."
            return "Elapsed time was not recorded for this run."

        if "help" in lowered or "what can you do" in lowered:
            return (
                "I can spot check segments, recap each FHIR resource, hint at mapping follow-ups, and share suggested next steps.\n"
                "Try asking things like “How did PID map?” or “Any anomalies I should fix?”"
            )

        return None

    def conversation(self) -> Iterable[ChatMessage]:
        return tuple(self.history)

    def as_dicts(self) -> list[dict[str, Any]]:
        return [message.__dict__ for message in self.history]

    @classmethod
    def from_payload(
        cls,
        hl7_text: str,
        conversion_result: Any | None,
        *,
        duration_seconds: float | None = None,
        error: Exception | None = None,
    ) -> "ConversionCopilot":
        from .context import build_context
        from .llm import load_responder_from_env

        context = build_context(
            hl7_text,
            conversion_result,
            duration_seconds=duration_seconds,
            error=error,
        )
        llm = load_responder_from_env()
        return cls(context, llm=llm)


__all__ = ["ChatMessage", "ConversionCopilot"]
