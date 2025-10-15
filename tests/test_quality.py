from app.quality.anomaly import detect_anomalies
from app.quality.completeness import completeness_score


def test_completeness_score_bounds():
    patient = {
        "resourceType": "Patient",
        "identifier": [{"value": "123"}],
        "name": [{"family": "Doe", "given": ["John"]}],
        "gender": "male",
        "birthDate": "1980-01-01",
    }
    score = completeness_score("Patient", patient)
    assert 0 <= score <= 1
    assert score == 1.0


def test_detects_encounter_period_anomaly():
    encounter = {
        "resourceType": "Encounter",
        "period": {"start": "2025-01-02T00:00:00+00:00", "end": "2025-01-01T00:00:00+00:00"},
    }
    anomalies = detect_anomalies("Encounter", encounter)
    assert any("start occurs after end" in msg for msg in anomalies)


def test_observation_missing_code_anomaly():
    observation = {"resourceType": "Observation", "status": "final", "subject": {"reference": "Patient/1"}}
    anomalies = detect_anomalies("Observation", observation)
    assert "missing a code" in " ".join(anomalies)
