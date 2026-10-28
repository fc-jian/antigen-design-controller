"""Execution tests use tiny synthetic database files and fake searches."""
from copy import deepcopy
from pathlib import Path
import subprocess
import pytest
from antigen_design.config import DEFAULTS, validate_config
from antigen_design import msa_execution as execution
from antigen_design.io import read_json


def database(root):
    root.mkdir()
    for db in ('db1', 'db3'):
        for suffix in ('', '_seq', '_h'):
            for ending in ('.dbtype', '.index'):
                (root / (db + suffix + ending)).write_bytes(b'0000')
            for i in range(2):
                (root / f'{db}{suffix}.{i}').write_bytes(b'data')
        (root / (db + '.idx')).write_bytes(b'index')
    return root


def test_split_database_and_missing_shard(tmp_path):
    root = database(tmp_path / 'db')
    assert len(execution.database_files(root, 'db1', 'db3')) == 26
    (root / 'db1_seq.0').unlink()
    with pytest.raises(ValueError, match='non-contiguous'):
        execution.database_files(root, 'db1', 'db3')
    with pytest.raises(ValueError, match='basenames'):
        execution.database_files(root, '../db1', 'db3')


def test_staging_keeps_split_layout_and_cleans_only_owned_dir(tmp_path):
    config = deepcopy(DEFAULTS['msa'])
    root = database(tmp_path / 'db')
    config.update(database=str(root), db1='db1', db3='db3')
    config['optimization'].update(staging='disk', disk_root=str(tmp_path / 'local'))
    (tmp_path / 'untouched').write_text('keep')
    with execution.staged_database(config, tmp_path) as staged:
        stage = Path(staged)
        assert (stage / 'db1_seq.0').read_bytes() == b'data'
        assert not (stage / 'db1_seq').exists()
    assert not stage.exists()
    assert (tmp_path / 'untouched').read_text() == 'keep'
    config['optimization'].update(staging='hybrid', memory_budget_gib=0)
    with pytest.raises(ValueError, match='memory budget'):
        with execution.staged_database(config, tmp_path):
            pass


def test_lanes_bounded_by_allocation_preserve_queries_and_search_flags(tmp_path, monkeypatch):
    config = deepcopy(DEFAULTS['msa'])
    config.update(database='/db', threads=2)
    config['optimization'].update(enabled=True, lanes=8, chunk_size=1)
    monkeypatch.setattr(execution, 'allocated_cpus', lambda: [4, 5, 8, 9])
    query, output = tmp_path / 'query.fasta', tmp_path / 'result'
    query.write_text('>q0\nACD\n>q1\nAAA\n>q2\nACD:EFG\n')
    calls = []
    def fake(argv, **kwargs):
        calls.append(argv)
        fasta, out = Path(argv[4]), Path(argv[6])
        out.mkdir()
        name = fasta.read_text().splitlines()[0][1:]
        (out / (name + '.a3m')).write_text(fasta.read_text())
    monkeypatch.setattr(execution.subprocess, 'run', fake)
    args = ['colabfold_search', str(query), '/db', str(output), '--threads', '2', '--db-load-mode', '0']
    assert execution.search(config, args, {}, tmp_path) == 3
    assert (output / 'q2.a3m').read_text() == '>q2\nACD:EFG\n'
    assert all(call[-4:] == ['--threads', '2', '--db-load-mode', '0'] for call in calls)
    assert {call[2] for call in calls} <= {'4,5', '8,9'}
    assert read_json(tmp_path / 'msa_execution.json')['effective_lanes'] == 2


def test_failed_search_does_not_claim_execution_success(tmp_path, monkeypatch):
    config = deepcopy(DEFAULTS['msa'])
    config.update(database='/db', threads=1)
    config['optimization'].update(enabled=True, chunk_size=1)
    monkeypatch.setattr(execution, 'allocated_cpus', lambda: [0])
    query = tmp_path / 'query.fasta'
    query.write_text('>q0\nACD\n')
    def fail(argv, **kwargs):
        raise subprocess.CalledProcessError(1, argv)
    monkeypatch.setattr(execution.subprocess, 'run', fail)
    with pytest.raises(subprocess.CalledProcessError):
        execution.search(config, ['search', str(query), '/db', str(tmp_path / 'result')], {}, tmp_path)
    assert not (tmp_path / 'msa_execution.json').exists()


def test_indices_stage_only_indices_and_preserve_shared_files(tmp_path,monkeypatch):
    root=database(tmp_path/'db')
    config=deepcopy(DEFAULTS['msa']);config.update(database=str(root),db1='db1',db3='db3')
    config['optimization'].update(staging='indices',disk_root=str(tmp_path/'local'),shm_root=str(tmp_path/'shm'),reserve_gib=1,working_memory_gib=1,memory_budget_gib=4)
    (tmp_path/'shm').mkdir()
    monkeypatch.setattr(execution.shutil,'disk_usage',lambda p:type('Usage',(),{'free':10*1024**3})())
    original=Path.read_text
    monkeypatch.setattr(Path,'read_text',lambda p,*a,**k:'MemAvailable: 10485760 kB' if str(p)=='/proc/meminfo' else original(p,*a,**k))
    monkeypatch.setenv('SLURM_MEM_PER_NODE','4096')
    with execution.staged_database(config,tmp_path,lanes=2) as staged:
        stage=Path(staged)
        assert (stage/'db1_seq.0').resolve()==root/'db1_seq.0'
        assert (stage/'db1.idx').resolve().is_relative_to(tmp_path/'shm')
        assert (stage/'db1.idx').read_bytes()==b'index'
        report=read_json(tmp_path/'msa_staging.json')
        assert report['disk_bytes']==0 and report['shared_bytes']>0
        assert report['working_memory_gib']==2
    assert not stage.exists() and (root/'db1.idx').exists()
    assert not list((tmp_path/'shm').iterdir())
    config['optimization']['memory_budget_gib']=2
    with pytest.raises(ValueError,match='search workers'):
        with execution.staged_database(config,tmp_path,lanes=2):pass


def test_staging_refuses_memory_beyond_slurm(tmp_path,monkeypatch):
    root=database(tmp_path/'db')
    config=deepcopy(DEFAULTS['msa']);config.update(database=str(root),db1='db1',db3='db3')
    config['optimization'].update(staging='indices',disk_root=str(tmp_path/'local'),reserve_gib=1,working_memory_gib=1,memory_budget_gib=4)
    monkeypatch.setenv('SLURM_MEM_PER_NODE','2048')
    with pytest.raises(ValueError,match='Slurm'):
        with execution.staged_database(config,tmp_path):pass


def test_staging_rejects_insufficient_available_ram_before_copy(tmp_path,monkeypatch):
    root=database(tmp_path/'db')
    config=deepcopy(DEFAULTS['msa']);config.update(database=str(root),db1='db1',db3='db3')
    config['optimization'].update(staging='indices',disk_root=str(tmp_path/'local'),shm_root=str(tmp_path/'shm'),reserve_gib=1,working_memory_gib=1,memory_budget_gib=4)
    monkeypatch.delenv('SLURM_MEM_PER_NODE',raising=False)
    original=Path.read_text
    monkeypatch.setattr(Path,'read_text',lambda p,*a,**k:'MemAvailable: 1024 kB' if str(p)=='/proc/meminfo' else original(p,*a,**k))
    with pytest.raises(ValueError,match='Available node memory'):
        with execution.staged_database(config,tmp_path):pass
    assert not list((tmp_path/'local').iterdir())
