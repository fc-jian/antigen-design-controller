import pytest
from antigen_design.io import write_json
from antigen_design.web_ui.resource_store import Store


@pytest.mark.parametrize('state', ['queued', 'running', 'succeeded', 'failed', 'cancelled'])
def test_name_persists_without_changing_run(tmp_path, state):
    store = Store(tmp_path)
    job = 'a' * 32
    root = store.root / 'runs' / job
    write_json(store.registry, [job])
    write_json(root / 'request.json', {'name': 'original', 'mode': 'folding', 'profile': 'test'})
    write_json(root / 'status.json', {'state': state})
    before = {p: p.read_bytes() for p in root.iterdir()}
    assert store.detail(job)['name'] == 'original'
    assert store.rename(job, {'name': '  新名称 <test>  '}) == {'id': job, 'name': '新名称 <test>'}
    reloaded = Store(tmp_path)
    assert reloaded.detail(job)['name'] == '新名称 <test>'
    assert reloaded.list_jobs()['items'][0]['name'] == '新名称 <test>'
    assert all(p.read_bytes() == content for p, content in before.items())
    assert set(root.iterdir()) == set(before) | {root / 'display-name.json'}
    for data in ({'name': ''}, {'name': ' '}, {'name': 'x' * 121}, {'name': None},
                 {'name': 'ok', 'state': 'queued'}, [], {}):
        with pytest.raises(ValueError):
            store.rename(job, data)
    with pytest.raises(ValueError):
        store.rename('b' * 32, {'name': 'unknown'})
    assert reloaded.detail(job)['name'] == '新名称 <test>'
