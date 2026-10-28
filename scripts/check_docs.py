"""Validate local Markdown links, example configuration, and benchmark provenance."""
import hashlib
import json
from pathlib import Path
import re
import sys
import yaml
from check_public_tree import public_files, ROOT
from antigen_design.config import load_config, build_plan, merge, DEFAULTS
from antigen_design.deployment import settings
from antigen_design.gpu_routing import validate_routing


def main():
    checked = 0
    for relative in public_files():
        if relative.suffix != '.md':
            continue
        path = ROOT / relative
        for match in re.finditer(r'\[[^\]]*\]\(([^\s)]+)(?:\s+[^)]*)?\)', path.read_text()):
            link = match[1].strip('<>').split('#', 1)[0]
            if not link or re.match(r'\w+://|mailto:', link):
                continue
            target = path.parent / link
            if not target.exists():
                raise ValueError(f'{relative}: missing link {link}')
            checked += 1
    for name in ('ubiquitin', 'ubiquitin-p3', 'ubiquitin-matrix'):
        build_plan(load_config(ROOT / 'projects/1ubq/configs' / (name + '.yaml')))
    for path in (ROOT / 'configs/servers').glob('*template.yaml'):
        merge(DEFAULTS, yaml.safe_load(path.read_text()))
    settings(ROOT, ROOT / 'configs/webapp.example.yaml')
    validate_routing(yaml.safe_load((ROOT / 'configs/gpu-targets.example.yaml').read_text()))
    provenance = json.loads((ROOT / 'projects/1ubq/inputs/1ubq.provenance.json').read_text())
    digest = hashlib.sha256((ROOT / 'projects/1ubq/inputs/1ubq.cif').read_bytes()).hexdigest()
    if digest != provenance['sha256']:
        raise ValueError('1UBQ input checksum mismatch')
    print(f'Documentation checks passed: {checked} local links, example schemas and 1UBQ checksum.')
    return 0


if __name__ == '__main__':
    sys.exit(main())
