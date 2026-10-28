"""Portable MSA capsules: validate lineage, queries, hashes before atomic import."""
from copy import deepcopy
from pathlib import Path
import shutil
from .io import digest, fingerprint, read_json, write_json
from .gpu_transfer import archive_files, extract


def records(plan, task):
    from .adapters import sequence_record
    values = [sequence_record(plan, m) for m in task.get("members", [task])]
    if len({r["id"] for r in values}) != len(values):
        raise ValueError("Duplicate candidate IDs")
    return values


def pack(plan, request):
    from .worker import receipt_valid
    from .msa_remote import target_config
    name, route = request["task"], request["route"]
    task = plan["tasks"][name]
    if task["stage"] != "msa":
        raise ValueError("Only MSA tasks may use this backend")
    root = Path(plan["config"]["output"])
    for dep in task["deps"]:
        if not receipt_valid(root/"tasks"/dep, plan["tasks"][dep]["signature"]):
            raise ValueError("MSA upstream receipt invalid")
    values = records(plan, task)
    base = root/"transfers"/route["id"]
    receipt = base/"input-transfer.json"
    if receipt.exists():
        result = read_json(receipt)
        if digest(Path(result["directory"])/"input.tar.gz") != result["sha256"]:
            raise ValueError("Saved input capsule corrupted")
        return result
    directory = base/"input-build"
    directory.mkdir(parents=True, exist_ok=True)
    c = target_config(plan["config"])
    c.update(mode="folding", output=route["root"], input=str(Path(route["root"])/"input.json"))
    write_json(directory/"input.json", values)
    capsule = {"mode":"folding", "config":c, "fold_records":values, "code_signature":plan["code_signature"],
        "input_sha256":digest(directory/"input.json"), "environment":{"msa":plan["environment"].get("msa_remote",plan["environment"]["msa"])},
        "msa_capsule":{"route_id":route["id"],"origin_identity":plan["identity"],"origin_task_signature":task["signature"]}}
    capsule["identity"] = fingerprint(capsule)
    child = deepcopy(task)
    child.update(deps=[], host=c["execution"]["cpu_host"], members=[{"record":i} for i in range(len(values))])
    child["signature"] = fingerprint({"task":child,"capsule":capsule["identity"]})
    capsule["tasks"] = {name:child}
    write_json(directory/"plan.json",capsule)
    out = base/"outbox"
    out.mkdir(exist_ok=True)
    result = archive_files(directory,out/"input.tar.gz") | {"plan":capsule,"directory":str(out)}
    write_json(receipt,result)
    return result


def results(plan, request):
    from .worker import receipt_valid
    name = request["task"]
    root = Path(plan["config"]["output"])
    source = root/"tasks"/name
    if not receipt_valid(source,plan["tasks"][name]["signature"]):
        raise ValueError("MSA worker receipt invalid")
    base = root/f"return-a{request['attempt']}"
    saved = base/"export.json"
    if saved.exists():
        result = read_json(saved)
        if digest(result["path"]) != result["sha256"]:
            raise ValueError("Saved export corrupted")
        return result
    directory = base/"build"
    directory.mkdir(parents=True,exist_ok=True)
    receipt = read_json(source/"complete.json")
    mapping = {}
    def copy(path, relative):
        path = Path(path)
        if receipt["artifacts"].get(str(path)) != digest(path):
            raise ValueError("Unreceipted MSA artifact")
        target = directory/relative
        target.parent.mkdir(parents=True,exist_ok=True)
        shutil.copyfile(path,target)
        mapping[relative] = str(path)
    for filename in ("runtime.json","msa_batch.json"):
        copy(source/filename,filename)
    for filename in ("msa_staging.json", "msa_execution.json"):
        if (source/filename).exists():
            copy(source/filename,filename)
    for record in plan["fold_records"]:
        f = source/"fold_inputs"/(record["id"]+".json")
        copy(f,"original-inputs/"+f.name)
        for job in read_json(f):
            for entity in job["sequences"]:
                protein = entity.get("proteinChain",{})
                for key in ("pairedMsaPath","unpairedMsaPath"):
                    if key in protein:
                        path = Path(protein[key])
                        copy(path,"msa/"+digest(path)+".a3m")
    write_json(directory/"capsule-plan.json",plan)
    write_json(directory/"worker-receipt.json",receipt)
    write_json(directory/"artifact-map.json",mapping)
    out = base/"outbox"
    out.mkdir(exist_ok=True)
    result = archive_files(directory,out/"result.tar.gz") | {"directory":str(out)}
    write_json(saved,result)
    return result


def import_results(plan, request):
    from .worker import receipt_valid
    from .adapters import a3m_query
    name,route = request["task"],request["route"]
    root=Path(plan["config"]["output"])
    destination=root/"tasks"/name
    if destination.exists():
        receipt=read_json(destination/"complete.json")
        if receipt.get("route_id")==route["id"] and receipt.get("job_id")==str(request["job_id"]) and receipt_valid(destination,plan["tasks"][name]["signature"]):
            return {"valid":True}
        raise ValueError("Refusing to overwrite a different MSA result")
    work=root/"transfers"/route["id"]/f"import-{request['job_id']}"
    extract(request["path"],work,request["sha256"])
    capsule=read_json(work/"capsule-plan.json")
    if capsule!=route["plan"]:
        raise ValueError("MSA capsule plan mismatch")
    proof=capsule["msa_capsule"]
    if proof!={"route_id":route["id"],"origin_identity":plan["identity"],"origin_task_signature":plan["tasks"][name]["signature"]}:
        raise ValueError("MSA lineage mismatch")
    receipt=read_json(work/"worker-receipt.json")
    if receipt["signature"]!=capsule["tasks"][name]["signature"] or receipt["environment"]!=capsule["environment"]["msa"]:
        raise ValueError("MSA environment/signature mismatch")
    mapping=read_json(work/"artifact-map.json")
    for rel,source in mapping.items():
        path=work/rel
        if not path.resolve().is_relative_to(work.resolve()) or path.is_symlink() or digest(path)!=receipt["artifacts"].get(source):
            raise ValueError("MSA artifact checksum mismatch")
    actual={str(p.relative_to(work)) for p in work.rglob("*") if p.is_file()}
    if actual!=set(mapping)|{"capsule-plan.json","worker-receipt.json","artifact-map.json"}:
        raise ValueError("Unreceipted returned files")
    if not {"runtime.json","msa_batch.json"}.issubset(mapping):
        raise ValueError("Missing MSA runtime/summary")
    if str(read_json(work/"runtime.json")["slurm_job_id"])!=str(request["job_id"]):
        raise ValueError("MSA Slurm job mismatch")
    expected=records(plan,plan["tasks"][name])
    if expected!=capsule["fold_records"]:
        raise ValueError("MSA candidate provenance mismatch")
    publish=root/"transfers"/route["id"]/f"publish-{request['job_id']}"
    if publish.exists():
        shutil.rmtree(publish)  # only this attempt's private, unpublished directory
    publish.mkdir()
    for rel in mapping:
        if rel.startswith("msa/"):
            target=publish/rel
            target.parent.mkdir(parents=True,exist_ok=True)
            shutil.copyfile(work/rel,target)
    original_task=plan["tasks"][name]
    for record in expected:
        rel="original-inputs/"+record["id"]+".json"
        if rel not in mapping:
            raise ValueError("Missing candidate result")
        payload=read_json(work/rel)
        if len(payload)!=1 or payload[0]["name"]!=record["id"]:
            raise ValueError("MSA candidate mismatch")
        if 'job' in record:
            entities=deepcopy(payload[0]['sequences'])
            for entity in entities:
                if 'proteinChain' in entity:
                    entity['proteinChain'].pop('pairedMsaPath',None);entity['proteinChain'].pop('unpairedMsaPath',None)
            if entities!=record['job']['sequences'] or payload[0].get('covalent_bonds',[])!=record['job'].get('covalent_bonds',[]):
                raise ValueError('MSA returned molecular input changed')
        seen={}
        for entity in payload[0]["sequences"]:
            protein=entity.get("proteinChain")
            if not protein:
                continue
            ids=protein["id"]
            if not ids or len(ids)!=len(set(ids)) or any(i in seen or protein['sequence']!=record['chains'].get(i) for i in ids):
                raise ValueError("MSA chain mapping mismatch")
            seen.update({i:protein['sequence'] for i in ids})
            for key in ("pairedMsaPath","unpairedMsaPath"):
                source=protein[key]
                sha=receipt["artifacts"].get(source)
                rel="msa/"+str(sha)+".a3m"
                if rel not in mapping or a3m_query(work/rel)!=protein["sequence"]:
                    raise ValueError("MSA query/receipt mismatch")
                protein[key]=str(destination/rel)
        if seen!=record["chains"]:
            raise ValueError("Missing MSA chain")
        output=publish/"fold_inputs"/(record["id"]+".json") if "members" in original_task else publish/"fold_input.json"
        write_json(output,payload)
    for filename in ("capsule-plan.json","worker-receipt.json","runtime.json","msa_batch.json"):
        shutil.copyfile(work/filename,publish/filename)
    for filename in ("msa_staging.json", "msa_execution.json"):
        if (work/filename).exists():
            shutil.copyfile(work/filename,publish/filename)
    artifacts={str(destination/p.relative_to(publish)):digest(p) for p in publish.rglob("*") if p.is_file()}
    write_json(publish/"msa_artifacts.json",{k:v for k,v in artifacts.items() if k.endswith(".a3m")})
    artifacts[str(destination/"msa_artifacts.json")]=digest(publish/"msa_artifacts.json")
    write_json(publish/"complete.json",{"signature":original_task["signature"],"environment":plan["environment"]["msa"],
        "artifacts":artifacts,"route_id":route["id"],"job_id":str(request["job_id"]),"transfer_sha256":request["sha256"]})
    destination.parent.mkdir(parents=True,exist_ok=True)
    publish.rename(destination)
    return {"valid":True}
