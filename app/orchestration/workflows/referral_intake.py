"""Referral intake workflow steps."""

from __future__ import annotations

from typing import Any, Mapping

from app.adapters.fhir_client import FHIRClient
from app.orchestration.agent import Agent
from app.security.audit import audit_event

__all__ = ["build_referral_intake_workflow"]


def _identifier_value(patient: Mapping[str, Any]) -> str | None:
    identifiers = patient.get("identifier")
    if isinstance(identifiers, list) and identifiers:
        value = identifiers[0].get("value")
        if value:
            return str(value)
    return None


def build_referral_intake_workflow(
    client: FHIRClient | None,
    patient_resource: Mapping[str, Any],
    encounter_template: Mapping[str, Any] | None = None,
) -> Agent:
    """Create an Agent that orchestrates patient lookup and planned encounter creation."""

    def find_or_create_patient(ctx: dict[str, Any]) -> dict[str, Any]:
        patient = dict(patient_resource)
        identifier = _identifier_value(patient)
        if client and identifier:
            try:
                response = client.search("Patient", {"identifier": identifier})
                data = response.json()
                if isinstance(data, Mapping) and data.get("entry"):
                    existing = data["entry"][0].get("resource")
                    if isinstance(existing, Mapping):
                        ctx.setdefault("notes", []).append("Existing patient located.")
                        patient = dict(existing)
                else:
                    create_resp = client.create("Patient", patient_resource)
                    created = create_resp.json()
                    if isinstance(created, Mapping):
                        patient = dict(created)
                        audit_event("create", "Patient", patient.get("id"), "referral-intake")
                        ctx.setdefault("notes", []).append("New patient created.")
            except Exception as exc:  # pragma: no cover - network variability
                ctx.setdefault("warnings", []).append(f"Patient search/create failed: {exc}")
        return {"patient": patient}

    def create_planned_encounter(ctx: dict[str, Any]) -> dict[str, Any]:
        patient = ctx.get("patient")
        if not isinstance(patient, Mapping):
            ctx.setdefault("errors", []).append("No patient in context; cannot create encounter.")
            return {}
        encounter = dict(encounter_template or {})
        encounter.setdefault("resourceType", "Encounter")
        encounter.setdefault("status", "planned")
        encounter.setdefault("class", {"code": "AMB"})
        encounter["subject"] = {"reference": f"Patient/{patient.get('id', 'temp')}"}
        if client:
            try:
                response = client.create("Encounter", encounter)
                created = response.json()
                if isinstance(created, Mapping):
                    encounter = dict(created)
                    audit_event("create", "Encounter", encounter.get("id"), "referral-intake")
                    ctx.setdefault("notes", []).append("Encounter created.")
            except Exception as exc:  # pragma: no cover
                ctx.setdefault("warnings", []).append(f"Encounter create failed: {exc}")
        return {"encounter": encounter}

    return Agent([find_or_create_patient, create_planned_encounter])
