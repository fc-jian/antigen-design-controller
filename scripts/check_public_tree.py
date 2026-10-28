"""Check publishable Git files; never read ignored user projects or environments."""
import argparse
import ipaddress
from pathlib import Path
import re
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
RULES = {
    'private key': re.compile(r'-----BEGIN (?:OPENSSH|RSA|EC|DSA|PGP)? ?PRIVATE KEY-----'),
    'provider token': re.compile(r'\b(?:gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{40,}|AKIA[A-Z0-9]{16}|sk-[A-Za-z0-9]{32,})\b'),
    'credential URL': re.compile(r'https?://[^\s/:]+:[^\s/@]+@'),
    'personal home path': re.compile(r'(?:/home/|/Users/|[A-Za-z]:[\\/]+Users[\\/]+)[A-Za-z0-9_.-]+'),
    'private shared-storage path': re.compile(r'/(?:flash|lustre)/grp/[^\s\"\']+'),
}
IP = re.compile(r'(?<![\w.])(?:\d{1,3}\.){3}\d{1,3}(?![\w.])')
DOCUMENTATION_NETS = [ipaddress.ip_network(s) for s in ('192.0.2.0/24', '198.51.100.0/24', '203.0.113.0/24')]


def public_files(root=ROOT):
    raw = subprocess.check_output(['git', 'ls-files', '--cached', '--others', '--exclude-standard', '-z'], cwd=root)
    return sorted({Path(p.decode()) for p in raw.split(b'\0') if p})


def check(files, deny=(), root=ROOT):
    errors = []
    for relative in files:
        path = root / relative
        name = relative.as_posix()
        if path.is_symlink() or not path.resolve().is_relative_to(root.resolve()):
            errors.append(f'{name}: external or symbolic file')
            continue
        if not path.is_file():
            continue  # Deleted tracked files are handled by Git.
        if name.startswith('projects/') and not (name == 'projects/README.md' or name.startswith('projects/1ubq/')):
            errors.append(f'{name}: private project')
        if re.search(r'(?:^|/)(?:runs|evidence|\.venv|__pycache__|node_modules)/', name):
            errors.append(f'{name}: generated/private directory')
        if '.local.' in path.name or name in ('configs/webapp.yaml', 'configs/gpu-targets.yaml'):
            errors.append(f'{name}: private deployment configuration')
        if path.suffix in ('.pt', '.pth', '.ckpt', '.safetensors', '.pem', '.key'):
            errors.append(f'{name}: model or credential file')
        try:
            content = path.read_text(encoding='utf-8')
        except UnicodeError:
            errors.append(f'{name}: unreviewed binary file')
            continue
        for number, line in enumerate(content.splitlines(), 1):
            for label, expression in RULES.items():
                if expression.search(line):
                    errors.append(f'{name}:{number}: {label}')
            if any(word and word.casefold() in line.casefold() for word in deny):
                errors.append(f'{name}:{number}: operator-supplied private identifier')
            # Upstream viewer may contain numeric codec constants; still scan it for credentials above.
            if name.endswith('/vendor/3Dmol-min.js'):
                continue
            for match in IP.finditer(line):
                try:
                    address = ipaddress.ip_address(match[0])
                except ValueError:
                    continue
                if not (address.is_loopback or address.is_unspecified or any(address in net for net in DOCUMENTATION_NETS)):
                    errors.append(f'{name}:{number}: non-documentation IP address')
    return sorted(set(errors))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--deny', action='append', default=[], help='Additional private identifier; may be repeated')
    args = parser.parse_args()
    files = public_files()
    errors = check(files, args.deny)
    if errors:
        print('\n'.join(errors), file=sys.stderr)
        return 1
    print(f'Public-tree scan passed: {len(files)} candidate files; ignored runtime data excluded.')
    return 0


if __name__ == '__main__':
    sys.exit(main())
