# Agent instructions

## Scope and entry points

Maintain this portable open-source Python application and its bundled Antigen Studio web UI. Use human ubiquitin PDB 1UBQ for integration examples. Do not import historical biological cases, private run archives, credentials or site inventories. Do not optimize biological activity or infer experimental function from computational scores.

Start with `git status --short`. Preserve unrelated edits. Read only the relevant source and documentation:

| Task | Entry point |
| --- | --- |
| Install, connect SSH or configure backends | [README](README.md), [configuration](docs/configuration.md) |
| Run stages, collect, import, recover | [workflow](docs/workflow.md) |
| Resource routing or multiple storage domains | [advanced deployment](docs/advanced-deployment.md) |
| Web UI/server changes | `src/antigen_design/web/`, `web_ui/`, `web_store.py`, `webapp.py` |
| Report verification | [verification](docs/verification.md) |

No vendor-specific agent runtime or plugin is required. Do not launch subagents unless the user explicitly requests them.

## Portability and configuration

- Controllers and workers run under Linux/WSL. Work from this checkout; never assume a specific drive, username, machine, SSH alias or absolute repository path.
- Use `.venv/bin/python` and `.venv/bin/antigen-design`. Native Windows is an editing/SSH bridge environment only.
- Keep runtime parameters in configuration files. Backend credentials belong to the operator's SSH agent/config, never YAML or source.
- Commit examples/templates only. Private deployment files, user inputs, runs, logs, environment snapshots and caches remain ignored. Run `scripts/check_public_tree.py` before publication and inspect staged files.
- Store real projects under `projects/<id>/{inputs,configs,runs,evidence,docs}`. Keep reusable code/tests/docs at root. Only the explicitly allowlisted 1UBQ inputs and examples belong in Git.
- Scientific YAML overrides server YAML. Paths for CLI input/output resolve relative to the scientific YAML; web deployment file paths resolve relative to that file. Remote dependencies use explicit absolute Linux paths.

## Execution and provenance

- A documentation, packaging or diagnosis request does not authorize compute submission or database conversion. `doctor --remote` deploys small source snapshots but submits no compute jobs. `resume` may submit; `status` only reads the local snapshot.
- Use only the operator's configured hosts and project-owned writable directories. Never scan unrelated jobs/data, modify shared weights/databases/environments, or broadly cancel jobs.
- Freeze code, input, scientific parameters and dependency identities during active runs. Use a new output directory after changes or after a dry-run. Never alter plans/hashes/receipts to bypass checks.
- Preserve candidate provenance separately from sequence deduplication and MSA cache reuse. Validate cache hashes, mappings, sequences and receipts before accepting outputs.
- Never silently change model, seed, precision, kernels or parameters. Current folding is explicitly Protenix v2; its weights are separately licensed and not shipped.
- Ordinary CPU/MSA/GPU execution requires identical shared paths. Cross-storage routing must use explicitly configured archive transfer and verification. Never assume cross-cluster Slurm dependencies.
- GPU MSA is opt-in and requires a CUDA-capable MMseqs2 binary and GPU-prepared databases. Do not automatically convert databases or claim full-database GPU acceptance.
- Ambiguous submission outcomes must be reconciled against exact job identity before retrying. Preserve evidence; never submit duplicates to resolve a transport timeout.

## Checks and reporting

For code changes, run focused Linux/WSL tests and then `.venv/bin/python -m pytest -q`. For UI changes also run `node --test tests/web_ui.test.cjs` and inspect the rendered application when possible. For packaging changes build a wheel/sdist and inspect their file lists. Use scheduler/model doubles for routine tests; no real inference without user authorization.

For documentation-only changes run `.venv/bin/python scripts/check_docs.py`, check referenced CLI syntax, and run the privacy scan. Do not launch inference to validate prose.

Report changed files, commands actually run, observed results and remaining limits. Distinguish tests, dry-runs, scheduler acceptance, real inference and validated artifacts. Never reuse historical test counts as if newly run. An HTTP 200 or a successful Slurm submission does not establish end-to-end model success.
