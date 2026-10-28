"""Stage boundaries are execution controls, never changes to scientific identity."""
ORDER = ("rfd3", "mpnn", "msa", "fold", "metrics")


def uses_gpu(config, stage):
    return stage in ("rfd3", "mpnn", "fold") or (stage == "msa" and config["msa"].get("gpu", False))


def stage_host(config, stage):
    if stage == "msa" and config["execution"].get("msa_remote", {}).get("enabled"):
        return config["execution"]["msa_remote"]["host"]
    if stage == "msa" and config["execution"].get("msa_host"):
        return config["execution"]["msa_host"]
    return config["execution"]["gpu_host" if uses_gpu(config, stage) else "cpu_host"]


def resource_key(config, stage):
    if stage == "msa" and uses_gpu(config, stage):
        return "msa_gpu"
    return "gpu" if uses_gpu(config, stage) else "metrics" if stage == "metrics" else "cpu"


def selected_stages(until=None, only=None):
    if until and only:
        raise ValueError("Choose --until or --only, not both")
    if until is not None:
        if until not in ORDER:
            raise ValueError(f"Unknown stage: {until}")
        return set(ORDER[:ORDER.index(until) + 1])
    if only is not None:
        if only not in ORDER:
            raise ValueError(f"Unknown stage: {only}")
        return {only}
    return set(ORDER)


def boundary_complete(plan, manifest, stages):
    return all(state["state"] in {"succeeded", "failed", "blocked"}
               for name, state in manifest["tasks"].items()
               if plan["tasks"][name]["stage"] in stages)


def check_prerequisites(plan, manifest, stages):
    for name, task in plan["tasks"].items():
        if task["stage"] not in stages or manifest["tasks"][name]["state"] == "succeeded":
            continue
        for dep in task["deps"]:
            if plan["tasks"][dep]["stage"] not in stages and manifest["tasks"][dep]["state"] != "succeeded":
                raise ValueError(f"{name} requires completed {dep}; run its upstream stage first")
