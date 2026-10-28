"""Design capsule and scheduler doubles; no model inference is performed here."""
from copy import deepcopy
from pathlib import Path
import shutil
import pytest
from antigen_design.config import build_plan
from antigen_design.pipeline import task_graph
from antigen_design.structure import Protein
from antigen_design.io import write_json, read_json, digest
from antigen_design.contracts import candidate_record
from antigen_design.gpu_transfer import pack, extract, results, import_results
from antigen_design.worker import receipt_valid


def setup_design(config, stage, with_backbone=False):
    config['rfd3'].update(enabled=stage == 'rfd3' or with_backbone, samples=1)
    config['mpnn']['sequences'] = 1
    plan = build_plan(config)
    plan.update(identity='original-run', environment={})
    plan['tasks'] = task_graph(plan)
    root = Path(config['output'])
    root.mkdir()
    Protein(config['input']).normalized(root / 'reference.pdb')
    name = stage + '_b0000'
    if with_backbone:
        dep = root / 'tasks/rfd3_b0000'
        dep.mkdir(parents=True)
        shutil.copyfile(root / 'reference.pdb', dep / 'backbone.pdb')
        write_json(dep / 'complete.json', {'signature': plan['tasks']['rfd3_b0000']['signature'],
                   'artifacts': {str(dep / 'backbone.pdb'): digest(dep / 'backbone.pdb')}})
    route = {'id': 'design-route', 'root': str(root.parent / 'capsule'),
             'source_dir': '/release/src', 'resources': config['execution']['resources']['gpu'],
             'environment': {'stage': stage}, 'portable': {'checkpoint': 'exact'},
             'target': {'host': 'target', 'root': '/owned', 'design': {'python': '/env/python', 'weights': '/weights'}}}
    payload = pack(plan, {'task': name, 'route': route})
    route['plan'] = payload['plan']
    extract(payload['path'], route['root'], payload['sha256'])
    return plan, name, route


@pytest.mark.parametrize('stage,with_backbone', [('rfd3', False), ('mpnn', False), ('mpnn', True)])
def test_design_capsule_roundtrip_preserves_maps_parameters_and_provenance(config, stage, with_backbone):
    plan, name, route = setup_design(config, stage, with_backbone)
    capsule = route['plan']
    assert capsule['backbones'] == plan['backbones']
    for key in ('rfd3', 'mpnn', 'folding', 'seed'):
        assert capsule['config'][key] == plan['config'][key]
    assert capsule['tasks'][name]['deps'] == []
    assert not (Path(route['root']) / 'fold_input.json').exists()
    if with_backbone:
        assert (Path(route['root']) / 'tasks/rfd3_b0000/backbone.pdb').is_file()
    task = Path(route['root']) / 'tasks' / name
    task.mkdir(parents=True)
    if stage == 'rfd3':
        shutil.copyfile(config['input'], task / 'generated.cif')
        shutil.copyfile(Path(config['output']) / 'reference.pdb', task / 'backbone.pdb')
        write_json(task / 'mapping.json', plan['backbones'][0]['mapping'])
        write_json(task / 'rfd3_engine.json', {'trainer_precision': '32-true'})
    else:
        shutil.copyfile(config['input'], task / 's0000.cif')
        chains = {'A': ''.join(r.amino_acid for r in Protein(config['input']).records)}
        b = plan['backbones'][0]
        record = candidate_record(capsule, b['id'], 0, chains, b['seed'])
        assert record == candidate_record(plan, b['id'], 0, chains, b['seed'])
        write_json(task / 'sequences.json', [record])
        write_json(task / 'mpnn_input.json', {})
    write_json(task / 'runtime.json', {'slurm_job_id': '456'})
    write_json(task / 'complete.json', {'signature': capsule['tasks'][name]['signature'],
        'environment': capsule['environment'][stage], 'artifacts': {str(p): digest(p) for p in task.iterdir()}})
    payload = results(capsule, {'task': name, 'attempt': 1})
    assert import_results(plan, dict(payload, task=name, route=route, job_id='456'))['valid']
    assert receipt_valid(Path(config['output']) / 'tasks' / name, plan['tasks'][name]['signature'])


def test_design_backbone_transfer_requires_valid_receipt(config):
    plan, name, route = setup_design(config, 'mpnn', True)
    (Path(config['output']) / 'tasks/rfd3_b0000/backbone.pdb').write_text('changed')
    route['id'] = 'another'
    with pytest.raises(ValueError, match='Upstream receipt'):
        pack(plan, {'task': name, 'route': route})


def test_reject_foreign_candidate_even_with_valid_archive(config):
    from antigen_design.gpu_transfer import validate_design_result
    plan, name, route = setup_design(config, 'mpnn')
    task = Path(route['root']) / 'fake'
    task.mkdir()
    shutil.copyfile(config['input'], task / 's0000.cif')
    chains = {'A': ''.join(r.amino_acid for r in Protein(config['input']).records)}
    b = plan['backbones'][0]
    record = candidate_record(dict(plan, identity='unrelated-run'), b['id'], 0, chains, b['seed'])
    write_json(task / 'sequences.json', [record])
    with pytest.raises(ValueError, match='provenance'):
        validate_design_result(plan, name, task)


@pytest.mark.parametrize('stage', ['rfd3', 'mpnn'])
def test_design_reroute_waits_for_confirmed_cancellation(stage, tmp_path, monkeypatch):
    from antigen_design import gpu_routing, remote
    plan = {'local_output': str(tmp_path), 'config': {'execution': {'gpu_routing': {'queue_grace_seconds': 60}}}}
    state = dict(stage=stage, state='submitted', slurm_state='PENDING', submitted_at=0,
                 host='old', job_id='123', attempts=1, memory_tier=0, history=[])
    manifest = {'tasks': {'task': state}}
    def choose(*args, **kwargs):
        assert kwargs['stage_name'] == stage
        return {}, {}
    monkeypatch.setattr(gpu_routing, 'choose', choose)
    monkeypatch.setattr(remote, 'control', lambda *args: None)
    gpu_routing.reroute_pending(plan, manifest, None)
    assert state['state'] == 'submitted'
    assert not gpu_routing.finish_cancel(state, 'UNKNOWN')
    assert gpu_routing.finish_cancel(state, 'CANCELLED')
    assert state['stage'] == stage and state['history'][0]['job_id'] == '123'


def test_design_skips_fold_only_target_and_checks_matching_stage(monkeypatch):
    from antigen_design import gpu_routing
    resources = {'gres': 'gpu:1', 'cpus': 4, 'memory': '32G'}
    targets = [{'id': 'yt', 'host': 'yt', 'resources': resources},
               {'id': 'primary', 'host': 'primary', 'design': {}, 'resources': resources}]
    plan = {'config': {'execution': {'gpu_routing': {'targets': targets},
                                   'resources': {'gpu': resources}}}}
    manifest = {'gpu_routing': {}, 'tasks': {}}
    observed = [{'id': 'yt', 'free_gpus': 8, 'nodes': [{'gpus': 8, 'cpus': 32, 'memory_mb': 262144}]},
                {'id': 'primary', 'free_gpus': 1, 'nodes': [{'gpus': 1, 'cpus': 4, 'memory_mb': 32768}]}]
    monkeypatch.setattr(gpu_routing, 'snapshots', lambda *args, **kwargs: observed)
    called = []
    monkeypatch.setattr(gpu_routing, 'ready', lambda p,t,tr,s: called.append((t['id'],s)) or {})
    assert gpu_routing.choose(plan, manifest, None, stage_name='mpnn')[0]['id'] == 'primary'
    assert called == [('primary', 'mpnn')]
    assert observed[0]['compatible'] is False
