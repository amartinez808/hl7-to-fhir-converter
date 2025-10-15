"""Simple AuditEvent logger."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

AUDIT_PATH = Path("out") / "audit_events.ndjson"

__all__ = ["audit_event"]


def audit_event(action: str, resource_type: str, resource_id: str | None, who: str | None) -> dict:
    """Emit a lightweight AuditEvent and append it to the NDJSON log."""
    event = {
        "resourceType": "AuditEvent",
        "type": {
            "system": "http://terminology.hl7.org/CodeSystem/audit-event-type",
            "code": action,
        },
        "recorded": datetime.now(timezone.utc).isoformat(),
        "entity": [
            {
                "reference": f"{resource_type}/{resource_id}" if resource_id else resource_type,
                "type": {"text": resource_type},
            }
        ],
        "agent": [{"who": {"display": who or "anonymous"}}],
    }
    AUDIT_PATH.parent.mkdir(parents=True, exist_ok=True)
    with AUDIT_PATH.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(event, ensure_ascii=False) + "\n")
    return event
