"""Explicit cross-storage CPU MSA routing over the managed transfer wrapper."""
from copy import deepcopy
from pathlib import Path
import json
import re
import subprocess
import time
import uuid
from .io import read_json, write_json


def enabled(config):
    return config["execution"].get("msa_remote", {}).get("enabled", False)


def validate(config):
    s = config["execution"].get("msa_remote", {})
    if type(s.get("enabled", False)) is not bool:
        raise ValueError("msa_remote.enabled must be boolean")
    if not s.get("enabled"):
        return
    from .config import positive, merge
    if type(s.get('fallback_when_busy', False)) is not bool:
        raise ValueError('msa_remote.fallback_when_busy must be boolean')
    positive(s.get('poll_seconds',30),'msa_remote.poll_seconds')
    if s.get('optimization') is not None:
        opt = merge(config['msa']['optimization'],s['optimization'])
        from .config import validate_config
        check = deepcopy(config);check['execution']['msa_remote']['enabled']=False;check['msa']['optimization']=opt
        validate_config(check)
    if s.get('fallback_when_busy'):
        if config['execution']['cpu_host'] == s['host']:
            raise ValueError('Busy-only MSA routing requires distinct primary and remote hosts')
        if config['msa']['optimization']['enabled']:
            raise ValueError('Configure primary-only optimization under msa_remote.optimization; primary fallback stays unstaged')
    pool = s.get("pool", {})
    if type(pool.get("enabled", False)) is not bool:
        raise ValueError("msa_remote.pool.enabled must be boolean")
    if pool.get("enabled"):
        from .config import positive
        for key in ("idle_seconds", "max_seconds", "request_timeout_seconds", "queue_limit"):
            positive(pool[key], "msa_remote.pool." + key)
        if pool["request_timeout_seconds"] >= pool["max_seconds"] or pool["max_seconds"] > 86400:
            raise ValueError("Pool requires bounded lifetime greater than request timeout (at most 24h)")
        opt = merge(config["msa"]["optimization"], s["optimization"]) if s.get("optimization") is not None else config["msa"]["optimization"]
        if not opt["enabled"] or opt["staging"] != "indices":
            raise ValueError("MSA pool requires opt-in indices staging")
        if not s["resources"].get("time"):
            raise ValueError("MSA pool requires an explicit Slurm walltime")
        raw = str(s["resources"]["time"])
        if not re.fullmatch(r"(?:[0-9]+-)?[0-9]+:[0-9]{2}:[0-9]{2}", raw):
            raise ValueError("Pool Slurm walltime must be [days-]HH:MM:SS")
        days, clock = raw.split("-",1) if "-" in raw else ("0",raw)
        h,m,sec = map(int,clock.split(":"))
        if int(days)*86400+h*3600+m*60+sec < pool["max_seconds"]+120:
            raise ValueError("Pool lifetime must leave 120 seconds before Slurm walltime")
    from .transport import valid_host
    from .scheduler import slurm_script
    valid_host(s["host"])
    if config["execution"]["backend"] != "slurm" or config["execution"]["transport"] != "openssh" or config["msa"]["gpu"]:
        raise ValueError("Remote MSA requires OpenSSH Slurm CPU execution")
    if config["execution"].get("msa_host"):
        raise ValueError("Use msa_remote instead of shared-storage msa_host")
    for key in ("root", "python", "database", "script", "mmseqs", "cache_root"):
        value = s.get(key)
        if not isinstance(value, str) or not re.fullmatch(r"/[A-Za-z0-9_./-]+", value) or ".." in Path(value).parts:
            raise ValueError("Invalid msa_remote path: " + key)
    if len(Path(s['root']).parts) < 3:
        raise ValueError('MSA root must be a dedicated project-owned directory')
    if not isinstance(s.get("transfer_command"), list) or any(not isinstance(x,str) or not x for x in s["transfer_command"]):
        raise ValueError("transfer_command must be an argv list; [] uses the built-in OpenSSH relay")
    slurm_script(["true"], "/tmp", "/tmp/log", s["resources"], "probe")
    if s["resources"]["cpus"] < config["msa"]["threads"]:
        raise ValueError("Remote MSA threads exceed allocated CPUs")


def target_config(config):
    c = deepcopy(config)
    s = c["execution"]["msa_remote"]
    for key in ("database", "script", "mmseqs", "cache_root"):
        c["msa"][key] = s[key]
    c["execution"].update(cpu_host=s["host"], msa_host=s["host"], remote_root=s["root"], source_dir=s["source_dir"])
    c["execution"]["python"].update(msa=s["python"], metrics=s["python"])
    c["execution"]["resources"]["cpu"] = deepcopy(s["resources"])
    if s.get('optimization') is not None:
        from .config import merge
        c['msa']['optimization'] = merge(c['msa']['optimization'],s['optimization'])
    c["execution"]["msa_remote"]["enabled"] = False
    return c


def sync(settings, direction, primary, remote, evidence, *, transport=None, primary_host=None, filename=None):
    """Explicit archive relay, or an operator-supplied verified transfer command."""
    if not settings['transfer_command']:
        from .ssh_relay import transfer
        return transfer(transport, primary_host, settings['host'], direction,
                        primary, remote, filename, evidence)
    evidence = Path(evidence)
    evidence.mkdir(parents=True, exist_ok=True)
    base = [*settings["transfer_command"], "sync", direction, "--primary-path", str(primary),
            "--remote-path", str(remote), "--minimum-free-bytes", str(5 * 1024**3)]
    for phase, extra in (("preview", []), ("execute", ["--execute"]), ("verify", [])):
        result = subprocess.run(base + extra, capture_output=True, text=True, timeout=1800)
        text = result.stdout + result.stderr
        (evidence / (phase + ".log")).write_text(text)
        matches = re.findall(r"Summary: planned=(\d+), skipped=(\d+), deleted=(\d+), ignored_extra=(\d+), conflicts=(\d+), failures=(\d+)", text)
        if result.returncode or len(matches) != 1 or any(int(matches[0][i]) for i in (2,3,4,5)):
            raise RuntimeError("Managed MSA transfer failed: " + str(evidence / (phase + ".log")))
        if phase == "verify" and int(matches[0][0]):
            raise RuntimeError("MSA transfer incomplete")
        write_json(evidence / (phase + ".json"), dict(zip(("planned","skipped","deleted","ignored_extra","conflicts","failures"), map(int,matches[0]))))


def publish_source(archive, target, expected_hash, expected_signature):
    """Self-contained bootstrap, sent as management code before package import."""
    import hashlib, json, tarfile, tempfile
    from pathlib import Path
    target = Path(target)
    package = target / "src/antigen_design"
    def signature(path):
        files = {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in path.glob("*.py")}
        return hashlib.sha256(json.dumps(files, sort_keys=True).encode()).hexdigest()
    if package.exists():
        if signature(package) != expected_signature:
            raise ValueError("Refusing to replace a different immutable source release")
        return
    if hashlib.sha256(Path(archive).read_bytes()).hexdigest() != expected_hash:
        raise ValueError("Transferred source archive checksum mismatch")
    target.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="source-", dir=target) as tmp:
        staging = Path(tmp)
        with tarfile.open(archive) as stream:
            members = stream.getmembers()
            if len({m.name for m in members}) != len(members) or sum(m.size for m in members) > 32*1024**2:
                raise ValueError("Invalid source archive size or duplicate members")
            for member in members:
                relative = Path(member.name)
                if not member.isfile() or len(relative.parts) != 3 or relative.parts[:2] != ("src","antigen_design") or relative.suffix != ".py":
                    raise ValueError("Source archive must contain only package Python files")
                dest = staging / relative
                dest.parent.mkdir(parents=True, exist_ok=True)
                dest.write_bytes(stream.extractfile(member).read())
        if signature(staging / "src/antigen_design") != expected_signature:
            raise ValueError("Source package signature mismatch")
        try:
            (staging / "src").rename(target / "src")
        except OSError:
            if not package.exists() or signature(package) != expected_signature:
                raise


def deploy(config, local_output):
    from .transport import get_transport
    from .worker import code_signature
    import inspect
    s = config["execution"]["msa_remote"]
    signature = code_signature()
    target = Path(s["root"]) / "releases" / signature
    source = Path(config["execution"]["source_dir"]).parent
    transport = get_transport(config)
    # Only source.tar enters the transfer tree; never synchronize __pycache__.
    outbox = Path(config["execution"]["remote_root"]) / "source-transfers" / signature
    transport.execute(config["execution"]["cpu_host"], ["mkdir", "-p", str(outbox)])
    transport.execute(config["execution"]["cpu_host"], ["cp", "-p", str(source/"source.tar"), str(outbox/"source.tar")])
    sync(s, "up", outbox, target/"inbox", Path(local_output) / "msa-transfer" / "source",
         transport=transport, primary_host=config['execution']['cpu_host'], filename='source.tar')
    expected = transport.execute(config["execution"]["cpu_host"], ["sha256sum", str(outbox/"source.tar")]).split()[0]
    bootstrap = inspect.getsource(publish_source) + "\nimport sys; publish_source(*sys.argv[1:])"
    transport.execute(s["host"], [s["python"], "-c", bootstrap, str(target/"inbox/source.tar"), str(target), expected, signature])
    s["source_dir"] = str(target / "src")


def stage(plan, manifest, name, transport):
    from .remote import control_raw
    root = Path(plan["local_output"])
    state = manifest["tasks"][name]
    s = plan["config"]["execution"]["msa_remote"]
    if "route" not in state:
        rid = uuid.uuid4().hex
        state["route"] = {"kind": "msa", "id": rid, "target": {"host":s["host"]},
            "root": str(Path(s["root"]) / "runs" / rid), "resources": s["resources"]}
        state["transfer_phase"] = "preparing"
        write_json(root / "manifest.json", manifest)
    route = state["route"]
    host = plan["config"]["execution"]["cpu_host"]
    if "plan" not in route:
        payload = control_raw(transport, plan, host, "msa_pack", {"task": name, "route": route})
        route.update(plan=payload["plan"], input_sha256=payload["sha256"], input_bytes=payload["bytes"], input_dir=payload["directory"])
        write_json(root / "manifest.json", manifest)
    state["transfer_phase"] = "uploading"
    write_json(root / "manifest.json", manifest)
    sync(s, "up", route["input_dir"], Path(route["root"]) / "inbox", root / "msa-transfer" / route["id"] / "up",
         transport=transport, primary_host=host, filename='input.tar.gz')
    transport.execute(s["host"], ["env", "PYTHONPATH="+s["source_dir"], s["python"], "-m", "antigen_design.gpu_transfer", "extract",
        json.dumps({"path":str(Path(route["root"])/"inbox/input.tar.gz"), "root":route["root"], "sha256":route["input_sha256"]})])
    transport.execute(s["host"], ["mkdir","-p", *[str(Path(route["root"])/d) for d in ("logs","scripts","control")]])
    state.update(host=s["host"], transfer_phase="ready", routing_reason="remote CPU MSA；结果校验回传后释放预测依赖")
    write_json(root / "manifest.json", manifest)


def progress(plan, state, name, phase):
    state["transfer_phase"] = phase
    path = Path(plan["local_output"]) / "manifest.json"
    if path.exists():
        saved = read_json(path)
        saved["tasks"][name] = state
        write_json(path, saved)


def returned(plan, state, name, transport):
    from .remote import control_raw
    route = state["route"]
    s = plan["config"]["execution"]["msa_remote"]
    local = Path(plan["local_output"]) / "msa-transfer" / route["id"]
    if state.get("transfer_retry_at", 0) > time.time():
        return False
    try:
        progress(plan, state, name, "exporting")
        payload = control_raw(transport, route["plan"], state["host"], "msa_results", {"task": name, "attempt":state["attempts"]})
        destination = Path(plan["config"]["output"])/"transfers"/route["id"]/f"return-a{state['attempts']}"
        progress(plan, state, name, "returning")
        sync(s, "down", destination, payload["directory"], local / f"down-a{state['attempts']}",
             transport=transport, primary_host=plan['config']['execution']['cpu_host'], filename='result.tar.gz')
        progress(plan, state, name, "validating")
        control_raw(transport, plan, plan["config"]["execution"]["cpu_host"], "msa_import", {
            "task":name, "route":route,"path":str(destination/"result.tar.gz"),"sha256":payload["sha256"],"job_id":state["job_id"]})
        state.update(imported=True, transfer_phase="verified")
        state.pop("transfer_error",None)
        progress(plan, state, name, "verified")
        return True
    except (OSError, subprocess.SubprocessError, RuntimeError) as exc:
        state.update(transfer_error=(getattr(exc,"stderr",None) or str(exc))[-2000:], transfer_retry_at=time.time()+60)
        progress(plan, state, name, state["transfer_phase"])
        return False
