# Configuration reference

## Web deployment

The complete starter is [webapp.example.yaml](../configs/webapp.example.yaml). Copy it to `configs/webapp.yaml`; private copies are ignored. `antigen-web --config FILE` selects a file explicitly and passes its absolute path to controller subprocesses through `ANTIGEN_WEB_CONFIG`. Without the flag, that environment variable takes precedence over `configs/webapp.yaml` in the selected workspace. If no configuration file exists, the local UI starts with no compute backend.

| Field | Meaning |
| --- | --- |
| `version` | Schema version, currently integer 1 |
| `repo` | Workspace root, relative to deployment YAML; default `..` |
| `project` | One directory name under `projects/`; default `webapp` |
| `host`, `port` | Loopback `127.0.0.1`, integer port 1–65535; default 8776 |
| `read_only` | Disable all POST operations and controller launch |
| `server_profile` | Path to the operator's OpenSSH/Slurm YAML; null disables submissions |
| `gpu_targets` | Optional file containing the full `gpu_routing` object; null uses the profile |
| `resource_policy`, `resource_limits` | Paths to mutable private resource JSON files |
| `max_gpu_jobs`, `max_msa_jobs` | Initial global limits, default 1 each; GPU 0 means unlimited |
| `queue_grace_seconds` | Initial pending-allocation grace, 30–86400 seconds |
| `scientific_defaults` | Overrides for seed, selection, RFD3, MPNN, MSA and folding |

`--repo`, `--project`, `--port`, `--read-only` can override startup values for diagnostics. All ordinary deployment settings can remain in YAML. Restart after changing startup settings or default science parameters. Preserve the configuration while jobs are active. Mutable JSON controls admission only, not the scientific identity of existing jobs.

An empty GPU catalog means the profile's ordinary GPU resource is used directly. Resource checkboxes apply to explicitly configured routing targets. MSA choices are derived from the profile: `primary` means its shared-storage MSA resource, `remote` means the optional independent MSA host. These are roles, not built-in site names.

## Scientific configuration

[DEFAULTS and validation](../src/antigen_design/config.py) are authoritative. Unknown keys are rejected. [1UBQ fixed-backbone](../projects/1ubq/configs/ubiquitin.yaml), [RFD3](../projects/1ubq/configs/ubiquitin-p3.yaml) and [matrix](../projects/1ubq/configs/ubiquitin-matrix.yaml) examples are included. For CLI: defaults → server YAML → scientific YAML. For web: defaults → server profile → web scientific defaults → request fields. JSON input `modelSeeds` takes precedence over default folding seeds for that record.

| Group | Fields and constraints |
| --- | --- |
| Identity | `schema_version: 1`, `mode: design` or `folding`, `input`, `output`, nonnegative `seed` |
| Numbering | `original` keeps chain/author numbering/insertion codes; `parsed` uses parsed positions |
| Selection | `regions: ['A:10-12']`; optional `{region: 'A:10-12', length: [3,5]}`; non-overlapping |
| Region sets | `region_sets: [{name: example, regions: [...]}]`; exclusive with `regions`/surface |
| Surface | `surface: true`, `threshold` in 0–1; exclusive with explicit selection |
| RFD3 | `enabled`, positive `samples`, registered name or absolute `checkpoint`, `options` object |
| MPNN | Positive `sequences`, `temperature` in (0,2], registered name or absolute `checkpoint`, `options` |
| MSA | `method: colabfold`, `database`, explicit `database_version`, `cache_root`, converter `script`, `mmseqs` executable |
| MSA batching | Positive `threads`/`batch_size`, `batch_scope: all` or `backbone` |
| MSA databases | `db1`, `db3` are the installed file prefixes, not download URLs |
| MSA GPU | `gpu`, `gpu_server`, `db_load_mode` 0–3, `ignore_index`; all opt-in |
| Prediction | `model: protenix-v2`, distinct nonnegative `seeds`, positive `samples`, `weights`, `data_root`, `need_atom_confidence` |
| Prediction options | `cycle`, `step`, `dtype`, `trimul_kernel`, `triatt_kernel`, `chunk_size`, `diffusion_chunk_size` |

RFD3 is required for changed interval lengths. New positions retain an explicit unmapped state; mapping coverage is not automatically 100%. RFD3/MPNN options cannot override pipeline-owned constraints, input/output, scope, sampling or seeds. Invalid or unsupported options fail rather than silently selecting another backend.

`input`/`output` resolve relative to the scientific YAML. Relative `execution.python.*`, `msa.database` and `msa.script` in CLI configuration also resolve relative to the scientific YAML. For portable backend profiles, use absolute Linux paths for **all** executables, databases, weights and cache locations. These paths describe the backend, not the local controller. No remote environment-variable expansion is performed.

## Execution

| Group | Fields |
| --- | --- |
| Mode | `backend: local` or `slurm`; `transport: native` or `openssh` |
| Hosts | `cpu_host`, `gpu_host`, optional shared-storage `msa_host`; names from SSH config |
| Storage | `remote_root`, `shared_root`; dedicated operator-owned absolute paths |
| Tools | `ssh_binary`, `scp_binary`; default `ssh`/`scp` |
| Environments | `python.prepare`, `rfd3`, `mpnn`, `msa`, `fold`, `metrics` |
| Concurrency | `max_gpu_jobs`, `max_msa_jobs`, optional absolute local `runtime_limits_file` |
| Resources | `resources.cpu`, `.gpu`, `.msa_gpu`, `.metrics` |
| Advanced | `gpu_routing`, `msa_remote`; see [advanced deployment](advanced-deployment.md) |

Slurm resources use `cpus`, `memory`, `time`, `partition`, `account`, `qos`, `constraint`, `gres`, and permitted scheduler options from [scheduler.py](../src/antigen_design/scheduler.py). `time: null` omits a walltime request; the cluster limit still applies. Standard GPU stages request one GPU unless explicitly configured otherwise; routed GPU jobs require the supported single-GPU contract. A resource request does not establish availability.

An optional legacy `ssh-skill` transport accepts a separately installed external bridge; it is not required, installed or configured by this distribution. Ordinary portable deployment uses OpenSSH.
