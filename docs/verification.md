# Verification scope

This is a clean source release prepared on **2026-10-28**. Private deployment profiles, scheduler evidence, historical model results and unrelated projects were not imported. Tests use the public 1UBQ structure and synthetic/mock scheduler/model fixtures.

Observed checks in the open-source workspace:

- A new Linux/WSL Python 3.12.13 virtual environment installed this checkout and its core/test dependencies.
- Final full `.venv/bin/python -m pytest -q`: **415 passed**.
- `node --test tests/web_ui.test.cjs`: **20 passed**.
- The public-tree privacy scan passed for all 129 publishable files, including checks against the source workspace's private account and host identifiers. Ignored runtime files were excluded from the release.
- Documentation checks passed for 46 local links, configuration schemas and the public 1UBQ input checksum.
- Both wheel and source archive built successfully. Their contents were inspected for private configurations, user identifiers and generated data; the source archive's account/group names and IDs were removed. The wheel contains the UI and its bundled viewer/license.
- The generated wheel installed into a separate clean virtual environment; both `antigen-design --help` and `antigen-web --help` succeeded outside the source directory.
- A local browser loaded the Design page, parsed the public 76-residue 1UBQ structure and rendered its 3D view. The monitor showed zero submitted jobs. Startup without a backend displayed configuration instructions and disabled resource saving.

The full tests include actual local HTTP serving of the bundled UI/viewer, origin rejection, arbitrary operator SSH aliases, relative config paths, configuration precedence, unconfigured startup, checksummed transfer doubles, recovery and provenance. Scheduler doubles do not establish real cluster availability. Local non-model process tests do not establish GPU inference.

Check commands are listed in the [README](../README.md). CI configuration for Python 3.10 and 3.12 is included, but no hosted CI run is claimed until the repository is pushed and that run actually completes. Local verification used Python 3.12 only.

No real SSH backend was contacted for this release, no compute jobs were submitted, no database was converted, and no new model inference was performed. To claim end-to-end acceptance on a deployment, obtain required dependencies/authorized weights, run a fresh 1UBQ case, check scheduler accounting and validate real artifacts and receipts. The installed adapter version and all model parameters must remain explicit.

The current supported folding model is Protenix v2. Its separately licensed weights are not distributed. GPU MSA full-database acceptance, arbitrary non-Slurm GPU servers, and all future upstream model API versions are outside these checks.

## Frontend synchronization — 2026-10-29

Synchronized required task names, independent display-name editing, list archive/restore, 50-row project pagination, lazy log/scheduling panels, request cancellation, and the supporting compact read APIs/cache. The portable deployment configuration, configured scientific defaults, unconfigured-backend safeguards and generic host fixtures remain in place.

- Full Linux/WSL Python suite: **429 passed**.
- Frontend Node suite: **26 passed**.
- Browser verification used an isolated 120-task synthetic record with controllers disabled: rename updated both detail and list, pagination showed rows 51–100, logs loaded on expansion, and archive/restore worked directly from the list.
- Public-tree privacy scanning and documentation checks passed. No real backend connection, compute submission or inference was used for this synchronization.

## Input and result UI synchronization — 2026-10-29

Synchronized the streamlined interface, original-input read-only view, editable copies with fresh submission IDs/source provenance, per-chain FASTA copying with manual fallback, camera reset and identifiable CIF downloads. Added only their supporting input API; the open-source deployment configuration and existing project-local web storage remain unchanged. Packaged the same public 1UBQ example for wheel installations.

- Full Linux/WSL Python suite: **437 passed**; frontend Node suite: **42 passed**.
- Tests cover source-file checksums, path confinement, cross-origin rejection, immutable source requests, fresh-copy identity, and late configuration defaults preserving original inputs/copies.
- Browser verification used synthetic records with controllers disabled. Original inputs stayed read-only, new copies required a new name, and the public 1UBQ structure rendered with working sequence-copy and camera-reset controls. It was explicitly labeled as a public structure, not a new prediction.
- Public-file/privacy and documentation checks passed; wheel and source archives were rebuilt and inspected, including the bundled benchmark. No real backend connection, compute submission or inference was performed.
