"""CPU resource observations and decisions are scheduler doubles, not real submissions."""
from copy import deepcopy
import json
import pytest
from antigen_design import msa_routing as routing
from antigen_design.config import DEFAULTS
from antigen_design.io import read_json

PARTITION='PartitionName=compute State=UP ReqResv=NO'
NODE='NodeName=node01 State=IDLE Partitions=compute CPUTot=384 CPUEfctv=384 CPUAlloc=0 RealMemory=1547034 AllocMem=0'

class Transport:
    def __init__(self,nodes=NODE,partition=PARTITION,error=False):self.nodes=nodes;self.partition=partition;self.error=error;self.calls=[]
    def execute(self,host,argv,**kwargs):
        self.calls.append((host,argv))
        assert host=='msa-server' and argv[0]=='scontrol'
        if self.error:raise RuntimeError('unreachable')
        return self.nodes if 'nodes' in argv else self.partition

@pytest.fixture
def setup(tmp_path):
    c=deepcopy(DEFAULTS);c['execution']['cpu_host']='cluster-cpu'
    c['execution']['msa_remote'].update(enabled=True,fallback_when_busy=True,resources={'cpus':32,'memory':'1400G','partition':'compute'})
    plan={'config':c,'local_output':str(tmp_path),'environment':{'msa':{'site':'primary'},'msa_remote':{'site':'remote'}}}
    return plan,{'tasks':{'msa':{'stage':'msa','state':'pending'}}}

@pytest.mark.parametrize('mode',['folding','design'])
def test_both_workflows_prefer_remote_when_capacity_available(setup,mode):
    p,m=setup;p['config']['mode']=mode
    assert routing.choose(p,m,'msa',Transport())=='remote'
    assert read_json(p['local_output']+'/manifest.json')['tasks']['msa']['msa_destination']=='remote'

@pytest.mark.parametrize('node',[NODE.replace('CPUAlloc=0','CPUAlloc=360'),NODE.replace('AllocMem=0','AllocMem=400000'),NODE.replace('IDLE','IDLE+DRAIN'),NODE.replace('compute','other')])
def test_confirmed_capacity_miss_uses_primary(setup,node):
    p,m=setup;assert routing.choose(p,m,'msa',Transport(nodes=node))=='primary'

@pytest.mark.parametrize('transport',[Transport(error=True),Transport(nodes=''),Transport(nodes='broken'),Transport(partition='broken'),Transport(nodes=NODE.replace(' CPUAlloc=0',''))])
def test_unknown_never_falls_back_and_retries_are_bounded(setup,transport):
    p,m=setup;assert routing.choose(p,m,'msa',transport) is None
    assert 'msa_destination' not in m['tasks']['msa']
    count=len(transport.calls);assert routing.choose(p,m,'msa',transport) is None and len(transport.calls)==count

def test_mixed_node_uses_allocatable_memory_not_free_page_count(setup):
    p,m=setup;assert routing.choose(p,m,'msa',Transport(nodes=NODE.replace('IDLE','MIXED')+' FreeMem=1'))=='remote'

def test_existing_route_and_durable_primary_choice_never_redirect(setup):
    p,m=setup;t=Transport(error=True);m['tasks']['msa']['route']={'kind':'msa'}
    assert routing.choose(p,m,'msa',t)=='remote' and not t.calls
    m['tasks']['msa'].pop('route');m['tasks']['msa']['msa_destination']='primary'
    assert routing.choose(p,m,'msa',t)=='primary' and not t.calls

def test_reusable_pool_preferred_even_if_nodes_allocated(setup,monkeypatch):
    p,m=setup;p['config']['execution']['msa_remote']['pool']['enabled']=True
    monkeypatch.setattr(routing,'pool_capacity',lambda *a:{'kind':'reuse','job_id':'123'})
    t=Transport(error=True);assert routing.choose(p,m,'msa',t)=='remote' and not t.calls

def test_full_pool_with_idle_hardware_waits_not_primary(setup,monkeypatch):
    p,m=setup;p['config']['execution']['msa_remote']['pool']['enabled']=True
    monkeypatch.setattr(routing,'pool_capacity',lambda *a:{'kind':'busy'})
    assert routing.choose(p,m,'msa',Transport()) is None

def test_full_pool_without_capacity_falls_back(setup,monkeypatch):
    p,m=setup;p['config']['execution']['msa_remote']['pool']['enabled']=True
    monkeypatch.setattr(routing,'pool_capacity',lambda *a:{'kind':'busy'})
    assert routing.choose(p,m,'msa',Transport(nodes=NODE.replace('IDLE','ALLOCATED')))=='primary'

def test_pool_status_error_is_not_busy(setup,monkeypatch):
    p,m=setup;p['config']['execution']['msa_remote']['pool']['enabled']=True
    def fail(*a):raise RuntimeError('pool allocation unknown')
    monkeypatch.setattr(routing,'pool_capacity',fail)
    assert routing.choose(p,m,'msa',Transport()) is None

def test_doctor_freezes_both_site_environments(setup,monkeypatch):
    from antigen_design.pipeline import doctor
    from antigen_design import transport
    p,m=setup;c=p['config'];c['execution'].update(transport='openssh',gpu_host='cluster-cpu');c['execution']['python']['msa']='python'
    s=c['execution']['msa_remote'];s.update(python='python',root='/remote',database='/db',script='/converter',mmseqs='/mmseqs',cache_root='/cache',source_dir='/src')
    class Probe:
        def execute(self,host,argv,**kwargs):
            if argv[0]=='bash':return 'scheduler available'
            return json.dumps({'code':'same-source','errors':[],'site':host})
    monkeypatch.setattr(transport,'get_transport',lambda *a:Probe())
    report=doctor(c,remote=True,stages={'msa'})
    assert report['ready'] and report['stages']['msa']['site']=='cluster-cpu'
    assert report['stages']['msa_remote']['site']=='msa-server'

def test_primary_optimization_does_not_leak_into_primary(setup):
    from antigen_design.msa_remote import target_config
    p,m=setup;c=p['config'];s=c['execution']['msa_remote'];s.update(python='python',root='/remote',database='/db',script='/converter',mmseqs='/mmseqs',cache_root='/cache',source_dir='/src',optimization={'enabled':True,'staging':'indices'})
    target=target_config(c)
    assert target['msa']['optimization']['enabled'] and target['msa']['optimization']['staging']=='indices'
    assert not c['msa']['optimization']['enabled'] and c['msa']['optimization']['staging']=='none'


@pytest.mark.parametrize('target',['remote','primary',None])
@pytest.mark.parametrize('mode',['folding','design'])
def test_submission_path_and_unknown_capacity_never_submit(setup,monkeypatch,target,mode):
    from antigen_design import remote,msa_remote,pipeline
    p,m=setup;p['mode']=mode;p['config']['mode']=mode
    p['tasks']={'msa':{'id':'msa','stage':'msa','deps':[],'host':'msa-server'}}
    m['tasks']['msa'].update(host='msa-server',attempts=0,memory_tier=0)
    monkeypatch.setattr(remote,'refresh',lambda *a:None)
    monkeypatch.setattr(pipeline,'summarize',lambda *a:None)
    monkeypatch.setattr(routing,'choose',lambda *a:target)
    staged=[];submitted=[]
    def stage(plan,manifest,name,t):
        staged.append(name);manifest['tasks'][name]['route']={'plan':{'capsule':True}};manifest['tasks'][name]['host']='msa-server'
    monkeypatch.setattr(msa_remote,'stage',stage)
    def control(t,plan,host,operation,request):
        submitted.append((plan,host,operation));return {'job_id':'123'}
    monkeypatch.setattr(remote,'control',control)
    remote._advance(p,m,None,False,{'msa'},{'max_gpu_jobs':1,'max_msa_jobs':2},[])
    if target is None:
        assert not submitted and not staged and m['tasks']['msa']['attempts']==0
    else:
        assert submitted[0][1]==('msa-server' if target=='remote' else 'cluster-cpu')
        assert bool(staged)==(target=='remote')
        assert (submitted[0][0] is p)==(target=='primary')
