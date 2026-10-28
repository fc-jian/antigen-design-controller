"""Routing tests use scheduler doubles; they do not establish real GPU success."""
from copy import deepcopy
import io
import tarfile
from pathlib import Path
import pytest
from antigen_design.config import DEFAULTS
from antigen_design.io import digest, read_json, write_json
from antigen_design.gpu_routing import idle_nodes, finish_cancel, validate_routing, reroute_pending
from antigen_design.gpu_transfer import extract, pack, results, import_results
from antigen_design.worker import receipt_valid

RES = {"partition": "gpu-a", "cpus": 4, "memory": "64G", "gres": "gpu:1"}
PART = "PartitionName=gpu-a State=UP ReqResv=NO"
NODE = ("NodeName=n1 State=MIXED Partitions=gpu-a Owner=N/A "
        "CPUEfctv=32 CPUAlloc=20 RealMemory=200000 AllocMem=100000 "
        "Gres=gpu:4090:8(S:0-1) CfgTRES=cpu=32,mem=200000M,gres/gpu=8 AllocTRES=cpu=20,gres/gpu=7")


@pytest.mark.parametrize("change", [
    ("State=MIXED", "State=MIXED+DRAIN"), ("State=MIXED", "State=DOWN"),
    ("gres/gpu=7", "gres/gpu=8"), ("CPUAlloc=20", "CPUAlloc=31"),
    ("AllocMem=100000", "AllocMem=190000"), ("Owner=N/A", "Owner=other"),
])
def test_idle_requires_all_resources_and_healthy_state(change):
    assert idle_nodes(NODE, PART, RES)[0]["gpus"] == 1
    assert not idle_nodes(NODE.replace(*change), PART, RES)


def test_idle_checks_partition_gpu_type_and_constraint():
    assert not idle_nodes(NODE, PART.replace("UP", "DOWN"), RES)
    assert not idle_nodes(NODE, PART, RES | {"gres": "gpu:a100:1"})
    assert not idle_nodes(NODE, PART, RES | {"constraint": "a100"})
    assert not idle_nodes(NODE, PART.replace("ReqResv=NO", "ReqResv=YES"), RES)


def test_only_confirmed_cancel_allows_resubmit():
    state = {"state": "submitted", "reroute_requested": 10, "attempts": 2, "memory_tier": 0,
             "stage": "fold", "history": [], "host": "cluster-cpu", "job_id": "123", "slurm_state": "CANCELLED"}
    for status in ("UNKNOWN", "PENDING", "CANCELLED+"):
        copy = deepcopy(state)
        assert not finish_cancel(copy, status)
        assert copy["state"] == "submitted"
    assert finish_cancel(state, "CANCELLED")
    assert state["state"] == "pending" and state["attempts"] == 2
    assert state["history"][0]["job_id"] == "123"
    assert "job_id" not in state
    running = dict(state, state="running", reroute_requested=10)
    assert not finish_cancel(running, "RUNNING")
    assert "reroute_requested" not in running


def test_pending_cancellation_uses_scheduler_state_filter(monkeypatch):
    from antigen_design import remote_control
    calls = []
    monkeypatch.setattr(remote_control, "monitor", lambda p,r: {"f": {"state":"PENDING", "job_id":"123"}})
    monkeypatch.setattr(remote_control.subprocess, "run", lambda argv,**kw: calls.append(argv))
    remote_control.cancel_pending({}, {"f": {}})
    assert calls == [["scancel", "--state=PENDING", "123"]]
    monkeypatch.setattr(remote_control, "monitor", lambda p,r: {"f": {"state":"RUNNING", "job_id":"123"}})
    remote_control.cancel_pending({}, {"f": {}})
    assert len(calls) == 1
    monkeypatch.setattr(remote_control, "monitor", lambda p,r: {"f": {"error":"wrong owner"}})
    with pytest.raises(ValueError):
        remote_control.cancel_pending({}, {"f": {}})


def test_reroute_records_intent_before_cancel(monkeypatch, tmp_path):
    from antigen_design import gpu_routing, remote
    plan = {"local_output": str(tmp_path), "config": {"execution": {"gpu_routing": {"queue_grace_seconds":60}}}}
    state = {"stage":"fold","state":"submitted","slurm_state":"PENDING","submitted_at":0,
             "host":"h","job_id":"123"}
    manifest = {"tasks":{"f":state}}
    monkeypatch.setattr(gpu_routing, "choose", lambda *a,**k: ({},{}))
    def control(*args):
        assert read_json(tmp_path / "manifest.json")["tasks"]["f"]["reroute_requested"]
        assert args[3] == "cancel_pending"
        assert state["state"] == "submitted"  # cancellation is not completion
    monkeypatch.setattr(remote, "control", control)
    reroute_pending(plan, manifest, None)


@pytest.mark.parametrize("name,kind", [("../escape","file"), ("/escape","file"), ("safe","symlink")])
def test_transfer_refuses_path_escape_and_links(tmp_path, name, kind):
    archive = tmp_path / "test.tar"
    with tarfile.open(archive, "w") as stream:
        item = tarfile.TarInfo(name)
        if kind == "symlink":
            item.type = tarfile.SYMTYPE
            item.linkname = "/etc/passwd"
            stream.addfile(item)
        else:
            item.size = 1
            stream.addfile(item, io.BytesIO(b"x"))
    with pytest.raises(ValueError):
        extract(archive, tmp_path / "out", digest(archive))


def capsule_fixture(tmp_path):
    origin = tmp_path / "origin"
    origin.mkdir()
    input_path = origin / "input.json"
    input_path.write_text("input")
    msa = origin / "tasks" / "msa"
    msa.mkdir(parents=True)
    external = tmp_path / "cache.a3m"
    external.write_text(">q\nACD\n")
    write_json(msa / "fold_input.json", [{"name":"x", "sequences":[{"proteinChain":{
        "sequence":"ACD","pairedMsaPath":str(external),"unpairedMsaPath":str(external)}}]}])
    write_json(msa / "complete.json", {"signature":"msa-sig","artifacts":{
        str(msa/"fold_input.json"):digest(msa/"fold_input.json"),str(external):digest(external)}})
    config = deepcopy(DEFAULTS)
    config.update(output=str(origin), input=str(input_path), mode="folding")
    plan = {"config":config,"mode":"folding","identity":"origin-id","input_sha256":digest(input_path),
        "fold_records":[{"id":"x", "chains":{"A":"ACD"}, "job":{}}],
        "tasks":{"msa":{"signature":"msa-sig"},"fold":{"stage":"fold","signature":"fold-sig","deps":["msa"],"record":0,"id":"fold","seed":101}},
        "environment":{}}
    target = {"host":"yt","root":str(tmp_path/"target"),"weights":"/models/checkpoint","data_root":"/models","python":"/python"}
    route = {"id":"route123","target":target,"root":str(tmp_path/"target"/"capsule"),"source_dir":"/src",
        "resources":RES,"environment":{"versions":{"protenix":"2.0.0"}},"portable":{"model":"exact"}}
    payload = pack(plan, {"task":"fold","route":route})
    route["plan"] = payload["plan"]
    return plan, route, payload, external


def test_capsule_transfers_only_referenced_msa_and_preserves_lineage(tmp_path):
    plan, route, payload, external = capsule_fixture(tmp_path)
    extract(payload["path"], route["root"], payload["sha256"])
    capsule = read_json(Path(route["root"])/"plan.json")
    assert capsule["config"]["folding"]["options"] == plan["config"]["folding"]["options"]
    assert capsule["gpu_capsule"]["origin_task_signature"] == "fold-sig"
    assert capsule["tasks"]["fold"]["deps"] == []
    assert len(list((Path(route["root"])/"msa").glob("*.a3m"))) == 1
    transferred = read_json(Path(route["root"])/"fold_input.json")[0]["sequences"][0]["proteinChain"]
    assert Path(transferred["pairedMsaPath"]).read_bytes() == external.read_bytes()
    assert Path(transferred["pairedMsaPath"]).is_relative_to(Path(route["root"]))
    assert read_json(Path(plan["config"]["output"])/"tasks/msa/complete.json")["signature"] == "msa-sig"


def test_result_import_verifies_capsule_and_artifacts_then_publishes(tmp_path):
    plan, route, payload, _ = capsule_fixture(tmp_path)
    extract(payload["path"], route["root"], payload["sha256"])
    capsule = route["plan"]
    task = Path(route["root"])/"tasks/fold"
    task.mkdir(parents=True)
    (task/"model.cif").write_text("structure")
    write_json(task/"confidence.json", {"plddt":90})
    write_json(task/"predictions.json", [{"path":str(task/"model.cif"),"confidence":str(task/"confidence.json"),"sample":0}])
    write_json(task/"runtime.json", {"slurm_job_id":"999"})
    write_json(task/"command.json", ["protenix-v2"])
    write_json(task/"complete.json", {"signature":capsule["tasks"]["fold"]["signature"],
        "environment":capsule["environment"]["fold"],"artifacts":{str(p):digest(p) for p in task.iterdir()}})
    returned = results(capsule, {"task":"fold","attempt":1})
    request = dict(returned, task="fold", route=route, job_id="999")
    assert import_results(plan, request)["valid"]
    destination = Path(plan["config"]["output"])/"tasks/fold"
    assert receipt_valid(destination, "fold-sig")
    receipt = read_json(destination/"complete.json")
    assert receipt["kind"] == "verified_gpu_capsule_import"
    assert read_json(destination/"transfer.json")["job_id"] == "999"
    assert read_json(destination/"predictions.json")[0]["path"] == str(destination/"model.cif")
    assert import_results(plan, request)["valid"]  # idempotent recovery
    (destination/"model.cif").write_text("corrupted")
    with pytest.raises(ValueError):
        import_results(plan, request)


def test_invalid_routing_configuration_fails_closed():
    with pytest.raises(ValueError):
        validate_routing({"enabled":True,"targets":[]})
    with pytest.raises(ValueError):
        validate_routing({"poll_seconds":1})


def test_large_control_request_uses_verified_file_instead_of_ssh_argument(tmp_path):
    from antigen_design.remote import control_raw
    plan = {"local_output":str(tmp_path), "config":{"output":"/owned/run",
        "execution":{"python":{"metrics":"/python"},"source_dir":"/release/src"}}}
    payload = {"text":"x"*40000}
    class Transport:
        def __init__(self):
            self.saved = {}
        def upload(self, host, source, destination):
            self.saved[str(destination)] = Path(source).read_bytes()
        def execute(self, host, argv, **kwargs):
            if argv[0] == "mkdir":
                return ""
            assert argv[-1].startswith("@/owned/run/control/requests/")
            request_path, checksum = argv[-1][1:].rsplit("#",1)
            import hashlib, json
            assert hashlib.sha256(self.saved[request_path]).hexdigest() == checksum
            assert json.loads(self.saved[request_path]) == payload
            return '{"ok":true}'
    assert control_raw(Transport(), plan, "host", "gpu_import", payload) == {"ok":True}


def test_file_control_request_is_scoped_and_checksummed(tmp_path):
    from antigen_design.remote_control import load_request
    root = tmp_path/"run"
    request = root/"control/requests/id.json"
    write_json(request, {"x":1})
    plan = {"config":{"output":str(root)}}
    assert load_request(plan, "@"+str(request)+"#"+digest(request)) == {"x":1}
    with pytest.raises(ValueError):
        load_request(plan, "@"+str(request)+"#"+"0"*64)
    outside = tmp_path/"other.json"
    write_json(outside, {"x":1})
    with pytest.raises(ValueError):
        load_request(plan, "@"+str(outside)+"#"+digest(outside))
