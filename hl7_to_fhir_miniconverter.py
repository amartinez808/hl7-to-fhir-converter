#!/usr/bin/env python3
"""
HL7 v2 → FHIR R4 (minimal) converter

Supported mappings:
- ADT^A01 / ADT^A03 → Patient (+ Encounter with start/end/status)
- ORU^R01 → Observation(s) (+ DiagnosticReport)
  * OBX-11 result status → FHIR Observation.status (F→final, P→preliminary, C→amended)
  * OBX-14 observation time → Observation.effectiveDateTime
  * OBX-8 flags (H/L/N) → Observation.interpretation
- RDE^O11 → MedicationRequest (dose/unit/route/freq)

CLI:
  python hl7_to_fhir_miniconverter.py path/to/message.hl7 > bundle.json
  # or:
  cat msg.hl7 | python hl7_to_fhir_miniconverter.py
"""
from __future__ import annotations

import json
import sys
from collections.abc import Iterable
from copy import deepcopy
from datetime import datetime, timezone

from fhir.resources.address import Address

# FHIR resources (R4)
from fhir.resources.bundle import Bundle
from fhir.resources.codeableconcept import CodeableConcept
from fhir.resources.codeablereference import CodeableReference
from fhir.resources.coding import Coding
from fhir.resources.diagnosticreport import DiagnosticReport
from fhir.resources.dosage import Dosage, DosageDoseAndRate
from fhir.resources.encounter import Encounter, EncounterParticipant
from fhir.resources.humanname import HumanName
from fhir.resources.medicationrequest import MedicationRequest
from fhir.resources.observation import Observation
from fhir.resources.patient import Patient
from fhir.resources.period import Period
from fhir.resources.quantity import Quantity
from fhir.resources.reference import Reference
from fhir.resources.timing import Timing
from hl7apy.core import Segment
from hl7apy.parser import parse_message


# --------------------------
# Helpers
# --------------------------
def comp(field: str | None, idx: int) -> str | None:
    """Return ^-component idx (1-based) of a field string, or None."""
    if not field:
        return None
    parts = str(field).split("^")
    if 1 <= idx <= len(parts):
        return parts[idx - 1] or None
    return None


def as_dt_iso(ts: str | None) -> str | None:
    """Convert HL7 TS (YYYYMMDD[HHMM[SS]]) to ISO8601."""
    if not ts:
        return None
    ts = str(ts)
    fmt = {14: "%Y%m%d%H%M%S", 12: "%Y%m%d%H%M", 8: "%Y%m%d"}.get(len(ts))
    if not fmt:
        return None
    try:
        dt = datetime.strptime(ts, fmt)
    except ValueError:
        return None
    if len(ts) > 8:
        return dt.replace(tzinfo=timezone.utc).isoformat()
    return dt.date().isoformat()


def gender_map(g: str | None) -> str | None:
    return {"M": "male", "F": "female", "O": "other", "U": "unknown"}.get(str(g or "").upper())


def iter_segments(node) -> Iterable[Segment]:
    for child in getattr(node, "children", []):
        if isinstance(child, Segment):
            yield child
        else:
            yield from iter_segments(child)


def first(segs: Iterable[Segment], name: str) -> Segment | None:
    return next((s for s in segs if s.name == name), None)


def segment_fields(segment: Segment) -> list[str]:
    return segment.to_er7().split("|")


def field_value(segment: Segment, fields: list[str], idx: int) -> str | None:
    if idx <= 0:
        return None
    real_idx = idx - 1 if segment.name == "MSH" else idx
    if real_idx <= 0:
        return None
    if real_idx < len(fields):
        value = fields[real_idx]
    elif real_idx == len(fields) and fields:
        value = fields[-1]
    else:
        return None
    return value or None


def msg_type_and_event(msh: Segment) -> tuple[str | None, str | None]:
    """Return (message_type, trigger_event) from MSH-9: e.g., ('ADT','A01')."""
    fields = segment_fields(msh)
    mt = field_value(msh, fields, 9)
    return comp(mt, 1), comp(mt, 2)


def normalize_hl7(text: str) -> str:
    """
    Normalize input to use carriage returns between segments for hl7apy.
    - Convert CRLF -> LF -> CR
    - Convert literal backslash-r sequences ('\\r') to real CR
    """
    t = text.replace("\r\n", "\n").replace("\n", "\r")
    t = t.replace("\\r", "\r")
    return t.strip("\r\n")


# --------------------------
# Builders
# --------------------------
def build_patient(pid: Segment) -> tuple[Patient, str]:
    fields = segment_fields(pid)
    pid3 = field_value(pid, fields, 3)
    mrn = comp(pid3, 1) or comp(pid3, 4) or "unknown"

    name_raw = field_value(pid, fields, 5)
    family = comp(name_raw, 1)
    given = comp(name_raw, 2)

    addr_raw = field_value(pid, fields, 11)
    line = comp(addr_raw, 1)
    city = comp(addr_raw, 3)
    state = comp(addr_raw, 4)
    postal = comp(addr_raw, 5)

    birth_date = None
    birth_raw = field_value(pid, fields, 7)
    if birth_raw and birth_raw.isdigit() and len(birth_raw) >= 8:
        birth_date = datetime.strptime(birth_raw[:8], "%Y%m%d").date().isoformat()

    pat_id = f"pat-{mrn}"
    patient = Patient(
        id=pat_id,
        identifier=[{"system": "urn:sys:HOSP", "value": mrn}],
        name=[HumanName(family=family, given=[given] if given else None)],
        gender=gender_map(field_value(pid, fields, 8)),
        birthDate=birth_date,
        address=[Address(line=[line] if line else None, city=city, state=state, postalCode=postal)],
    )
    return patient, mrn


def build_encounter(
    pv1: Segment, msh: Segment, mrn: str, msg_event: str | None, patient_ref: Reference
) -> Encounter:
    pv1_fields = segment_fields(pv1)
    msh_fields = segment_fields(msh)

    # Encounter class from PV1-2
    cls_code = (field_value(pv1, pv1_fields, 2) or "U").upper()
    class_map = {
        "I": ("IMP", "inpatient encounter"),
        "O": ("AMB", "ambulatory"),
        "E": ("EMER", "emergency"),
    }
    code, display = class_map.get(cls_code, ("UNK", "unknown"))

    loc_raw = field_value(pv1, pv1_fields, 3)
    loc_text = (
        "/".join([p for p in (comp(loc_raw, 1), comp(loc_raw, 2), comp(loc_raw, 3)) if p]) or None
    )

    att_raw = field_value(pv1, pv1_fields, 7)
    participant = None
    if att_raw:
        participant = EncounterParticipant(
            actor=Reference(
                display=" ".join([v for v in [comp(att_raw, 3), comp(att_raw, 2)] if v])
            )
        )

    msg_ts_raw = field_value(msh, msh_fields, 7) or "na"
    msg_time = as_dt_iso(msg_ts_raw)
    control_id = field_value(msh, msh_fields, 10) or "noctrl"
    enc_id = f"enc-{mrn}-{control_id}"

    status = "in-progress"
    actual_period: Period | None = Period(start=msg_time) if msg_time else None
    if (msg_event or "").upper() == "A03":
        status = "finished"
        actual_period = Period(end=msg_time) if msg_time else None

    return Encounter(
        id=enc_id,
        status=status,
        class_fhir=[
            CodeableConcept(
                coding=[
                    Coding(
                        system="http://terminology.hl7.org/CodeSystem/v3-ActCode",
                        code=code,
                        display=display,
                    )
                ],
                text=display,
            )
        ],
        subject=patient_ref,
        actualPeriod=actual_period,
        participant=[participant] if participant else None,
        location=[{"location": {"display": loc_text}}] if loc_text else None,
    )


def obx_status_map(v: str | None) -> str:
    m = {"F": "final", "C": "amended", "P": "preliminary"}
    return m.get((v or "").upper(), "unknown")


def obx_flag_to_code(v: str | None) -> tuple[str, str] | None:
    mapping = {"H": ("H", "High"), "L": ("L", "Low"), "N": ("N", "Normal")}
    if not v:
        return None
    return mapping.get(v.upper())


def build_observation_from_obx(
    obx: Segment, patient_ref: Reference, enc_ref: Reference | None, mrn: str, idx: int
) -> Observation:
    fields = segment_fields(obx)
    obx3 = field_value(obx, fields, 3)
    loinc_code = comp(obx3, 1)
    loinc_text = comp(obx3, 2)

    value_type = (field_value(obx, fields, 2) or "ST").upper()
    val = field_value(obx, fields, 5)
    units_raw = field_value(obx, fields, 6)
    unit = comp(units_raw, 1) or units_raw
    ref_range = field_value(obx, fields, 7)
    flag = field_value(obx, fields, 8)
    status = obx_status_map(field_value(obx, fields, 11))
    eff = as_dt_iso(field_value(obx, fields, 14))

    obs_id = f"obs-{mrn}-{(loinc_code or 'unk')}-{idx:02d}"
    obs = Observation(
        id=obs_id,
        status=status,
        code=CodeableConcept(
            coding=[Coding(system="http://loinc.org", code=loinc_code, display=loinc_text)],
            text=loinc_text or loinc_code,
        ),
        subject=patient_ref,
        encounter=enc_ref,
        effectiveDateTime=eff,
    )

    if value_type == "NM" and val is not None:
        try:
            obs.valueQuantity = Quantity(value=float(val), unit=unit)
        except ValueError:
            obs.valueString = val
    elif value_type in ("TX", "FT", "ST"):
        obs.valueString = val
    else:
        obs.valueString = val

    if ref_range:
        obs.referenceRange = [{"text": ref_range}]
    interp = obx_flag_to_code(flag)
    if interp:
        code, display = interp
        obs.interpretation = [
            CodeableConcept(
                coding=[
                    Coding(
                        system="http://terminology.hl7.org/CodeSystem/v3-ObservationInterpretation",
                        code=code,
                        display=display,
                    )
                ],
                text=display,
            )
        ]
    return obs


def build_diag_report_from_obrs(
    obrs: list[Segment],
    patient_ref: Reference,
    enc_ref: Reference | None,
    obs_list: list[Observation],
) -> DiagnosticReport:
    panel_code = panel_text = None
    if obrs:
        obr = obrs[0]
        obr_fields = segment_fields(obr)
        obr4 = field_value(obr, obr_fields, 4)
        panel_code = comp(obr4, 1)
        panel_text = comp(obr4, 2)

    dr = DiagnosticReport(
        status="final",
        code=CodeableConcept(
            coding=[Coding(system="http://loinc.org", code=panel_code, display=panel_text)],
            text=panel_text or panel_code,
        ),
        subject=patient_ref,
        encounter=enc_ref,
        result=[
            Reference(reference=f"Observation/{o.id}") for o in obs_list if getattr(o, "id", None)
        ],
    )
    return dr


def build_med_request_from_rxe(rxe: Segment, patient_ref: Reference) -> MedicationRequest:
    # Example RXE: RXE|^^^12345^Lisinopril|10|mg|PO|QD
    fields = segment_fields(rxe)
    drug_raw = field_value(rxe, fields, 2)
    if not drug_raw or "^" not in (drug_raw or ""):
        drug_raw = field_value(rxe, fields, 1) or field_value(rxe, fields, 4)
    med_code = comp(drug_raw, 4) or comp(drug_raw, 1)
    med_text = comp(drug_raw, 5) or comp(drug_raw, 2) or "Medication"

    dose = None
    dose_raw = field_value(rxe, fields, 3) or field_value(rxe, fields, 2)
    if dose_raw:
        try:
            dose = float(dose_raw)
        except ValueError:
            dose = None

    unit_raw = field_value(rxe, fields, 4) or field_value(rxe, fields, 3)
    unit = comp(unit_raw, 1) or unit_raw or "mg"

    route = field_value(rxe, fields, 4) or field_value(rxe, fields, 5)
    freq = field_value(rxe, fields, 5) or field_value(rxe, fields, 6)
    timing = Timing(code=CodeableConcept(text=freq)) if freq else None

    dose_and_rate = (
        [DosageDoseAndRate(doseQuantity=Quantity(value=dose, unit=unit))]
        if dose is not None
        else None
    )

    text_parts = [
        str(dose) if dose is not None else "",
        unit or "",
        route or "",
        freq or "",
    ]
    dosage = Dosage(
        text=" ".join([p for p in text_parts if p]).strip() or None,
        route=CodeableConcept(text=route) if route else None,
        doseAndRate=dose_and_rate,
        timing=timing,
    )

    return MedicationRequest(
        status="active",
        intent="order",
        subject=patient_ref,
        medication=CodeableReference(
            concept=CodeableConcept(coding=[Coding(code=med_code, display=med_text)], text=med_text)
        ),
        dosageInstruction=[dosage],
    )


def _post_process_bundle_dict(data: dict) -> dict:
    result = deepcopy(data)
    for entry in result.get("entry", []):
        resource = entry.get("resource")
        if not isinstance(resource, dict):
            continue
        rtype = resource.get("resourceType")
        if rtype == "Encounter":
            actual = resource.get("actualPeriod")
            if actual and "period" not in resource:
                resource["period"] = actual
        elif rtype == "MedicationRequest":
            medication = resource.get("medication")
            if isinstance(medication, dict):
                concept = medication.get("concept")
                if concept and "medicationCodeableConcept" not in resource:
                    resource["medicationCodeableConcept"] = concept
    return _json_ready(result)


def _json_ready(value):
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, list):
        return [_json_ready(v) for v in value]
    if isinstance(value, dict):
        return {k: _json_ready(v) for k, v in value.items()}
    return value


class BundleResult:
    def __init__(self, bundle: Bundle):
        self._bundle = bundle

    def dict(self, *args, **kwargs) -> dict:
        data = self._bundle.dict(*args, **kwargs)
        return _post_process_bundle_dict(data)

    def __getattr__(self, item):
        return getattr(self._bundle, item)


# --------------------------
# Orchestrator
# --------------------------
def convert_hl7_to_fhir_bundle(hl7_text: str) -> BundleResult:
    normalized = normalize_hl7(hl7_text)
    msg = parse_message(normalized, validation_level=2)
    segs = list(iter_segments(msg))
    msh = first(segs, "MSH")
    pid = first(segs, "PID")
    if not (msh and pid):
        raise ValueError("MSH and PID are required in this demo converter")

    message_type, event = msg_type_and_event(msh)

    patient, mrn = build_patient(pid)
    patient_ref = Reference(reference=f"Patient/{patient.id}")
    entries = [{"resource": patient.dict(exclude_none=True)}]

    pv1 = first(segs, "PV1")
    enc_ref: Reference | None = None
    if pv1:
        enc = build_encounter(pv1, msh, mrn, event, patient_ref)
        entries.append({"resource": enc.dict(exclude_none=True)})
        enc_ref = Reference(reference=f"Encounter/{enc.id}")

    # Observations & DiagnosticReport
    obrs = [s for s in segs if s.name == "OBR"]
    obxs = [s for s in segs if s.name == "OBX"]
    obs_resources: list[Observation] = []
    if obxs:
        for i, obx in enumerate(obxs, 1):
            o = build_observation_from_obx(obx, patient_ref, enc_ref, mrn, i)
            obs_resources.append(o)
            entries.append({"resource": o.dict(exclude_none=True)})
        if obrs:
            dr = build_diag_report_from_obrs(obrs, patient_ref, enc_ref, obs_resources)
            entries.append({"resource": dr.dict(exclude_none=True)})

    # MedicationRequest
    rxe = first(segs, "RXE")
    if rxe:
        mr = build_med_request_from_rxe(rxe, patient_ref)
        entries.append({"resource": mr.dict(exclude_none=True)})

    bundle = Bundle(type="collection", entry=entries)
    return BundleResult(bundle)


def main():
    if sys.stdin.isatty() and len(sys.argv) < 2:
        print(
            "Usage: python hl7_to_fhir_miniconverter.py <hl7_file> | "
            "cat msg.hl7 | python hl7_to_fhir_miniconverter.py",
            file=sys.stderr,
        )
        sys.exit(2)

    hl7_text = (
        open(sys.argv[1], encoding="utf-8").read() if len(sys.argv) >= 2 else sys.stdin.read()
    )
    bundle = convert_hl7_to_fhir_bundle(hl7_text)
    print(json.dumps(bundle.dict(exclude_none=True), indent=2))


if __name__ == "__main__":
    main()
