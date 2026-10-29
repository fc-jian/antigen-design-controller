"""Web read models, synthetic run records only; no compute submissions."""
import io
import json
import threading
import pytest
from antigen_design.io import read_json, write_json, fingerprint
from antigen_design.web_ui.read_cache import ReadCache, ObjectStream, compact_manifest
from antigen_design.web_ui.resource_store import Store


@pytest.fixture
def run(tmp_path):
    store = Store(tmp_path)
    job = 'a'*32
    root = store.root/'runs'/job
    write_json(store.registry,[job])
    write_json(root/'request.json',dict(name='test',mode='folding',profile='auto'))
    write_json(root/'status.json',dict(state='succeeded'))
    plan = {'config':{},'tasks':{f'fold_{i}':{'stage':'fold','host':'test','deps':[]} for i in range(120)}}
    manifest={'tasks':{name:{'state':'succeeded','attempts':1,'history':[],
        'route':{'plan':{'large':'x'*10000}},'routing_reason':'ready'} for name in plan['tasks']}}
    write_json(root/'pipeline/plan.json',plan)
    write_json(root/'pipeline/manifest.json',manifest)
    write_json(root/'monitor.json',{'tasks':{name:{'state':'COMPLETED','log_tail':'log '*1000} for name in plan['tasks']}})
    return store,job,root


def test_streaming_decoder_handles_split_strings_numbers_and_nested_objects():
    value={'text':'引号 " \\ newline\n','number':123456789.123,'negative':-1e30,'nested':[{},True,None,False]}
    class Tiny(io.StringIO):
        def read(self,n):
            return super().read(min(n,3))
    parser=ObjectStream(Tiny(json.dumps(value)))
    assert dict(parser.items(lambda _:parser.value()))==value
    assert parser.peek()==''


def test_manifest_stream_preserves_fingerprints_and_rejects_trailing_data(tmp_path):
    path=tmp_path/'manifest.json'
    original={'tasks':{'one':{'route':{'plan':{'nested':{'a':[1,2]}}},'history':[{'route':{'plan':{'b':1}}}]}}}
    write_json(path,original)
    before=path.read_bytes()
    value=compact_manifest(path)['tasks']['one']
    assert value['route']=={'plan_fingerprint':fingerprint({'nested':{'a':[1,2]}})}
    assert value['history'][0]['route']=={'plan_fingerprint':fingerprint({'b':1})}
    assert path.read_bytes()==before
    path.write_text(path.read_text()+'{}')
    with pytest.raises(ValueError):
        compact_manifest(path)


def test_persisted_projection_is_reused_and_invalidated_by_atomic_replacement(run,monkeypatch):
    store,job,root=run
    assert store.list_jobs()['items'][0]['completed_tasks']==120
    import antigen_design.web_ui.listing as listing
    real=listing.compact_manifest
    calls=[]
    def counted(path):
        calls.append(path)
        return real(path)
    monkeypatch.setattr(listing,'compact_manifest',counted)
    fresh=Store(store.repo)
    assert fresh.list_jobs()['items'][0]['completed_tasks']==120
    assert not calls
    value=read_json(root/'pipeline/manifest.json')
    value['tasks']['fold_0']['state']='running'
    write_json(root/'pipeline/manifest.json',value)
    assert fresh.list_jobs()['items'][0]['completed_tasks']==119
    assert len(calls)==1
    write_json(root/'status.json',dict(state='running'))
    fresh.rename(job,{'name':'new'})
    assert fresh.list_jobs(state='active')['items'][0]['name']=='new'


def test_overview_omits_hidden_payload_and_extras_are_bounded(run):
    store,job,root=run
    before=(root/'pipeline/manifest.json').read_bytes()
    detail=store.overview(job)
    assert len(detail['tasks'])==120
    assert len(json.dumps(detail))<50000
    assert all('route' not in t and 'history' not in t and 'log_tail' not in t['observation'] for t in detail['tasks'])
    page=store.extras(job,'logs',offset=50,limit=50)
    assert len(page['items'])==50 and page['total']==120 and page['items'][0]['id']=='fold_50'
    assert len(store.extras(job,'scheduling',offset=100)['items'])==20
    assert before==(root/'pipeline/manifest.json').read_bytes()
    with pytest.raises(ValueError):store.extras(job,'logs',limit=10000)


def test_archive_never_loads_detail_and_still_guards_active_jobs(run,monkeypatch):
    store,job,root=run
    monkeypatch.setattr(store,'detail',lambda *_:pytest.fail('Archive must not render detail'))
    assert store.archive(job,True)=={'id':job,'archived':True}
    assert store.list_jobs()['total']==0
    assert store.list_jobs(archive='archived')['total']==1
    store.archive(job,False)
    value=read_json(root/'pipeline/manifest.json')
    value['tasks']['fold_0']['state']='submitting'
    write_json(root/'pipeline/manifest.json',value)
    with pytest.raises(ValueError,match='ambiguous'):store.archive(job,True)
    assert read_json(root/'archive.json')['archived'] is False


def test_result_index_survives_restart_and_checks_both_stages(run,monkeypatch):
    store,job,root=run
    plan=read_json(root/'pipeline/plan.json')
    plan['tasks']['metrics_0']={'stage':'metrics','deps':['fold_0'],'host':'test'}
    write_json(root/'pipeline/plan.json',plan)
    manifest=read_json(root/'pipeline/manifest.json')
    manifest['tasks']['metrics_0']={'state':'succeeded','attempts':1}
    write_json(root/'pipeline/manifest.json',manifest)
    write_json(root/'pipeline/tasks/metrics_0/metrics.json',[dict(name='result',fold_seed=101,sample=0)])
    assert store.overview(job)['results'][0]['id']=='metrics_0~0'
    from antigen_design.web_ui import results
    original=results.read_json
    def no_scan(path):
        assert path.name!='metrics.json','Restart should reuse the matching result index'
        return original(path)
    monkeypatch.setattr(results,'read_json',no_scan)
    fresh=Store(store.repo)
    assert fresh.overview(job)['results'][0]['id']=='metrics_0~0'
    manifest['tasks']['fold_0']['state']='running'
    write_json(root/'pipeline/manifest.json',manifest)
    assert fresh.overview(job)['results']==[]


def test_background_clock_does_not_block_page_or_schedule_duplicates(tmp_path,monkeypatch):
    from antigen_design.web_ui import native_timing
    timing=native_timing.NativeTiming()
    entered,release=threading.Event(),threading.Event()
    calls=[]
    monkeypatch.setattr(native_timing,'request_for',lambda *args:{'request':{},'host':'test'})
    def slow(*args):
        calls.append(1);entered.set();release.wait(5)
    monkeypatch.setattr(timing,'record',slow)
    try:
        for _ in range(3):
            result=timing.enrich(tmp_path,{},'fold',{'backend':'baremetal','state':'succeeded'},
                                 {'state':'COMPLETED','elapsed':999},background=True)
            assert result['elapsed'] is None and '后台' in result['timing_error']
        assert entered.wait(1) and len(calls)==1
    finally:
        release.set();timing.pool.shutdown()


def test_cache_is_bounded_and_does_not_serve_deleted_records(tmp_path):
    cache=ReadCache(limit=2)
    for i in range(3):
        path=tmp_path/str(i);write_json(path,{'i':i});cache.get(path)
    assert len(cache.values)==2
    path.unlink()
    with pytest.raises(FileNotFoundError):cache.get(path)


def test_http_overview_extras_and_archive_keep_origin_and_readonly_guards(run):
    from http.server import ThreadingHTTPServer
    import urllib.request
    import urllib.error
    from antigen_design.web_ui.__main__ import Handler
    from antigen_design.web_ui.application import Application
    store,job,_=run
    server=ThreadingHTTPServer(('127.0.0.1',0),Handler)
    server.app=Application(store,launch=False)
    worker=threading.Thread(target=server.serve_forever,daemon=True);worker.start()
    url=f'http://127.0.0.1:{server.server_address[1]}/api/jobs/{job}'
    open_local=urllib.request.build_opener(urllib.request.ProxyHandler({})).open
    try:
        with open_local(url+'/overview') as response:
            assert len(json.load(response)['tasks'])==120
        with open_local(url+'/extras?kind=logs&offset=100') as response:
            assert len(json.load(response)['items'])==20
        headers={'Content-Type':'application/json','X-Antigen-Client':'local'}
        for extra in ({'Origin':'https://evil.test'}, {'Host':'evil.test'}):
            request=urllib.request.Request(url+'/archive',data=b'{}',headers=headers|extra)
            with pytest.raises(urllib.error.HTTPError) as exc:open_local(request)
            assert exc.value.code==403
        request=urllib.request.Request(url+'/archive',data=b'{}',headers=headers)
        with open_local(request) as response:
            assert json.load(response)=={'id':job,'archived':True}
        server.app.read_only=True
        request=urllib.request.Request(url+'/unarchive',data=b'{}',headers=headers)
        with pytest.raises(urllib.error.HTTPError) as exc:open_local(request)
        assert exc.value.code==403
    finally:
        server.shutdown();worker.join();server.server_close()
