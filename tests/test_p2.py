from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace
import csv
import json
import sys
import pytest

from antigen_design import adapters, pipeline, remote, remote_control, worker
from antigen_design.config import DEFAULTS, load_config, build_plan, validate_config
from antigen_design.contracts import candidate_record
from antigen_design.io import read_json, write_json, digest
from antigen_design.stages import selected_stages, check_prerequisites
from antigen_design.structure import Protein
from antigen_design.scheduler import slurm_script
from antigen_design.transport import OpenSSH
from antigen_design.report import report
from test_pipeline import Scheduler, make_plan, receipt


def test_benchmark_provenance(config):
    source = Path(config["input"])
    metadata = read_json(source.with_suffix(".provenance.json"))
    assert digest(source) == metadata["sha256"]
    assert metadata["pdb_id"] == "1UBQ"
    assert len(Protein(source).records) == 76


def test_server_separation(tmp_path, config):
    site = tmp_path / "site.yaml"
    site.write_text("execution:\n  cpu_host: cpu-site\n  gpu_host: gpu-site\n  resources:\n    cpu:\n      time: null\nmsa:\n  threads: 12\n")
    spec = tmp_path / "design.yaml"
    spec.write_text(f"input: {config['input']}\nmsa:\n  threads: 3\nselection:\n  regions: ['A:10-12']\n")
    c = load_config(spec, site)
    assert c["execution"]["cpu_host"] == "cpu-site"
    assert c["msa"]["threads"] == 3
    assert c["execution"]["resources"]["cpu"]["time"] is None
    site.write_text("selection: {regions: ['A:1-5']}\n")
    with pytest.raises(ValueError, match="Server profile"):
        load_config(spec, site)


def test_scope_rejects_implicit_inputs_and_accepts_optional_backbone(config, tmp_path):
    p = tmp_path / "empty.yaml"
    p.write_text("{}")
    with pytest.raises(ValueError, match="input"):
        load_config(p)
    config["rfd3"]["enabled"] = True
    validate_config(config)


def test_identities_preserve_origins(config):
    p = build_plan(config) | {"identity": "run1"}
    a = candidate_record(p, "b0", 0, {"A": "AAAA"}, 42)
    b = candidate_record(p, "b1", 0, {"A": "AAAA"}, 42)
    assert a["sequence_id"] == b["sequence_id"]
    assert a["candidate_id"] != b["candidate_id"]
    c = candidate_record(p, "b0", 1, {"A": "AA", "B": "AA"}, 42)
    assert a["sequence_id"] != c["sequence_id"]


def test_msa_boundary_never_submits_fold(config):
    plan, state = make_plan(config)
    scheduler = Scheduler()
    stages = selected_stages(until="msa")
    pipeline.advance(plan, state, scheduler, stages=stages)
    mpnn = scheduler.submitted[0][0]["id"]
    receipt(plan, mpnn)
    scheduler.states["1"] = "COMPLETED"
    pipeline.advance(plan, state, scheduler, stages=stages)
    for task, _ in scheduler.submitted[1:]:
        receipt(plan, task["id"])
        scheduler.states[state["tasks"][task["id"]]["job_id"]] = "COMPLETED"
    assert pipeline.advance(plan, state, scheduler, stages=stages)
    assert {t[0]["stage"] for t in scheduler.submitted} == {"mpnn", "msa"}
    assert all(v["state"] == "pending" for n, v in state["tasks"].items() if plan["tasks"][n]["stage"] == "fold")
    pipeline.advance(plan, state, scheduler, stages={"fold"})
    assert [t[0]["stage"] for t in scheduler.submitted][-2:] == ["fold", "fold"]


def test_only_fold_rejects_missing_msa(config):
    plan, state = make_plan(config)
    with pytest.raises(ValueError, match="upstream"):
        check_prerequisites(plan, state, {"fold"})


def test_doctor_only_probes_selected_stage(config, monkeypatch):
    seen = []
    def command(argv, host, timeout):
        seen.append((argv, host))
        return json.dumps({"errors": []})
    config["execution"]["python"]["msa"] = "python"
    monkeypatch.setattr(pipeline, "command", command)
    result = pipeline.doctor(config, stages={"msa"})
    assert result["ready"] and set(result["stages"]) == {"msa"}
    assert len(seen) == 1


def test_cpu_script_has_no_gpu_request(tmp_path):
    text = slurm_script(["python", "-m", "antigen_design.worker"], tmp_path, tmp_path / "log",
                        {"cpus": 4, "time": None, "partition": "cpu"}, "msa")
    assert "--gres" not in text and "--time" not in text


def test_openssh_quotes_remote_argv(config, monkeypatch):
    got = []
    monkeypatch.setattr("antigen_design.transport.subprocess.run", lambda argv, **kw: got.append(argv) or SimpleNamespace(stdout="ok\n"))
    assert OpenSSH(config).execute("cpu-host", ["python", "a;echo wrong"]) == "ok"
    assert got[0][-1].startswith("bash -lc ")
    assert "BatchMode=yes" in got[0]
    with pytest.raises(ValueError):
        OpenSSH(config).execute("-oProxyCommand=evil", ["true"])


def test_local_closed_loop_with_explicit_doubles(config, monkeypatch):
    """Real parser, controller, receipts and metrics; model and MSA calls are test doubles."""
    config["mpnn"]["sequences"] = 1
    config["execution"]["python"].update({s: sys.executable for s in ("mpnn", "msa", "fold", "metrics")})
    env = {s: {"errors": [], "test_double": True} for s in ("mpnn", "msa", "fold", "metrics")}
    monkeypatch.setattr(pipeline, "doctor", lambda *a, **k: {"ready": True, "stages": {s: env[s] for s in k.get("stages", env) if s in env}})
    monkeypatch.setattr(worker, "probe", lambda stage, c: env[stage])
    def mpnn(plan, task, directory):
        p = Protein(config["input"])
        chains = {"A": "".join(r.amino_acid for r in p.records)}
        write_json(directory / "sequences.json", [candidate_record(plan, "b0000", 0, chains, 42)])
    def msa(plan, task, directory):
        write_json(directory / "fold_input.json", {"test_double": True})
    def fold(plan, task, directory):
        p = directory / "prediction.pdb"
        Protein(config["input"]).normalized(p)
        cf = directory / "confidence.json"
        write_json(cf, {"test_double": True})
        write_json(directory / "predictions.json", [{"path": str(p), "sample": 0, "confidence": str(cf)}])
    monkeypatch.setattr(adapters, "mpnn", mpnn)
    monkeypatch.setattr(adapters, "msa", msa)
    monkeypatch.setattr(adapters, "fold", fold)
    def run(argv, **kwargs):
        worker.execute(argv[3], argv[4])
        return SimpleNamespace(returncode=0)
    monkeypatch.setattr(pipeline.subprocess, "run", run)
    plan = pipeline.initialize(config, stages={"mpnn", "msa"})
    state = pipeline.resume(config["output"], until="msa")
    assert state["tasks"]["fold_b0000_s0000_seed101"]["state"] == "pending"
    state = pipeline.resume(config["output"], only="fold")
    assert state["tasks"]["metrics_b0000_s0000_seed101"]["state"] == "pending"
    state = pipeline.resume(config["output"], only="metrics")
    assert all(t["state"] == "succeeded" for t in state["tasks"].values())
    rows = list(csv.DictReader(open(Path(config["output"]) / "summary.csv")))
    assert float(rows[0]["reference_all_rmsd_fit_all"]) < 0.002
    assert rows[0]["candidate_id"] and rows[0]["metric_version"] == "ca-mapped-v1"
    assert report(config["output"])["counts"] == {"computationally_evaluable": 1}


def test_report_does_not_pass_missing_metrics(tmp_path):
    (tmp_path / "summary.csv").write_text("design,status\na,succeeded\n")
    assert report(tmp_path)["results"][0]["decision"] == "insufficient_evidence"


def test_remote_no_retry_on_oom(config, monkeypatch):
    plan, state = make_plan(config)
    plan["local_output"] = config["output"]
    for name, t in state["tasks"].items():
        t.update(host=plan["tasks"][name]["host"], stage=plan["tasks"][name]["stage"], memory_tier=0, history=[])
    first = next(iter(state["tasks"]))
    state["tasks"][first].update(state="submitted", job_id="1", attempts=1)
    monkeypatch.setattr(remote, "control", lambda *a: {first: {"slurm_state": "OUT_OF_MEMORY", "error": {"error": "CUDA out of memory"}}})
    remote.refresh(plan, state, None)
    assert state["tasks"][first]["state"] == "failed"


def test_remote_submit_idempotence(config, tmp_path, monkeypatch):
    config["execution"]["python"]["mpnn"] = "/env/python"
    plan, state = make_plan(config)
    seen = []
    monkeypatch.setattr(remote_control.subprocess, "run", lambda argv, **kw: seen.append(argv) or SimpleNamespace(returncode=0, stdout="123;cluster\n", stderr=""))
    request = {"task": "mpnn_b0000", "attempt": 1}
    a = remote_control.submit(plan, request)
    b = remote_control.submit(plan, request)
    assert a == b and a["job_id"] == "123" and len(seen) == 1
    assert seen[0][2:4] == ["--chdir", str(Path(config["output"]))]


@pytest.mark.parametrize("mapping", [[-1], [76], [True], [0.5]])
def test_metrics_reject_invalid_indices(mapping):
    import numpy as np
    from antigen_design.metrics import compare
    with pytest.raises(ValueError, match="Mapping index"):
        compare(np.zeros((76, 3)), np.zeros((1, 3)), mapping, set())


def test_metrics_reject_nonfinite_coordinates():
    import numpy as np
    from antigen_design.metrics import compare
    with pytest.raises(ValueError, match="finite"):
        compare(np.zeros((3, 3)), np.full((3, 3), np.nan), [0, 1, 2], {0})


def test_worker_rejects_implicit_parameter_fallback():
    with pytest.raises(ValueError, match="fallback"):
        worker.execute("not-read.json", "not-run", memory_tier=1)


def test_msa_cache_reuse_and_tamper_rebuild(config, tmp_path, monkeypatch):
    converter = tmp_path / "converter.py"
    converter.write_text('''from pathlib import Path
class A3MProcessor:
    def __init__(self, source, out):
        self.chain_info = [1]
        base = Path(out)/"msa"/"0"
        base.mkdir(parents=True, exist_ok=True)
        for name in ("pairing.a3m", "non_pairing.a3m"):
            (base/name).write_text(">query\\nAAAA\\n")
''')
    config["msa"].update(script=str(converter), database_version="test-db", database=str(tmp_path), cache_root=str(tmp_path / "cache"))
    plan = {"config": config, "environment": {"msa": {"code": "test", "versions": {}}}}
    monkeypatch.setattr(adapters, "sequence_record", lambda *a: {"id": "test-candidate", "chains": {"A": "AAAA"}})
    calls = []
    def search(argv, **kwargs):
        calls.append(argv)
        out = Path(argv[3]); out.mkdir(parents=True)
        (out / "query.a3m").write_text(">query\nAAAA\n")
    monkeypatch.setattr(adapters.subprocess, "run", search)
    for i in range(2):
        out = tmp_path / f"task-{i}"; out.mkdir()
        adapters.msa(plan, {}, out)
    assert len(calls) == 1
    assert calls[0][-2:] == ["--gpu", "0"]
    artifact = next((tmp_path / "cache").rglob("pairing.a3m"))
    artifact.write_text(">query\nCCCC\n")
    out = tmp_path / "task-2"; out.mkdir()
    adapters.msa(plan, {}, out)
    assert len(calls) == 2
    result = read_json(out / "fold_input.json")
    assert result[0]["sequences"][0]["proteinChain"]["sequence"] == "AAAA"


def test_fold_never_invokes_search(config, tmp_path, monkeypatch):
    """Capture inference argv; no model execution in this contract test."""
    config["folding"]["weights"] = str(tmp_path)
    (tmp_path / "protenix-v2.pt").write_text("test checkpoint placeholder")
    monkeypatch.setattr(adapters, "sequence_record", lambda *a: {"id": "x", "chains": {"A": "AAAA"}})
    calls = []
    monkeypatch.setattr(adapters.subprocess, "run", lambda argv, **kwargs: calls.append(argv))
    with pytest.raises(ValueError, match="produced 0"):
        adapters.fold({"config": config}, {"seed": 1}, tmp_path / "fold")
    assert len(calls) == 1 and calls[0][1:3] == ["-m", "runner.inference"]
    assert "--use_msa" in calls[0] and "colabfold_search" not in " ".join(calls[0])
