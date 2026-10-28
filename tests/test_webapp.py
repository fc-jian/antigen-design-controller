"""Web/input contracts; scheduler and model calls here are test doubles."""
from copy import deepcopy
import json
from pathlib import Path
import threading
import urllib.request
import urllib.error
import uuid
import pytest
import yaml
from antigen_design.config import DEFAULTS, build_plan
from antigen_design.folding_input import parse_input
from antigen_design.io import read_json, write_json
from antigen_design.pipeline import task_graph
from antigen_design.web_store import Store


def test_af3_mixed_entities_seeds_modifications_and_bonds():
    payload = {"name": "Mixed", "dialect": "alphafold3", "version": 1, "modelSeeds": [7, 9],
               "sequences": [
                   {"protein": {"id": ["A", "B"], "sequence": "ACD",
                                "modifications": [{"ptmType": "SEP", "ptmPosition": 1}]}},
                   {"rna": {"id": "R", "sequence": "ACGU"}},
                   {"dna": {"id": "D", "sequence": "ACGT",
                            "modifications": [{"modificationType": "6MA", "basePosition": 1}]}},
                   {"ligand": {"id": "L", "ccdCodes": ["NAG", "NAG"]}},
                   {"ligand": {"id": "S", "smiles": "CCO"}}],
               "bondedAtomPairs": [[["A", 2, "SG"], ["L", 1, "C1"]]]}
    job = parse_input(json.dumps(payload))[0]
    assert job["seeds"] == [7, 9]
    assert job["sequences"][0]["proteinChain"]["count"] == 2
    assert job["sequences"][0]["proteinChain"]["modifications"][0]["ptmType"] == "CCD_SEP"
    assert job["sequences"][3]["ligand"]["ligand"] == "CCD_NAG_NAG"
    assert job["covalent_bonds"][0] == {"entity1": "1", "copy1": 1, "position1": "2", "atom1": "SG",
                                             "entity2": "4", "copy2": 1, "position2": "1", "atom2": "C1"}
    assert payload["sequences"][0]["protein"]["modifications"][0]["ptmType"] == "SEP"


@pytest.mark.parametrize("patch", [
    {"userCCD": "data_bad"}, {"userCCDPath": "/etc/passwd"}, {"constraint": {}},
    {"modelSeeds": []}, {"modelSeeds": [True]}, {"modelSeeds": [1, 1]},
    {"sequences": [{"protein": {"id": "A", "sequence": "ACD", "unpairedMsa": ""}}]},
    {"sequences": [{"protein": {"id": ["A", "A"], "sequence": "ACD"}}]},
    {"sequences": [{"protein": {"id": "A", "sequence": "ACD", "templates": []}}]},
    {"sequences": [{"protein": {"id": "A", "sequence": "ACD", "modifications": [{"ptmType": "SEP", "ptmPosition": 8}]}}]},
    {"sequences": [{"ligand": {"id": "L", "smiles": "FILE_/etc/passwd"}}]},
    {"sequences": [{"ligand": {"id": "L", "ccdCodes": ["ATP"], "smiles": "CCO"}}]},
])
def test_unsupported_input_never_silently_dropped(patch):
    raw = {"name": "test", "dialect": "alphafold3", "version": 1,
           "sequences": [{"protein": {"id": "A", "sequence": "ACD"}}]} | patch
    with pytest.raises(ValueError):
        parse_input(json.dumps(raw))


def test_fasta_independent_jobs_and_safe_identifiers():
    jobs = parse_input(">../../escape\nACD\nEF\n>same name\nMKT")
    assert [j["name"] for j in jobs] == ["input_0000", "input_0001"]
    assert jobs[0]["display_name"] == "../../escape"
    assert jobs[0]["sequences"][0]["proteinChain"]["sequence"] == "ACDEF"
    with pytest.raises(ValueError):
        parse_input(">bad\nACD:EF")


def test_folding_graph_has_no_design_and_preserves_per_job_seeds(tmp_path, config):
    path = tmp_path / "fold.json"
    path.write_text(json.dumps([{"name": "one", "modelSeeds": [1, 2], "sequences": [{"proteinChain": {"sequence": "ACD", "count": 1}}]},
                                {"name": "two", "modelSeeds": [3], "sequences": [{"rnaSequence": {"sequence": "ACGU", "count": 1}}]}]))
    config.update(mode="folding", input=str(path))
    config["msa"]["batch_size"] = 100
    plan = build_plan(config)
    plan["identity"] = "test"
    tasks = task_graph(plan)
    assert plan["expected_predictions"] == 3
    assert len([t for t in tasks.values() if t["stage"] == "msa"]) == 1
    assert {t["stage"] for t in tasks.values()} == {"msa", "fold", "metrics"}
    assert [t["seed"] for t in tasks.values() if t["stage"] == "fold"] == [1, 2, 3]
    assert plan["fold_records"][1]["chains"] == {}


@pytest.fixture
def store(tmp_path):
    profiles = tmp_path / "configs" / "servers"
    profiles.mkdir(parents=True)
    (profiles / "test.yaml").write_text(yaml.safe_dump({"execution": {"backend": "slurm", "transport": "openssh",
                 "remote_root": "/shared/owned", "cpu_host": "cpu", "gpu_host": "gpu"}}))
    return Store(tmp_path)


def request():
    return {"id": uuid.uuid4().hex, "name": "Test", "profile": "test", "mode": "folding",
            "input": ">one\nACDEFG", "seeds": [101], "samples": 1}


def test_submission_deduplication_and_project_storage(store):
    req = request()
    first = store.submit(req)
    assert store.submit(req)["id"] == first["id"]
    assert store.registered() == [req["id"]]
    assert store.detail(req["id"])["state"] == "queued"
    config = read_json(store.root / "configs" / (req["id"] + ".json"))
    assert Path(config["input"]).is_relative_to(store.root / "inputs")
    assert Path(config["output"]).is_relative_to(store.root / "runs")
    with pytest.raises(ValueError, match="different submission"):
        store.submit(req | {"name": "changed"})
    with pytest.raises(ValueError):
        store.job_root("../1ubq")
    with pytest.raises(ValueError):
        store.job_root(uuid.uuid4().hex)


def test_archive_preserves_run_and_prevents_active_or_unknown_jobs(store):
    from antigen_design.webapp import Application
    req = request()
    store.submit(req)
    root = store.job_root(req["id"])
    app = Application(store, launch=False)
    with pytest.raises(ValueError, match="stopped"):
        app.action(req["id"], "archive")
    write_json(root / "status.json", {"state": "cancelled"})
    write_json(root / "pipeline/manifest.json", {"tasks": {"one": {"state": "submitting"}}})
    with pytest.raises(ValueError, match="ambiguous"):
        app.action(req["id"], "archive")
    write_json(root / "pipeline/manifest.json", {"tasks": {"one": {"state": "blocked"}}})
    assert app.action(req["id"], "archive")["archived"]
    assert read_json(root / "request.json") == req
    assert req["id"] in store.registered()
    with pytest.raises(ValueError, match="Restore"):
        app.action(req["id"], "retry")
    assert not app.action(req["id"], "unarchive")["archived"]


def test_structure_original_numbering_and_variable_length(store, synthetic):
    asset = store.upload("synthetic.pdb", synthetic.read_text())
    assert any(r["original_id"] == "A:11A" for r in asset["residues"])
    req = {"name": "test", "profile": "test", "mode": "design", "asset": asset["id"],
           "regions": [{"region": "A:11A-15", "length": [2, 4]}], "rfd3": True, "backbones": 3, "sequences": 2}
    preview = store.preview(req)
    assert preview["selected"] == ["A:11A", "A:14", "A:15"]
    assert preview["expected_predictions"] == 6
    with pytest.raises(ValueError, match="Variable-length"):
        store.preview(req | {"rfd3": False})
    with pytest.raises(ValueError, match="Overlapping"):
        store.preview(req | {"regions": ["A:10-14", "A:11A-15"]})


def test_surface_threshold_mapping_and_input_identity(store, synthetic, monkeypatch):
    from antigen_design.structure import Protein
    asset = store.upload("synthetic.pdb", synthetic.read_text())
    values = [0.19999, 0.2, 0.20001] + [0.0] * (len(asset["residues"]) - 3)
    monkeypatch.setattr(Protein, "surface", lambda self, **kwargs: (set(), values))
    surface = store.surface(asset["id"])
    assert [r["surface"] for r in surface["residues"][:3]] == [False, True, True]
    assert [r["original_id"] for r in surface["residues"]] == [r["original_id"] for r in asset["residues"]]
    assert surface["points"] == 960
    path = store.root / "inputs" / asset["id"] / asset["path"]
    path.write_text(path.read_text() + "\n")
    with pytest.raises(ValueError, match="changed since upload"):
        store.surface(asset["id"])


def test_surface_1ubq_has_buried_and_exposed_residues(store):
    import math
    source = Path(__file__).parents[1] / "projects/1ubq/inputs/1ubq.cif"
    asset = store.upload("1ubq.cif", source.read_text())
    result = store.surface(asset["id"])
    assert len(result["residues"]) == 76
    assert all(math.isfinite(r["rsasa"]) and r["rsasa"] >= 0 for r in result["residues"])
    assert 0 < sum(r["surface"] for r in result["residues"]) < 76
    assert result["context"] == "all_protein_chains"


def test_cancel_and_retry_never_cross_registry(store):
    from antigen_design.webapp import Application
    app = Application(store, launch=False)
    req = request()
    store.submit(req)
    app.action(req["id"], "cancel")
    assert (store.job_root(req["id"]) / "cancel.request").exists()
    with pytest.raises(ValueError):
        app.action(uuid.uuid4().hex, "cancel")
    root = store.job_root(req["id"])
    write_json(root / "status.json", {"state": "attention"})
    write_json(root / "pipeline" / "manifest.json", {"tasks": {"msa": {"state": "submitting"}}})
    with pytest.raises(ValueError, match="unknown"):
        app.action(req["id"], "retry")


def test_http_origin_and_host_guards(store):
    from http.server import ThreadingHTTPServer
    from antigen_design.webapp import Application, Handler
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server.app = Application(store, launch=False)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    url = f"http://127.0.0.1:{server.server_address[1]}"
    open_local = urllib.request.build_opener(urllib.request.ProxyHandler({})).open
    try:
        with open_local(url + "/api/config") as response:
            assert json.load(response)["local_only"] is True
        for headers in [
            {"Host": "evil.test"},
            {"Origin": "https://evil.test", "X-Antigen-Client": "local", "Content-Type": "application/json"},
            {"Content-Type": "application/json"}]:
            req = urllib.request.Request(url + "/api/preview", data=json.dumps(request()).encode(), headers=headers)
            with pytest.raises(urllib.error.HTTPError) as exc:
                open_local(req)
            assert exc.value.code == 403
        req = urllib.request.Request(url + "/api/preview", data=json.dumps(request()).encode(),
                                     headers={"X-Antigen-Client": "local", "Content-Type": "application/json"})
        with open_local(req) as response:
            assert json.load(response)["expected_predictions"] == 1
        assert store.registered() == []  # preview must never enqueue computation
    finally:
        server.shutdown()
        server.server_close()


def test_cancel_checks_identity_before_scancel(monkeypatch):
    from antigen_design import remote_control
    import getpass
    plan = {"identity": "abcdef12345", "config": {"output": "/owned/run"},
            "tasks": {"msa": {"signature": "s"}}}
    requests = {"msa": {"job_id": "123", "attempts": 1}}
    calls = []
    monkeypatch.setattr(remote_control.subprocess, "run", lambda argv, **kw: calls.append(argv))
    monkeypatch.setattr(remote_control.subprocess, "check_output", lambda *a, **kw:
                        f"123|RUNNING|0:0|00:01|8|16G|cpu|WRONG|/owned/run|{getpass.getuser()}|\n")
    with pytest.raises(ValueError):
        remote_control.cancel(plan, requests)
    assert calls == []
    monkeypatch.setattr(remote_control.subprocess, "check_output", lambda *a, **kw:
                        f"123|RUNNING|0:0|00:01|8|16G|cpu|ah_abcdef12_msa_a1|/owned/run|{getpass.getuser()}|\n")
    remote_control.cancel(plan, requests)
    assert calls == [["scancel", "123"]]


def test_collect_rejects_archive_traversal(store, monkeypatch):
    import io
    import tarfile
    from antigen_design import remote
    from antigen_design.web_runner import extract_results
    job = request()
    store.submit(job)
    root = store.job_root(job["id"])
    archive = root / "bad.tar.gz"
    with tarfile.open(archive, "w:gz") as stream:
        member = tarfile.TarInfo("../../escape")
        member.size = 3
        stream.addfile(member, io.BytesIO(b"bad"))
    monkeypatch.setattr(remote, "collect", lambda root: archive)
    with pytest.raises(ValueError, match="Unsafe"):
        extract_results(root, {"config": {"output": "/remote"}})


def test_nucleic_only_msa_preserves_entities_without_search(tmp_path, monkeypatch):
    from antigen_design import msa_batch
    from antigen_design.io import digest
    c = deepcopy(DEFAULTS)
    converter = tmp_path / "converter.py"
    converter.write_text("# test double")
    c["msa"].update(script=str(converter), database_version="test", cache_root=str(tmp_path / "cache"))
    job = parse_input('[{"name":"rna","sequences":[{"rnaSequence":{"sequence":"ACGU","count":1}}]}]')[0]
    record = {"id": "input_0000", "chains": {}, "job": job}
    plan = {"mode": "folding", "config": c, "environment": {"msa": {"code": "test"}},
            "fold_records": [record]}
    def no_search(*args, **kwargs):
        raise AssertionError("Nucleic-only batch must not call protein search")
    monkeypatch.setattr(msa_batch.subprocess, "run", no_search)
    msa_batch.run(plan, {"members": [{"record": 0}]}, tmp_path)
    payload = read_json(tmp_path / "fold_inputs" / "input_0000.json")
    assert payload[0]["sequences"] == job["sequences"]
    assert read_json(tmp_path / "msa_batch.json")["search_invocations"] == 0


def test_folding_dry_run_needs_no_fake_reference(config, tmp_path):
    from antigen_design.pipeline import initialize, verify_plan
    path = tmp_path / "sequence.fa"
    path.write_text(">test\nACDEF")
    config.update(mode="folding", input=str(path))
    plan = initialize(config, dry_run=True)
    assert "reference_sha256" not in plan
    assert not (Path(config["output"]) / "reference.pdb").exists()
    assert "summary.csv" in {p.name for p in Path(config["output"]).iterdir()}
    with pytest.raises(ValueError, match="Dry-run"):
        verify_plan(plan)


def test_collected_result_hashes_confidence_and_pae(store, synthetic, monkeypatch):
    import tarfile
    from Bio.PDB import PDBParser, MMCIFIO
    from antigen_design import remote
    from antigen_design.io import digest
    from antigen_design.web_runner import extract_results
    req = request()
    store.submit(req)
    root = store.job_root(req["id"])
    source = root / "test-source"
    fold = source / "tasks" / "fold_test"
    metrics = source / "tasks" / "metrics_test"
    fold.mkdir(parents=True)
    metrics.mkdir(parents=True)
    writer = MMCIFIO()
    writer.set_structure(PDBParser(QUIET=True).get_structure("test", synthetic))
    writer.save(str(fold / "test_sample_0.cif"))
    write_json(fold / "test_summary_confidence_sample_0.json", {"plddt": 20.0, "ptm": .6})
    write_json(fold / "test_full_data_sample_0.json", {"token_pair_pae": [[0.5, 3.0], [4.0, 0.5]]})
    write_json(metrics / "metrics.json", [{"design": "test", "fold_seed": 101, "sample": 0,
                "prediction": "/remote/tasks/fold_test/test_sample_0.cif",
                "confidence_path": "/remote/tasks/fold_test/test_summary_confidence_sample_0.json"}])
    for directory in (fold, metrics):
        artifacts = {"/remote/" + str(p.relative_to(source)): digest(p) for p in directory.iterdir()}
        write_json(directory / "complete.json", {"signature": directory.name, "artifacts": artifacts})
    archive = root / "results.tar.gz"
    with tarfile.open(archive, "w:gz") as stream:
        for path in source.rglob("*"):
            if path.is_file():
                stream.add(path, arcname=str(path.relative_to(source)))
    monkeypatch.setattr(remote, "collect", lambda root: archive)
    plan = {"config": {"output": "/remote"}, "expected_predictions": 1,
            "tasks": {"fold_test": {"signature": "fold_test"}, "metrics_test": {"signature": "metrics_test"}}}
    extract_results(root, plan)
    result = read_json(root / "result-0.json")
    assert len(result["residues"]) == 10
    assert result["residues"][0]["plddt"] == 20.0
    assert result["pae"][0][1] == 3.0
    assert "pae" not in read_json(root / "results.json")[0]
    plan["tasks"]["fold_test"]["signature"] = "tampered"
    with pytest.raises(ValueError, match="signature mismatch"):
        extract_results(root, plan)


def test_folding_output_identity_rejects_wrong_chemistry(synthetic, tmp_path):
    from Bio.PDB import PDBParser, MMCIFIO
    from antigen_design.folding_results import validate_structure
    writer = MMCIFIO()
    writer.set_structure(PDBParser(QUIET=True).get_structure("test", synthetic))
    path = tmp_path / "prediction.cif"
    writer.save(str(path))
    # Synthetic author's numbering is gapped; output identity requires explicit 1-based positions.
    from antigen_design.structure import Protein
    normalized = tmp_path / "normalized.pdb"
    Protein(synthetic).normalized(normalized)
    writer.set_structure(PDBParser(QUIET=True).get_structure("test", normalized))
    writer.save(str(path))
    job = parse_input('[{"sequences":[{"proteinChain":{"sequence":"AAAAA","count":2,"id":["A","B"]}}]}]')[0]
    assert len(validate_structure(path, job)) == 10
    job["sequences"][0]["proteinChain"]["sequence"] = "CAAAA"
    with pytest.raises(ValueError, match="chemical identity"):
        validate_structure(path, job)


def test_cancel_before_initialize_never_submits(store, monkeypatch):
    from antigen_design import pipeline
    from antigen_design.web_runner import run
    req = request()
    store.submit(req)
    root = store.job_root(req["id"])
    (root / "cancel.request").touch()
    def forbidden(*args, **kwargs):
        raise AssertionError("Cancelled queued request must not initialize or submit")
    monkeypatch.setattr(pipeline, "initialize", forbidden)
    run(store, req["id"])
    assert store.detail(req["id"])["state"] == "cancelled"


@pytest.fixture
def web_server(store):
    from http.server import ThreadingHTTPServer
    from antigen_design.webapp import Application, Handler
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server.app = Application(store, launch=False)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{server.server_address[1]}"
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    def call(path, data=None):
        req = urllib.request.Request(base + path, data=None if data is None else json.dumps(data).encode(),
            headers={"X-Antigen-Client": "local", "Content-Type": "application/json"})
        with opener.open(req, timeout=5) as response:
            return json.load(response)
    yield server.app, call
    server.shutdown()
    server.server_close()
    thread.join(timeout=5)


def test_manager_isolates_bad_records_launch_errors_and_recovers(store, monkeypatch):
    from antigen_design.webapp import Application
    from antigen_design import webapp
    bad, launch_error, good = request(), request(), request()
    for req in (good, launch_error, bad):
        store.submit(req)
    (store.job_root(bad["id"]) / "status.json").unlink()
    launched = []
    class Process:
        def poll(self): return None
    def popen(argv, **kwargs):
        if argv[-1] == launch_error["id"]:
            raise OSError("simulated launch failure")
        launched.append(argv[-1])
        return Process()
    monkeypatch.setattr(webapp.subprocess, "Popen", popen)
    app = Application(store, launch=False)
    app.manage_once()
    assert launched == [good["id"]]
    assert set(app.health()["errors"]) == {bad["id"], launch_error["id"]}
    for req in (bad, launch_error):
        write_json(store.job_root(req["id"]) / "status.json", {"state": "cancelled"})
    app.manage_once()
    assert app.health()["errors"] == {}
    assert launched == [good["id"]]  # existing controller is not launched twice
    store.registry.write_text('{"broken": true}')
    app.manage_once()
    assert "registry" in app.health()["errors"]
    write_json(store.registry, [])
    app.manage_once()
    assert app.health()["errors"] == {}


def test_initialization_window_and_corrupt_record_do_not_hide_jobs(store, web_server):
    _, call = web_server
    req = request()
    store.submit(req)
    root = store.job_root(req["id"])
    write_json(root / "pipeline/plan.json", {"tasks": {}})
    (root / "controller.log").write_text("initialization diagnostic")
    assert call(f'/api/jobs/{req["id"]}')["initializing"] is True
    assert call(f'/api/jobs/{req["id"]}')["controller_log"] == "initialization diagnostic"
    assert call('/api/jobs')["items"][0]["id"] == req["id"]
    write_json(root / "pipeline/manifest.json", {"tasks": {}})
    assert not call(f'/api/jobs/{req["id"]}').get("initializing")
    (root / "status.json").write_text('{broken')
    healthy = request()
    store.submit(healthy)
    page = call('/api/jobs')
    assert page["total"] == 2
    assert page["items"][1]["unavailable"]
    assert call(f'/api/jobs/{req["id"]}')["unavailable"]


def test_incomplete_initialization_stops_before_remote_access(store, monkeypatch):
    from antigen_design.web_runner import run
    from antigen_design import pipeline
    req = request()
    store.submit(req)
    root = store.job_root(req["id"])
    write_json(root / "pipeline/plan.json", {"unchanged": True})
    def forbidden(*args, **kwargs):
        raise AssertionError("Incomplete local records must not contact a remote host")
    monkeypatch.setattr(pipeline, "doctor", forbidden)
    monkeypatch.setattr(pipeline, "initialize", forbidden)
    run(store, req["id"])
    assert read_json(root / "status.json")["state"] == "attention"
    assert "manifest missing" in read_json(root / "status.json")["error"]
    assert read_json(root / "pipeline/plan.json") == {"unchanged": True}
    assert not (root / "pipeline/manifest.json").exists()


@pytest.mark.parametrize("route,method,payload", [
    ("/api/preview", "preview", {}),
    ("/api/assets", "upload", {"filename": "test.pdb", "content": "test"}),
    ("/api/assets/" + "a" * 32 + "/surface", "surface", {}),
])
def test_slow_requests_do_not_block_cancel_or_health(store, web_server, monkeypatch, route, method, payload):
    from concurrent.futures import ThreadPoolExecutor
    app, call = web_server
    req = request()
    store.submit(req)
    entered, release = threading.Event(), threading.Event()
    def slow(*args):
        entered.set()
        assert release.wait(5)
        return {"done": True}
    monkeypatch.setattr(store, method, slow)
    with ThreadPoolExecutor() as pool:
        pending = pool.submit(call, route, payload)
        try:
            assert entered.wait(2)
            cancelled = pool.submit(call, f'/api/jobs/{req["id"]}/cancel', {}).result(timeout=2)
            assert cancelled["id"] == req["id"]
            assert (store.job_root(req["id"]) / "cancel.request").exists()
            assert call('/api/health')["enabled"] is False
            assert app.lock.acquire(blocking=False)
            app.lock.release()
        finally:
            release.set()
        assert pending.result(timeout=2) == {"done": True}


def test_concurrent_submissions_keep_registry_and_idempotency(store, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    original = store.compile
    barrier = threading.Barrier(4)
    def compile(*args):
        value = original(*args)
        barrier.wait(timeout=5)
        return value
    monkeypatch.setattr(store, "compile", compile)
    duplicate = request()
    requests = [duplicate, duplicate, request(), request()]
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(store.submit, requests))
    assert len(store.registered()) == 3
    assert {r["id"] for r in results} == set(store.registered())
    config = read_json(store.root / "configs" / (duplicate["id"] + ".json"))
    assert Path(config["input"]).read_text() == duplicate["input"]


def test_list_is_paginated_filtered_and_does_not_read_heavy_details(store, web_server, monkeypatch):
    _, call = web_server
    ids = [f"{i:032x}" for i in range(30)]
    write_json(store.registry, ids)
    for i, job in enumerate(ids):
        root = store.root / "runs" / job
        write_json(root / "request.json", {"name": f"job {i}", "mode": "folding", "profile": "test"})
        write_json(root / "status.json", {"state": "succeeded"})
        write_json(root / "archive.json", {"archived": i < 5})
        write_json(root / "pipeline/manifest.json", {"tasks": {"one": {"state": "succeeded"}}})
        # Invalid heavy data must not be read by the summary endpoint.
        (root / "pipeline/plan.json").write_text('broken')
        (root / "monitor.json").write_text('broken')
        (root / "results.json").write_text('broken')
    monkeypatch.setattr(store, "detail", lambda *a: pytest.fail("List must not load full details"))
    page = call('/api/jobs?archive=all&offset=25&limit=5&profile=test&state=succeeded')
    assert [j["id"] for j in page["items"]] == ids[25:]
    assert page["total"] == page["counts"]["total"] == 30
    assert all(j["completed_tasks"] == 1 for j in page["items"])
    assert all(not {"tasks", "results", "controller_log"} & j.keys() for j in page["items"])
    assert call('/api/jobs?archive=archived')["total"] == 5
    assert call('/api/jobs')["total"] == 25
    assert call('/api/jobs?state=problem')["total"] == 0
    for query in ('limit=101', 'offset=-1', 'archive=invalid'):
        with pytest.raises(urllib.error.HTTPError) as error:
            call('/api/jobs?' + query)
        assert error.value.code == 400


def test_read_only_mode_cannot_submit_or_mutate(store, web_server, monkeypatch):
    from antigen_design.webapp import Application
    app, call = web_server
    app.read_only = True
    with pytest.raises(urllib.error.HTTPError) as error:
        call('/api/jobs', request())
    assert error.value.code == 403
    assert store.registered() == []
    readonly = Application(store, read_only=True)
    assert readonly.manager_thread is None
    assert readonly.health()["enabled"] is False


def test_design_annotation_uses_exact_backbone_and_preserves_cached_result(store):
    from antigen_design.folding_results import annotate_design
    result = {"metrics": {"backbone": "b1"}, "residues": [
        {"chain": "A", "position": "1", "resname": "ALA"},
        {"chain": "A", "position": "2", "resname": "ALA"},
        {"chain": "B", "position": "1", "resname": "GLY"}]}
    plan = {"backbones": [{"id": "b0", "mapping": []}, {"id": "b1", "mapping": [
        {"chain": "A", "position": 1, "design": False, "reference": 0},
        {"chain": "A", "position": 2, "design": True, "reference": None},
        {"chain": "B", "position": 1, "design": True, "reference": 4}]}]}
    original = deepcopy(result)
    annotated = annotate_design(result, plan)
    assert annotated["design_mapping_available"]
    assert [r["is_designed"] for r in annotated["residues"]] == [False, True, True]
    assert result == original  # no rewrite of stored historical result or scientific identity
    for invalid in ({}, {"backbones": plan["backbones"][:1]}):
        unknown = annotate_design(result, invalid)
        assert not unknown["design_mapping_available"]
        assert all(r["is_designed"] is None for r in unknown["residues"])
    changed = deepcopy(result)
    changed["residues"][0]["position"] = "99"
    assert not annotate_design(changed, plan)["design_mapping_available"]
    req = request(); store.submit(req)
    root = store.job_root(req["id"])
    write_json(root / "pipeline/plan.json", plan)
    write_json(root / "results.json", [{"id": 0}])
    write_json(root / "result-0.json", result)
    from antigen_design.io import digest
    before = digest(root / "result-0.json")
    assert store.result_view(req["id"], 0)["design_mapping_available"]
    assert digest(root / "result-0.json") == before
