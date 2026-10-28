from pathlib import Path
import importlib.util
import pytest
from antigen_design.config import load_config, build_plan
from antigen_design.io import digest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('archive_verifier', ROOT / 'scripts/verify_acceptance.py')
verifier = importlib.util.module_from_spec(spec)
spec.loader.exec_module(verifier)


@pytest.mark.parametrize('name', ['ubiquitin', 'ubiquitin-p3', 'ubiquitin-matrix'])
def test_relocated_config_paths(name):
    project = ROOT / 'projects/1ubq'
    config = load_config(project / 'configs' / f'{name}.yaml')
    assert Path(config['input']) == project / 'inputs/1ubq.cif'
    assert Path(config['output']).is_relative_to(project / 'runs')
    assert build_plan(config)['expected_predictions'] == (10 if name == 'ubiquitin-matrix' else 1)


def test_archive_reference_override_is_hash_checked(tmp_path):
    original = ROOT / 'projects/1ubq/inputs/1ubq.cif'
    plan = {'local_input': '/old/unavailable/1ubq.cif', 'input_sha256': digest(original)}
    assert verifier.reference_path(plan, original) == original
    wrong = tmp_path / 'wrong.cif'
    wrong.write_text('not the archived input')
    with pytest.raises(ValueError, match='checksum'):
        verifier.reference_path(plan, wrong)
