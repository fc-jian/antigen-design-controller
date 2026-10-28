# Advanced deployment

Start with a single shared-storage SSH/Slurm profile from the [README](../README.md). The following features are optional and disabled by default. Real acceptance on your own hardware is required before relying on them.

## Multiple GPU sites

Copy [gpu-targets.example.yaml](../configs/gpu-targets.example.yaml) to a private file, set `gpu_targets` in the web deployment YAML, and replace every placeholder. CLI configurations can put the same object under `execution.gpu_routing`.

Each target specifies a unique `id`, SSH `host`, dedicated `root`, absolute `python`/`weights`/`data_root`, and Slurm `resources`. A `design` object adds its own Python and Foundry weights for RFD3/MPNN; without it, the target accepts folding only. Explicit sites are observed for allocatable CPU, memory and GPU capacity. Results travel in checksummed capsules with provenance preserved, not cross-cluster Slurm dependencies.

Ordinary Slurm GPU sites relay archives through the controller using its OpenSSH configuration; allow local disk space and traffic in both directions. The optional bare-metal MUSA route instead requires explicitly allowed server-to-server SSH/SCP access through its configured gateway. See [GPU routing transfer code](../src/antigen_design/gpu_routing.py) for the transport contract. Compatible environment/model identities are verified; unavailable or mismatched sites are not interchangeable fallback models.

## Separate MSA storage

Merge this block into the operator's full server profile, replacing all paths and resource requests:

```yaml
execution:
  msa_remote:
    enabled: true
    host: msa-login
    root: /CONFIGURE/owned/msa-capsules
    python: /CONFIGURE/envs/msa/bin/python
    database: /CONFIGURE/databases/colabfold
    script: /CONFIGURE/Protenix/scripts/colabfold_msa.py
    mmseqs: /CONFIGURE/bin/mmseqs
    cache_root: /CONFIGURE/owned/msa-cache
    transfer_command: []
    fallback_when_busy: false
    poll_seconds: 30
    resources: {cpus: 8, memory: 64G, time: '04:00:00', partition: cpu}
```

This requires OpenSSH/Slurm CPU MSA. Do not also set shared-storage `msa_host`. `root` must be a dedicated operator-owned directory. `resources.cpus` must cover `msa.threads`. `fallback_when_busy: true` allows the configured primary CPU resource, with a distinct host and separately frozen environment, to receive work when capacity policy permits. Transport failures are not proof of no capacity. The per-deployment resource JSON selects the `primary` and/or `remote` roles.

With `transfer_command: []`, the controller uses its configured OpenSSH transport to download one capsule archive into a temporary local directory, verify SHA-256, upload to a unique partial file, verify again and publish the target filename. Only source/input/result capsules are relayed; databases and weights are never synchronized. Allow local disk space for one archive and network traffic in both directions. Failed remote partial files may remain for diagnosis; only remove explicitly identified project-owned files.

An optional nonempty `transfer_command` is an operator-maintained executable argv, never a shell string. Its contract is:

```text
COMMAND sync up|down --primary-path ABSOLUTE_DIRECTORY --remote-path ABSOLUTE_DIRECTORY --minimum-free-bytes 5368709120 [--execute]
```

It must preview without `--execute`, transfer with it, and emit one `Summary: planned=N, skipped=N, deleted=0, ignored_extra=0, conflicts=0, failures=0` line. The controller previews, executes and verifies; the last phase requires `planned=0`. Authentication stays outside command arguments. No site-specific wrapper is bundled or silently selected.

## MSA CPU optimization and GPU MSA

CPU optimization is under `msa.optimization`: `enabled`, positive `lanes`/`chunk_size`, `staging: none|disk|hybrid|indices`, absolute project-owned `disk_root`/`shm_root`, `reserve_gib`, `memory_budget_gib` and `working_memory_gib`. For remote-only optimization use `execution.msa_remote.optimization`; with primary fallback enabled the primary stays unstaged.

The optional remote shared-index pool has `pool.enabled`, `idle_seconds`, `max_seconds`, `request_timeout_seconds`, `queue_limit`. It requires opt-in `indices` staging and a Slurm walltime at least 120 seconds longer than its bounded lifetime. A pool allocation running does not mean an individual request has completed.

GPU MSA requires an appropriate CUDA MMseqs2 binary, explicitly GPU-prepared databases, `msa.gpu: true`, an appropriate `msa_host`, and `resources.msa_gpu`. It cannot be combined with remote CPU MSA or CPU optimization. `gpu_server: true` connects to an existing compatible service; it does not launch one. Full-database GPU performance and output equivalence are not established by this release's tests. Database conversion is an explicit operator activity.

## Bare-metal MUSA adapter

The retained adapter in [baremetal.py](../src/antigen_design/baremetal.py) targets `mthreads-gmi` and MTT S5000, with project-owned process journals, device leases and exact PID/boot/start identity. It is not a generic NVIDIA backend. Its configured `native` object names root, gateway, optional destination, devices, model and allowed transfer hosts. Stage identity hashes are mandatory. No production identities or example private host inventory are distributed.

Supported explicit kernel defaults and identity validation are described by [validate_routing](../src/antigen_design/gpu_routing.py). Do not invent identity hashes or treat synthetic test values as a deployment. Use local CLI or Slurm for other hardware until an independently verified adapter exists.
