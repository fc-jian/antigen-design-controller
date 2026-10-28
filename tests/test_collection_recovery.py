import io
import json
from pathlib import Path
import subprocess
import tarfile
from types import SimpleNamespace
import pytest
from antigen_design.io import digest, fingerprint, read_json, write_json
from antigen_design.web_ui import collection, collection_archive, controller


@pytest.fixture
def case(tmp_path):
    root = tmp_path / 'remote'
    task = root / 'tasks/fold_test'
    task.mkdir(parents=True)
    (root / 'input.cif').write_text('input')
    (task / 'result.cif').write_text('result')
    write_json(task / 'complete.json', {'signature': 'sig',
        'artifacts': {str(task / 'result.cif'): digest(task / 'result.cif')}})
    plan = {'identity': 'identity', 'mode': 'folding',
        'config': {'output': str(root), 'input': str(root / 'input.cif'),
                   'execution': {'python': {'metrics': 'python'}, 'cpu_host': 'host'}},
        'tasks': {'fold_test': {'signature': 'sig', 'stage': 'fold'}}}
    write_json(root / 'plan.json', plan)
    return root, plan


def collect_case(case):
    root, plan = case
    return collection_archive.collect_remote(str(root / 'plan.json'), plan['identity'])


def test_legacy_completed_archive_is_adopted_without_repacking(case):
    from antigen_design.remote_control import collect
    root, plan = case
    original = collect(plan)
    stamp = (root / 'deliverables.tar.gz').stat().st_mtime_ns
    recovered = collect_case(case)
    assert recovered['sha256'] == original['sha256']
    assert recovered['reused_existing_archive'] is True
    assert (root / 'deliverables.tar.gz').stat().st_mtime_ns == stamp
    assert collect_case(case) == recovered


def test_incomplete_legacy_archive_preserved_before_new_atomic_collection(case):
    root, _ = case
    target = root / 'deliverables.tar.gz'
    target.write_bytes(b'partial evidence')
    result = collect_case(case)
    assert Path(result['preserved_incomplete_archive']).read_bytes() == b'partial evidence'
    assert result['reused_existing_archive'] is False
    assert result['validated_tasks'] == 1
    assert collect_case(case) == result


@pytest.mark.parametrize('corruption', ['artifact', 'signature', 'archive', 'inventory', 'identity'])
def test_collection_refuses_corruption_without_overwriting_evidence(case, corruption):
    root, plan = case
    result = collect_case(case)
    receipt = root / 'tasks/fold_test/complete.json'
    if corruption == 'artifact':
        (root / 'tasks/fold_test/result.cif').write_text('corrupt')
    elif corruption == 'signature':
        write_json(receipt, read_json(receipt) | {'signature': 'wrong'})
    elif corruption == 'archive':
        (root / 'deliverables.tar.gz').write_bytes(b'corrupt archive')
    elif corruption == 'inventory':
        (root / 'logs').mkdir()
        (root / 'logs/new.log').write_text('changed')
    else:
        write_json(root / 'plan.json', plan | {'identity': 'wrong'})
    with pytest.raises(ValueError):
        collect_case(case)
    assert read_json(root / 'web-collection.json') == result
    if corruption == 'archive':
        assert (root / 'deliverables.tar.gz').read_bytes() == b'corrupt archive'


def test_lost_collection_confirmation_reconciles_without_duplicate_pack(case, monkeypatch):
    root, plan = case
    calls = []
    class Transport:
        def execute(self, host, argv, timeout):
            result = collect_case(case)
            calls.append((root / 'deliverables.tar.gz').stat().st_mtime_ns)
            if len(calls) == 1:
                raise subprocess.TimeoutExpired('ssh', timeout)
            return json.dumps(result)
    monkeypatch.setattr(collection_archive.time, 'sleep', lambda _: None)
    result = collection_archive.remote_archive(Transport(), plan)
    assert result['validated_tasks'] == 1
    assert len(calls) == 2 and calls[0] == calls[1]


def test_remote_validation_failure_is_not_retried(case, monkeypatch):
    calls = []
    class Transport:
        def execute(self, *args, **kwargs):
            calls.append(1)
            raise subprocess.CalledProcessError(1, 'ssh', stderr='invalid receipt')
    with pytest.raises(subprocess.CalledProcessError):
        collection_archive.remote_archive(Transport(), case[1])
    assert calls == [1]


def test_all_local_receipts_and_artifacts_verified(case, tmp_path):
    root, plan = case
    result = collect_case(case)
    report = collection.unpack(root / 'deliverables.tar.gz', tmp_path / 'collected', plan, result)
    assert report['tasks'] == 1
    assert report['local_artifact_hashes'] == 1
    assert report['archive_members'] == len(result['files'])


@pytest.mark.parametrize('bad', ['traversal', 'duplicate', 'member_hash', 'missing_receipt', 'size', 'capacity'])
def test_untrusted_or_incomplete_collection_rejected(case, tmp_path, monkeypatch, bad):
    root, plan = case
    result = collect_case(case)
    target = tmp_path / 'collected'
    archive = root / 'deliverables.tar.gz'
    if bad == 'size':
        result['uncompressed_bytes'] += 1
    elif bad == 'capacity':
        monkeypatch.setattr(collection.shutil, 'disk_usage', lambda p: SimpleNamespace(free=0))
    else:
        name = '../escape' if bad == 'traversal' else 'file.txt'
        content = b'test'
        with tarfile.open(archive, 'w:gz') as tar:
            for _ in range(2 if bad == 'duplicate' else 1):
                info = tarfile.TarInfo(name); info.size = len(content)
                tar.addfile(info, io.BytesIO(content))
        import hashlib
        result['files'] = {name: {'bytes': 4, 'sha256': hashlib.sha256(content).hexdigest()}}
        if bad == 'member_hash':
            result['files'][name]['sha256'] = '0' * 64
        result.update(bytes=archive.stat().st_size, sha256=digest(archive),
            inventory_sha256=fingerprint(result['files']), uncompressed_bytes=8 if bad == 'duplicate' else 4)
    with pytest.raises(ValueError):
        collection.unpack(archive, target, plan, result)
    assert not (tmp_path / 'escape').exists()


@pytest.mark.parametrize('fails', [False, True])
def test_completed_controller_collects_without_preflight_or_scheduling(tmp_path, monkeypatch, fails):
    root = tmp_path / 'run'
    pipeline = root / 'pipeline'; pipeline.mkdir(parents=True)
    plan = {'dry_run': False, 'code_signature': 'code', 'local_input': 'input',
        'input_sha256': 'hash', 'tasks': {'fold': {'stage': 'fold'}}, 'config': {}}
    manifest = {'tasks': {'fold': {'stage': 'fold', 'state': 'succeeded', 'job_id': '7', 'attempts': 1}}}
    write_json(pipeline / 'plan.json', plan)
    write_json(pipeline / 'manifest.json', manifest)
    write_json(root / 'status.json', {'state': 'attention', 'error': 'collect timeout'})
    monkeypatch.setattr(controller, 'code_signature', lambda: 'code')
    monkeypatch.setattr(controller, 'digest', lambda p: 'hash')
    monkeypatch.setattr('antigen_design.transport.get_transport', lambda p: None)
    def forbidden(*a, **kw):
        pytest.fail('A completed project must not probe or submit new computation')
    monkeypatch.setattr(controller, 'checked_preflight', forbidden)
    monkeypatch.setattr('antigen_design.remote.advance', forbidden)
    calls = []
    def extract(root, plan):
        calls.append(1)
        if fails:
            raise ValueError('invalid archive')
    monkeypatch.setattr(controller, 'extract_results', extract)
    controller.run(SimpleNamespace(job_root=lambda j: root), 'run')
    status = read_json(root / 'status.json')
    assert calls == [1] and status['state'] == ('attention' if fails else 'succeeded')
    assert read_json(pipeline / 'manifest.json') == manifest
    assert status['error'] == ('invalid archive' if fails else None)
