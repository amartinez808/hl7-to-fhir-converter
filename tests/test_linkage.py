from app.quality.linkage import is_duplicate, match


def test_match_scores_identical_records():
    score = match(
        ("John Doe", "John Doe"),
        ("1980-01-01", "1980-01-01"),
        ("12345", "12345"),
        ("123 Main St, City", "123 Main St, City"),
    )
    assert score == 1.0


def test_is_duplicate_detects_similar_patients():
    patient_a = {
        "resourceType": "Patient",
        "identifier": [{"value": "12345"}],
        "name": [{"family": "Doe", "given": ["John"]}],
        "birthDate": "1980-01-01",
        "address": [{"line": ["123 Main St"], "city": "Anytown", "state": "CA", "postalCode": "12345"}],
    }
    patient_b = {
        "resourceType": "Patient",
        "identifier": [{"value": "12345"}],
        "name": [{"family": "Doe", "given": ["Johnathan"]}],
        "birthDate": "1980-01-01",
        "address": [{"line": ["123 Main Street"], "city": "Anytown", "state": "CA", "postalCode": "12345"}],
    }
    assert is_duplicate(patient_a, patient_b, threshold=0.8)


def test_is_duplicate_rejects_distinct_patients():
    patient_a = {
        "resourceType": "Patient",
        "identifier": [{"value": "12345"}],
        "name": [{"family": "Doe", "given": ["John"]}],
    }
    patient_b = {
        "resourceType": "Patient",
        "identifier": [{"value": "98765"}],
        "name": [{"family": "Smith", "given": ["Jane"]}],
    }
    assert not is_duplicate(patient_a, patient_b)
