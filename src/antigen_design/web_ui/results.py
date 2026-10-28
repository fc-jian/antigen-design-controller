"""On-demand, verified previews; never advance or mutate scheduler state."""
import fcntl
import json
import math
import re
import shutil
import tarfile
import tempfile
import threading
from pathlib import Path
from antigen_design.io import read_json, digest, fingerprint
from antigen_design.folding_results import cif_residues, summary, annotate_design
from antigen_design.transport import get_transport
from antigen_design.web_store import Store as BaseStore
from . import export as exporter
from .native_timing import NativeTiming


def eligible(plan, manifest, name):
    task = plan['tasks'].get(name, {})
    return (task.get('stage') == 'metrics' and len(task.get('deps', [])) == 1
            and plan['tasks'][task['deps'][0]]['stage'] == 'fold'
            and all(manifest['tasks'].get(key, {}).get('state') == 'succeeded'
                    for key in (name, *task['deps'])))


def unpack(archive, target, plan, name, routes=None):
    fold = plan['tasks'][name]['deps'][0]
    allowed = {f'tasks/{key}' for key in (name, fold)}
    with tarfile.open(archive) as tar:
        members = tar.getmembers()
        if sum(m.size for m in members) > exporter.MAX_BYTES:
            raise ValueError('Preview archive exceeds size limit')
        seen = set()
        for member in members:
            path = Path(member.name)
            if (not member.isfile() or path.is_absolute() or '..' in path.parts
                    or len(path.parts) < 3 or '/'.join(path.parts[:2]) not in allowed
                    or member.name in seen):
                raise ValueError('Unsafe preview archive member')
            seen.add(member.name)
        for member in members:
            path = target / member.name
            path.parent.mkdir(parents=True, exist_ok=True)
            with tar.extractfile(member) as source, path.open('wb') as output:
                shutil.copyfileobj(source, output)
    verify(target, plan, name, routes)


def verify(target, plan, name, routes=None):
    remote = Path(plan['config']['output'])
    known = set()
    for key in (name, plan['tasks'][name]['deps'][0]):
        receipt_path = target / 'tasks' / key / 'complete.json'
        receipt = read_json(receipt_path)
        stage = plan['tasks'][key]['stage']
        expected_environment=plan['environment'][stage]
        if receipt.get('kind')=='verified_gpu_capsule_import':
            capsule=read_json(receipt_path.parent/'capsule-plan.json')
            route=(routes or {}).get(key,{}).get('route',{})
            proof=capsule['gpu_capsule']
            expected=route.get('plan_fingerprint') or (fingerprint(route['plan']) if 'plan' in route else None)
            if (fingerprint(capsule)!=expected or receipt.get('route_id')!=route.get('id')
                    or proof['origin_identity']!=plan['identity']
                    or proof['origin_task_signature']!=plan['tasks'][key]['signature']):
                raise ValueError('Preview capsule lineage mismatch')
            expected_environment=capsule['environment'][stage]
        if (receipt['signature'] != plan['tasks'][key]['signature']
                or receipt['environment'] != expected_environment or not receipt['artifacts']):
            raise ValueError('Preview receipt identity mismatch')
        for raw, expected in receipt['artifacts'].items():
            path = Path(raw)
            if '..' in path.parts or not path.is_relative_to(remote / 'tasks' / key):
                raise ValueError('Preview receipt path escape')
            local = target / path.relative_to(remote)
            if local.is_symlink() or not local.is_file() or digest(local) != expected:
                raise ValueError('Preview artifact checksum mismatch')
            known.add(str(local.relative_to(target)))
        known.add(str(receipt_path.relative_to(target)))
    actual = {str(p.relative_to(target)) for p in (target / 'tasks').rglob('*') if p.is_file()}
    if actual != known:
        raise ValueError('Unreceipted preview artifact')


def collect_preview(root, plan, name, routes=None):
    routes=routes if routes is not None else read_json(root/'pipeline/manifest.json')['tasks']
    cache = root / 'web-previews'
    cache.mkdir(exist_ok=True)
    destination = cache / plan['tasks'][name]['signature']
    with (cache / (name + '.lock')).open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        if destination.exists():
            verify(destination, plan, name, routes)
            return destination
        transport = get_transport(plan['config'])
        execution = plan['config']['execution']
        host = execution['cpu_host']
        helper = Path(exporter.__file__)
        remote_root = Path(plan['config']['output'])
        remote_helper = remote_root / 'web-previews' / 'tools' / (digest(helper) + '.py')
        transport.execute(host, ['mkdir','-p',str(remote_helper.parent)])
        transport.upload(host, helper, remote_helper)
        raw = transport.execute(host, ['env','PYTHONPATH='+execution['source_dir'],
            execution['python']['metrics'],str(remote_helper),str(remote_root/'plan.json'),name,plan['identity']], timeout=180)
        info = json.loads(raw.splitlines()[-1])
        expected_path = remote_root / 'web-previews' / plan['tasks'][name]['signature'] / 'preview.tar.gz'
        if info['path'] != str(expected_path) or not 0 < info['bytes'] <= exporter.MAX_BYTES:
            raise ValueError('Invalid preview export')
        with tempfile.TemporaryDirectory(dir=cache) as temporary:
            temporary = Path(temporary)
            archive = temporary / 'preview.tar.gz'
            transport.download(host, expected_path, archive)
            if digest(archive) != info['sha256']:
                raise ValueError('Preview archive checksum mismatch')
            staged = temporary / 'verified'
            staged.mkdir()
            unpack(archive, staged, plan, name, routes)
            staged.rename(destination)
    return destination


def preview_view(root, plan, name, index, result_id, routes=None):
    target = collect_preview(root, plan, name, routes)
    return result_from_directory(root, plan, target, name, index, result_id)


def result_from_directory(root, plan, target, name, index, result_id):
    """Build the same structure/metric view from a verified preview or collection."""
    rows = read_json(target / 'tasks' / name / 'metrics.json')
    if not 0 <= index < len(rows):
        raise ValueError('Unknown result')
    row = rows[index]
    remote = Path(plan['config']['output'])
    fold = plan['tasks'][name]['deps'][0]
    artifacts = read_json(target / 'tasks' / fold / 'complete.json')['artifacts']
    def local(raw):
        if raw not in artifacts:
            raise ValueError('Result is not covered by the folding receipt')
        return target / Path(raw).relative_to(remote)
    prediction, confidence = local(row['prediction']), local(row['confidence_path'])
    result = {'id':result_id,'name':row.get('name',row.get('design',name)),
        'seed':row['fold_seed'],'sample':row['sample'],'metrics':row,'confidence':summary(confidence),
        'residues':cif_residues(prediction),'structure':str(prediction.relative_to(root)),
        'summary_file':str(confidence.relative_to(root)), 'pae':None,'pae_labels':None,
        'verification':'folding and metrics receipts + archive and artifact SHA256'}
    full = [p for p in prediction.parent.glob(f'*full*sample_{row["sample"]}.json')
            if str(remote / p.relative_to(target)) in artifacts]
    if len(full) == 1:
        result['full_confidence_file'] = str(full[0].relative_to(root))
        if full[0].stat().st_size < 64 * 1024**2:
            data = read_json(full[0])
            if isinstance(data,list) and len(data)==1:
                data=data[0]
            if isinstance(data,dict):
                pae=data.get('pae',data.get('token_pair_pae'))
                if (isinstance(pae,list) and 0<len(pae)<=2000
                        and all(isinstance(r,list) and len(r)==len(pae) for r in pae)
                        and all(isinstance(v,(float,int)) and math.isfinite(v) and v>=0 for r in pae for v in r)):
                    result['pae']=pae
                    ids,positions=data.get('token_chain_ids'),data.get('token_res_ids')
                    if isinstance(ids,list) and isinstance(positions,list) and len(ids)==len(positions)==len(pae):
                        result['pae_labels']=[f'{c}:{p}' for c,p in zip(ids,positions)]
    return annotate_design(result,plan)


class Store(BaseStore):
    def __init__(self,*args,**kwargs):
        super().__init__(*args,**kwargs)
        self._snapshots={}
        self._snapshot_lock=threading.RLock()
        self._native_timing=NativeTiming()

    def snapshot(self,root):
        path=root/'pipeline/manifest.json'
        stat=path.stat()
        key=(stat.st_ino,stat.st_mtime_ns,stat.st_size)
        with self._snapshot_lock:
            cached=self._snapshots.get(str(root))
            if cached and cached[0]==key:
                return cached[1],cached[2]
            plan=read_json(root/'pipeline/plan.json')
            manifest=read_json(path)
            # Capsules embed complete plans; retain their checksums, not hundreds
            # of megabytes of copies, in the web read cache.
            for state in manifest['tasks'].values():
                for record in (state,*state.get('history',[])):
                    route=record.get('route',{})
                    capsule=route.pop('plan',None)
                    if capsule is not None and record.get('stage')=='fold':
                        route['plan_fingerprint']=fingerprint(capsule)
            self._snapshots[str(root)]=(key,plan,manifest)
            return plan,manifest

    def _detail(self, job):
        root = self.job_root(job)
        request,status=read_json(root/'request.json'),read_json(root/'status.json')
        value={'id':job,**{k:request[k] for k in ('name','mode','profile')},**status,'tasks':[],'results':[]}
        value['archived']=read_json(root/'archive.json')['archived'] if (root/'archive.json').exists() else False
        value['download_ready'] = (root / 'pipeline/deliverables.tar.gz').is_file() and (root/'results.json').exists()
        if (root/'results.json').exists():
            value['results']=read_json(root/'results.json')
        if (root/'controller.log').exists():
            with (root/'controller.log').open('rb') as stream:
                stream.seek(max(0,(root/'controller.log').stat().st_size-16000))
                value['controller_log']=stream.read().decode(errors='replace')
        if not (root/'pipeline/manifest.json').exists():
            if (root/'pipeline/plan.json').exists():
                value.update(initializing=True,error='Initialization incomplete; waiting for manifest')
            return value
        plan,manifest=self.snapshot(root)
        monitor=read_json(root/'monitor.json') if (root/'monitor.json').exists() else {}
        value.update(observed_at=monitor.get('observed_at'),monitor_error=monitor.get('error'),gpu_routing=manifest.get('gpu_routing'))
        for name,task in plan['tasks'].items():
            state=manifest['tasks'][name]
            item={'id':name,'stage':task['stage'],'host':task['host'],**state,'observation':monitor.get('tasks',{}).get(name)}
            item['observation']=self._native_timing.enrich(root,plan,name,state,item['observation'])
            if state.get('route'):
                item['route']={k:v for k,v in state['route'].items() if k in ('id','target','resources','input_sha256','input_bytes')}
            item['history']=[{k:v for k,v in h.items() if k in ('host','job_id','attempts','slurm_state','execution_state','backend','state','error','resources')}
                             for h in state.get('history',[])]
            value['tasks'].append(item)
        previews=[]
        for name in plan['tasks']:
            if not eligible(plan,manifest,name):
                continue
            path=root/'pipeline/tasks'/name/'metrics.json'
            if not path.exists():
                continue
            for index,row in enumerate(read_json(path)):
                previews.append({'id':f'{name}~{index}','task':name,
                    'name':row.get('name',row.get('design',name)), 'seed':row['fold_seed'],
                    'sample':row['sample'],'metrics':row,'preview':True})
        if previews:
            value['results']=previews
        return value

    def result_view(self, job, index):
        value=str(index)
        if value.isdigit():
            return super().result_view(job,int(value))
        match=re.fullmatch(r'(metrics_[A-Za-z0-9_]+)~([0-9]+)',value)
        if not match:
            raise ValueError('Invalid result ID')
        root=self.job_root(job)
        plan,manifest=self.snapshot(root)
        name,number=match.groups()
        if not eligible(plan,manifest,name):
            raise ValueError('Folding and metrics have not both completed')
        if (root/'results.json').exists():
            rows=read_json(root/'pipeline/tasks'/name/'metrics.json')
            if not 0<=int(number)<len(rows):
                raise ValueError('Unknown result')
            row=rows[int(number)]
            for legacy in read_json(root/'results.json'):
                metrics=legacy.get('metrics',{})
                if all(metrics.get(k)==row[k] for k in ('prediction','fold_seed','sample')):
                    result=super().result_view(job,legacy['id'])
                    result['id']=value
                    return result
        return preview_view(root,plan,name,int(number),value,manifest['tasks'])

    def result_file(self, job, index, kind):
        root=self.job_root(job)
        result=self.result_view(job,index)
        field={'structure':'structure','confidence':'summary_file','full-confidence':'full_confidence_file'}.get(kind)
        if not field or field not in result:
            raise FileNotFoundError('Artifact unavailable')
        target=(root/result[field]).resolve()
        if not any(target.is_relative_to((root/folder).resolve()) for folder in ('collected','web-previews')):
            raise ValueError('Invalid artifact path')
        return target
