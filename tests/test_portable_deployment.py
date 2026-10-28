"""Portable deployments tested with isolated files and local HTTP only."""
from copy import deepcopy
import json
from pathlib import Path
import shutil
import threading
from http.server import ThreadingHTTPServer
from urllib.request import urlopen, Request
from urllib.error import HTTPError
import pytest
import yaml
from antigen_design.config import DEFAULTS
from antigen_design.deployment import settings, profile
from antigen_design.web_ui.resource_store import Store
from antigen_design.web_ui.__main__ import Handler
from antigen_design.web_ui.application import Application
from antigen_design import ssh_relay
from antigen_design.io import digest


@pytest.fixture
def site(tmp_path, monkeypatch):
    monkeypatch.delenv('ANTIGEN_WEB_CONFIG', raising=False)
    directory = tmp_path / 'configs'
    directory.mkdir()
    backend = {'execution': {'backend': 'slurm', 'transport': 'openssh',
        'cpu_host': 'my-university-login', 'gpu_host': 'my-university-login',
        'remote_root': '/scratch/owned/workflows'},
        'msa': {'batch_size': 7, 'threads': 12}}
    (directory / 'site.local.yaml').write_text(yaml.safe_dump(backend))
    (directory / 'webapp.yaml').write_text(yaml.safe_dump({
        'server_profile': 'site.local.yaml', 'max_gpu_jobs': 2,
        'scientific_defaults': {'msa': {'batch_size': 3}, 'folding': {'seeds': [5], 'samples': 2}}}))
    return tmp_path


def test_arbitrary_ssh_backend_and_configuration_precedence(site):
    store = Store(site)
    config, plan = store.compile({'name': 'test', 'mode': 'folding', 'input': '>test\nACDE'},
                                 write_input(site), site / 'out')
    assert config['execution']['cpu_host'] == 'my-university-login'
    assert config['msa']['threads'] == 12 and config['msa']['batch_size'] == 3
    assert config['folding']['need_atom_confidence'] is True
    assert config['folding']['seeds'] == [5] and plan['expected_predictions'] == 2
    assert store.resources()['settings']['gpu_targets'] == []
    assert store.resources()['msa_catalog'] == [{'id': 'primary', 'label': 'my-university-login'}]
    assert store.ui_defaults()['seeds'] == [5]
    assert store.scheduler_limits()['max_gpu_jobs'] == 2


def write_input(repo):
    path = repo / 'input.fasta'
    path.write_text('>test\nACDE')
    return path


def test_explicit_config_is_independent_of_working_directory(site, tmp_path, monkeypatch):
    path = site / 'configs/webapp.yaml'
    value = settings('/unrelated/location', path)
    assert value['repo'] == str(site)
    assert value['server_profile'] == str(site / 'configs/site.local.yaml')
    monkeypatch.setenv('ANTIGEN_WEB_CONFIG', str(path))
    assert profile('/unrelated/location')['execution']['cpu_host'] == 'my-university-login'


def test_unconfigured_ui_does_not_freeze_an_empty_resource_policy(tmp_path, monkeypatch):
    monkeypatch.delenv('ANTIGEN_WEB_CONFIG', raising=False)
    store = Store(tmp_path)
    assert store.profiles() == [] and store.resources()['configured'] is False
    assert not (tmp_path / 'configs/web-resources.local.json').exists()
    with pytest.raises(ValueError, match='server_profile'):
        store.resources(store.resources()['settings'])
    with pytest.raises(ValueError, match='server_profile'):
        store.preview({'name': 'test', 'mode': 'folding', 'input': '>test\nACDE'})


@pytest.mark.parametrize('patch', [{'project': '../escape'}, {'port': 0}, {'host': '0.0.0.0'},
                                 {'read_only': 'false'}, {'unknown': True},
                                 {'scientific_defaults': {'folding': {'samples': 0}}}])
def test_invalid_web_settings_rejected(site, patch):
    path = site / 'configs/webapp.yaml'
    path.write_text(yaml.safe_dump(patch))
    with pytest.raises(ValueError):
        settings(site)


def test_http_service_serves_static_assets_and_rejects_cross_origin(site):
    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    server.app = Application(Store(site), launch=False)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = 'http://127.0.0.1:' + str(server.server_port)
    try:
        assert b'Antigen Studio' in urlopen(base).read()
        assert b'function' in urlopen(base + '/app.js').read()
        assert len(urlopen(base + '/vendor/3Dmol-min.js').read()) > 1000
        config = json.load(urlopen(base + '/api/config'))
        assert config['defaults']['seeds'] == [5]
        assert json.load(urlopen(base + '/api/resources'))['configured'] is True
        with pytest.raises(HTTPError) as error:
            urlopen(Request(base + '/api/resources', headers={'Origin': 'https://example.org'}))
        assert error.value.code == 403
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


@pytest.mark.parametrize('corrupt', [False, True])
def test_ssh_relay_checks_archive_before_publishing(tmp_path, corrupt):
    original = tmp_path / 'source.tar'
    original.write_bytes(b'capsule-data')
    calls = []
    class Transport:
        def remote_path(self, host, path):
            assert host in ('primary-login', 'msa-login')
        def execute(self, host, argv):
            calls.append((host, argv))
            if argv[0] == 'sha256sum':
                return ('0' * 64 if corrupt and host == 'msa-login' else digest(original)) + ' archive'
            return ''
        def download(self, host, source, local):
            shutil.copyfile(original, local)
        def upload(self, host, local, target):
            assert digest(local) == digest(original)
    args = (Transport(), 'primary-login', 'msa-login', 'up', '/owned/outbox', '/owned/inbox',
            'source.tar', tmp_path / 'evidence')
    if corrupt:
        with pytest.raises(ValueError, match='checksum'):
            ssh_relay.transfer(*args)
        assert not any(argv[0] == 'mv' for _, argv in calls)
    else:
        ssh_relay.transfer(*args)
        assert any(argv[0] == 'mv' for _, argv in calls)
        assert json.loads((tmp_path / 'evidence/transfer.json').read_text())['verified'] is True


def test_remote_msa_allows_user_owned_hosts_and_roots():
    from antigen_design.msa_remote import validate
    config = deepcopy(DEFAULTS)
    config['execution'].update(backend='slurm', transport='openssh', cpu_host='my-primary')
    config['execution']['msa_remote'].update(enabled=True, host='my-msa', root='/scratch/team/msa',
        python='/env/python', database='/db', script='/converter.py', mmseqs='/bin/mmseqs',
        cache_root='/cache', resources={'cpus': 8, 'memory': '64G'}, fallback_when_busy=True)
    validate(config)
