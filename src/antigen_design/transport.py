"""Operator-configured OpenSSH transport with an optional legacy bridge."""
import json
import os
from pathlib import Path
import shlex
import subprocess
import sys
import re
import uuid


def valid_host(host):
    if not isinstance(host, str) or not re.fullmatch(r"[A-Za-z0-9_.@-]+", host) or host.startswith("-"):
        raise ValueError("Invalid SSH host alias")
    return host


class OpenSSH:
    """Configured OpenSSH; WSL may use Windows ssh.exe/scp.exe and its existing agent."""
    def __init__(self, config):
        self.ssh = config["execution"]["ssh_binary"]
        self.scp = config["execution"]["scp_binary"]
        self.native = {target['host']: target for target in
                       config['execution'].get('gpu_routing', {}).get('targets', [])
                       if target.get('backend') == 'baremetal'}

    def execute(self, host, argv, timeout=60):
        return self.shell(host, shlex.join(map(str, argv)), timeout)

    def shell(self, host, command, timeout=60):
        target = self.native.get(host)
        if target and target['native'].get('destination'):
            command = shlex.join(['ssh', '-o', 'BatchMode=yes', '-o', 'ConnectTimeout=15',
                                 '-o', 'StrictHostKeyChecking=yes', target['native']['destination'],
                                 'bash -lc ' + shlex.quote(command)])
            host = target['native']['gateway']
        argv = [self.ssh, "-o", "BatchMode=yes", "-o", "ConnectTimeout=15", valid_host(host),
                "bash -lc " + shlex.quote(command)]
        result = subprocess.run(argv, capture_output=True, text=True, timeout=timeout, check=True)
        return result.stdout.strip()

    def local_path(self, path):
        path = str(Path(path).resolve())
        if self.scp.lower().endswith(".exe") and sys.platform != "win32":
            path = subprocess.check_output(["wslpath", "-w", path], text=True).strip()
        return path

    def copy(self, source, target):
        subprocess.run([self.scp, "-o", "BatchMode=yes", "-o", "ConnectTimeout=15", str(source), str(target)],
                       capture_output=True, text=True, check=True, timeout=300)

    def remote_path(self, host, path):
        # Restrict transfer paths; no remote shell metacharacters or ambiguous colon syntax.
        if not re.fullmatch(r"/[A-Za-z0-9_./-]+", str(path)):
            raise ValueError("Remote transfer path must be absolute and contain no shell metacharacters")
        return valid_host(host) + ":" + str(path)

    def upload(self, host, source, target):
        if host in self.native and self.native[host]['native'].get('destination'):
            site = self.native[host]
            gateway, destination = site['native']['gateway'], site['native']['destination']
            relay = site['root'] + '/relay/' + uuid.uuid4().hex
            self.execute(gateway, ['mkdir', '-p', str(Path(relay).parent)])
            self.copy(self.local_path(source), self.remote_path(gateway, relay))
            self.execute(gateway, ['scp', '-o', 'BatchMode=yes', '-o', 'StrictHostKeyChecking=yes',
                                  relay, self.remote_path(destination, target)], timeout=300)
            self.execute(gateway, ['rm', '--', relay])
            return
        self.copy(self.local_path(source), self.remote_path(host, target))

    def download(self, host, source, target):
        Path(target).parent.mkdir(parents=True, exist_ok=True)
        if host in self.native and self.native[host]['native'].get('destination'):
            site = self.native[host]
            gateway, destination = site['native']['gateway'], site['native']['destination']
            relay = site['root'] + '/relay/' + uuid.uuid4().hex
            self.execute(gateway, ['mkdir', '-p', str(Path(relay).parent)])
            self.execute(gateway, ['scp', '-o', 'BatchMode=yes', '-o', 'StrictHostKeyChecking=yes',
                                  self.remote_path(destination, source), relay], timeout=300)
            self.copy(self.remote_path(gateway, relay), self.local_path(target))
            self.execute(gateway, ['rm', '--', relay])
            return
        self.copy(self.remote_path(host, source), self.local_path(target))

    def transfer_native(self, host, path, native_host, native_path, returning=False):
        """Transfer between explicitly configured primary and native GPU hosts."""
        site = self.native[native_host]
        if host not in site['native']['transfer_hosts']:
            raise ValueError('Native direct transfer host is not explicitly configured')
        gateway, destination = site['native']['gateway'], site['native'].get('destination')
        options = ['scp', '-o', 'BatchMode=yes', '-o', 'ConnectTimeout=15',
                   '-o', 'StrictHostKeyChecking=yes']
        relay = site['root'] + '/relay/' + uuid.uuid4().hex if destination else native_path
        self.execute(gateway, ['mkdir', '-p', str(Path(relay).parent)])
        if returning:
            if destination:
                self.execute(gateway, [*options, self.remote_path(destination, native_path), relay], timeout=300)
            self.execute(host, ['mkdir', '-p', str(Path(path).parent)])
            self.execute(host, [*options, self.remote_path(gateway, relay), path], timeout=300)
        else:
            self.execute(host, [*options, path, self.remote_path(gateway, relay)], timeout=300)
            if destination:
                self.execute(gateway, [*options, relay, self.remote_path(destination, native_path)], timeout=300)
        if destination:
            self.execute(gateway, ['rm', '--', relay])


def get_transport(config):
    return OpenSSH(config) if config["execution"]["transport"] == "openssh" else SSHSkill(config)


class SSHSkill:
    def __init__(self, config):
        self.root = Path(config["execution"]["ssh_skill"])
        if not (self.root / "scripts" / "ssh_execute.py").is_file():
            raise ValueError(f"ssh-skill scripts not found: {self.root}")

    def invoke(self, script, args, timeout=90):
        env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1", SSH_SKILL_NO_AUTO_INSTALL="1")
        result = subprocess.run([sys.executable, str(self.root / "scripts" / script), *map(str, args)],
                                capture_output=True, text=True, timeout=timeout, env=env)
        try:
            data = json.loads(result.stdout)
        except json.JSONDecodeError as exc:
            raise RuntimeError(f"ssh-skill returned invalid JSON: {result.stdout[-1000:]} {result.stderr[-1000:]}") from exc
        if result.returncode or not data.get("success"):
            raise RuntimeError(f"ssh-skill {script}: {data.get('stderr') or data.get('error') or result.stderr}")
        return data

    def execute(self, host, argv, timeout=60):
        return self.shell(host, shlex.join(map(str, argv)), timeout)

    def shell(self, host, command, timeout=60):
        return self.invoke("ssh_execute.py", [host, command, "--timeout", str(timeout), "--no-daemon"], timeout + 15)["stdout"].strip()

    def upload(self, host, source, target):
        return self.invoke("ssh_upload.py", [host, source, target, "--no-progress"], 300)

    def download(self, host, source, target):
        Path(target).parent.mkdir(parents=True, exist_ok=True)
        return self.invoke("ssh_download.py", [host, source, target, "--no-progress"], 300)
