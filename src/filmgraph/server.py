"""Shared local and Vercel HTTP handler for the cinema explorer."""
from __future__ import annotations
import copy
import json
import gzip
import mimetypes
import os
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse
from .db import connect
from .search import search, coverage, film_detail, person_graph
from . import accounts, settings

STATIC = Path(__file__).parent / 'static'
API_PER_MINUTE = int(os.environ.get('FILMGRAPH_API_PER_MINUTE', '60'))  # all /api calls, per IP, per instance
_hits: dict[str, list] = {}
_hits_lock = threading.Lock()
HIDDEN = '(hidden on the hosted demo)'


def client_ip(handler) -> str:
    # The forwarded header is trusted only behind Vercel's proxy, which overwrites it.
    ip = handler.headers.get('x-vercel-forwarded-for', '').split(',')[0].strip() if os.environ.get('VERCEL') else ''
    return ip or handler.client_address[0]


def throttled(ip: str) -> int:
    """Fixed one-minute window per IP. Returns seconds to wait, or 0 if the request may proceed."""
    window = int(time.time() // 60)
    with _hits_lock:
        if len(_hits) > 10000:  # bound memory under a flood of spoofed or rotating IPs
            _hits.clear()
        start, count = _hits.get(ip, (window, 0))
        if start != window:
            start, count = window, 0
        if count >= API_PER_MINUTE:
            return 60 - int(time.time() % 60)
        _hits[ip] = (start, count + 1)
    return 0


def bounded(raw, default: int, low: int, high: int) -> int:
    try:
        return min(high, max(low, int(raw)))
    except (TypeError, ValueError):
        return default


def public_settings(info: dict) -> dict:
    """Hosted servers do not reveal file paths, model hosts or database URIs."""
    info = copy.deepcopy(info)
    info['file'] = HIDDEN
    for block in ('defaults', 'current', 'saved'):
        group = info.get(block) or {}
        if isinstance(group.get('paths'), dict):
            group['paths'] = {k: HIDDEN for k in group['paths']}
        models = group.get('models')
        if isinstance(models, dict):
            for key in ('ollama_host', 'llm_base_url', 'neo4j_uri', 'neo4j_username', 'neo4j_database'):
                if key in models:
                    models[key] = HIDDEN
    return info


def make_handler(db_path: str, use_vectors: bool = False):
    settings._runtime['db'] = db_path  # the --db this server started with is the default data path
    class Handler(BaseHTTPRequestHandler):
        def log_message(self,format,*args):
            # Search text and account details do not go into access logs.
            print(self.command+' '+urlparse(self.path).path,flush=True)

        def quota(self,question,filters):
            result=accounts.consume_quota(self,question,filters)
            if not result['allowed']:
                if result.get('reason')=='minute':
                    message=f"Too many questions in a minute ({result.get('limit_per_minute')}/min). Try again in {result.get('retry_after',60)} s."
                    headers=[('Retry-After',str(result.get('retry_after',60)))]
                else:
                    message='Daily query limit reached. Sign in for 200 questions/day.' if not result.get('user') else 'Daily query limit reached. Try again after 00:00 UTC.'
                    headers=None
                self.json_response({'error':message,'quota':result},429,headers)
                return None
            return result

        def do_POST(self):
            try:
                origin=self.headers.get('Origin')
                if origin and urlparse(origin).netloc!=self.headers.get('Host'):
                    return self.json_response({'error':'Origin not allowed'},403)
                path=urlparse(self.path).path
                wait=throttled(client_ip(self))
                if wait:return self.json_response({'error':f'Too many requests. Try again in {wait} s.'},429,[('Retry-After',str(wait))])
                if not self.headers.get('Content-Type','').startswith('application/json'):
                    return self.json_response({'error':'Use application/json'},415)
                size=int(self.headers.get('Content-Length','0'))
                if size<1 or size>65536:return self.json_response({'error':'Invalid request size'},413)
                body=json.loads(self.rfile.read(size))
                if not isinstance(body,dict):raise ValueError('JSON object required')
                if path.startswith('/api/auth/'):
                    data,headers=accounts.auth(path.rsplit('/',1)[-1],body,self)
                    return self.json_response(data,headers=headers)
                if path=='/api/settings':
                    # Paths and model hosts are machine configuration: only the local operator may change them.
                    if not settings.writable() or self.client_address[0] not in ('127.0.0.1','::1'):
                        return self.json_response({'error':'Settings can only be changed on a local server'},403)
                    settings.save({} if body.get('reset') is True else body.get('values',{}))
                    with connect(settings.get('paths.db')) as con:
                        return self.json_response(settings.describe(con))
                with connect(settings.get('paths.db')) as con:
                    if path=='/api/query':
                        question=body.get('question','')
                        if not isinstance(question,str) or not 1<=len(question)<=1000:raise ValueError('Enter a question of at most 1000 characters')
                        filters=body.get('filters',{})
                        if not isinstance(filters,dict):raise ValueError('Invalid filters')
                        filters={k:str(v)[:100] for k,v in filters.items() if k in {'year','year_from','year_to','role','technology','location'} and v}
                        plan=body.get('plan')
                        if plan is not None:
                            from .plans import validate_plan
                            validate_plan(con,plan,question)
                        quota=self.quota(question,filters)
                        if quota is None:return
                        data=search(con,question,bounded(body.get('limit'),20,1,50),use_vectors,bounded(body.get('offset'),0,0,5000),filters,query_plan=plan)
                        data['quota']=quota
                    elif path=='/api/feedback':
                        if accounts.public_demo() and not accounts.enabled():
                            raise accounts.AccountError('Feedback is turned off on the public demo.',503)
                        from .feedback import submit
                        data=submit(self,body)
                    elif path=='/api/chat':
                        from .pipeline import answer
                        question=body.get('question','')
                        if not isinstance(question,str) or not 1<=len(question)<=500:raise ValueError('Enter a question of at most 500 characters')
                        context=body.get('context') if isinstance(body.get('context'),dict) else None
                        overrides=body.get('overrides') if isinstance(body.get('overrides'),dict) else None
                        backend=body.get('backend') if body.get('backend') in {'auto','neo4j','sqlite'} else None  # None -> settings
                        quota=self.quota(question+json.dumps(overrides or {}),{})
                        if quota is None:return
                        data=answer(con,question,context,overrides,body.get('use_llm') is not False,backend)
                        data['quota']=quota
                        try:
                            from .compare import typical_rag
                            data['baseline']=typical_rag(con,question)  # what plain chunk-RAG would retrieve
                        except Exception:
                            data['baseline']=None
                    elif path in {'/api/library','/api/preferences','/api/follow','/api/delete_account'}:
                        data=accounts.update_private(self,path.rsplit('/',1)[-1],body,con)
                    elif path=='/api/recommendations':
                        from .recommend import recommendations
                        seeds=body.get('seed_ids',[]) if body.get('personalization') is True else []
                        if not isinstance(seeds,list) or len(seeds)>50:raise ValueError('Invalid recommendation seeds')
                        data=recommendations(con,seeds)
                    else:return self.json_response({'error':'Endpoint not found'},404)
                return self.json_response(data,headers=accounts.session_headers() if path=='/api/delete_account' else None)
            except accounts.AccountError as exc:self.json_response({'error':str(exc)},exc.status)
            except (ValueError,TypeError) as exc:self.json_response({'error':str(exc)},400)
            except Exception:
                import traceback
                traceback.print_exc()
                self.json_response({'error':'The operation could not be completed'},503)

        def do_GET(self):
            parsed=urlparse(self.path);query=parse_qs(parsed.query)
            value=lambda key,default='':query.get(key,[default])[0]
            try:
                if parsed.path.startswith('/api/'):
                    wait=throttled(client_ip(self))
                    if wait:return self.json_response({'error':f'Too many requests. Try again in {wait} s.'},429,[('Retry-After',str(wait))])
                if parsed.path=='/api/settings':
                    with connect(settings.get('paths.db')) as con:
                        info=settings.describe(con)
                        return self.json_response(info if settings.writable() else public_settings(info))
                if parsed.path.startswith('/api/'):
                    with connect(settings.get('paths.db')) as con:
                        if parsed.path in {'/api/search','/api/chat'}:
                            question=value('q')[:1000]
                            filters={k:value(k)[:100] for k in ('year','year_from','year_to','role','technology','location') if value(k)}
                            if not question.strip() and not filters:
                                question='all movies'
                            quota=self.quota(question,filters)
                            if quota is None:return
                            data=search(con,question,bounded(value('limit','20'),20,1,50),use_vectors,
                                        bounded(value('offset','0'),0,0,5000),filters)
                            data['quota']=quota
                        elif parsed.path in {'/api/stats','/api/coverage'}:data=coverage(con)
                        elif parsed.path=='/api/entities':
                            from .plans import entity_dictionary
                            data={'entities':entity_dictionary(con)}
                        elif parsed.path=='/api/film':
                            data=film_detail(con,value('id')[:300])
                            if data is None:return self.json_response({'error':'Film not found'},404)
                        elif parsed.path=='/api/person':
                            quota=self.quota(value('name')[:200],{})
                            if quota is None:return
                            data=person_graph(con,value('name')[:200])
                            if 'error' in data:return self.json_response(data,404)
                        elif parsed.path in {'/api/account','/api/export'}:
                            data=accounts.account_data(self)
                        elif parsed.path=='/api/health':
                            data={'ok':True,'films':con.execute('SELECT count(*) FROM films').fetchone()[0]}
                        else:return self.json_response({'error':'Endpoint not found'},404)
                    return self.json_response(data)
                path=(STATIC/('index.html' if parsed.path=='/' else parsed.path.lstrip('/'))).resolve()
                if not path.is_relative_to(STATIC.resolve()) or not path.is_file():
                    return self.respond(b'Not found','text/plain; charset=utf-8',404)
                self.respond(path.read_bytes(),mimetypes.guess_type(path.name)[0] or 'application/octet-stream')
            except accounts.AccountError as exc:
                self.json_response({'error':str(exc)},exc.status)
            except (ValueError,TypeError) as exc:
                self.json_response({'error':str(exc)},400)
            except Exception:
                import traceback
                traceback.print_exc()
                self.json_response({'error':'The query could not be completed. Check the server log.'},500)

        def json_response(self,data,status=200,headers=None):
            self.respond(json.dumps(data,ensure_ascii=False).encode(),'application/json; charset=utf-8',status,headers)

        def respond(self,body,content_type,status=200,headers=None):
            compressed=len(body)>4096 and 'gzip' in self.headers.get('Accept-Encoding','') and content_type.startswith(('application/json','text/','application/javascript'))
            if compressed:body=gzip.compress(body,compresslevel=3)
            self.send_response(status)
            if compressed:
                self.send_header('Content-Encoding','gzip')
                self.send_header('Vary','Accept-Encoding')
            for name,value in headers or []:self.send_header(name,value)
            self.send_header('Content-Type',content_type)
            self.send_header('Content-Length',str(len(body)))
            self.send_header('Cache-Control','no-store' if content_type.startswith(('application/json','text/html')) else 'public, max-age=3600')
            self.send_header('X-Content-Type-Options','nosniff')
            self.send_header('Referrer-Policy','strict-origin-when-cross-origin')
            self.send_header('Permissions-Policy','camera=(), microphone=(), geolocation=(), payment=(), usb=()')
            self.send_header('Cross-Origin-Opener-Policy','same-origin')
            if urlparse(self.path).path.startswith('/api/'):
                self.send_header('X-Robots-Tag','noindex, nofollow')
            if urlparse(self.path).path=='/model-lab.html':
                policy="default-src 'self'; script-src 'self' https://esm.sh 'wasm-unsafe-eval'; style-src 'self' 'unsafe-inline'; connect-src 'self' https:; worker-src 'self' blob:; object-src 'none'; base-uri 'self'"
            elif urlparse(self.path).path in ('/','/index.html'):
                # Smart answers load WebLLM from esm.sh (pinned version) and model weights over HTTPS; nothing else changes.
                policy="default-src 'self'; script-src 'self' https://esm.sh 'wasm-unsafe-eval'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; connect-src 'self' https:; worker-src 'self' blob:; object-src 'none'; base-uri 'self'; frame-ancestors 'self' chrome-extension:"
            else:
                policy="default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; connect-src 'self'; object-src 'none'; base-uri 'self'; frame-ancestors 'self' chrome-extension:"
            self.send_header('Content-Security-Policy',policy)
            self.end_headers();self.wfile.write(body)
    return Handler


def serve(db_path: str, host: str='127.0.0.1', port: int=8765,use_vectors: bool=False):
    server=ThreadingHTTPServer((host,port),make_handler(db_path,use_vectors))
    print(f'FilmGraph running at http://{host}:{port}',flush=True)
    try:server.serve_forever()
    except KeyboardInterrupt:pass
    finally:server.server_close()
