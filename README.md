# HL7 to FHIR Converter

A lightweight Python toolkit for turning HL7 v2 messages into deterministic FHIR R4 bundles.

> **Note:** Test data only — no real PHI should be processed with this demo.

## Quickstart

### 1. Activate the virtual environment
- macOS / Linux
  ```bash
  source .venv/bin/activate
  ```
- Windows (PowerShell)
  ```powershell
  .venv\Scripts\Activate.ps1
  ```

### 2. Install dependencies
```bash
pip install -r requirements.txt
```

### 3. Run the command-line converter
```bash
python hl7_to_fhir_miniconverter.py samples/adt_a01.hl7 > bundle.json
```

### 4. Run tests
```bash
pytest -q
```

### 5. Format & lint
```bash
make format
make lint
# or
make all
```

### 6. Launch the Streamlit demo UI
```bash
streamlit run streamlit_app.py
```
*Use the sidebar to pick one of the included samples or upload your own HL7 message, then click **Convert to FHIR** to explore the resulting bundle interactively. You can download the bundle as FHIR JSON for further testing.*

### 7. Run via Docker (optional)
Build and start the Streamlit demo in a container:
```bash
docker build -t hl7-to-fhir:dev .
docker run --rm -p 8501:8501 hl7-to-fhir:dev
```
Or with Docker Compose (includes a simple healthcheck):
```bash
docker compose up --build
```
Then browse to http://localhost:8501.

## What's included
- Robust HL7 v2 → FHIR R4 mappings for ADT/ORU/RDE messages (Patient, Encounter, Observation, DiagnosticReport, MedicationRequest).
- YAML-backed terminology normalization (local codes → SNOMED CT / LOINC).
- Data quality tooling: completeness scoring, anomaly detection, probabilistic record linkage.
- Privacy & audit helpers: patient de-identification toggle, AuditEvent NDJSON log.
- Workflow orchestration agent with a sample referral-intake workflow and minimal FHIR client.
- Streamlit UI with summary chips, quality tab, JSON/NDJSON download, optional POST to a FHIR server.

## Supported message types
- `ADT^A01` / `ADT^A03` → `Patient`, `Encounter`
- `ORU^R01` → `Observation` (with interpretation/status) + `DiagnosticReport`
- `RDE^O11` → `MedicationRequest`

All generated resources include typed references (`Patient/<id>`, `Encounter/<id>`, etc.) so follow-on systems can consume them reliably.

## Batch conversion example
```bash
mkdir -p output
for f in samples/*.hl7; do
  python hl7_to_fhir_miniconverter.py "$f" > "output/$(basename "$f" .hl7).json"
done
```

## Post converted resources
After converting, you can send a resource bundle to a FHIR server (replace `$FHIR_BASE` with your endpoint):
```bash
curl -X POST "$FHIR_BASE/Patient" \
  -H "Content-Type: application/fhir+json" \
  -d @bundle.json
```
