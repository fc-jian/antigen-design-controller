# Workflow, artifacts and recovery

Only 1UBQ is distributed as an integration input. Make each new scientific configuration/output belong to a project, and keep private data ignored. See the [README deployment tutorial](../README.md) for prerequisites.

## Stage boundaries

`validate` parses configuration and structure/mappings without SSH or model imports. `run --dry-run` writes a plan without computation. Use a fresh directory for a real run after a dry-run.

The dependency chain is optional `rfd3` → `mpnn` → `msa` → `fold` → `metrics`; folding-only input starts at MSA. `run --until msa --wait` stops at that boundary. `resume DIRECTORY --only fold --wait` and then `--only metrics --wait` make boundaries explicit. Without `--wait`, rerun resume to continue orchestration; a running scheduler job may outlive the controller.

For folding-only CLI input, save a project-local FASTA file containing the 1UBQ sequence, or supported JSON, and use:

```yaml
schema_version: 1
mode: folding
input: ../inputs/ubiquitin.fasta
output: ../runs/folding-001
folding: {model: protenix-v2, seeds: [101], samples: 1}
```

FASTA records are independent jobs. Use supported AlphaFold 3 / AlphaFold Server / Protenix entity JSON for complexes. Protein, DNA/RNA, CCD/SMILES ligand, modification and bond fields have explicit validation. External MSA, templates, unknown fields and custom CCD are rejected; the pipeline currently searches protein MSA only. JSON dialect support does not mean another prediction model is run.

## Outputs and acceptance

`plan.json` freezes code, configuration, input and environment identities; `mapping.json` records residue correspondence. `manifest.json` is mutable scheduler state. Per-task directories contain native output, logs, normalized artifacts, and `complete.json` receipts. The web project adds a registry, request/config JSON, controller logs, collection state and result indexes. All of these are private generated data by default.

Sequence identity and candidate provenance are separate. Different candidates may share an MSA cache without becoming the same candidate. Receipt hashes, input/query sequences, expected sample indices, chains and mappings are checked. A scheduler state of COMPLETED alone is not accepted as model success.

`report DIRECTORY` creates/reports metrics. `collect DIRECTORY` retrieves the remote deliverables archive and verifies its integrity. Remote-only database/cache artifacts are not misrepresented as locally validated. Keep archives and provenance when moving a project; do not rewrite an old plan to pretend it ran elsewhere.

Optional screening uses an explicit policy:

```bash
.venv/bin/antigen-design report projects/1ubq/runs/ubiquitin --policy projects/1ubq/configs/screening-example.json
```

The example thresholds are engineering demonstrations, not experimental calibration. Policies include a version, model, metric version, gates, objectives and diversity limits. Structural comparisons require matching residue-map contexts; do not merge incompatible maps.

## Explicit external prediction import

`import-results MANIFEST.json OUTPUT_DIRECTORY` imports explicitly named structures and recomputes metrics against an explicit reference. The manifest has `schema_version: 1`, `reference`, and `records`. Each record names `id`, `prediction`, `model`, `mapping` and `reference_design`, with optional `legacy_metadata`. See [the importer](../src/antigen_design/legacy_import.py) and [its regression test](../tests/test_completion.py) for the exact schema. Each mapping position has explicit chain/position/design/reference correspondence; ambiguous automatic sequence alignment is not a replacement for that mapping.

Imported metadata is retained separately; an old reported RMSD is not trusted instead of recomputation. Importing a structure does not claim that this repository executed the external model.

## Recovery

`status DIRECTORY` reads only local state and does not refresh the scheduler. `resume` can refresh and submit tasks. Reconnect after a transport error only within the recorded identity. Source, input, model or environment changes require a new run.

Unknown submission outcomes are handled conservatively. Read the saved task script, job name, work directory, user, attempt and scheduler records. When the exact job is established, explicitly associate it:

```bash
.venv/bin/antigen-design reconcile RUN_DIRECTORY --task TASK_ID --job-id EXACT_JOB_ID
```

`resume --retry-failed` is an explicit compute action, not a read-only repair. Do not retry while any job is live or ambiguous. Do not cancel unrelated jobs. Global rerouting first confirms cancellation of an unstarted allocation; failed/unknown capacity queries do not imply no capacity.

The optional [acceptance driver](../scripts/run_acceptance.py) resumes an existing **real** run through MSA, folding and metrics, then collects and verifies. It can submit computation. The [archive verifier](../scripts/verify_acceptance.py) checks collected artifacts. Neither script is used automatically during installation or documentation checks.
