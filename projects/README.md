# Project storage

Keep each actual project in `projects/<project-id>/`, with `inputs/`, `configs/`, `runs/`, `evidence/` and `docs/`. Shared source, reusable scripts, deployment templates and platform documentation remain at repository root.

User projects and generated outputs are ignored by default. The only public benchmark is [1UBQ](1ubq/README.md), with explicitly allowlisted input/configuration files. Add a new public fixture deliberately after reviewing its provenance and privacy; do not force-add an entire project directory.
