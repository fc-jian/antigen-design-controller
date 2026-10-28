"""Tiny database fixtures and scheduler doubles; no real MSA/Slurm work."""
from copy import deepcopy
from contextlib import contextmanager
import json
from pathlib import Path
import subprocess
import pytest
from antigen_design import msa_pool as pool, msa_execution, worker
from antigen_design.config import DEFAULTS
from antigen_design.io import digest, read_json, write_json


@pytest.fixture
def setup(tmp_path,monkeypatch):
    db=tmp_path/'db';db.mkdir()
    for name in ('db1','db3'):
        for suffix in ('','_seq','_h'):
            for ending in ('','.dbtype','.index'):(db/(name+suffix+ending)).write_bytes(b'data')
        (db/(name+'.idx')).write_bytes(b'index')
    files=[{'name':f.name,'size':f.stat().st_size,'sha256':digest(f)} for f in db.iterdir()]
    write_json(db/'transfer-manifest.json',{'database_version':'v1','files':files})
    c=deepcopy(DEFAULTS);c['msa'].update(database=str(db),database_version='v1',db1='db1',db3='db3',cache_root=str(tmp_path/'cache'),threads=1)
    (tmp_path/'shm').mkdir()
    c['msa']['optimization'].update(enabled=True,staging='indices',reserve_gib=1,working_memory_gib=1,memory_budget_gib=4,disk_root=str(tmp_path/'disk'),shm_root=str(tmp_path/'shm'))
    c['execution']['msa_remote'].update(root=str(tmp_path/'service'))
    c['execution']['msa_remote']['pool'].update(enabled=True,idle_seconds=1)
    c['execution'].update(source_dir='/source')
    c['execution']['python']['msa']='python'
    c['execution']['resources']['cpu'].update(cpus=1,memory='4G')
    plans=[]
    for label in ('one','two'):
        cfg=deepcopy(c);cfg['output']=str(tmp_path/label);cfg['msa']['cache_root']+=label
        Path(cfg['output']).mkdir();(Path(cfg['output'])/'control').mkdir()
        plan={'config':cfg,'identity':label,'msa_capsule':{'route_id':label},'code_signature':worker.code_signature(),'environment':{'msa':{'code':worker.code_signature()}},'tasks':{'msa':{'stage':'msa','signature':label}}}
        write_json(Path(cfg['output'])/'plan.json',plan);plans.append(plan)
    submissions=[]
    def sbatch(argv,**kwargs):
        assert argv[0]=='sbatch';submissions.append(argv)
        return subprocess.CompletedProcess(argv,0,str(100+len(submissions))+'\n','')
    monkeypatch.setattr(pool.subprocess,'run',sbatch)
    monkeypatch.setattr(pool,'allocation',lambda session,job=None:{'job_id':job or read_json(Path(session)/'session.json')['job_id'],'state':'RUNNING'})
    monkeypatch.setenv('SLURM_MEM_PER_NODE','4096')
    original=Path.read_text
    monkeypatch.setattr(Path,'read_text',lambda f,*a,**kw:'MemAvailable: 10485760 kB' if str(f)=='/proc/meminfo' else original(f,*a,**kw))
    return plans,submissions


def submit(plan):return pool.submit(plan,{'task':'msa','attempt':1})


def test_independent_requests_share_allocation_and_duplicate_is_idempotent(setup):
    plans,calls=setup
    first=submit(plans[0]);second=submit(plans[1])
    assert first['job_id']==second['job_id']=='101' and len(calls)==1
    assert first['pool']['request_id']!=second['pool']['request_id']
    assert first['pool']['session']==second['pool']['session']
    assert submit(plans[0])==first and len(calls)==1


def test_incompatible_configuration_does_not_share_pool(setup):
    plans,calls=setup;first=submit(plans[0])
    plans[1]['config']['msa']['threads']=2
    write_json(Path(plans[1]['config']['output'])/'plan.json',plans[1])
    second=submit(plans[1])
    assert first['pool']['session']!=second['pool']['session'] and len(calls)==2


def test_queue_limit_and_unknown_submission_never_duplicate(setup,monkeypatch):
    plans,calls=setup
    for plan in plans:
        plan['config']['execution']['msa_remote']['pool']['queue_limit']=1
        write_json(Path(plan['config']['output'])/'plan.json',plan)
    submit(plans[0]);assert 'rejected' in submit(plans[1]);assert len(calls)==1


def test_ambiguous_sbatch_requires_reconciliation(setup,monkeypatch):
    plans,calls=setup
    def bad(argv,**kwargs):calls.append(argv);return subprocess.CompletedProcess(argv,0,'unknown','')
    monkeypatch.setattr(pool.subprocess,'run',bad)
    with pytest.raises(RuntimeError,match='reconcile'):submit(plans[0])
    with pytest.raises(RuntimeError,match='reconciliation'):submit(plans[0])
    assert len(calls)==1


def test_cancel_marks_only_own_request_without_scancel(setup):
    plans,calls=setup;first=submit(plans[0]);second=submit(plans[1])
    pool.cancel(plans[0],{'msa':{'attempts':1}})
    assert (Path(first['pool']['session'])/'requests'/first['pool']['request_id']/'cancel.request').exists()
    assert not (Path(second['pool']['session'])/'requests'/second['pool']['request_id']/'cancel.request').exists()
    assert len(calls)==1


def test_completed_request_valid_while_allocation_still_running(setup,monkeypatch):
    plans,_=setup;result=submit(plans[0]);ref=result['pool']
    write_json(Path(ref['session'])/'requests'/ref['request_id']/'state.json',{'state':'succeeded'})
    monkeypatch.setattr(worker,'receipt_valid',lambda *a:True)
    info=pool.inspect(plans[0],{'msa':{'attempts':1}})['msa']
    assert info['slurm_state']=='RUNNING' and info['execution_state']=='COMPLETED' and info['valid']


def test_allocation_death_fails_unfinished_request_without_resubmit(setup,monkeypatch):
    plans,calls=setup;submit(plans[0])
    monkeypatch.setattr(pool,'allocation',lambda *a,**k:{'state':'NODE_FAIL','job_id':'101'})
    info=pool.inspect(plans[0],{'msa':{'attempts':1}})['msa']
    assert info['execution_state']=='FAILED' and 'no automatic retry' in info['error']['error']
    assert len(calls)==1


def test_request_provenance_tamper_is_rejected(setup):
    plans,_=setup;submit(plans[0])
    path=Path(plans[0]['config']['output'])/'plan.json';path.write_text(path.read_text()+' ')
    with pytest.raises(ValueError,match='provenance'):pool.inspect(plans[0],{'msa':{'attempts':1}})


def test_one_staging_for_two_distinct_workers_with_atomic_receipts(setup,monkeypatch):
    plans,calls=setup;first=submit(plans[0]);second=submit(plans[1]);session=Path(first['pool']['session'])
    monkeypatch.setenv('SLURM_JOB_ID','101');contexts=[];stage_calls=[]
    original=msa_execution.staged_database
    @contextmanager
    def counted(*args,**kwargs):
        stage_calls.append(1)
        with original(*args,**kwargs) as db:yield db
    monkeypatch.setattr(msa_execution,'staged_database',counted)
    class Child:
        returncode=0
        def __init__(self,argv,env,**kwargs):
            incoming=read_json(argv[3]);root=Path(incoming['config']['output'])/'tasks'/'msa';root.mkdir(parents=True)
            contexts.append(env['ANTIGEN_MSA_POOL_CONTEXT'])
            with monkeypatch.context() as scoped:
                scoped.setenv('ANTIGEN_MSA_POOL_REQUEST',env['ANTIGEN_MSA_POOL_REQUEST'])
                pool.reuse_context(incoming['config']['msa'],env['ANTIGEN_MSA_POOL_CONTEXT'],root)
            (root/'result').write_text(incoming['identity'])
            write_json(root/'complete.json',{'signature':incoming['tasks']['msa']['signature'],'artifacts':{str(root/'result'):digest(root/'result')}})
        def poll(self):return 0
    monkeypatch.setattr(pool.subprocess,'Popen',Child)
    pool.serve(session)
    assert len(stage_calls)==1 and len(contexts)==2 and len(set(contexts))==1
    for plan in plans:assert pool.inspect(plan,{'msa':{'attempts':1}})['msa']['valid']
    assert read_json(session/'status.json')['phase']=='closed'
    assert not list(Path(plans[0]['config']['msa']['optimization']['shm_root']).iterdir())


def test_validated_index_changed_is_rejected(setup,monkeypatch,tmp_path):
    plans,_=setup;plan=plans[0];session=tmp_path/'context';session.mkdir();monkeypatch.setenv('SLURM_JOB_ID','101')
    with msa_execution.staged_database(plan['config']['msa'],session) as db:
        ctx=pool.create_context(plan,session,db)
        pool.reuse_context(plan['config']['msa'],session/'context.json',tmp_path)
        path=Path(db)/'db1.idx';path.chmod(0o600);path.write_bytes(b'INDEX')
        with pytest.raises(ValueError,match='view changed'):pool.reuse_context(plan['config']['msa'],session/'context.json',tmp_path)


def test_wrong_allocation_or_database_identity_rejected(setup,monkeypatch,tmp_path):
    plans,_=setup;plan=plans[0];session=tmp_path/'context';session.mkdir();monkeypatch.setenv('SLURM_JOB_ID','101')
    with msa_execution.staged_database(plan['config']['msa'],session) as db:
        pool.create_context(plan,session,db)
        monkeypatch.setenv('SLURM_JOB_ID','102')
        with pytest.raises(ValueError,match='allocation'):pool.reuse_context(plan['config']['msa'],session/'context.json',tmp_path)
        monkeypatch.setenv('SLURM_JOB_ID','101')
        (Path(plan['config']['msa']['database'])/'db1_seq').write_bytes(b'BAD!')
        with pytest.raises(ValueError,match='source database'):pool.reuse_context(plan['config']['msa'],session/'context.json',tmp_path)


def test_context_rejects_bad_publication_hash(setup,monkeypatch,tmp_path):
    plans,_=setup;plan=plans[0];session=tmp_path/'context';session.mkdir();monkeypatch.setenv('SLURM_JOB_ID','101')
    with msa_execution.staged_database(plan['config']['msa'],session) as db:
        (Path(db)/'db1.idx').write_bytes(b'WRONG')
        with pytest.raises(ValueError,match='checksum'):pool.create_context(plan,session,db)


def test_request_failure_does_not_mark_peers_failed(setup,monkeypatch):
    plans,_=setup;first=submit(plans[0]);second=submit(plans[1]);session=Path(first['pool']['session'])
    monkeypatch.setenv('SLURM_JOB_ID','101')
    visited=[]
    def fake_execute(plan,session,payload,*args):
        visited.append(payload['id'])
        if payload['id']==first['pool']['request_id']:raise RuntimeError('isolated failure')
        write_json(session/'requests'/payload['id']/'state.json',{'state':'succeeded'})
    monkeypatch.setattr(pool,'execute_request',fake_execute)
    pool.serve(session)
    assert len(visited)==2
    assert read_json(session/'requests'/first['pool']['request_id']/'state.json')['state']=='failed'
    assert read_json(session/'requests'/second['pool']['request_id']/'state.json')['state']=='succeeded'


@pytest.mark.parametrize('field',[7,8,9])
def test_accounting_rejects_foreign_allocation(tmp_path,monkeypatch,field):
    import getpass
    session=tmp_path/'session';session.mkdir()
    write_json(session/'session.json',{'job_id':'101','job_name':'owned'})
    row=['101','RUNNING','0:0','00:01:00','32','1400G','compute','owned',str(session),getpass.getuser()]
    row[field]='foreign'
    monkeypatch.setattr(pool.subprocess,'check_output',lambda *a,**k:'|'.join(row)+'|\n')
    with pytest.raises(ValueError,match='owner/name/workdir'):pool.allocation(session)


def test_running_cancel_terminates_only_request_process_group(setup,monkeypatch):
    plans,calls=setup;first=submit(plans[0]);second=submit(plans[1]);session=Path(first['pool']['session'])
    directory=session/'requests'/first['pool']['request_id'];payload=read_json(directory/'request.json')
    killed=[]
    class Child:
        pid=424242
        returncode=None
        def __init__(self,*a,**kw):
            assert kw['start_new_session'] is True
            (directory/'cancel.request').touch()
        def poll(self):return self.returncode
        def wait(self,timeout=None):self.returncode=-15;return self.returncode
    monkeypatch.setattr(pool.subprocess,'Popen',Child)
    monkeypatch.setattr(pool.os,'killpg',lambda pid,sig:killed.append((pid,sig)))
    pool.execute_request(plans[0],session,payload,session/'context.json',pool.time.time()+30)
    assert killed==[(424242,pool.signal.SIGTERM)]
    assert read_json(directory/'state.json')['state']=='cancelled'
    assert read_json(session/'requests'/second['pool']['request_id']/'state.json')['state']=='queued'
    assert len(calls)==1


def test_draining_session_rejects_new_request_without_new_allocation(setup):
    plans,calls=setup;first=submit(plans[0])
    pool.drain(plans[0],{'task':'msa','attempt':1})
    assert 'rejected' in submit(plans[1]) and len(calls)==1


def test_concurrent_controllers_submit_one_shared_allocation(setup,monkeypatch,tmp_path):
    import multiprocessing
    plans,_=setup;ctx=multiprocessing.get_context('fork');gate=ctx.Barrier(2)
    submissions=tmp_path/'sbatch.log'
    def sbatch(argv,**kwargs):
        with submissions.open('a') as stream:stream.write('submitted\n')
        return subprocess.CompletedProcess(argv,0,'101\n','')
    monkeypatch.setattr(pool.subprocess,'run',sbatch)
    def controller(index):
        gate.wait(timeout=10)
        write_json(tmp_path/f'controller-{index}.json',submit(plans[index]))
    children=[ctx.Process(target=controller,args=(i,)) for i in range(2)]
    try:
        for child in children:child.start()
        for child in children:child.join(timeout=15)
        assert all(child.exitcode==0 for child in children)
        results=[read_json(tmp_path/f'controller-{i}.json') for i in range(2)]
        assert submissions.read_text()=='submitted\n'
        assert results[0]['pool']['session']==results[1]['pool']['session']
        assert results[0]['pool']['request_id']!=results[1]['pool']['request_id']
    finally:
        for child in children:
            if child.is_alive():child.terminate();child.join(timeout=5)
