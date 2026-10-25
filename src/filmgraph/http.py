"""Identified, rate-limited HTTPS requests with persistent compressed caches."""
from __future__ import annotations
import gzip
import hashlib
import json
import os
import ssl
import threading
import time
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

DEFAULT_AGENT = 'MalayalamFilmGraph/0.2 (https://github.com/abhilashvj; personal cinema research)'

class Client:
    def __init__(self, cache: Path = Path('data/cache/http'), user_agent: str = DEFAULT_AGENT,
                 interval: float = .3):
        self.cache = Path(cache)
        self.cache.mkdir(parents=True, exist_ok=True)
        self.user_agent = user_agent
        self.interval = interval
        self.lock = threading.Lock()
        self.last = 0.0
        self.context = ssl.create_default_context()
        # python.org macOS installs may lack a configured CA file. Keep TLS verified.
        if not ssl.get_default_verify_paths().cafile and not os.environ.get('SSL_CERT_FILE'):
            for path in ('/etc/ssl/cert.pem', '/opt/homebrew/etc/ca-certificates/cert.pem'):
                if Path(path).exists():
                    self.context.load_verify_locations(path)
                    break

    def get(self, url: str, refresh: bool = False) -> bytes:
        dest = self.cache / (hashlib.sha256(url.encode()).hexdigest() + '.gz')
        if dest.exists() and not refresh:
            return gzip.decompress(dest.read_bytes())
        for attempt in range(5):
            with self.lock:
                time.sleep(max(0, self.interval - (time.monotonic() - self.last)))
                self.last = time.monotonic()
            try:
                request = Request(url, headers={'User-Agent': self.user_agent,
                                               'Accept': 'application/json, text/html;q=0.9, */*;q=0.8'})
                with urlopen(request, timeout=60, context=self.context) as response:
                    data = response.read()
                temp = dest.with_suffix('.tmp')
                temp.write_bytes(gzip.compress(data))
                temp.replace(dest)
                return data
            except HTTPError as exc:
                if exc.code not in {429, 500, 502, 503, 504} or attempt == 4:
                    raise
                delay = exc.headers.get('Retry-After', '')
                time.sleep(min(60, int(delay) if delay.isdigit() else 2 ** (attempt + 1)))
            except (URLError, TimeoutError):
                if attempt == 4:
                    raise
                time.sleep(2 ** (attempt + 1))
        raise RuntimeError('Request failed')

    def json(self, url: str) -> dict:
        return json.loads(self.get(url))
