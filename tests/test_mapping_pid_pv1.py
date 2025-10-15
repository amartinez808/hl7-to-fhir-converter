from pathlib import Path

from hl7_to_fhir_miniconverter import convert_hl7_to_fhir


def _get_resource(pairs, resource_type):
    return next((res for rtype, res in pairs if rtype == resource_type), None)


def test_patient_and_encounter_present():
    text = Path("samples/adt_a01.hl7").read_text(encoding="utf-8")
    pairs = convert_hl7_to_fhir(text)
    patient = _get_resource(pairs, "Patient")
    encounter = _get_resource(pairs, "Encounter")
    assert patient is not None
    assert encounter is not None
    assert patient["identifier"][0]["value"] == "123456"
    assert encounter["subject"]["reference"].startswith("Patient/")
    assert encounter["class"][0]["coding"][0]["code"] in {"IMP", "AMB", "EMER", "UNK"}
