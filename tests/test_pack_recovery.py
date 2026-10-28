import json
import subprocess
import pytest
from antigen_design import gpu_transfer
from antigen_design.io import read_json, write_json, digest, fingerprint
from antigen_design.web_ui.pack_recovery import pack_remote, pack

FROZEN_PACK = gpu_transfer.pack


@pytest.fixture
def case(tmp_path, monkeypatch):
    p={'identity':'origin','config':{'output':str(tmp_path)},'tasks':{'task':{'stage':'rfd3','signature':'sig','deps':[]}}}
    request={'task':'task','route':{'id':'a'*32,'root':'/target','portable':{'v':1},'environment':{'e':2}}}
    write_json(tmp_path/'plan.json',p)
    calls=[]
    def fake(plan,req):
        calls.append(1)
        base=tmp_path/'transfers'/req['route']['id']
        (base/'input').mkdir()
        archive=base/'input.tar.gz'
        archive.write_bytes(b'archive')
        meta={'origin_identity':'origin','route_id':req['route']['id'],'origin_task_signature':'sig','portable':{'v':1},'inputs':{}}
        capsule={'identity':fingerprint({'origin':'origin','route':req['route'],'inputs':{}}),
            'gpu_capsule':meta,'tasks':{'task':{}},'config':{'output':'/target'},'environment':{'rfd3':{'e':2}}}
        result={'path':str(archive),'bytes':7,'sha256':digest(archive),'plan':capsule}
        write_json(base/'input-transfer.json',result)
        return result
    monkeypatch.setattr(gpu_transfer,'pack',fake)
    return tmp_path,p,request,calls


def test_saved_pack_is_reused_without_writing(case):
    root,p,req,calls=case
    first=pack_remote(str(root/'plan.json'),req,'origin')
    assert pack_remote(str(root/'plan.json'),req,'origin')==first
    assert calls==[1]


@pytest.mark.parametrize('bad', ['archive','origin','task','route','environment','target'])
def test_invalid_saved_receipt_refused(case,bad):
    root,p,req,calls=case
    pack_remote(str(root/'plan.json'),req,'origin')
    base=root/'transfers'/req['route']['id']
    r=read_json(base/'input-transfer.json')
    if bad=='archive': (base/'input.tar.gz').write_bytes(b'corrupt')
    if bad=='origin': r['plan']['gpu_capsule']['origin_identity']='wrong'
    if bad=='task': r['plan']['gpu_capsule']['origin_task_signature']='wrong'
    if bad=='route': req['route']['resources']={'memory':'different'}
    if bad=='environment': req['route']['environment']={'e':3}
    if bad=='target': req['route']['root']='/other'
    write_json(base/'input-transfer.json',r)
    with pytest.raises(ValueError): pack_remote(str(root/'plan.json'),req,'origin')
    assert calls==[1]


def test_partial_pack_preserved(case,monkeypatch):
    root,p,req,_=case
    directory=root/'transfers'/req['route']['id']/'input'
    directory.mkdir(parents=True)
    (directory/'partial').write_text('evidence')
    monkeypatch.setattr(gpu_transfer,'pack',FROZEN_PACK)
    with pytest.raises(ValueError,match='already exists'):
        pack_remote(str(root/'plan.json'),req,'origin')
    assert (directory/'partial').read_text()=='evidence'


def test_confirmation_lost_reuses_pack(case,monkeypatch):
    root,p,req,calls=case
    p['config']['execution']={'python':{'metrics':'python'}}
    attempts=[]
    class Transport:
        def execute(self,*args,**kwargs):
            result=pack_remote(str(root/'plan.json'),req,'origin')
            attempts.append(1)
            if len(attempts)==1: raise subprocess.TimeoutExpired('ssh',300)
            return json.dumps(result)
    monkeypatch.setattr('antigen_design.web_ui.pack_recovery.time.sleep',lambda _:None)
    assert pack(Transport(),p,'host',req)['bytes']==7
    assert calls==[1] and len(attempts)==2


def test_invalid_route_cannot_escape(case):
    root,p,req,calls=case
    req['route']['id']='../other'
    with pytest.raises(ValueError): pack_remote(str(root/'plan.json'),req,'origin')
    assert calls==[]
