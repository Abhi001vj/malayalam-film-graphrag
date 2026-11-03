"""Vercel Python entrypoint. A versioned, read-only catalogue is unpacked to /tmp."""
import gzip
import hashlib
import os
from pathlib import Path
import sys

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'src'))
from filmgraph.server import make_handler

archive=ROOT/'data'/'deploy.sqlite3.gz'
if not archive.exists():
    raise RuntimeError('Create the deployment catalogue with: filmgraph snapshot')
fingerprint=hashlib.sha256(archive.read_bytes()).hexdigest()[:16]
database=Path('/tmp')/('filmgraph-'+fingerprint+'.sqlite3')
if not database.exists():
    temporary=database.with_suffix('.tmp.'+str(os.getpid()))
    with gzip.open(archive,'rb') as source,temporary.open('wb') as dest:
        import shutil
        shutil.copyfileobj(source,dest)
    temporary.replace(database)
# Vercel finds the function by a top-level class named `handler` (a BaseHTTPRequestHandler subclass).
class handler(make_handler(str(database))):
    pass
