"""Original-input browsing and new submission identity; no compute is launched."""
import json
from pathlib import Path
import threading
import urllib.error
import urllib.request

import pytest
import yaml

from antigen_design.io import digest, read_json, write_json
from antigen_design.web_ui.resource_store import Store


@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.delenv('ANTIGEN_WEB_CONFIG', raising=False)
    configs = tmp_path / 'configs'
    configs.mkdir()
    (configs / 'server.local.yaml').write_text(yaml.safe_dump({'execution': {
        'backend': 'slurm', 'transport': 'openssh', 'cpu_host': 'example-login',
        'gpu_host': 'example-login', 'remote_root': '/scratch/owned/workflows'}}))
    (configs / 'webapp.yaml').write_text(yaml.safe_dump({'server_profile': 'server.local.yaml'}))
    return Store(tmp_path)


def register(store, request):
    job = request['id']
    write_json(store.registry, [job, *store.registered()])
    root = (store.root / 'runs') / job
    write_json(root / 'request.json', request)
    write_json(root / 'status.json', {'state': 'succeeded'})
    write_json(root / 'relocation.json', {'history': True})
    return root


@pytest.mark.parametrize('text', ['>one\r\nACD\r\n>two\r\nGGA\r\n',
    ' {"name":"保留排版", "modelSeeds":[0,7],\n "sequences":[{"proteinChain":{"sequence":"ACD","count":1}}]}\n'])
def test_original_folding_input_is_exact_and_independent_of_display_name(store, text):
    request = {'id': 'a' * 32, 'name': 'Original', 'mode': 'folding', 'profile': 'old-host',
               'input': text, 'seeds': [0, 101], 'samples': 3}
    root = register(store, request)
    store.rename(request['id'], {'name': 'Renamed'})
    before = {p: p.read_bytes() for p in root.iterdir()}
    value = store.original_input(request['id'])
    assert value['name'] == 'Renamed'
    assert value['request'] == request
    assert value['request']['input'] == text
    assert all(p.read_bytes() == content for p, content in before.items())
    assert root.is_relative_to(store.root)


def test_original_design_includes_verified_asset_and_every_region(store, synthetic):
    asset = store.upload('original structure.pdb', synthetic.read_text())
    request = {'id': 'a' * 32, 'name': 'Original', 'mode': 'design', 'profile': 'old-host',
               'asset': asset['id'], 'regions': [{'region': 'A:11A-15', 'length': [2, 4]}, 'B:10-11'],
               'rfd3': True, 'backbones': 3, 'sequences': 4, 'temperature': .2,
               'seed': 0, 'seeds': [1, 101], 'samples': 2}
    root = register(store, request)
    original = (store.root / 'inputs') / asset['id'] / asset['path']
    before = {p: digest(p) for directory in (root, original.parent) for p in directory.iterdir()}
    value = store.original_input(request['id'])
    assert value['request'] == request and value['asset'] == asset
    assert store.original_input_file(request['id'], 'file').read_text() == synthetic.read_text()
    assert store.original_input_file(request['id'], 'structure').name == 'viewer.pdb'
    assert all(digest(p) == expected for p, expected in before.items())


def test_missing_or_changed_structure_never_silently_loads_other_input(store, synthetic):
    asset = store.upload('original.pdb', synthetic.read_text())
    request = {'id': 'a' * 32, 'name': 'Original', 'mode': 'design', 'asset': asset['id']}
    register(store, request)
    original = (store.root / 'inputs') / asset['id'] / asset['path']
    original.write_text(original.read_text() + '\n')
    value = store.original_input(request['id'])
    assert value['request'] == request and 'asset' not in value
    assert '校验失败' in value['asset_error']
    with pytest.raises(ValueError, match='校验失败'):
        store.original_input_file(request['id'], 'file')
    original.unlink()
    assert 'asset_error' in store.original_input(request['id'])


def test_copy_uses_new_storage_and_preserves_source_history_and_deduplication(store):
    original = {'id': 'a' * 32, 'name': 'Original', 'mode': 'folding', 'profile': 'legacy',
                'input': '>fixture\nACD', 'seeds': [101], 'samples': 1}
    root = register(store, original)
    before = {p: p.read_bytes() for p in root.iterdir()}
    draft = {'name': 'New', 'mode': 'folding', 'source_job': original['id'],
             'input': '>new fixture\nACD', 'seeds': [0, 7], 'samples': 2}
    assert store.preview(draft)['expected_predictions'] == 4
    assert store.registered() == [original['id']]
    draft['id'] = 'b' * 32
    assert store.submit(draft)['id'] == draft['id']
    assert store.submit(draft)['id'] == draft['id']
    assert store.registered() == [draft['id'], original['id']]
    saved = read_json((store.root / 'runs') / draft['id'] / 'request.json')
    assert saved == dict(draft, profile='auto')
    config = read_json((store.root / 'configs') / (draft['id'] + '.json'))
    assert config['output'] == str((store.root / 'runs') / draft['id'] / 'pipeline')
    assert Path(config['input']).read_text() == draft['input']
    assert all(p.read_bytes() == content for p, content in before.items())
    for source in ('unknown', 'c' * 32, '../outside'):
        with pytest.raises(ValueError):
            store.preview(dict(draft, source_job=source))
    with pytest.raises(ValueError, match='新的运行 ID'):
        store.preview(dict(draft, id=original['id']))


def test_input_files_reject_paths_outside_the_uploaded_asset(store, synthetic):
    asset = store.upload('original.pdb', synthetic.read_text())
    request = {'id': 'a' * 32, 'name': 'Original', 'mode': 'design', 'asset': asset['id']}
    register(store, request)
    write_json((store.root / 'inputs') / asset['id'] / 'asset.json', dict(asset, path=str(synthetic)))
    with pytest.raises(ValueError, match='Invalid input file'):
        store.original_input_file(request['id'], 'file')
    with pytest.raises(ValueError):
        store.original_input('b' * 32)


def test_original_input_http_is_available_in_read_only_mode(store, synthetic):
    from http.server import ThreadingHTTPServer
    from antigen_design.web_ui.__main__ import Handler
    from antigen_design.web_ui.application import Application
    asset = store.upload('source.pdb', synthetic.read_text())
    request = {'id': 'a' * 32, 'name': 'Original', 'mode': 'design', 'asset': asset['id']}
    register(store, request)
    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    server.app = Application(store, read_only=True)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        base = f'http://127.0.0.1:{server.server_port}/api/jobs/{request["id"]}/input'
        with opener.open(base) as response:
            assert json.load(response)['request'] == request
        with opener.open(base + '/file') as response:
            assert response.read() == synthetic.read_bytes()
        with opener.open(base + '/structure') as response:
            assert response.read() == store.original_input_file(request['id'], 'structure').read_bytes()
        with pytest.raises(urllib.error.HTTPError) as error:
            opener.open(urllib.request.Request(base, headers={'Origin': 'https://evil.test'}))
        assert error.value.code == 403
        with pytest.raises(urllib.error.HTTPError) as error:
            opener.open(base + '/not-an-artifact')
        assert error.value.code == 400
        assert store.registered() == [request['id']]
        assert not server.app.health()['enabled']
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_packaged_benchmark_upload_needs_no_workspace_example(tmp_path, monkeypatch):
    from http.server import ThreadingHTTPServer
    from antigen_design.web_ui.__main__ import Handler
    from antigen_design.web_ui.application import Application
    from antigen_design.webapp import STATIC
    monkeypatch.delenv('ANTIGEN_WEB_CONFIG', raising=False)
    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    server.app = Application(Store(tmp_path), launch=False)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        request = urllib.request.Request(f'http://127.0.0.1:{server.server_port}/api/benchmark',
            data=b'{}', headers={'Content-Type': 'application/json', 'X-Antigen-Client': 'local'})
        with opener.open(request) as response:
            value = json.load(response)
        assert len(value['residues']) == 76
        assert value['sha256'] == digest(STATIC / 'examples/1ubq.cif')
        assert not (tmp_path / 'projects/1ubq').exists()
        assert server.app.store.registered() == []
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
