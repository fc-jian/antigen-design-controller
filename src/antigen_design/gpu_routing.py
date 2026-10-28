"""Idle-aware GPU stage routing; never duplicate a live or ambiguous allocation."""
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
import json
from pathlib import Path
import re
import time
import uuid
from .io import read_json, write_json, digest, fingerprint


def validate_routing(settings):
    if type(settings.get("enabled", False)) is not bool:
        raise ValueError("gpu_routing.enabled must be boolean")
    for key in ("poll_seconds", "queue_grace_seconds"):
        if type(settings.get(key, 45)) is not int or settings.get(key, 45) < 30:
            raise ValueError(f"gpu_routing.{key} must be at least 30 seconds")
    targets = settings.get("targets", [])
    if not isinstance(targets, list) or (settings.get("enabled") and not targets):
        raise ValueError("GPU routing requires explicit targets")
    ids = set()
    from .transport import valid_host
    from .scheduler import slurm_script
    for target in targets:
        if set(target) - {"design", "backend", "native", "identity", "label", "folding_kernel_defaults"} != {"id", "host", "root", "python", "weights", "data_root", "resources"}:
            raise ValueError("GPU target fields: id, host, root, python, weights, data_root, resources")
        if not re.fullmatch(r"[a-z0-9-]+", target["id"]) or target["id"] in ids:
            raise ValueError("GPU target IDs must be unique")
        ids.add(target["id"])
        valid_host(target["host"])
        for key in ("root", "python", "weights", "data_root"):
            if not re.fullmatch(r"/[A-Za-z0-9_./-]+", target[key]) or ".." in Path(target[key]).parts:
                raise ValueError("GPU target paths must be absolute and safe")
        slurm_script(["true"], "/tmp", "/tmp/log", target["resources"], "probe")
        backend = target.get('backend', 'slurm')
        if backend not in ('slurm', 'baremetal'):
            raise ValueError('Unsupported GPU execution backend')
        if backend == 'baremetal':
            kernels = target.get('folding_kernel_defaults', {})
            if (not isinstance(kernels, dict) or set(kernels) - {'trimul_kernel', 'triatt_kernel'}
                    or kernels.get('trimul_kernel', 'torch') != 'torch'
                    or kernels.get('triatt_kernel', 'torch') not in ('torch', 'musa_sdpa')):
                raise ValueError('Native defaults may select only supported MUSA triangle kernels')
            native = target.get('native', {})
            if (set(native) != {'root', 'gateway', 'destination', 'devices', 'model', 'transfer_hosts'}
                    or native['root'] != target['root'] or native['model'] != 'MTT S5000'
                    or not isinstance(native['devices'], list) or not native['devices']
                    or any(type(d) is not int or not 0 <= d < 64 for d in native['devices'])
                    or len(set(native['devices'])) != len(native['devices'])):
                raise ValueError('Invalid explicit bare-metal GPU allocation settings')
            valid_host(native['gateway'])
            if native['destination'] is not None:
                valid_host(native['destination'])
                if native['gateway'] == target['host']:
                    raise ValueError('Native peer cannot route recursively through itself')
            if not isinstance(native['transfer_hosts'], list) or not native['transfer_hosts']:
                raise ValueError('Native transfers require explicit source hosts')
            for host in native['transfer_hosts']:
                valid_host(host)
            expected = {'fold', 'rfd3', 'mpnn'} if 'design' in target else {'fold'}
            identities = target.get('identity', {})
            if set(identities) != expected or any(not re.fullmatch('[0-9a-f]{64}', str(v)) for v in identities.values()):
                raise ValueError('Bare-metal environments must have explicit frozen stage identities')
        elif 'native' in target or 'identity' in target or 'folding_kernel_defaults' in target:
            raise ValueError('Native settings require the baremetal backend')
        if "design" in target:
            if not isinstance(target["design"], dict) or set(target["design"]) != {"python", "weights"}:
                raise ValueError("GPU design target requires python and weights")
            for value in target["design"].values():
                if not isinstance(value, str) or not re.fullmatch(r"/[A-Za-z0-9_./-]+", value) or ".." in Path(value).parts:
                    raise ValueError("Design target paths must be absolute and safe")


def memory_mb(value):
    match = re.fullmatch(r"(\d+(?:\.\d+)?)([KMGT]?)", str(value))
    if not match:
        raise ValueError(f"Unsupported memory size: {value}")
    return float(match[1]) * {"": 1, "K": 1/1024, "M": 1, "G": 1024, "T": 1024**2}[match[2]]


def fields(line):
    return dict(re.findall(r"(?:^|\s)(\w+)=([^\s]*)", line))


def idle_nodes(nodes, partitions, resources):
    """Use Slurm allocatable TRES, not nvidia-smi utilisation or node MIXED alone."""
    allowed = {r["PartitionName"] for line in partitions.splitlines() for r in [fields(line)]
               if r.get("State") == "UP" and r.get("ReqResv", "NO") == "NO"}
    partition = resources["partition"]
    if partition not in allowed:
        return []
    match = re.fullmatch(r"gpu(?::([A-Za-z0-9_-]+))?:(\d+)", resources.get("gres", "gpu:1"))
    if not match:
        raise ValueError("GPU routing supports a single GPU GRES request")
    gpu_type, count = match[1], int(match[2])
    result = []
    for line in nodes.splitlines():
        row = fields(line)
        state = row.get("State", "")
        if state not in ("IDLE", "MIXED") or partition not in row.get("Partitions", "").split(","):
            continue
        if row.get("Owner", "N/A") not in ("N/A", "(null)"):
            continue
        if gpu_type and not re.search(r"gpu:" + re.escape(gpu_type) + r":", row.get("Gres", "")):
            continue
        if resources.get("constraint") and resources["constraint"] not in row.get("ActiveFeatures", "").split(","):
            continue
        cfg = dict(item.split("=", 1) for item in row.get("CfgTRES", "").split(",") if "=" in item)
        alloc = dict(item.split("=", 1) for item in row.get("AllocTRES", "").split(",") if "=" in item)
        free_gpu = int(cfg.get("gres/gpu", 0)) - int(alloc.get("gres/gpu", 0))
        free_cpu = int(row.get("CPUEfctv", row.get("CPUTot", 0))) - int(row.get("CPUAlloc", 0))
        free_memory = int(row.get("RealMemory", 0)) - int(row.get("AllocMem", 0))
        if free_gpu >= count and free_cpu >= resources["cpus"] and free_memory >= memory_mb(resources["memory"]):
            result.append({"node": row["NodeName"], "gpus": free_gpu, "cpus": free_cpu, "memory_mb": free_memory})
    return result


def resource_request(plan, target):
    result = dict(plan["config"]["execution"]["resources"]["gpu"]) | target["resources"]
    if target.get('backend') == 'baremetal':
        result = {k: result[k] for k in ('cpus', 'memory', 'time', 'gres') if k in result}
        if result.get('gres', 'gpu:1') != 'gpu:1':
            raise ValueError('Native tasks require exactly one GPU')
    return result


def folding_options(config, target):
    """Explicit site kernel defaults; scientific overrides always take precedence."""
    options = dict(target.get('folding_kernel_defaults', {})) | config['folding']['options']
    if target.get('backend') == 'baremetal':
        if (options.get('trimul_kernel', 'torch') != 'torch'
                or options.get('triatt_kernel', 'torch') not in ('torch', 'musa_sdpa')):
            raise ValueError('Requested triangle kernel is unavailable on the MUSA backend')
    return options


def native_source(plan, target):
    from .remote import deploy_code
    cache = Path(plan['local_output']) / 'routing' / (target['id'] + '-source.json')
    if not cache.exists():
        config = deepcopy(plan['config'])
        config['execution'].update(cpu_host=target['host'], remote_root=target['root'])
        write_json(cache, {'source_dir': deploy_code(config)})
    return read_json(cache)['source_dir']


def snapshots(plan, manifest, transport, force=False):
    settings = plan["config"]["execution"]["gpu_routing"]
    cached = manifest.get("gpu_routing", {})
    if not force and time.time() - cached.get("checked_at", 0) < settings["poll_seconds"]:
        return cached.get("targets", [])
    def check(target):
        result = {"id": target["id"], "host": target["host"], "partition": target["resources"].get("partition"),
                  'backend': target.get('backend', 'slurm')}
        try:
            if target.get('backend') == 'baremetal':
                source = native_source(plan, target)
                observed = transport.execute(target['host'], ['env', 'PYTHONPATH=' + source, target['python'],
                    '-m', 'antigen_design.baremetal', 'capacity', json.dumps({
                        'native': target['native'], 'resources': resource_request(plan, target)})], timeout=45)
                result.update(json.loads(observed.splitlines()[-1]))
                return result
            nodes = transport.execute(target["host"], ["scontrol", "show", "nodes", "-o"], timeout=25)
            partitions = transport.execute(target["host"], ["scontrol", "show", "partition", target["resources"]["partition"], "-o"], timeout=25)
            result["nodes"] = idle_nodes(nodes, partitions, resource_request(plan, target))
            result["free_gpus"] = sum(n["gpus"] for n in result["nodes"])
        except Exception as exc:
            result.update(error=str(exc), nodes=[], free_gpus=0)
        return result
    with ThreadPoolExecutor(max_workers=min(4, len(settings["targets"]))) as pool:
        observed = list(pool.map(check, settings["targets"]))
    previous = {item["id"]: item for item in cached.get("targets", [])}
    for item in observed:
        old = previous.get(item["id"], {})
        if old.get("retry_after", 0) > time.time():
            item.update({k: old[k] for k in ("retry_after", "compatibility_stage", "compatible", "error") if k in old})
    manifest["gpu_routing"] = {"checked_at": time.time(), "targets": observed}
    return observed


def portable_identity(transport, host, python, source_dir, config, stage_name="fold"):
    config = deepcopy(config)
    # Inventory is irrelevant to a stage probe and can exceed Windows SSH argv limits.
    config['execution']['gpu_routing']['targets'] = []
    output = transport.execute(host, ["env", "PYTHONPATH=" + source_dir, python,
        "-m", "antigen_design.gpu_transfer", "identity", json.dumps({"config": config, "stage": stage_name})], timeout=300)
    return json.loads(output.splitlines()[-1])


def ready(plan, target, transport, stage_name="fold"):
    from .remote import deploy_code
    cache = Path(plan["local_output"]) / "routing"
    cache.mkdir(exist_ok=True)
    cached = cache / (target["id"] + "-" + stage_name + "-identity.json")
    if cached.exists():
        return read_json(cached)
    reference = cache / ("reference-" + stage_name + "-identity.json")
    c = plan["config"]
    if not reference.exists():
        value = portable_identity(transport, c["execution"]["gpu_host"], c["execution"]["python"][stage_name],
                                  c["execution"]["source_dir"], c, stage_name)
        if value["environment"] != plan["environment"][stage_name]:
            raise ValueError("Source inference environment changed after the plan was frozen")
        write_json(reference, value)
    expected = read_json(reference)
    target_config = deepcopy(c)
    target_config["execution"].update(cpu_host=target["host"], remote_root=target["root"])
    source_dir = native_source(plan, target) if target.get('backend') == 'baremetal' else deploy_code(target_config)
    target_config["execution"]["source_dir"] = source_dir
    if stage_name == "fold":
        target_config["folding"].update(weights=target["weights"], data_root=target["data_root"])
        target_config["folding"]["options"] = folding_options(c, target)
        python = target["python"]
    else:
        target_config["foundry_weights"] = target["design"]["weights"]
        python = target["design"]["python"]
    target_config['execution']['python'][stage_name] = python
    if target.get('backend') == 'baremetal':
        target_config['execution']['native_gpu'] = target['native']
    observed = portable_identity(transport, target["host"], python, source_dir, target_config, stage_name)
    if target.get('backend') == 'baremetal':
        if fingerprint(observed['portable']) != target['identity'][stage_name]:
            raise ValueError('Native environment differs from the explicitly frozen MUSA backend')
        # Backend software is explicitly different, but models and inference data
        # must still match the scientific source. Never substitute either.
        keys = ['checkpoint'] + (['ccd_components_file', 'ccd_components_rdkit_mol_file',
                                 'pdb_cluster_file', 'obsolete_release_data_csv'] if stage_name == 'fold' else [])
        if any(observed['portable']['resources'][k] != expected['portable']['resources'][k] for k in keys):
            raise ValueError('Native model/data bytes differ from the frozen scientific source')
    elif observed["portable"] != expected["portable"]:
        raise ValueError(f"{stage_name} software/model/data identity differs from the frozen source environment")
    value = observed | {"source_dir": source_dir}
    write_json(cached, value)
    return value


def choose(plan, manifest, transport, exclude=None, stage_name="fold"):
    # Never reuse an idle snapshot after another submission in the same pass.
    observed = snapshots(plan, manifest, transport, force=True)
    targets = {t["id"]: t for t in plan["config"]["execution"]["gpu_routing"]["targets"]}
    manifest["gpu_routing"]["stage"] = stage_name
    for observation in observed:
        if observation.get("compatibility_stage") != stage_name:
            observation.pop("compatible", None)
            if observation.get("nodes"):
                observation.pop("error", None)
    # Prefer more free allocations. Stable configured ordering breaks ties.
    for observation in sorted(observed, key=lambda r: -r["free_gpus"]):
        if not observation["free_gpus"] or observation["id"] == exclude:
            continue
        target = targets[observation["id"]]
        resources = resource_request(plan, target)
        gpu_count = int(resources.get("gres", "gpu:1").rsplit(":", 1)[-1])
        slots = sum(min(n["gpus"] // gpu_count, n["cpus"] // resources["cpus"],
                        int(n["memory_mb"] // memory_mb(resources["memory"])))
                    for n in observation.get("nodes", []))
        # Pending and ambiguous submissions may not yet appear in Slurm AllocTRES.
        # Conservatively reserve their whole allocation until refresh sees RUNNING.
        reserved = sum(s.get("state") in ("submitting", "submitted", "running")
                       and (s.get('execution_state') or s.get("slurm_state")) != "RUNNING"
                       and s.get("route", {}).get("target", {}).get("id") == target["id"]
                       for s in manifest["tasks"].values())
        if slots <= reserved:
            continue
        if stage_name != "fold" and "design" not in target:
            observation.update(compatible=False, error=f"{stage_name}: 未配置已验证的设计环境")
            continue
        if observation.get("compatibility_stage") == stage_name and observation.get("retry_after", 0) > time.time():
            continue
        try:
            readiness = ready(plan, target, transport, stage_name)
            observation["compatible"] = True
            observation["compatibility_stage"] = stage_name
            observation.pop("error", None)
            return target, readiness
        except Exception as exc:
            observation.update(compatible=False, compatibility_stage=stage_name, error=str(exc), retry_after=time.time() + 300)
    return None


def stage(plan, manifest, name, transport, selected):
    """No sbatch here; persist capsule identity before any scheduler mutation."""
    from .remote import control
    target, readiness = selected
    stage_name = plan["tasks"][name]["stage"]
    python = target["python"] if stage_name == "fold" else target["design"]["python"]
    state = manifest["tasks"][name]
    if state.get("route"):
        state["history"].append({k: v for k, v in state.items() if k != "history"})
        state.pop("imported", None)
    route_id = uuid.uuid4().hex
    route = {"id": route_id, "target": target, "resources": resource_request(plan, target),
        "root": str(Path(target["root"]) / "capsules" / route_id),
        "source_dir": readiness["source_dir"], "environment": readiness["environment"],
        "portable": readiness["portable"]}
    payload = control(transport, plan, plan["config"]["execution"]["cpu_host"], "gpu_pack", {"task": name, "route": route})
    transport.execute(target["host"], ["mkdir", "-p", route["root"]])
    remote_archive = str(Path(route["root"]) / "input.tar.gz")
    if target.get('backend') == 'baremetal':
        transport.transfer_native(plan['config']['execution']['cpu_host'], payload['path'],
                                  target['host'], remote_archive)
    else:
        local = Path(plan["local_output"]) / "routing" / (route_id + "-input.tar.gz")
        transport.download(plan["config"]["execution"]["cpu_host"], payload["path"], local)
        if digest(local) != payload["sha256"]:
            raise ValueError("Downloaded input capsule checksum mismatch")
        transport.upload(target["host"], local, remote_archive)
    transport.execute(target["host"], ["env", "PYTHONPATH=" + route["source_dir"], python,
        "-m", "antigen_design.gpu_transfer", "extract",
        json.dumps({"path": remote_archive, "root": route["root"], "sha256": payload["sha256"]})])
    transport.execute(target["host"], ["mkdir", "-p", *[str(Path(route["root"]) / d) for d in ("control", "logs", "scripts")]])
    route.update(plan=payload["plan"], input_sha256=payload["sha256"], input_bytes=payload["bytes"])
    manifest["tasks"][name].update(route=route, host=target["host"], routing_reason="已选择兼容的空闲 GPU")
    return route


def returned(plan, state, name, transport):
    from .remote import control
    route = state["route"]
    payload = control(transport, route["plan"], state["host"], "gpu_results", {"task": name, "attempt": state["attempts"]})
    host = plan["config"]["execution"]["cpu_host"]
    path = str(Path(plan["config"]["output"]) / "transfers" / route["id"] / "result.tar.gz")
    if route['target'].get('backend') == 'baremetal':
        transport.transfer_native(host, path, state['host'], payload['path'], returning=True)
    else:
        local = Path(plan["local_output"]) / "routing" / (route["id"] + "-result.tar.gz")
        transport.download(state["host"], payload["path"], local)
        if digest(local) != payload["sha256"]:
            raise ValueError("Downloaded inference result checksum mismatch")
        transport.upload(host, local, path)
    result = control(transport, plan, host, "gpu_import", {"task": name, "route": route,
        "path": path, "sha256": payload["sha256"], "job_id": state["job_id"]})
    state["imported"] = True
    return result


def reroute_pending(plan, manifest, transport):
    from .remote import control
    grace = plan["config"]["execution"]["gpu_routing"]["queue_grace_seconds"]
    for name, state in manifest["tasks"].items():
        if state["stage"] not in ("fold", "rfd3", "mpnn") or state["state"] != "submitted" or (state.get('execution_state') or state.get("slurm_state")) != "PENDING":
            continue
        if time.time() - state["submitted_at"] < grace:
            continue
        if state.get("reroute_requested"):
            if time.time() - state.get("reroute_sent_at", 0) >= grace:
                control(transport, plan, state["host"], "cancel_pending", {name: state})
                state["reroute_sent_at"] = time.time()
            continue
        old_target = state.get("route", {}).get("target", {}).get("id")
        selected = choose(plan, manifest, transport, exclude=old_target, stage_name=state["stage"])
        if not selected:
            continue
        # Record intent before mutation; PENDING filter protects the start/cancel race.
        state["reroute_requested"] = time.time()
        write_json(Path(plan["local_output"]) / "manifest.json", manifest)
        control(transport, plan, state["host"], "cancel_pending", {name: state})
        state["reroute_sent_at"] = time.time()


def finish_cancel(state, slurm_state):
    if not state.get("reroute_requested"):
        return False
    if slurm_state == "CANCELLED":
        history = state["history"]
        history.append({k: v for k, v in state.items() if k != "history"})
        attempts, tier, stage_name = state["attempts"], state["memory_tier"], state["stage"]
        host = state["host"]
        avoid = state.get("route", {}).get("target", {}).get("id")
        state.clear()
        state.update(state="pending", attempts=attempts, memory_tier=tier, stage=stage_name,
            host=host, history=history, avoid_target=avoid, avoid_until=time.time() + 300,
            routing_reason="旧排队作业已确认 CANCELLED，正在选择新的 GPU")
        return True
    if slurm_state in ("RUNNING", "COMPLETING", "COMPLETED"):
        state.pop("reroute_requested", None)
        state.pop("reroute_sent_at", None)
    return False
