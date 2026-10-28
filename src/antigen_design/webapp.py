"""Single-user loopback HTTP application. Run under Linux/WSL."""
import argparse
from contextlib import nullcontext
import fcntl
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import mimetypes
from pathlib import Path
import subprocess
import sys
import threading
from urllib.parse import urlsplit, parse_qs
from .io import read_json, write_json
from .web_store import Store, identifier, now

STATIC = Path(__file__).parent / "web"


class Application:
    def __init__(self, store, launch=True, read_only=False):
        self.store = store
        self.read_only = read_only
        launch = launch and not read_only
        self.lock = threading.RLock()
        self.processes = {}
        self.stopping = threading.Event()
        self.manager_health = {"enabled": launch, "checked_at": None, "errors": {}}
        self.manager_thread = None
        if launch:
            self.manager_thread = threading.Thread(target=self.manage, daemon=True)
            self.manager_thread.start()

    def manage(self):
        while not self.stopping.wait(2):
            self.manage_once()

    def health(self):
        return {**self.manager_health, "alive": bool(self.manager_thread and self.manager_thread.is_alive())}

    def manage_once(self):
        errors = {}
        try:
            jobs = self.store.registered()
        except Exception as exc:
            jobs = []
            errors["registry"] = str(exc)
        for job in jobs:
            try:
                with self.lock:
                    root = self.store.job_root(job)
                    state = read_json(root / "status.json")["state"]
                    process = self.processes.get(job)
                    if process and process.poll() is None:
                        continue
                    if state not in ("queued", "starting", "running", "collecting", "cancelling"):
                        continue
                    with open(root / ".runner.lock", "a") as lock:
                        try:
                            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                        except BlockingIOError:
                            continue
                    with open(root / "controller.log", "ab") as log:
                        self.processes[job] = subprocess.Popen(
                            [sys.executable, "-u", "-m", "antigen_design.web_runner", "--repo",
                             str(self.store.repo), "--project", self.store.root.name, job],
                            stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
            except Exception as exc:
                errors[str(job)] = str(exc)
        self.manager_health = {**self.manager_health, "checked_at": now(), "errors": errors}

    def action(self, job, action):
        root = self.store.job_root(job)
        status = read_json(root / "status.json")
        if action in ("archive", "unarchive"):
            return self.store.archive(job, action == "archive")
        if (root / "archive.json").exists() and read_json(root / "archive.json")["archived"]:
            raise ValueError("Restore the archived task before other actions")
        if action == "cancel":
            if status["state"] in ("succeeded", "cancelled", "failed"):
                raise ValueError("Task is already terminal")
            (root / "cancel.request").touch()
            if status["state"] == "attention":
                write_json(root / "status.json", status | {"state": "queued", "updated_at": now()})
        elif action in ("retry", "resume"):
            if status["state"] not in ("failed", "cancelled", "attention"):
                raise ValueError("Controller is active or the task has succeeded")
            with open(root / ".runner.lock", "a") as lock:
                try:
                    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    raise ValueError("Controller is still active; wait for it to stop") from None
                if action == "retry":
                    manifest_path = root / "pipeline" / "manifest.json"
                    if manifest_path.exists() and any(s["state"] in ("submitting", "submitted", "running")
                                                     for s in read_json(manifest_path)["tasks"].values()):
                        raise ValueError("Active/unknown scheduler jobs exist; use resume to reconcile their state")
                    (root / "cancel.request").unlink(missing_ok=True)
                    (root / "retry.request").touch()
                write_json(root / "status.json", status | {"state": "queued", "updated_at": now(), "error": None})
        else:
            raise ValueError("Unknown action")
        return self.store.detail(job)


class Handler(BaseHTTPRequestHandler):
    server_version = "AntigenLocal/1"

    def log_message(self, format, *args):
        pass

    @property
    def app(self):
        return self.server.app

    def trusted(self, mutate=False):
        port = self.server.server_address[1]
        hosts = {f"127.0.0.1:{port}", f"localhost:{port}"}
        if self.headers.get("Host") not in hosts:
            raise PermissionError("This is a local-only service")
        origin = self.headers.get("Origin")
        if origin and origin not in {"http://" + host for host in hosts}:
            raise PermissionError("Cross-origin access denied")
        if mutate and (self.headers.get("X-Antigen-Client") != "local" or
                       self.headers.get_content_type() != "application/json"):
            raise PermissionError("Use the same-origin application client")

    def respond(self, value, status=200):
        body = json.dumps(value, ensure_ascii=False, allow_nan=False).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(body)

    def file(self, path, download=False):
        if not path.is_file():
            raise FileNotFoundError(path.name)
        self.send_response(200)
        self.send_header("Content-Type", mimetypes.guess_type(path.name)[0] or "application/octet-stream")
        self.send_header("Content-Length", str(path.stat().st_size))
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Cache-Control", "no-cache")
        if path.suffix == ".html":
            self.send_header("Content-Security-Policy",
                             "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; "
                             "img-src 'self' data:; connect-src 'self'; worker-src 'self' blob:; "
                             "object-src 'none'; frame-ancestors 'none'; base-uri 'self'")
        if download:
            self.send_header("Content-Disposition", f'attachment; filename="{path.name}"')
        self.end_headers()
        import shutil
        with open(path, "rb") as stream:
            shutil.copyfileobj(stream, self.wfile)

    def do_GET(self):
        try:
            self.trusted()
            path = urlsplit(self.path).path
            parts = path.strip("/").split("/")
            store = self.app.store
            if path == "/api/health":
                return self.respond(self.app.health())
            if path == "/api/scheduler-limits":
                return self.respond(store.scheduler_limits())
            if path == "/api/config":
                return self.respond({"profiles": store.profiles(), "model": "protenix-v2",
                                     "project": store.root.name, "local_only": True,
                                     "defaults": getattr(store, 'ui_defaults', lambda: {})()})
            if path == "/api/jobs":
                query = parse_qs(urlsplit(self.path).query)
                return self.respond(store.list_jobs(
                    archive=query.get("archive", ["current"])[0],
                    profile=query.get("profile", [""])[0], state=query.get("state", [""])[0],
                    offset=int(query.get("offset", ["0"])[0]), limit=int(query.get("limit", ["25"])[0])))
            if len(parts) >= 3 and parts[:2] == ["api", "jobs"]:
                root = store.job_root(parts[2])
                if len(parts) == 3:
                    return self.respond(store.detail(parts[2]))
                if len(parts) == 4 and parts[3] == "download":
                    return self.file(root / "pipeline" / "deliverables.tar.gz", True)
                if len(parts) == 5 and parts[3] == "results":
                    index = int(parts[4])
                    return self.respond(store.result_view(parts[2], index))
                if len(parts) == 6 and parts[3] == "results":
                    index = int(parts[4])
                    if index < 0:
                        raise ValueError("Invalid result index")
                    result = read_json(root / "results.json")[index]
                    field = {"structure": "structure", "confidence": "summary_file",
                             "full-confidence": "full_confidence_file"}.get(parts[5])
                    if field is None or field not in result:
                        raise FileNotFoundError("Artifact unavailable")
                    target = (root / result[field]).resolve()
                    if not target.is_relative_to((root / "collected").resolve()):
                        raise ValueError("Invalid artifact path")
                    return self.file(target)
            if len(parts) == 4 and parts[:2] == ["api", "assets"] and parts[3] == "structure":
                store.asset(parts[2])
                return self.file(store.root / "inputs" / identifier(parts[2]) / "viewer.pdb")
            static = STATIC / ("index.html" if path == "/" else path.lstrip("/"))
            if not static.resolve().is_relative_to(STATIC.resolve()):
                raise PermissionError("Invalid path")
            return self.file(static)
        except (ValueError, KeyError, IndexError, TypeError) as exc:
            self.respond({"error": str(exc)}, 400)
        except FileNotFoundError:
            self.respond({"error": "Not found"}, 404)
        except PermissionError as exc:
            self.respond({"error": str(exc)}, 403)

    def do_POST(self):
        try:
            self.trusted(mutate=True)
            if self.app.read_only:
                raise PermissionError("Read-only verification mode; mutations and job submission are disabled")
            length = int(self.headers.get("Content-Length", "0"))
            if not 0 < length <= 24 * 1024 * 1024:
                raise ValueError("Request size must be 1 byte–24 MiB")
            data = json.loads(self.rfile.read(length))
            path = urlsplit(self.path).path
            if path == "/api/scheduler-limits":
                with self.app.lock:
                    result = self.app.store.scheduler_limits(data)
            elif path == "/api/assets":
                result = self.app.store.upload(data["filename"], data["content"])
            elif path == "/api/benchmark":
                benchmark = self.app.store.repo / "projects" / "1ubq" / "inputs" / "1ubq.cif"
                result = self.app.store.upload("1ubq.cif", benchmark.read_text())
            elif path == "/api/preview":
                result = self.app.store.preview(data)
            elif path.startswith("/api/assets/") and path.endswith("/surface"):
                parts = path.strip("/").split("/")
                if len(parts) != 4:
                    raise ValueError("Unknown endpoint")
                result = self.app.store.surface(parts[2])
            elif path == "/api/jobs":
                result = self.app.store.submit(data)
            else:
                parts = path.strip("/").split("/")
                if len(parts) != 4 or parts[:2] != ["api", "jobs"]:
                    raise ValueError("Unknown endpoint")
                with self.app.lock:
                    result = self.app.action(parts[2], parts[3])
            self.respond(result)
        except (ValueError, KeyError, TypeError, OSError) as exc:
            self.respond({"error": str(exc)}, 403 if isinstance(exc, PermissionError) else 400)
        except Exception as exc:
            # Parser errors are user-facing; HTTP thread must survive malformed structures.
            self.respond({"error": str(exc)}, 422)


def main():
    from .web_ui.__main__ import main as configured_main
    return configured_main()


if __name__ == "__main__":
    main()
