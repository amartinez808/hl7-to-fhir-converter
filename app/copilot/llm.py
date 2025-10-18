"""Optional GPT-backed responder for the conversion copilot."""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from functools import lru_cache
from typing import Sequence

import httpx

from .context import ConversionContext, ResourceInsight

logger = logging.getLogger(__name__)

_SYSTEM_PROMPT = (
    "You are ClipFHIR, a playful but professional assistant who explains HL7 v2 to FHIR R4 conversions. "
    "Keep answers concise (under 6 sentences), cite concrete segment names when available, and highlight any risks. "
    "If protected health information appears, remind the user to mask it."
)


def _safe_summary(insight: ResourceInsight, *, include_phi: bool) -> str:
    summary_items: list[str] = []
    for key, value in insight.summary.items():
        if not include_phi and key in {"name", "identifier_value"}:
            continue
        summary_items.append(f"{key}={value}")
    if insight.completeness is not None:
        summary_items.append(f"completeness={insight.completeness:.0%}")
    if insight.anomalies:
        summary_items.append("anomalies=" + ", ".join(insight.anomalies))
    return "; ".join(summary_items) if summary_items else "no notable attributes"


def _context_snapshot(context: ConversionContext, *, include_phi: bool) -> str:
    segment_overview = ", ".join(f"{seg}:{count}" for seg, count in sorted(context.segment_counts.items())) or "none"
    warning_block = " | ".join(context.warnings) if context.warnings else "none"
    error_block = " | ".join(context.errors) if context.errors else "none"

    resource_lines: list[str] = []
    for insight in context.resources:
        descriptor = _safe_summary(insight, include_phi=include_phi)
        resource_lines.append(f"- {insight.resource_type}: {descriptor}")
    resources_block = "\n".join(resource_lines) if resource_lines else "- none"

    message_type = context.message_type or "unknown"
    trigger = context.trigger_event or ""
    trigger_suffix = f"^{trigger}" if trigger else ""

    return (
        f"Message type: {message_type}{trigger_suffix}\n"
        f"Segments observed: {segment_overview}\n"
        f"Warnings: {warning_block}\n"
        f"Errors: {error_block}\n"
        f"Resources observed:\n{resources_block}\n"
    )


@dataclass(slots=True)
class GPTResponder:
    """Thin wrapper around the OpenAI chat completions endpoint."""

    model: str
    api_key: str
    api_base: str = "https://api.openai.com/v1/chat/completions"
    temperature: float = 0.25
    timeout: float = 20.0
    include_phi: bool = False

    def reply(self, question: str, context: ConversionContext, history: Sequence[dict[str, str]]) -> str | None:
        if not self.api_key or not self.model:
            return None
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }
        context_blob = _context_snapshot(context, include_phi=self.include_phi)
        messages: list[dict[str, str]] = [{"role": "system", "content": _SYSTEM_PROMPT}]
        messages.extend(history[-6:])
        messages.append(
            {
                "role": "user",
                "content": f"Conversion context:\n{context_blob}\nUser question: {question}",
            }
        )
        payload = {
            "model": self.model,
            "temperature": self.temperature,
            "messages": messages,
        }
        try:
            response = httpx.post(self.api_base, json=payload, headers=headers, timeout=self.timeout)
            response.raise_for_status()
            data = response.json()
            choices = data.get("choices") or []
            if not choices:
                return None
            message = choices[0].get("message") or {}
            content = message.get("content")
            return content.strip() if isinstance(content, str) else None
        except Exception as exc:  # noqa: BLE001
            logger.warning("ClipFHIR GPT request failed: %s", exc)
            return None


@lru_cache(maxsize=1)
def load_responder_from_env() -> GPTResponder | None:
    """Create a GPT responder if the environment is configured, else return None."""

    enabled_flag = os.getenv("COPILOT_USE_GPT", "").strip().lower()
    if enabled_flag in {"0", "false", "off"}:
        return None

    api_key = os.getenv("OPENAI_API_KEY")
    if not api_key:
        return None

    model = os.getenv("COPILOT_GPT_MODEL") or os.getenv("OPENAI_MODEL") or "gpt-4o-mini"
    api_base = os.getenv("OPENAI_API_BASE", "https://api.openai.com/v1/chat/completions")
    temperature = float(os.getenv("COPILOT_GPT_TEMPERATURE", "0.25"))
    timeout = float(os.getenv("COPILOT_GPT_TIMEOUT", "20"))
    include_phi = os.getenv("COPILOT_INCLUDE_PHI", "").strip().lower() in {"1", "true", "yes"}

    logger.info("ClipFHIR GPT responder enabled with model=%s base=%s", model, api_base)
    return GPTResponder(
        model=model,
        api_key=api_key,
        api_base=api_base,
        temperature=temperature,
        timeout=timeout,
        include_phi=include_phi,
    )
