"""Remote-first CPU MSA admission; primary only on a confirmed capacity miss."""
import json
import re
import time
from .io import fingerprint,write_json
from pathlib import Path


def available_nodes(nodes, partition_text, resources):
    from .gpu_routing import fields,memory_mb
    partition=resources['partition'];rows=[fields(line) for line in partition_text.splitlines() if line.strip()]
    selected=[row for row in rows if row.get('PartitionName')==partition]
    if len(selected)!=1 or 'State' not in selected[0]:raise ValueError('Invalid Slurm partition observation')
    if selected[0]['State']!='UP' or selected[0].get('ReqResv','NO')!='NO':return []
    rows=[fields(line) for line in nodes.splitlines() if line.strip()]
    if not rows or any('NodeName' not in row or 'State' not in row for row in rows):raise ValueError('Invalid Slurm node observation')
    names=resources.get('nodelist')
    if names and not re.fullmatch(r'[A-Za-z0-9_.-]+(?:,[A-Za-z0-9_.-]+)*',names):
        raise ValueError('MSA routing nodelist requires explicit node names')
    result=[]
    for row in rows:
        if row['State'] not in ('IDLE','MIXED') or partition not in row.get('Partitions','').split(','):continue
        if names and row['NodeName'] not in names.split(','):continue
        if row.get('Owner','N/A') not in ('N/A','(null)'):continue
        if resources.get('constraint') and resources['constraint'] not in row.get('ActiveFeatures','').split(','):continue
        cpus=int(row.get('CPUEfctv',row.get('CPUTot')))-int(row['CPUAlloc'])
        memory=int(row['RealMemory'])-int(row['AllocMem'])
        if cpus>=resources['cpus'] and memory>=memory_mb(resources['memory']):
            result.append({'node':row['NodeName'],'cpus':cpus,'memory_mb':memory})
    return result


def pool_capacity(plan,transport):
    from .msa_remote import target_config
    c=target_config(plan['config']);e=c['execution'];s=e['msa_remote']
    compact={'config':{'msa':c['msa'],'execution':{'resources':{'cpu':e['resources']['cpu']},
        'python':{'msa':e['python']['msa']},'msa_remote':{'root':s['root'],'pool':s['pool']}}},'code_signature':plan['code_signature']}
    payload={'plan':compact,'environment_hash':fingerprint(plan['environment']['msa_remote'])}
    code='import json,sys;from antigen_design.msa_pool import availability;r=json.loads(sys.argv[1]);print(json.dumps(availability(**r)))'
    raw=transport.execute(s['host'],['env','PYTHONPATH='+s['source_dir'],s['python'],'-c',code,json.dumps(payload)],timeout=30)
    return json.loads(raw.splitlines()[-1])


def choose(plan,manifest,name,transport):
    c=plan['config'];s=c['execution']['msa_remote'];state=manifest['tasks'][name]
    if not s.get('fallback_when_busy'):return 'remote'
    if state.get('route') or state.get('msa_destination')=='remote':return 'remote'
    if state.get('msa_destination')=='primary':return 'primary'
    if state.get('msa_routing_retry_at',0)>time.time():return None
    observation={'checked_at':time.time(),'primary':s['host'],'fallback':c['execution']['cpu_host']}
    try:
        if 'msa_remote' not in plan['environment']:raise ValueError('Missing frozen remote MSA environment')
        capacity=pool_capacity(plan,transport) if s.get('pool',{}).get('enabled') else {'kind':'new'}
        observation['pool']=capacity
        if capacity['kind']=='reuse':
            target='remote';reason='msa-server 兼容共享索引池可接收请求'
        elif capacity['kind'] in ('new','busy'):
            resources=s['resources']
            nodes=transport.execute(s['host'],['scontrol','show','nodes','-o'],timeout=25)
            partition=transport.execute(s['host'],['scontrol','show','partition',resources['partition'],'-o'],timeout=25)
            available=available_nodes(nodes,partition,resources);observation['available_nodes']=available
            if available and capacity['kind']=='busy':
                raise RuntimeError('Remote nodes have capacity but the compatible pool is draining/full; wait for admission')
            target='remote' if available else 'primary'
            reason='msa-server 有满足 CPU/内存要求的空闲资源' if available else 'msa-server 无满足 CPU/内存要求的空闲资源；回退 primary'
        else:raise ValueError('Unknown MSA pool capacity response')
        state.update(msa_destination=target,routing_reason=reason)
        state.pop('msa_routing_retry_at',None)
    except Exception as exc:
        observation['error']=str(exc)[-2000:];state.update(msa_routing_retry_at=time.time()+s.get('poll_seconds',30),routing_reason='msa-server 资源查询失败或状态不明；等待重查，不自动切换 primary')
        target=None
    state['msa_routing']=observation
    write_json(Path(plan['local_output'])/'manifest.json',manifest)
    return target
