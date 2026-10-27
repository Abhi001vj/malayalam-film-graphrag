"""Shared local and Vercel HTTP handler for the cinema explorer."""
from __future__ import annotations
import json
import gzip
import mimetypes
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse
from .db import connect
from .search import search, coverage, film_detail, person_graph
from . import accounts

STATIC = Path(__file__).parent / 'static'


def make_handler(db_path: str, use_vectors: bool = False):
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
                if not self.headers.get('Content-Type','').startswith('application/json'):
                    return self.json_response({'error':'Use application/json'},415)
                size=int(self.headers.get('Content-Length','0'))
                if size<1 or size>65536:return self.json_response({'error':'Invalid request size'},413)
                body=json.loads(self.rfile.read(size))
                if not isinstance(body,dict):raise ValueError('JSON object required')
                path=urlparse(self.path).path
                if path.startswith('/api/auth/'):
                    data,headers=accounts.auth(path.rsplit('/',1)[-1],body,self)
                    return self.json_response(data,headers=headers)
                with connect(db_path) as con:
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
                        data=search(con,question,int(body.get('limit',20)),use_vectors,int(body.get('offset',0)),filters,query_plan=plan)
                        data['quota']=quota
                    elif path=='/api/feedback':
                        from .feedback import submit
                        data=submit(self,body)
                    elif path=='/api/chat':
                        from .pipeline import answer
                        question=body.get('question','')
                        if not isinstance(question,str) or not 1<=len(question)<=500:raise ValueError('Enter a question of at most 500 characters')
                        context=body.get('context') if isinstance(body.get('context'),dict) else None
                        overrides=body.get('overrides') if isinstance(body.get('overrides'),dict) else None
                        backend=body.get('backend') if body.get('backend') in {'auto','neo4j','sqlite'} else 'auto'
                        quota=self.quota(question+json.dumps(overrides or {}),{})
                        if quota is None:return
                        data=answer(con,question,context,overrides,body.get('use_llm') is not False,backend)
                        data['quota']=quota
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
                    with connect(db_path) as con:
                        if parsed.path in {'/api/search','/api/chat'}:
                            question=value('q')[:1000]
                            filters={k:value(k)[:100] for k in ('year','year_from','year_to','role','technology','location') if value(k)}
                            if not question.strip() and not filters:
                                question='all movies'
                            quota=self.quota(question,filters)
                            if quota is None:return
                            data=search(con,question,int(value('limit','20')),use_vectors,
                                        int(value('offset','0')),filters)
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
            if urlparse(self.path).path=='/model-lab.html':
                policy="default-src 'self'; script-src 'self' https://esm.sh 'wasm-unsafe-eval'; style-src 'self' 'unsafe-inline'; connect-src 'self' https:; worker-src 'self' blob:; object-src 'none'; base-uri 'self'"
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
