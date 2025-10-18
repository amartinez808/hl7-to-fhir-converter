# HL7 to FHIR Converter

A lightweight Python toolkit for turning HL7 v2 messages into deterministic FHIR R4 bundles.

This project is built for interoperability sandboxes and proof-of-concept environments where you want to ingest legacy DoD/VA-style HL7 v2 feeds, normalize them, inspect quality, and optionally hand the converted resources off to a downstream FHIR server.

> **Note:** Test data only — no real PHI should be processed with this demo.

## Feature highlights
- **Robust mappings:** PID, PV1, OBR/OBX, and RXE segments become well-formed `Patient`, `Encounter`, `Observation`, `DiagnosticReport`, and `MedicationRequest` resources with typed references.
- **Terminology normalization:** Lightweight YAML dictionaries map local codes to LOINC and SNOMED so common labs and diagnoses surface with standard vocabularies.
- **Quality guardrails:** Completeness scoring, anomaly detection (future dates, inverted periods, missing codes), and probabilistic record linkage help spot data issues fast.
- **Privacy & audit:** A reusable de-identification helper masks patient names/identifiers, while an `AuditEvent` NDJSON log captures every POST or workflow run (`out/audit_events.ndjson`).
- **Streamlit experience:** Summary chips, JSON/NDJSON download buttons, de-id toggle, POST-to-FHIR controls, and a referral-intake workflow simulator keep exploration self-contained.
- **Workflow ready:** A minimal `FHIRClient` plus orchestration agent show how to stitch “find or create patient” and “create planned encounter” flows together.
- **Conversion copilot:** A conversational panel explains HL7 parsing decisions, highlights missing segments or anomalies, and suggests next steps directly in the Streamlit demo.
- **Container-friendly:** Non-root Docker image with health-checked `docker-compose.yml` makes demos portable.

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
- Privacy & audit helpers: patient de-identification toggle, AuditEvent NDJSON log written to `out/audit_events.ndjson`.
- Workflow orchestration agent with a sample referral-intake workflow and minimal FHIR client.
- Streamlit UI with summary chips, quality tab, JSON/NDJSON download, optional POST to a FHIR server, and workflow simulation.

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

## Streamlit demo tips
- Use the **De-identify PHI** toggle before sharing screenshots or POSTing outside the lab.
- Chat with the **Conversion Copilot** (bottom of the page) to get segment-by-segment explanations, warnings, and recommendation follow-ups after each conversion.
- The **Quality** tab lists completeness scores per resource; expand any red flag to read anomaly details.
- **Download JSON / NDJSON** buttons give you ready-to-share payloads.
- The **Send to FHIR** section uses the built-in `FHIRClient`; audit entries appear in `out/audit_events.ndjson`.
- The **Referral Intake Workflow** simulator shows how the orchestration agent could wire patient lookup and planned encounter creation together. Add your own client instance if you want it to call a real server.
