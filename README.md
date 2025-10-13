# HL7 to FHIR Converter

A lightweight Python tool for converting HL7 v2 messages to FHIR R4.

## Quickstart

### 1) Activate virtual environment
**macOS/Linux**
```bash
source .venv/bin/activate


Windows (PowerShell)

.venv\Scripts\Activate.ps1

2) Install dependencies
pip install -r requirements.txt

3) Run the converter
python hl7_to_fhir_miniconverter.py samples/adt_a01.hl7 > bundle.json

4) Run tests
pytest -q

5) Format & lint
make format
make lint
# or:
make all

Supported Types

ADT^A01/A03 → Patient + Encounter

ORU^R01 → Observation(s) + DiagnosticReport

RDE^O11 → MedicationRequest

Batch example
mkdir -p output
for f in samples/*.hl7; do
  python hl7_to_fhir_miniconverter.py "$f" > "output/$(basename "$f" .hl7).json"
done

