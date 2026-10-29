"""Web server with independent project pages and completed-pair previews."""
import argparse
from contextlib import nullcontext
import fcntl
import json
import os
from http.server import ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit, unquote, parse_qs
from antigen_design.webapp import Handler as BaseHandler
from .application import Application
from .connections import install
from .resource_store import Store
from antigen_design.deployment import settings


class Handler(BaseHandler):
    def do_GET(self):
        if urlsplit(self.path).path == '/api/resources':
            try:
                self.trusted()
                return self.respond(self.app.store.resources())
            except (ValueError, OSError) as exc:
                return self.respond({'error': str(exc)}, 403 if isinstance(exc, PermissionError) else 400)
        parts=urlsplit(self.path).path.strip('/').split('/')
        if len(parts)==4 and parts[:2]==['api','jobs'] and parts[3] in ('overview','extras'):
            try:
                self.trusted()
                if parts[3]=='overview':
                    return self.respond(self.app.store.overview(parts[2]))
                query=parse_qs(urlsplit(self.path).query)
                return self.respond(self.app.store.extras(parts[2],query.get('kind',['logs'])[0],
                    int(query.get('offset',['0'])[0]),int(query.get('limit',['50'])[0])))
            except PermissionError as exc:
                return self.respond({'error':str(exc)},403)
            except (ValueError,KeyError,TypeError,OSError) as exc:
                return self.respond({'error':str(exc)},400)
        if len(parts) in (5,6) and parts[:2]==['api','jobs'] and parts[3]=='results':
            try:
                self.trusted()
                result_id=unquote(parts[4])
                if len(parts)==5:
                    return self.respond(self.app.store.result_view(parts[2],result_id))
                return self.file(self.app.store.result_file(parts[2],result_id,parts[5]))
            except FileNotFoundError:
                return self.respond({'error':'Result artifact not found'},404)
            except PermissionError as exc:
                return self.respond({'error':str(exc)},403)
            except (ValueError,KeyError,IndexError,TypeError) as exc:
                return self.respond({'error':str(exc)},400)
            except Exception:
                return self.respond({'error':'结果收集或校验失败，请稍后重试；已有计算任务不受影响。'},502)
        return super().do_GET()

    def do_POST(self):
        path = urlsplit(self.path).path
        parts = path.strip('/').split('/')
        rename = len(parts) == 4 and parts[:2] == ['api', 'jobs'] and parts[3] == 'rename'
        if path != '/api/resources' and not rename:
            return super().do_POST()
        try:
            self.trusted(mutate=True)
            if self.app.read_only:
                raise PermissionError('Read-only verification mode')
            length = int(self.headers.get('Content-Length', '0'))
            if not 0 < length <= 65536:
                raise ValueError('Invalid resource settings size')
            data = json.loads(self.rfile.read(length))
            with self.app.lock:
                if rename:
                    return self.respond(self.app.store.rename(parts[2], data))
                return self.respond(self.app.store.resources(data))
        except (ValueError, TypeError, KeyError, OSError) as exc:
            return self.respond({'error': str(exc)}, 403 if isinstance(exc, PermissionError) else 400)


def main(argv=None):
    install()
    parser=argparse.ArgumentParser(description='Antigen Studio web UI (Linux/WSL)')
    parser.add_argument('--config', help='Deployment YAML; paths are relative to this file')
    parser.add_argument('--port',type=int)
    parser.add_argument('--repo', help='Workspace override')
    parser.add_argument('--project')
    parser.add_argument('--read-only',action='store_true', default=None)
    args=parser.parse_args(argv)
    try:
        value = settings(args.repo or Path.cwd(), args.config)
        if Path(value['config_path']).is_file():
            os.environ['ANTIGEN_WEB_CONFIG'] = value['config_path']
        port = args.port if args.port is not None else value['port']
        if not 1 <= port <= 65535:
            raise ValueError('port must be 1–65535')
        read_only = args.read_only if args.read_only is not None else value['read_only']
        store=Store(args.repo or value['repo'], args.project or value['project'])
        # Validate configuration before starting an HTTP thread or controller.
        store.resources()
    except (ValueError, OSError) as exc:
        parser.error(str(exc))
    with (nullcontext() if read_only else (store.root/'runs/.web.lock').open('a')) as lock:
        if lock is not None:
            fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        server=ThreadingHTTPServer((value['host'],port),Handler)
        server.app=Application(store,read_only=read_only)
        print(f'Antigen Studio: http://127.0.0.1:{port}',flush=True)
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass
        finally:
            server.app.stopping.set()
            server.server_close()


if __name__=='__main__':
    main()
