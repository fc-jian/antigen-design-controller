"""Slurm routing without cross-cluster scheduler dependencies."""
import re
import shlex
import subprocess


def command(argv, host=None, timeout=30):
    if host:
        if not re.fullmatch(r"[A-Za-z0-9_.@-]+", host) or host.startswith("-"):
            raise ValueError("Invalid SSH host")
        argv = ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=12", host, shlex.join(map(str, argv))]
    return subprocess.run(argv, check=True, capture_output=True, text=True, timeout=timeout).stdout.strip()


def slurm_script(argv, workdir, log, resources, job_name):
    allowed = {"cpus": "cpus-per-task", "memory": "mem", "time": "time", "gres": "gres",
               "partition": "partition", "account": "account", "constraint": "constraint", "qos": "qos", "nodelist": "nodelist"}
    lines = ["#!/bin/bash", f"#SBATCH --job-name={job_name}", f"#SBATCH --output={log}"]
    for key, value in resources.items():
        if value is None:
            if key not in allowed:
                raise ValueError(f"Unsupported Slurm option {key}")
            continue
        if key not in allowed or not re.fullmatch(r"[A-Za-z0-9_.,:+%/=-]+", str(value)):
            raise ValueError(f"Unsupported or unsafe Slurm option {key}={value}")
        lines.append(f"#SBATCH --{allowed[key]}={value}")
    if any(ch.isspace() for ch in str(log)) or "\n" in job_name:
        raise ValueError("Slurm log paths must not contain whitespace")
    lines += ["set -euo pipefail", f"cd {shlex.quote(str(workdir))}", "exec " + shlex.join(map(str, argv))]
    return "\n".join(lines) + "\n"


class Slurm:
    def submit(self, task, script):
        output = command(["sbatch", "--parsable", str(script)], task["host"])
        job = output.split(";")[0].strip()
        if not re.fullmatch(r"\d+", job):
            raise RuntimeError(f"Unexpected sbatch response: {output}")
        return job

    def state(self, task):
        result = command(["sacct", "-n", "-P", "-X", "-j", task["job_id"], "--format=JobIDRaw,State,ExitCode"], task["host"])
        for row in result.splitlines():
            fields = row.split("|")
            if fields[0] == task["job_id"]:
                state = fields[1].split()[0].rstrip("+")
                if state == "COMPLETED" and fields[2] != "0:0":
                    return "FAILED"
                return state
        # Accounting may lag; absence is not evidence of successful completion.
        return "UNKNOWN"
