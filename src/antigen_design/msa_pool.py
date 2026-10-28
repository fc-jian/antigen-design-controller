"""Bounded, single-allocation MSA queue. No cross-job tmpfs reuse or hidden retries."""
from contextlib import contextmanager
from copy import deepcopy
import fcntl
import getpass
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import threading
import time
import uuid
from .io import digest, fingerprint, read_json, write_json

TERMINAL = {'COMPLETED','FAILED','CANCELLED','TIMEOUT','OUT_OF_MEMORY','NODE_FAIL','PREEMPTED','BOOT_FAIL','DEADLINE','REVOKED'}


def enabled(plan):
    return bool(plan.get('msa_capsule') and plan['config']['execution'].get('msa_remote',{}).get('pool',{}).get('enabled'))


def settings(plan):
    return plan['config']['execution']['msa_remote']['pool']


def file_identity(path):
    stat=Path(path).stat()
    return [stat.st_ino,stat.st_size,stat.st_mtime_ns,stat.st_ctime_ns]


def database_snapshot(c):
    from .msa_execution import database_files
    root=Path(c['database']);manifest=root/'transfer-manifest.json'
    published=read_json(manifest)
    if published['database_version']!=c['database_version']:
        raise ValueError('Pool database publication version mismatch')
    entries={item['name']:item for item in published['files']}
    files=database_files(root,c['db1'],c['db3'])
    if any(f.name not in entries or entries[f.name]['size']!=f.stat().st_size for f in files):
        raise ValueError('Pool database publication inventory mismatch')
    return {'root':str(root.resolve()),'manifest_sha256':digest(manifest),
        'files':{f.name:file_identity(f) for f in files},
        'index_hashes':{db+'.idx':entries[db+'.idx']['sha256'] for db in (c['db1'],c['db3'])}}


def msa_identity(c):
    return {k:v for k,v in c.items() if k!='cache_root'}


def compatibility(plan, environment_hash=None):
    c=plan['config'];e=c['execution']
    return fingerprint({'msa':msa_identity(c['msa']),'database':database_snapshot(c['msa']),
        'environment':environment_hash or fingerprint(plan['environment']['msa']),'code':plan['code_signature'],
        'python':e['python']['msa'],'resources':e['resources']['cpu'],'pool':settings(plan)})



def availability(plan, environment_hash=None):
    """Read-only admission snapshot; no allocation or database staging."""
    base=pool_base(plan)/compatibility(plan,environment_hash)
    current=base/'current.json'
    if not current.exists():return {'kind':'new'}
    with locked(base/'coordinator.lock'):
        session=Path(read_json(current)['session']);meta=read_json(session/'session.json')
        if meta.get('rejected'):return {'kind':'new'}
        observed=allocation(session)
        if observed['state']=='UNKNOWN':raise RuntimeError('Pool allocation state unknown')
        if observed['state'] in TERMINAL:return {'kind':'new'}
        status=read_json(session/'status.json') if (session/'status.json').exists() else {}
        if status.get('heartbeat',time.time())<time.time()-120 or status.get('phase')=='failed':
            raise RuntimeError('Pool heartbeat or health uncertain')
        waiting=sum(read_json(f).get('state') in ('queued','running') for f in (session/'requests').glob('*/state.json'))
        busy=(waiting>=settings(plan)['queue_limit'] or (session/'drain.request').exists()
            or status.get('phase') in ('closing','closed')
            or status.get('started_at',time.time())+settings(plan)['max_seconds']-time.time()<settings(plan)['request_timeout_seconds'])
        return {'kind':'busy' if busy else 'reuse','session':str(session),'job_id':meta['job_id'],'queued_or_running':waiting}


def pool_base(plan):
    return Path(plan['config']['execution']['msa_remote']['root'])/'pools'


@contextmanager
def locked(path):
    path=Path(path);path.parent.mkdir(parents=True,exist_ok=True)
    with path.open('a') as stream:
        fcntl.flock(stream,fcntl.LOCK_EX)
        yield


def request_id(plan,name,attempt):
    if name not in plan['tasks'] or plan['tasks'][name]['stage']!='msa' or type(attempt) is not int or attempt<1:
        raise ValueError('Invalid pooled MSA request')
    return fingerprint({'identity':plan['identity'],'task':name,'attempt':attempt})


def record_path(plan,name,attempt):
    request_id(plan,name,attempt)
    return Path(plan['config']['output'])/'control'/f'{name}_a{attempt}.json'


def allocation(session,job=None):
    meta=read_json(Path(session)/'session.json');job=str(job or meta.get('job_id',''))
    if not job.isdigit():
        raise RuntimeError('Pool submission outcome unknown; reconcile exact allocation before retrying')
    raw=subprocess.check_output(['sacct','-n','-P','-X','-j',job,
        '--format=JobIDRaw,State,ExitCode,Elapsed,AllocCPUS,ReqMem,Partition,JobName%200,WorkDir%500,User'],text=True)
    rows=[r.split('|') for r in raw.splitlines() if r.split('|')[0]==job]
    if not rows:return {'state':'UNKNOWN','job_id':job}
    if len(rows)!=1 or rows[0][7:10]!=[meta['job_name'],str(session),getpass.getuser()]:
        raise ValueError('Pool allocation owner/name/workdir mismatch')
    row=rows[0];state=row[1].split()[0].rstrip('+')
    if state=='COMPLETED' and row[2]!='0:0':state='FAILED'
    return dict(zip(('job_id','state','exit_code','elapsed','cpus','memory','partition'),[row[0],state,*row[2:7]]))


def load_reference(plan,name,attempt):
    rec=read_json(record_path(plan,name,attempt));ref=rec['pool']
    session=Path(ref['session']).resolve()
    if not session.is_relative_to(pool_base(plan).resolve()) or session.parent.name!='sessions':
        raise ValueError('Invalid pool session path')
    rid=request_id(plan,name,attempt)
    if ref['request_id']!=rid:raise ValueError('Pool request identity mismatch')
    payload=read_json(session/'requests'/rid/'request.json')
    expected={'id':rid,'plan':str(Path(plan['config']['output'])/'plan.json'),'plan_sha256':digest(Path(plan['config']['output'])/'plan.json'),'task':name,'attempt':attempt}
    if any(payload[k]!=v for k,v in expected.items()):raise ValueError('Pool request provenance mismatch')
    meta=read_json(session/'session.json')
    if rec.get('job_id') and rec['job_id']!=meta.get('job_id'):raise ValueError('Pool allocation identity mismatch')
    return rec,session,payload


def submit(plan,request):
    from .scheduler import slurm_script
    name,attempt=request['task'],request['attempt'];rid=request_id(plan,name,attempt)
    if request.get('memory_tier',0)!=0:raise ValueError('Pool does not change memory tiers')
    key=compatibility(plan);base=pool_base(plan)/key;base.mkdir(parents=True,exist_ok=True)
    record=record_path(plan,name,attempt);record.parent.mkdir(exist_ok=True)
    with locked(base/'coordinator.lock'):
        if record.exists():
            old=read_json(record)
            if old.get('rejected'):return old
            old,session,payload=load_reference(plan,name,attempt)
            meta=read_json(session/'session.json')
            if not meta.get('job_id'):raise RuntimeError('Pool allocation submission unknown; explicit reconciliation required')
            result=old|{'job_id':meta['job_id']};write_json(record,result);return result
        current=base/'current.json';session=None
        if current.exists():
            candidate=Path(read_json(current)['session']);meta=read_json(candidate/'session.json')
            if meta.get('rejected'):observed={'state':'FAILED'}
            else:observed=allocation(candidate)
            status=read_json(candidate/'status.json') if (candidate/'status.json').exists() else {}
            if observed['state']=='UNKNOWN':raise RuntimeError('Pool allocation state unknown; no replacement submitted')
            if observed['state'] not in TERMINAL:
                stale=status.get('heartbeat',time.time())<time.time()-120
                expired=status.get('started_at',time.time())+settings(plan)['max_seconds']-time.time()<settings(plan)['request_timeout_seconds']
                if stale or expired or status.get('phase') in ('closing','failed','closed') or (candidate/'drain.request').exists():
                    return {'rejected':'Existing MSA pool is draining or unhealthy; wait for its exact allocation to terminate'}
                session=candidate
        new=session is None
        if new:
            session=base/'sessions'/uuid.uuid4().hex;session.mkdir(parents=True)
            (session/'requests').mkdir()
            meta={'key':key,'session':str(session),'first_plan':str(Path(plan['config']['output'])/'plan.json'),
                'first_plan_sha256':digest(Path(plan['config']['output'])/'plan.json'),
                'job_name':'amsa_'+session.name,'created_at':time.time(),'resources':plan['config']['execution']['resources']['cpu']}
            write_json(session/'session.json',meta);write_json(current,{'session':str(session)})
        waiting=sum(read_json(f).get('state') in ('queued','running') for f in (session/'requests').glob('*/state.json'))
        if waiting>=settings(plan)['queue_limit']:return {'rejected':'MSA pool request queue is full'}
        directory=session/'requests'/rid;directory.mkdir()
        payload={'id':rid,'plan':str(Path(plan['config']['output'])/'plan.json'),'plan_sha256':digest(Path(plan['config']['output'])/'plan.json'),
            'task':name,'attempt':attempt,'created_at':time.time(),'compatibility':key}
        write_json(directory/'request.json',payload);write_json(directory/'state.json',{'state':'queued','queued_at':time.time()})
        result={'pool':{'session':str(session),'request_id':rid},'memory_tier':0,'resources':meta['resources']}
        write_json(record,result)
        if new:
            e=plan['config']['execution']
            argv=['env','PYTHONPATH='+e['source_dir'],e['python']['msa'],'-m','antigen_design.msa_pool',str(session)]
            script=session/'worker.sh';script.write_text(slurm_script(argv,session,session/'worker.log',meta['resources'],meta['job_name']))
            completed=subprocess.run(['sbatch','--parsable','--chdir',str(session),str(script)],capture_output=True,text=True)
            if completed.returncode:
                meta['rejected']=completed.stderr.strip() or completed.stdout.strip();write_json(session/'session.json',meta)
                result['rejected']=meta['rejected'];write_json(record,result);return result
            job=completed.stdout.strip().split(';')[0]
            if not job.isdigit():raise RuntimeError('Unknown pool sbatch response; reconcile before retrying')
            meta['job_id']=job;write_json(session/'session.json',meta)
        result['job_id']=meta['job_id'];write_json(record,result)
        return result


def reconcile(plan,request):
    rec,session,payload=load_reference(plan,request['task'],request['attempt'])
    job=str(request['job_id'])
    with locked(session.parent.parent/'coordinator.lock'):
        meta=read_json(session/'session.json')
        if meta.get('job_id') not in (None,job) or meta.get('rejected'):raise ValueError('Pool cannot reconcile a different allocation')
        observed=allocation(session,job)
        if observed['state']=='UNKNOWN':raise ValueError('Exact pool allocation identity not available')
        meta['job_id']=job;write_json(session/'session.json',meta)
        rec.update(job_id=job,reconciled=True);write_json(record_path(plan,request['task'],request['attempt']),rec)
        return rec


def inspect(plan,requests):
    from .worker import receipt_valid
    results={}
    for name,req in requests.items():
        rec,session,payload=load_reference(plan,name,req['attempts'])
        observed=allocation(session);state=read_json(session/'requests'/payload['id']/'state.json')
        status=read_json(session/'status.json') if (session/'status.json').exists() else {'phase':'queued'}
        logical={'queued':'PENDING','running':'RUNNING','succeeded':'COMPLETED','failed':'FAILED','cancelled':'CANCELLED'}[state['state']]
        if observed['state'] in TERMINAL and logical in ('PENDING','RUNNING'):logical='FAILED'
        if observed['state']=='UNKNOWN' and logical in ('PENDING','RUNNING'):logical='UNKNOWN'
        info={'slurm_state':observed['state'],'execution_state':logical,'pool_state':state['state'],
            'pool_phase':status['phase'],'pool':rec['pool'],'allocation':observed,'recovered_job_id':observed['job_id']}
        if logical=='COMPLETED':info['valid']=receipt_valid(Path(plan['config']['output'])/'tasks'/name,plan['tasks'][name]['signature'])
        if logical=='FAILED':info['error']={'error':state.get('error') or status.get('error') or 'Pool allocation ended before request completed; no automatic retry'}
        if logical=='CANCELLED':info['error']={'error':'MSA request cancelled; shared allocation preserved'}
        results[name]=info
    return results


def monitor(plan,requests):
    result={}
    for name,info in inspect(plan,requests).items():
        result[name]=info['allocation']|{'pool_state':info['pool_state'],'pool_phase':info['pool_phase'],'request_state':info['execution_state']}
        log=Path(plan['config']['output'])/'logs'/f"{name}_a{requests[name]['attempts']}.log"
        if log.exists():
            with log.open('rb') as stream:
                stream.seek(max(0,log.stat().st_size-12000));result[name]['log_tail']=stream.read().decode(errors='replace')
    return result


def cancel(plan,requests):
    for name,req in requests.items():
        rec,session,payload=load_reference(plan,name,req['attempts'])
        allocation(session)  # exact owner/name/workdir, never scancel a shared allocation
        with locked(session.parent.parent/'coordinator.lock'):
            (session/'requests'/payload['id']/'cancel.request').touch()
    return monitor(plan,requests)


def drain(plan,request):
    rec,session,payload=load_reference(plan,request['task'],request['attempt'])
    allocation(session)
    with locked(session.parent.parent/'coordinator.lock'):(session/'drain.request').touch()
    return {'session':str(session),'draining':True}


def create_context(plan,session,database):
    c=plan['config']['msa'];snapshot=database_snapshot(c);started=time.monotonic()
    for name,expected in snapshot['index_hashes'].items():
        path=Path(database)/name
        if digest(path)!=expected:raise ValueError('Staged index differs from published database checksum')
        path.chmod(0o400)
    view={name:{'path':str((Path(database)/name).resolve()),'stat':file_identity(Path(database)/name)} for name in snapshot['files']}
    context={'session':str(session),'job_id':os.environ['SLURM_JOB_ID'],'code_signature':plan['code_signature'],
        'msa':msa_identity(c),'database_snapshot':snapshot,'database':str(database),'view':view,
        'checksum_seconds':time.monotonic()-started,'ready_at':time.time(),
        'staging':read_json(Path(session)/'msa_staging.json')}
    write_json(Path(session)/'context.json',context)
    return context


def reuse_context(config,path,directory):
    from .worker import code_signature
    context=read_json(path)
    if context['job_id']!=os.environ.get('SLURM_JOB_ID') or context['code_signature']!=code_signature() or context['msa']!=msa_identity(config):
        raise ValueError('MSA pool context allocation/code/config mismatch')
    if context['database_snapshot']!=database_snapshot(config):raise ValueError('MSA pool source database changed')
    for name,expected in context['view'].items():
        source=Path(context['database'])/name
        if str(source.resolve())!=expected['path'] or file_identity(source)!=expected['stat']:
            raise ValueError('Validated MSA pool database view changed')
    write_json(Path(directory)/'msa_staging.json',{'mode':'pool','reused':True,'pool_session':context['session'],
        'allocation_job_id':context['job_id'],'index_sha256':context['database_snapshot']['index_hashes'],
        'copy_seconds_once':context['staging']['elapsed_seconds'],'checksum_seconds_once':context['checksum_seconds'],
        'elapsed_seconds':0.0,'pool_request_id':os.environ.get('ANTIGEN_MSA_POOL_REQUEST')})
    return context['database']


def stop_child(child):
    if child.poll() is not None:return
    os.killpg(child.pid,signal.SIGTERM)
    try:child.wait(timeout=10)
    except subprocess.TimeoutExpired:
        os.killpg(child.pid,signal.SIGKILL);child.wait()


def execute_request(plan,session,payload,context_path,deadline):
    from .worker import receipt_valid
    directory=Path(session)/'requests'/payload['id'];state=read_json(directory/'state.json')
    if (directory/'cancel.request').exists():
        write_json(directory/'state.json',state|{'state':'cancelled','finished_at':time.time()});return
    path=Path(payload['plan'])
    if digest(path)!=payload['plan_sha256']:raise ValueError('Queued plan checksum changed')
    incoming=read_json(path)
    if compatibility(incoming)!=payload['compatibility'] or incoming['code_signature']!=plan['code_signature']:
        raise ValueError('Queued request no longer compatible with pool')
    root=Path(incoming['config']['output']);name=payload['task'];attempt=payload['attempt']
    if request_id(incoming,name,attempt)!=payload['id']:raise ValueError('Queued request identity changed')
    taskdir=root/'tasks'/name
    if taskdir.exists():
        old=root/'attempts'/f'{name}_before_a{attempt}';old.parent.mkdir(exist_ok=True);taskdir.rename(old)
    log=root/'logs'/f'{name}_a{attempt}.log';log.parent.mkdir(exist_ok=True)
    env=os.environ.copy();env.update(ANTIGEN_MSA_POOL_CONTEXT=str(context_path),ANTIGEN_MSA_POOL_REQUEST=payload['id'])
    state.update(state='running',started_at=time.time());write_json(directory/'state.json',state)
    with log.open('wb') as output:
        child=subprocess.Popen([incoming['config']['execution']['python']['msa'],'-m','antigen_design.worker',str(path),name,'--attempt',str(attempt)],env=env,stdout=output,stderr=subprocess.STDOUT,start_new_session=True)
        try:
            expires=min(deadline,time.time()+settings(plan)['request_timeout_seconds'])
            while child.poll() is None:
                if (directory/'cancel.request').exists() or time.time()>=expires:
                    stop_child(child);break
                time.sleep(1)
        finally:
            stop_child(child)
    cancelled=(directory/'cancel.request').exists()
    valid=child.returncode==0 and receipt_valid(taskdir,incoming['tasks'][name]['signature'])
    state.update(state='cancelled' if cancelled else 'succeeded' if valid else 'failed',finished_at=time.time(),exit_code=child.returncode)
    if not valid and not cancelled:state['error']='Pooled MSA worker failed or exceeded request deadline; see request log'
    write_json(directory/'state.json',state)


def serve(session):
    from .msa_execution import staged_database
    from .worker import code_signature
    session=Path(session).resolve();meta=read_json(session/'session.json')
    for _ in range(60):
        meta=read_json(session/'session.json')
        if meta.get('job_id'):break
        time.sleep(1)
    if meta.get('job_id')!=os.environ.get('SLURM_JOB_ID'):raise ValueError('Pool allocation identity not durably recorded')
    path=Path(meta['first_plan'])
    if digest(path)!=meta['first_plan_sha256']:raise ValueError('Pool bootstrap plan changed')
    plan=read_json(path)
    if code_signature()!=plan['code_signature'] or compatibility(plan)!=meta['key']:raise ValueError('Pool bootstrap identity changed')
    if os.environ.get('ANTIGEN_MSA_POOL_CONTEXT'):raise ValueError('Nested MSA pools are not supported')
    status={'phase':'staging','started_at':time.time(),'job_id':meta['job_id'],'completed_requests':0}
    stop=threading.Event();status_lock=threading.RLock()
    def save():
        with status_lock:write_json(session/'status.json',status|{'heartbeat':time.time()})
    def heartbeat():
        while not stop.wait(10):save()
    save();thread=threading.Thread(target=heartbeat,daemon=True);thread.start()
    deadline=status['started_at']+settings(plan)['max_seconds']
    try:
        with staged_database(plan['config']['msa'],session,lanes=plan['config']['msa']['optimization']['lanes']) as database:
            status['phase']='verifying';save()
            context=create_context(plan,session,database);status.update(phase='ready',ready_at=context['ready_at']);save()
            idle_since=time.time()
            while True:
                with locked(session.parent.parent/'coordinator.lock'):
                    pending=[]
                    for f in (session/'requests').glob('*/state.json'):
                        if read_json(f)['state']=='queued':pending.append(read_json(f.parent/'request.json'))
                    pending.sort(key=lambda r:(r['created_at'],r['id']))
                    closing=time.time()>=deadline-settings(plan)['request_timeout_seconds']
                    if not pending and ((session/'drain.request').exists() or time.time()-idle_since>=settings(plan)['idle_seconds'] or closing):
                        status['phase']='closing';save();break
                    if pending and closing:
                        for req in pending:write_json(session/'requests'/req['id']/'state.json',{'state':'failed','error':'Pool lifetime exhausted before request started','finished_at':time.time()})
                        status['phase']='closing';save();break
                if not pending:time.sleep(1);continue
                request=pending[0];status.update(phase='serving',active_request=request['id']);save()
                try:execute_request(plan,session,request,session/'context.json',deadline)
                except Exception as exc:
                    write_json(session/'requests'/request['id']/'state.json',{'state':'failed','error':str(exc),'finished_at':time.time()})
                idle_since=time.time();status.update(phase='ready',active_request=None,completed_requests=status['completed_requests']+1);save()
        status['phase']='closed';status['closed_at']=time.time();save()
    except Exception as exc:
        status.update(phase='failed',error=str(exc));save();raise
    finally:
        stop.set();thread.join(timeout=15)


if __name__=='__main__':
    with locked(Path(sys.argv[1])/'worker.lock'):
        serve(sys.argv[1])
