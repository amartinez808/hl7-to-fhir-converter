import json
from pathlib import Path

from hl7_to_fhir_miniconverter import convert_hl7_to_fhir_bundle

SAMPLES = Path("samples")


def _bundle_for(name: str):
    text = (SAMPLES / name).read_text()
    b = convert_hl7_to_fhir_bundle(text)
    data = b.dict(exclude_none=True)
    assert data["type"] == "collection"
    return data


def _find(resources, rtype):
    return [e["resource"] for e in resources if e["resource"]["resourceType"] == rtype]


def test_adt_a01_patient_encounter_refs():
    b = _bundle_for("adt_a01.hl7")
    pats = _find(b["entry"], "Patient")
    encs = _find(b["entry"], "Encounter")
    assert pats and encs
    assert pats[0]["identifier"][0]["value"] == "123456"
    # typed refs
    subj = encs[0]["subject"]["reference"]
    assert subj.startswith("Patient/")
    # in-progress with start
    assert encs[0]["status"] in {"in-progress", "arrived", "triaged"}
    assert "start" in encs[0]["period"]


def test_adt_a03_finished():
    b = _bundle_for("adt_a03.hl7")
    encs = _find(b["entry"], "Encounter")
    assert encs
    assert encs[0]["status"] in {"finished", "completed", "unknown"}
    assert "end" in encs[0]["period"]


def test_lab_oru_observation_and_report_semantics():
    b = _bundle_for("lab_oru_r01.hl7")
    obs = _find(b["entry"], "Observation")
    assert any(o["code"]["coding"][0]["code"] == "2345-7" for o in obs)
    o = next(o for o in obs if o["code"]["coding"][0]["code"] == "2345-7")
    assert o["status"] in {"final", "amended", "preliminary", "unknown"}
    assert "effectiveDateTime" in o
    dr = _find(b["entry"], "DiagnosticReport")
    assert dr and dr[0]["result"]


def test_vitals_multiple_obx_and_flags():
    b = _bundle_for("vitals_oru_r01.hl7")
    obs = _find(b["entry"], "Observation")
    codes = {o["code"]["coding"][0]["code"] for o in obs}
    assert {"8310-5", "8480-6", "8462-4"} <= codes
    assert any("interpretation" in o for o in obs)


def test_med_request_rde_o11():
    b = _bundle_for("med_rde_o11.hl7")
    mrs = _find(b["entry"], "MedicationRequest")
    assert mrs
    mr = mrs[0]
    assert "Lisinopril" in mr["medicationCodeableConcept"]["text"]
    di = mr["dosageInstruction"][0]
    assert "timing" in di or di.get("text")


def test_cli_json_serializable():
    data = _bundle_for("lab_oru_r01.hl7")
    json.dumps(data)
