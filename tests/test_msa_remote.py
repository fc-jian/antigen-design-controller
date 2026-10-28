"""Cross-storage tests use small fixtures, never a scheduler or real search."""
from copy import deepcopy
from pathlib import Path
import json
import pytest
from antigen_design.config import DEFAULTS, merge
from antigen_design.io import write_json, read_json, digest
from antigen_design.msa_transfer import pack, results, import_results
from antigen_design.gpu_transfer import extract
from antigen_design.worker import receipt_valid


def fixture(tmp_path, performance=False, homomer=False):
    c=deepcopy(DEFAULTS)
    source=tmp_path/"primary"; source.mkdir()
    target=tmp_path/"remote"
    c.update(mode="folding",output=str(source))
    c["execution"]["msa_remote"].update(enabled=True,root=str(target),database="/db",script="/converter",mmseqs="/mmseqs",cache_root=str(tmp_path/"cache"),python="/python",source_dir="/src",resources={"cpus":32,"memory":"256G"})
    record={"id":"query_1","chains":{"A":"ACDE"}}
    if homomer:
        record['chains']['B']='ACDE'
        record['job']={'name':'query_1','sequences':[{'proteinChain':{'id':['A','B'],'sequence':'ACDE','count':2}},{'rnaSequence':{'id':['R'],'sequence':'ACGU','count':1}}],'covalent_bonds':[]}
    p={"config":c,"mode":"folding","identity":"origin","code_signature":"code","environment":{"msa":{"code":"code","errors":[]}},"fold_records":[record],"tasks":{"msa":{"id":"msa","stage":"msa","deps":[],"signature":"task","members":[{"record":0}]}}}
    route={"id":"a"*32,"root":str(target),"kind":"msa"}
    payload=pack(p,{"task":"msa","route":route})
    route["plan"]=payload["plan"]
    extract(payload["path"],target,payload["sha256"])
    cp=payload["plan"]
    task=target/"tasks/msa";task.mkdir(parents=True)
    cache=tmp_path/"cache";cache.mkdir()
    msa=cache/"a.a3m";msa.write_text(">query\nACDE\n>hit\nAC-E\n")
    write_json(task/"runtime.json",{"slurm_job_id":"123"})
    write_json(task/"msa_batch.json",{"unique_sequences":1})
    write_json(task/"fold_inputs/query_1.json",[{"name":"query_1","sequences":[{"proteinChain":{"id":["A"],"sequence":"ACDE","count":1,"pairedMsaPath":str(msa),"unpairedMsaPath":str(msa)}}]}])
    if homomer:
        payload=deepcopy(record['job']);payload['sequences'][0]['proteinChain'].update(pairedMsaPath=str(msa),unpairedMsaPath=str(msa))
        write_json(task/'fold_inputs/query_1.json',[payload])
    if performance:
        for name in ('msa_staging.json','msa_execution.json'):
            write_json(task/name,{'pool_request_id':'independent-request','elapsed_seconds':12.5})
    artifacts={str(x):digest(x) for x in task.rglob("*") if x.is_file()};artifacts[str(msa)]=digest(msa)
    write_json(task/"complete.json",{"signature":cp["tasks"]["msa"]["signature"],"environment":cp["environment"]["msa"],"artifacts":artifacts})
    result=results(cp,{"task":"msa","attempt":1})
    request={"task":"msa","route":route,"path":result["path"],"sha256":result["sha256"],"job_id":"123"}
    return p,request,msa


def test_roundtrip_atomic_and_idempotent(tmp_path):
    p,request,_=fixture(tmp_path)
    assert import_results(p,request)=={"valid":True}
    dest=Path(p["config"]["output"])/"tasks/msa"
    assert receipt_valid(dest,"task")
    payload=read_json(dest/"fold_inputs/query_1.json")
    assert Path(payload[0]["sequences"][0]["proteinChain"]["unpairedMsaPath"]).is_relative_to(dest)
    assert import_results(p,request)=={"valid":True}


@pytest.mark.parametrize("kind",["job","hash","lineage","candidate"])
def test_bad_return_never_publishes(tmp_path,kind):
    p,q,_=fixture(tmp_path)
    if kind=="job":q["job_id"]="124"
    if kind=="hash":q["sha256"]="wrong"
    if kind=="lineage":p["identity"]="another"
    if kind=="candidate":p["fold_records"][0]["chains"]["A"]="AAAA"
    with pytest.raises(ValueError):import_results(p,q)
    assert not (Path(p["config"]["output"])/"tasks/msa/complete.json").exists()


def test_export_rejects_corrupt_source(tmp_path):
    p,q,msa=fixture(tmp_path)
    msa.write_text(">bad\nAAAA\n")
    with pytest.raises(ValueError):results(q["route"]["plan"],{"task":"msa","attempt":1})


def test_records_preserve_multichain_provenance(tmp_path):
    p,q,_=fixture(tmp_path)
    p["fold_records"][0]["chains"]["B"]="AAAA"
    from antigen_design.msa_transfer import records
    assert records(p,p["tasks"]["msa"])[0]["chains"]=={"A":"ACDE","B":"AAAA"}


def test_transfer_conflict_prevents_execute(tmp_path,monkeypatch):
    from antigen_design import msa_remote
    calls=[]
    class Result:
        returncode=0;stdout="Summary: planned=1, skipped=0, deleted=0, ignored_extra=0, conflicts=1, failures=0";stderr=""
    monkeypatch.setattr(msa_remote.subprocess,"run",lambda argv,**kwargs:calls.append(argv) or Result())
    with pytest.raises(RuntimeError):msa_remote.sync({"transfer_command":["wrapper"]},"up","/primary","/remote",tmp_path)
    assert len(calls)==1 and "--execute" not in calls[0]


def test_return_failure_keeps_job_and_retries_transfer_only(tmp_path,monkeypatch):
    from antigen_design import msa_remote,remote
    p,q,_=fixture(tmp_path);p["local_output"]=str(tmp_path)
    s={"route":q["route"],"host":"msa-server","job_id":"123","attempts":1,"state":"running"}
    monkeypatch.setattr(remote,"control_raw",lambda *a,**k:{"directory":"/export","sha256":"hash"})
    monkeypatch.setattr(msa_remote,"sync",lambda *a,**k:(_ for _ in ()).throw(RuntimeError("network")))
    assert not msa_remote.returned(p,s,"msa",None)
    assert s["job_id"]=="123" and not s.get("imported") and s["transfer_error"]=="network"
    assert not msa_remote.returned(p,s,"msa",None)


def test_remote_config_resources_merge():
    c=merge(DEFAULTS,{"execution":{"msa_remote":{"resources":{"partition":"compute","cpus":32,"memory":"256G"}}}})
    assert c["execution"]["msa_remote"]["resources"]["partition"]=="compute"


def test_transfer_pending_blocks_downstream(monkeypatch,tmp_path):
    from antigen_design import remote
    p,q,_=fixture(tmp_path);p["local_output"]=str(tmp_path)
    state={"state":"running","host":"msa-server","stage":"msa","route":q["route"],"job_id":"123","attempts":1}
    m={"tasks":{"msa":state}}
    monkeypatch.setattr(remote,"control",lambda *a,**kw:{"msa":{"slurm_state":"COMPLETED","transfer_pending":True}})
    remote.refresh(p,m,None)
    assert state["state"]=="running" and state["slurm_state"]=="COMPLETED"


def test_return_wrong_query_even_with_valid_receipt(tmp_path):
    from antigen_design.gpu_transfer import archive_files
    p,q,_=fixture(tmp_path)
    work=tmp_path/"tamper";extract(q["path"],work,q["sha256"])
    receipt=read_json(work/"worker-receipt.json")
    mapping=read_json(work/"artifact-map.json")
    old=next(k for k in mapping if k.startswith("msa/"))
    (work/old).write_text(">query\nAAAA\n")
    sha=digest(work/old);new="msa/"+sha+".a3m"
    (work/old).rename(work/new)
    source=mapping.pop(old);mapping[new]=source;receipt["artifacts"][source]=sha
    write_json(work/"artifact-map.json",mapping);write_json(work/"worker-receipt.json",receipt)
    bundle=archive_files(work,tmp_path/"tamper.tar.gz");q.update(path=bundle["path"],sha256=bundle["sha256"])
    with pytest.raises(ValueError,match="query"):import_results(p,q)


def test_web_profile_exposes_msa_host_and_accepts_multiple_chains(tmp_path):
    import yaml
    from antigen_design.web_store import Store
    settings={"execution":{"backend":"slurm","transport":"openssh","cpu_host":"primary","gpu_host":"primary","remote_root":"/primary",
        "msa_remote":{"enabled":True,"host":"msa-server","root":"/srv/antigen/msa-transfer/test","python":"/env/bin/python","database":"/db","script":"/converter","mmseqs":"/mmseqs","cache_root":"/cache","transfer_command":["python","wrapper"],"resources":{"cpus":32,"memory":"256G","partition":"compute"}}}}
    directory=tmp_path/"configs/servers";directory.mkdir(parents=True)
    (directory/"remote.yaml").write_text(yaml.safe_dump(settings))
    store=Store(tmp_path)
    assert store.profiles()[0]["msa_backend"]["host"]=="msa-server"
    request={"name":"test","mode":"folding","profile":"remote","input":">test\nACDE\n"}
    assert store.preview(request)["msa_host"]=="msa-server"
    request["input"]=json.dumps([{"name":"complex","sequences":[{"proteinChain":{"sequence":"ACDE","count":2}}]}])
    assert store.preview(request)["msa_host"]=="msa-server"


def test_publish_source_atomic_reuse_and_rejection(tmp_path):
    import hashlib, tarfile
    from antigen_design.msa_remote import publish_source
    source=tmp_path/"input/src/antigen_design";source.mkdir(parents=True)
    (source/"__init__.py").write_text("VALUE=1\n")
    hashes={"__init__.py":digest(source/"__init__.py")}
    signature=hashlib.sha256(json.dumps(hashes,sort_keys=True).encode()).hexdigest()
    archive=tmp_path/"source.tar"
    with tarfile.open(archive,"w") as stream:
        stream.add(source/"__init__.py",arcname="src/antigen_design/__init__.py")
    target=tmp_path/"release"
    with pytest.raises(ValueError,match="checksum"):
        publish_source(archive,target,"bad",signature)
    assert not (target/"src").exists()
    with pytest.raises(ValueError,match="signature"):
        publish_source(archive,target,digest(archive),"bad")
    assert not (target/"src").exists()
    publish_source(archive,target,digest(archive),signature)
    publish_source(archive,target,digest(archive),signature)
    assert (target/"src/antigen_design/__init__.py").read_text()=="VALUE=1\n"
    with pytest.raises(ValueError,match="immutable"):
        publish_source(archive,target,digest(archive),"different")


def test_publish_source_rejects_unsafe_member(tmp_path):
    import tarfile,io
    from antigen_design.msa_remote import publish_source
    archive=tmp_path/"source.tar"
    with tarfile.open(archive,"w") as stream:
        member=tarfile.TarInfo("../escape.py");member.size=1
        stream.addfile(member,io.BytesIO(b"x"))
    with pytest.raises(ValueError,match="only package"):
        publish_source(archive,tmp_path/"release",digest(archive),"bad")
    assert not (tmp_path/"escape.py").exists()


def test_return_phases_are_durable(tmp_path,monkeypatch):
    from antigen_design import msa_remote,remote
    p,q,_=fixture(tmp_path);p["local_output"]=str(tmp_path)
    state={"route":q["route"],"host":"msa-server","job_id":"123","attempts":1,"state":"running"}
    write_json(tmp_path/"manifest.json",{"tasks":{"msa":state,"fold":{"state":"pending"}}})
    observed=[]
    def control(*args):
        observed.append(read_json(tmp_path/"manifest.json")["tasks"]["msa"]["transfer_phase"])
        return {"directory":"/export","sha256":"hash"}
    monkeypatch.setattr(remote,"control_raw",control)
    monkeypatch.setattr(msa_remote,"sync",lambda *a,**k: observed.append(read_json(tmp_path/"manifest.json")["tasks"]["msa"]["transfer_phase"]))
    assert msa_remote.returned(p,state,"msa",None)
    assert observed==["exporting","returning","validating"]
    saved=read_json(tmp_path/"manifest.json")
    assert saved["tasks"]["msa"]["transfer_phase"]=="verified"
    assert saved["tasks"]["fold"]["state"]=="pending"


def test_pool_request_completes_without_waiting_for_shared_allocation(tmp_path,monkeypatch):
    from antigen_design import remote
    p,q,_=fixture(tmp_path);p['local_output']=str(tmp_path)
    state={'state':'running','host':'msa-server','stage':'msa','route':q['route'],'job_id':'123','attempts':1}
    m={'tasks':{'msa':state}}
    monkeypatch.setattr(remote,'control',lambda *a,**kw:{'msa':{'slurm_state':'RUNNING','execution_state':'COMPLETED','valid':True,'pool_state':'succeeded','pool_phase':'ready'}})
    remote.refresh(p,m,None)
    assert state['state']=='succeeded' and state['slurm_state']=='RUNNING'
    assert state['pool_state']=='succeeded'


def test_pool_cancelled_request_ends_while_shared_allocation_runs(tmp_path,monkeypatch):
    from antigen_design import remote
    p,q,_=fixture(tmp_path);p['local_output']=str(tmp_path)
    state={'state':'running','host':'msa-server','stage':'msa','route':q['route'],'job_id':'123','attempts':1}
    m={'tasks':{'msa':state}}
    monkeypatch.setattr(remote,'control',lambda *a,**kw:{'msa':{'slurm_state':'RUNNING','execution_state':'CANCELLED','pool_state':'cancelled'}})
    remote.refresh(p,m,None)
    assert state['state']=='failed' and state['slurm_state']=='RUNNING'


@pytest.mark.parametrize("kind",["staging","lifetime","walltime","boolean"])
def test_pool_configuration_rejects_unsafe_settings(kind):
    from antigen_design.msa_remote import validate
    c=deepcopy(DEFAULTS)
    s=c['execution']['msa_remote'];s.update(enabled=True,resources={'time':'04:00:00'})
    s['pool'].update(enabled=True)
    c['msa']['optimization'].update(enabled=True,staging='indices')
    if kind=='staging':c['msa']['optimization']['staging']='none'
    if kind=='lifetime':s['pool']['request_timeout_seconds']=s['pool']['max_seconds']
    if kind=='walltime':s['resources']['time']='00:30:00'
    if kind=='boolean':s['pool']['enabled']='true'
    with pytest.raises(ValueError,match='[Pp]ool'):validate(c)


def test_pool_performance_records_survive_verified_return(tmp_path):
    p,q,_=fixture(tmp_path,performance=True)
    assert import_results(p,q)['valid']
    dest=Path(p['config']['output'])/'tasks/msa'
    for name in ('msa_staging.json','msa_execution.json'):
        assert read_json(dest/name)['pool_request_id']=='independent-request'
    assert receipt_valid(dest,'task')


def test_homomer_and_rna_return_preserve_input(tmp_path):
    p,q,_=fixture(tmp_path,homomer=True)
    assert import_results(p,q)['valid']
    payload=read_json(Path(p['config']['output'])/'tasks/msa/fold_inputs/query_1.json')[0]
    assert payload['sequences'][0]['proteinChain']['id']==['A','B']
    assert payload['sequences'][0]['proteinChain']['count']==2
    assert payload['sequences'][1]==p['fold_records'][0]['job']['sequences'][1]


def test_capsule_uses_frozen_remote_environment(tmp_path):
    p,q,_=fixture(tmp_path)
    p['environment']['msa_remote']={'code':'code','errors':[],'site':'remote'}
    route=dict(q['route'],id='b'*32,root=str(tmp_path/'second-remote'))
    child=pack(p,{'task':'msa','route':route})['plan']
    assert child['environment']['msa']==p['environment']['msa_remote']
    assert p['environment']['msa']=={'code':'code','errors':[]}
