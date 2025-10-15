from hl7_to_fhir_miniconverter import convert_hl7_to_fhir


def test_observation_uses_loinc_mapping():
    hl7 = "\r".join(
        [
            "MSH|^~\\&|LAB|HOSP|EHR|HOSP|202510131200||ORU^R01|12345|P|2.3",
            "PID|1||987654^^^HOSP||Doe^Jane",
            "OBR|1||22222|LABPANEL^Complete Blood Count",
            "OBX|1|NM|HGB^Hemoglobin||13.4|g/dL|12-16|N||F|||202510131159|",
        ]
    )
    pairs = convert_hl7_to_fhir(hl7)
    observations = [res for rtype, res in pairs if rtype == "Observation"]
    assert observations, "Expected at least one Observation"
    obs = observations[0]
    coding = obs["code"]["coding"][0]
    assert coding["code"] == "718-7"
    assert coding["system"] == "http://loinc.org"
    assert obs["valueQuantity"]["value"] == 13.4
