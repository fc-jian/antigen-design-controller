import json
from pathlib import Path
import pytest
from antigen_design.config import load_config, build_plan
from antigen_design.pipeline import task_graph
from antigen_design.stages import stage_host, resource_key, uses_gpu
from antigen_design import adapters, msa_batch
from antigen_design.io import read_json


def test_region_matrix_mapping_and_batch_graph():
    c = load_config(Path(__file__).parents[1] / "projects/1ubq/configs/ubiquitin-matrix.yaml")
    plan = build_plan(c) | {"identity": "test"}
    assert [len(b["mapping"]) for b in plan["backbones"]] == [76, 75, 78, 76, 76]
    assert [len(b["selected"]) for b in plan["backbones"]] == [3, 5, 3, 6, 9]
    tasks = task_graph(plan)
    batches = [t for t in tasks.values() if t["stage"] == "msa"]
    assert len(batches) == 1 and len(batches[0]["members"]) == 10 and len(batches[0]["deps"]) == 5
    assert sum(t["stage"] == "fold" for t in tasks.values()) == 10
    for backbone in plan["backbones"]:
        retained = [m["reference"] for m in backbone["mapping"] if not m["design"]]
        assert retained == [i for i in range(76) if i not in backbone["selected"]]


def test_gpu_msa_routes_separately(config):
    config["msa"]["gpu"] = True
    config["execution"]["msa_host"] = "msa-gpu"
    assert stage_host(config, "msa") == "msa-gpu"
    assert resource_key(config, "msa") == "msa_gpu"
    assert uses_gpu(config, "msa")
    assert resource_key(config, "metrics") == "metrics"


@pytest.fixture
def batch_setup(config, tmp_path, monkeypatch):
    converter = tmp_path / "converter.py"
    converter.write_text('''from pathlib import Path
class A3MProcessor:
    def __init__(self, source, out):
        self.chain_info = [1]
        base = Path(out)/"msa"/"0"
        base.mkdir(parents=True, exist_ok=True)
        for name in ("pairing.a3m", "non_pairing.a3m"):
            (base/name).write_text(Path(source).read_text())
''')
    config["msa"].update(script=str(converter), database_version="test-db", database=str(tmp_path),
                         cache_root=str(tmp_path / "cache"), batch_size=10, ignore_index=True)
    records = [{"id": "c0", "chains": {"A": "AAAA"}}, {"id": "c1", "chains": {"A": "AAAC"}},
               {"id": "c2", "chains": {"A": "AAAA"}}]
    monkeypatch.setattr(adapters, "sequence_record", lambda plan, task: records[task["sequence"]])
    calls = []
    def search(argv, **kwargs):
        calls.append((argv, kwargs))
        out = Path(argv[3]); out.mkdir()
        lines = Path(argv[1]).read_text().splitlines()
        for i in range(0, len(lines), 2):
            (out / (lines[i][1:] + ".a3m")).write_text(lines[i] + "\n" + lines[i+1] + "\n")
    monkeypatch.setattr(msa_batch.subprocess, "run", search)
    plan = {"config": config, "environment": {"msa": {"code": "test", "versions": {}}}}
    task = {"members": [{"sequence": i} for i in range(3)]}
    return plan, task, calls


def test_batch_deduplicates_reuses_and_repairs_corruption(batch_setup, tmp_path):
    plan, task, calls = batch_setup
    for i in range(2):
        directory = tmp_path / f"out{i}"; directory.mkdir()
        msa_batch.run(plan, task, directory)
    assert len(calls) == 1
    assert calls[0][1]["env"]["MMSEQS_IGNORE_INDEX"] == "1"
    report = read_json(tmp_path / "out0/msa_batch.json")
    assert report["candidates"] == 3 and report["searched_sequences"] == 2
    assert read_json(tmp_path / "out1/msa_batch.json")["cache_hits"] == 2
    assert len(list((tmp_path / "out1/fold_inputs").glob("*.json"))) == 3
    next((tmp_path / "cache").rglob("pairing.a3m")).write_text(">broken\nCCCC\n")
    directory = tmp_path / "out2"; directory.mkdir()
    msa_batch.run(plan, task, directory)
    assert len(calls) == 2
    assert read_json(directory / "msa_batch.json")["searched_sequences"] == 1


def test_failed_batch_cannot_publish_completion(batch_setup, tmp_path, monkeypatch):
    plan, task, calls = batch_setup
    def failed(*a, **kw):
        raise RuntimeError("simulated search interruption")
    monkeypatch.setattr(msa_batch.subprocess, "run", failed)
    directory = tmp_path / "failed"; directory.mkdir()
    with pytest.raises(RuntimeError, match="interruption"):
        msa_batch.run(plan, task, directory)
    assert not list((tmp_path / "cache").rglob("complete.json"))
    assert not (directory / "msa_artifacts.json").exists()


def test_query_order_independent_of_cache_keys(batch_setup,tmp_path,monkeypatch):
    plan,task,calls=batch_setup
    original=msa_batch.fingerprint
    # Force lexical cache order opposite to candidate input order.
    monkeypatch.setattr(msa_batch,"fingerprint",lambda value: ("z" if value.get("chains")==["AAAA"] else "a")+original(value))
    directory=tmp_path/"ordered";directory.mkdir()
    msa_batch.run(plan,task,directory)
    assert (directory/"queries.fasta").read_text()==">q000000\nAAAA\n>q000001\nAAAC\n"
    assert read_json(directory/"msa_batch.json")["unique_sequences"]==2
