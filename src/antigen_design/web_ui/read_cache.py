"""Bounded, stat-invalidated read models; never rewrite scientific records."""
from collections import OrderedDict
import json
import threading
from antigen_design.io import fingerprint, read_json, write_json


def stamp(path):
    stat = path.stat()
    return [stat.st_dev, stat.st_ino, stat.st_mtime_ns, stat.st_ctime_ns, stat.st_size]


class ObjectStream:
    """Decode a manifest one task at a time instead of retaining embedded plans."""
    def __init__(self, stream):
        self.stream, self.buffer, self.pos, self.eof = stream, '', 0, False
        self.decoder = json.JSONDecoder()

    def more(self):
        chunk = self.stream.read(1024 * 1024)
        self.buffer = self.buffer[self.pos:] + chunk
        self.pos = 0
        self.eof = not chunk

    def peek(self):
        while True:
            while self.pos < len(self.buffer) and self.buffer[self.pos].isspace():
                self.pos += 1
            if self.pos < len(self.buffer):
                return self.buffer[self.pos]
            if self.eof:
                return ''
            self.more()

    def take(self, token):
        if self.peek() != token:
            raise ValueError('Invalid manifest JSON')
        self.pos += 1

    def value(self):
        self.peek()
        while True:
            try:
                value, end = self.decoder.raw_decode(self.buffer, self.pos)
                # A number may end at a chunk boundary but continue in the next.
                if (end == len(self.buffer) or self.buffer[end] not in ' \t\r\n,]}:') and not self.eof:
                    self.more()
                    continue
                self.pos = end
                return value
            except json.JSONDecodeError:
                if self.eof:
                    raise
                self.more()

    def items(self, value_reader):
        self.take('{')
        if self.peek() == '}':
            self.take('}')
            return
        while True:
            key = self.value()
            if not isinstance(key, str):
                raise ValueError('Invalid manifest key')
            self.take(':')
            yield key, value_reader(key)
            if self.peek() == '}':
                self.take('}')
                return
            self.take(',')


def compact_manifest(path):
    def compact(state):
        for record in (state, *state.get('history', [])):
            route = record.get('route', {})
            capsule = route.pop('plan', None)
            if capsule is not None:
                route['plan_fingerprint'] = fingerprint(capsule)
        return state

    with path.open(encoding='utf-8') as stream:
        parser = ObjectStream(stream)
        def read_value(key):
            if key == 'tasks':
                return {name: compact(state) for name, state in parser.items(lambda _: parser.value())}
            return parser.value()
        result = dict(parser.items(read_value))
        if parser.peek():
            raise ValueError('Trailing manifest data')
        return result


class ReadCache:
    def __init__(self, limit=256):
        self.limit = limit
        self.values = OrderedDict()
        self.guard = threading.RLock()
        # Striped locks bound memory and keep unrelated reads independent.
        self.locks = [threading.RLock() for _ in range(32)]

    def get(self, path, loader=read_json, persistent=None):
        key = str(path)
        with self.locks[hash(key) % len(self.locks)]:
            version = stamp(path)
            with self.guard:
                cached = self.values.get(key)
                if cached and cached[0] == version:
                    self.values.move_to_end(key)
                    return cached[1]
            value = None
            if persistent and persistent.exists():
                try:
                    saved = read_json(persistent)
                    if saved.get('version') == 1 and saved.get('stamp') == version:
                        value = saved['value']
                except (ValueError, OSError, KeyError, TypeError):
                    pass
            if value is None:
                value = loader(path)
                if stamp(path) != version:
                    raise ValueError('Run snapshot changed while reading; refresh to retry')
                if persistent:
                    write_json(persistent, {'version': 1, 'stamp': version, 'value': value})
            with self.guard:
                self.values[key] = (version, value)
                self.values.move_to_end(key)
                while len(self.values) > self.limit:
                    self.values.popitem(last=False)
            return value
