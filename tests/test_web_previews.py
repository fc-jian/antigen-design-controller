"""Incremental web result tests; fake transport, no model inference or scheduler."""
import io
import json
import shutil
import tarfile
from pathlib import Path
import pytest
from antigen_design.io import read_json, write_json, digest
from antigen_design.web_ui import results, export


@pytest.fixture
def preview(tmp_path, monkeypatch):
    store=results.Store(tmp_path)
    job='a'*32
    root=store.root/'runs'/job
    root.mkdir()
    write_json(store.registry,[job])
    write_json(root/'request.json',{'name':'test','mode':'folding','profile':'test'})
    write_json(root/'status.json',{'state':'running'})
    remote=tmp_path/'remote'
    fold=remote/'tasks/fold_0'; fold.mkdir(parents=True)
    metric=remote/'tasks/metrics_0'; metric.mkdir()
    shutil.copyfile(Path(__file__).parents[1]/'projects/1ubq/inputs/1ubq.cif',fold/'prediction.cif')
    write_json(fold/'confidence.json',{'plddt':95})
    rows=[{'prediction':str(fold/'prediction.cif'),'confidence_path':str(fold/'confidence.json'),
           'fold_seed':101,'sample':0,'name':'one'}]
    write_json(metric/'metrics.json',rows)
    tasks={'fold_0':{'stage':'fold','deps':[],'host':'cpu','signature':'f'*64},
           'metrics_0':{'stage':'metrics','deps':['fold_0'],'host':'cpu','signature':'a'*64},
           'fold_1':{'stage':'fold','deps':[],'host':'cpu','signature':'b'*64}}
    plan={'identity':'identity','tasks':tasks,'environment':{'fold':{},'metrics':{}},
          'config':{'output':str(remote),'execution':{'cpu_host':'cpu','python':{'metrics':'python'},'source_dir':'src'}}}
    for key in ('fold_0','metrics_0'):
        directory=remote/'tasks'/key
        write_json(directory/'complete.json',{'signature':tasks[key]['signature'],'environment':{},
                   'artifacts':{str(p):digest(p) for p in directory.iterdir()}})
    write_json(remote/'plan.json',plan)
    write_json(root/'pipeline/plan.json',plan)
    write_json(root/'pipeline/tasks/metrics_0/metrics.json',rows)
    manifest={'tasks':{key:{'state':'pending' if key=='fold_1' else 'succeeded','attempts':1}
                       for key in tasks}}
    write_json(root/'pipeline/manifest.json',manifest)
    class Transport:
        downloads=0
        def execute(self,host,args,**kwargs):
            if args[0]=='mkdir':
                return ''
            return json.dumps(export.export(*args[-3:]))
        def upload(self,*args):
            pass
        def download(self,host,source,target):
            self.downloads+=1
            shutil.copyfile(source,target)
    transport=Transport()
    monkeypatch.setattr(results,'get_transport',lambda c:transport)
    return store,job,root,plan,manifest,transport


def test_running_project_can_view_completed_pair_without_resubmission(preview):
    store,job,root,plan,manifest,transport=preview
    before=(root/'pipeline/manifest.json').read_bytes()
    detail=store.detail(job)
    assert detail['state']=='running'
    assert not detail['download_ready']
    assert [r['id'] for r in detail['results']]==['metrics_0~0']
    result=store.result_view(job,'metrics_0~0')
    assert result['confidence']['plddt']==95 and result['residues']
    assert store.result_file(job,'metrics_0~0','structure').is_file()
    assert transport.downloads==1
    assert (root/'pipeline/manifest.json').read_bytes()==before
    assert not (root/'results.json').exists()


@pytest.mark.parametrize('key',['fold_0','metrics_0'])
def test_both_stages_must_be_complete(preview,key):
    store,job,root,plan,manifest,transport=preview
    manifest['tasks'][key]['state']='running'
    write_json(root/'pipeline/manifest.json',manifest)
    assert store.detail(job)['results']==[]
    with pytest.raises(ValueError,match='both completed'):
        store.result_view(job,'metrics_0~0')
    assert transport.downloads==0


def test_cached_artifact_corruption_is_rejected(preview):
    store,job,root,*_=preview
    path=store.result_file(job,'metrics_0~0','structure')
    path.write_text('corrupt')
    with pytest.raises(ValueError,match='checksum'):
        store.result_view(job,'metrics_0~0')


def test_remote_receipt_corruption_is_rejected(preview):
    store,job,root,plan,*_=preview
    (Path(plan['config']['output'])/'tasks/fold_0/prediction.cif').write_text('corrupt')
    with pytest.raises(ValueError,match='receipt'):
        store.result_view(job,'metrics_0~0')
    assert not (root/'web-previews'/plan['tasks']['metrics_0']['signature']).exists()


@pytest.mark.parametrize('name',['../escape','/tmp/escape','tasks/fold_1/escape','tasks/fold_0/../../escape'])
def test_preview_archive_cannot_escape_pair(preview,tmp_path,name):
    plan=preview[3]
    archive=tmp_path/'bad.tar'
    with tarfile.open(archive,'w') as tar:
        info=tarfile.TarInfo(name);info.size=1
        tar.addfile(info,io.BytesIO(b'x'))
    with pytest.raises(ValueError,match='Unsafe'):
        results.unpack(archive,tmp_path/'target',plan,'metrics_0')


def test_legacy_result_ids_still_resolve(preview):
    store,job,root,*_=preview
    write_json(root/'results.json',[{'id':0}])
    write_json(root/'result-0.json',{'id':0,'metrics':{},'residues':[]})
    assert store.result_view(job,'0')['id']==0


def test_result_link_survives_project_collection(preview):
    store,job,root,*_=preview
    store.result_view(job,'metrics_0~0')
    write_json(root/'results.json',[{'id':0}])
    assert store.result_view(job,'metrics_0~0')['id']=='metrics_0~0'


def test_routed_fold_uses_frozen_capsule_environment_and_lineage(preview):
    store,job,root,plan,manifest,_=preview
    folder=Path(plan['config']['output'])/'tasks/fold_0'
    capsule={'identity':'capsule','environment':{'fold':{'target':'other-gpu'}},
             'gpu_capsule':{'origin_identity':plan['identity'],'origin_task_signature':plan['tasks']['fold_0']['signature']}}
    write_json(folder/'capsule-plan.json',capsule)
    receipt=read_json(folder/'complete.json')
    receipt.update(kind='verified_gpu_capsule_import',route_id='route',environment=capsule['environment']['fold'])
    receipt['artifacts'][str(folder/'capsule-plan.json')]=digest(folder/'capsule-plan.json')
    write_json(folder/'complete.json',receipt)
    manifest['tasks']['fold_0'].update(stage='fold',route={'id':'route','plan':capsule})
    write_json(root/'pipeline/manifest.json',manifest)
    assert store.result_view(job,'metrics_0~0')['confidence']['plddt']==95
    manifest['tasks']['fold_0']['route']['plan']['identity']='wrong-capsule'
    write_json(root/'pipeline/manifest.json',manifest)
    with pytest.raises(ValueError,match='lineage'):
        store.result_view(job,'metrics_0~0')


def test_collected_result_keeps_preview_link_and_avoids_remote_export(preview):
    store,job,root,plan,manifest,transport=preview
    row=read_json(root/'pipeline/tasks/metrics_0/metrics.json')[0]
    write_json(root/'results.json',[{'id':0,'metrics':row}])
    write_json(root/'result-0.json',{'id':0,'metrics':row,'residues':[],'confidence':{'plddt':95}})
    assert store.detail(job)['results'][0]['id']=='metrics_0~0'
    assert store.result_view(job,'metrics_0~0')['id']=='metrics_0~0'
    assert transport.downloads==0
