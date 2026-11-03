"""Supabase accounts/RLS data and gateway quotas; local guest limits persist in SQLite."""
from __future__ import annotations
import hashlib
import hmac
import json
import os
import sqlite3
import time
from datetime import datetime, timezone
from http.cookies import SimpleCookie
from contextlib import closing
from pathlib import Path
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import Request,urlopen
from .http import Client

# Local development loads only the project's explicit .env file; never logs secrets.
for line in Path('.env').read_text().splitlines() if Path('.env').exists() else []:
    name,sep,value=line.partition('=')
    if sep and name.strip() and not name.lstrip().startswith('#'):
        value=value.strip()
        if value.startswith('"'):
            value=json.loads(value)
        os.environ.setdefault(name.strip(),value)

URL=os.environ.get('SUPABASE_URL','').rstrip('/')
KEY=os.environ.get('SUPABASE_PUBLISHABLE_KEY','')
SECRET=os.environ.get('FILMGRAPH_GATEWAY_SECRET','')
GATEWAY=URL+'/functions/v1/filmgraph-gateway'

class AccountError(Exception):
    def __init__(self,message,status=400):super().__init__(message);self.status=status


def enabled():return bool(URL and KEY and len(SECRET)>=32)


def request(path,data=None,method=None,token=None):
    headers={'apikey':KEY,'Content-Type':'application/json','Accept':'application/json','Prefer':'return=representation,resolution=merge-duplicates'}
    if token:headers['Authorization']='Bearer '+token
    req=Request(URL+path,data=json.dumps(data).encode() if data is not None else None,headers=headers,method=method or ('POST' if data is not None else 'GET'))
    try:
        with urlopen(req,timeout=20,context=Client().context) as response:
            body=response.read()
            return json.loads(body) if body else {}
    except HTTPError as exc:
        try:detail=json.loads(exc.read());message=detail.get('msg') or detail.get('error_description') or detail.get('message') or 'Account service request failed'
        except Exception:message='Account service request failed'
        raise AccountError(message,exc.code if exc.code<500 else 503) from None


def gateway(data):
    body=json.dumps({**data,'timestamp':int(time.time())},separators=(',',':')).encode()
    signature=hmac.new(SECRET.encode(),body,hashlib.sha256).hexdigest()
    req=Request(GATEWAY,data=body,headers={'apikey':KEY,'Content-Type':'application/json','X-FilmGraph-Signature':signature})
    try:
        with urlopen(req,timeout=20,context=Client().context) as response:return json.load(response)
    except HTTPError as exc:
        try:message=json.loads(exc.read()).get('error','Account gateway unavailable')
        except Exception:message='Account gateway unavailable'
        raise AccountError(message,exc.code) from None


def cookies(handler):
    jar=SimpleCookie()
    try:jar.load(handler.headers.get('Cookie',''))
    except Exception:pass
    return {name:cookie.value for name,cookie in jar.items()}


def access_token(handler):return cookies(handler).get('fg_access','')


def session_headers(session=None):
    secure=bool(os.environ.get('VERCEL') or os.environ.get('FILMGRAPH_SECURE_COOKIES'))
    attributes='; Path=/; HttpOnly'+('; Secure; SameSite=None; Partitioned' if secure else '; SameSite=Lax')
    result=[]
    for field,name,lifetime in [('access_token','fg_access',3600),('refresh_token','fg_refresh',2592000)]:
        value=session.get(field,'') if session else ''
        result.append(('Set-Cookie',name+'='+value+'; Max-Age='+str(lifetime if value else 0)+attributes))
    return result


def get_user(handler,required=False):
    token=access_token(handler)
    user=gateway({'action':'identity','access_token':token}).get('user') if token and enabled() else None
    if required and not user:raise AccountError('Sign in to save your collection',401)
    return user


def consume_quota(handler,question,filters):
    token=access_token(handler)
    fingerprint=hashlib.sha256(json.dumps({'q':question.strip().casefold(),'filters':filters},sort_keys=True).encode()).hexdigest()
    # Forwarded IP is trusted only when running behind Vercel's managed proxy.
    ip=handler.headers.get('x-vercel-forwarded-for','').split(',')[0].strip() if os.environ.get('VERCEL') else handler.client_address[0]
    ip=ip or handler.client_address[0]
    guest=hashlib.sha256(('local:'+ip).encode()).hexdigest()
    if enabled():
        guest=hmac.new(SECRET.encode(),ip.encode(),hashlib.sha256).hexdigest()
        return gateway({'action':'quota','access_token':token,'guest_key':guest,'fingerprint':fingerprint})
    if os.environ.get('VERCEL') and not public_demo():
        raise AccountError('Account service must be configured before public queries are enabled',503)
    now=datetime.now(timezone.utc);day=now.date().isoformat();minute=now.strftime('%Y-%m-%dT%H:%M')
    from . import settings
    path=Path(settings.get('paths.accounts_db'));path.parent.mkdir(parents=True,exist_ok=True)
    limit=int(os.environ.get('FILMGRAPH_LOCAL_GUEST_LIMIT','20'))  # per day, distinct questions
    # every request counts, repeats included; tight only on the public demo
    per_minute=int(os.environ.get('FILMGRAPH_GUEST_PER_MINUTE','5' if public_demo() else '1000'))
    with closing(sqlite3.connect(path,timeout=30)) as con:
        con.execute('CREATE TABLE IF NOT EXISTS usage(subject TEXT,day TEXT,fingerprint TEXT,PRIMARY KEY(subject,day,fingerprint))')
        con.execute('CREATE TABLE IF NOT EXISTS usage_minute(subject TEXT,minute TEXT,n INTEGER,PRIMARY KEY(subject,minute))')
        con.execute('BEGIN IMMEDIATE')
        con.execute('DELETE FROM usage WHERE day<?',(day,))
        con.execute('DELETE FROM usage_minute WHERE minute<?',(minute,))
        recent=(con.execute('SELECT n FROM usage_minute WHERE subject=? AND minute=?',(guest,minute)).fetchone() or [0])[0]
        if recent>=per_minute:
            con.commit()
            return {'allowed':False,'reason':'minute','limit_per_minute':per_minute,'retry_after':60-now.second,
                    'limit':limit,'user':None,'mode':'public demo' if public_demo() else 'local guest'}
        con.execute('INSERT INTO usage_minute VALUES(?,?,1) ON CONFLICT(subject,minute) DO UPDATE SET n=n+1',(guest,minute))
        count=con.execute('SELECT count(*) FROM usage WHERE subject=? AND day=?',(guest,day)).fetchone()[0]
        seen=con.execute('SELECT 1 FROM usage WHERE subject=? AND day=? AND fingerprint=?',(guest,day,fingerprint)).fetchone()
        allowed=bool(seen or count<limit)
        if allowed and not seen:
            con.execute('INSERT INTO usage VALUES(?,?,?)',(guest,day,fingerprint));count+=1
        con.commit()
    return {'allowed':allowed,'limit':limit,'used':count,'remaining':max(0,limit-count),'reset':'00:00 UTC','user':None,
            'mode':'public demo' if public_demo() else 'local guest'}


def public_demo():
    """Hosted read-only demo without Supabase: per-instance guest limits in /tmp, no accounts, no stored feedback.
    The Vercel Firewall rate limit is the cross-instance backstop."""
    return os.environ.get('FILMGRAPH_PUBLIC_DEMO')=='1'


def account_data(handler):
    user=get_user(handler)
    result={'enabled':enabled(),'user':user,'guest_limit':20,'member_limit':200,'guest_per_minute':5,'member_per_minute':20,'terms_version':'2026-10-22-draft','mode':'supabase' if enabled() else 'local guest'}
    if user:
        token=access_token(handler);uid=user['id'];q='?'+urlencode({'user_id':'eq.'+uid})
        result.update(library=request('/rest/v1/filmgraph_library'+q,token=token),
                      follows=request('/rest/v1/filmgraph_follows'+q,token=token),
                      preferences=request('/rest/v1/filmgraph_preferences'+q,token=token),
                      notifications=request('/rest/v1/filmgraph_notifications'+q+'&order=created_at.desc&limit=50',token=token))
    return result


def auth(action,body,handler):
    if not enabled():raise AccountError('The local catalogue is available. Supabase accounts are not configured yet.',503)
    if action=='logout':
        token=access_token(handler)
        if token:
            try:request('/auth/v1/logout?scope=local',{},token=token)
            except AccountError as exc:
                if exc.status!=401:raise
        return {'signed_out':True},session_headers()
    if action=='refresh':
        refresh=cookies(handler).get('fg_refresh')
        if not refresh:raise AccountError('Sign in again',401)
        data=request('/auth/v1/token?grant_type=refresh_token',{'refresh_token':refresh})
        return {'user':data.get('user')},session_headers(data)
    email=body.get('email','').strip();password=body.get('password','')
    if '@' not in email or len(email)>254 or not isinstance(password,str) or not 12<=len(password)<=256:
        raise AccountError('Use a valid email and a password with 12–256 characters')
    if action=='signup':
        if body.get('accept_terms') is not True or body.get('adult') is not True:raise AccountError('Accept the terms and confirm you are 18 or older')
        data=request('/auth/v1/signup',{'email':email,'password':password})
        session=data.get('session') or (data if data.get('access_token') else None)
        return {'user':data.get('user'),'confirmation_required':not bool(session)},session_headers(session) if session else []
    if action=='login':
        data=request('/auth/v1/token?grant_type=password',{'email':email,'password':password})
        return {'user':{'id':data['user']['id'],'email':data['user'].get('email')}},session_headers(data)
    raise AccountError('Unknown account action',404)


def update_private(handler,action,body,con):
    user=get_user(handler,required=True);uid=user['id'];token=access_token(handler)
    if action=='library':
        film=con.execute('SELECT id,title FROM films WHERE id=?',(body.get('film_id',''),)).fetchone()
        if not film:raise AccountError('Film not found',404)
        fields={}
        for key in ('favorite','watchlisted','watched'):
            if key in body:
                if type(body[key]) is not bool:raise AccountError('Invalid list flag')
                fields[key]=body[key]
        if 'rating' in body:
            if body['rating'] is not None and (type(body['rating']) is not int or not 1<=body['rating']<=10):raise AccountError('Rating must be 1–10')
            fields['rating']=body['rating']
        q='?'+urlencode({'user_id':'eq.'+uid,'film_id':'eq.'+film['id']})
        old=request('/rest/v1/filmgraph_library'+q,token=token)
        data={**(old[0] if old else {}),'user_id':uid,'film_id':film['id'],'title':film['title'],**fields,'updated_at':datetime.now(timezone.utc).isoformat()}
        return request('/rest/v1/filmgraph_library?on_conflict=user_id,film_id',data,token=token)
    if action=='preferences':
        fields={}
        for key in ('personalization','analytics','email_alerts'):
            if key in body:
                if type(body[key]) is not bool:raise AccountError('Invalid preference')
                fields[key]=body[key]
        # No email delivery is connected in this version.
        if fields.get('email_alerts'):raise AccountError('Email alerts are not configured. In-app follows are available.')
        return request('/rest/v1/filmgraph_preferences?on_conflict=user_id',{'user_id':uid,**fields},token=token)
    if action=='follow':
        kind=body.get('target_kind');target=body.get('target_id','');name=body.get('target_name','')
        if kind not in {'person','film','company'} or not isinstance(target,str) or not 1<=len(target)<=300 or not isinstance(name,str) or not 1<=len(name)<=300:raise AccountError('Invalid follow target')
        if body.get('remove') is True:
            return request('/rest/v1/filmgraph_follows?'+urlencode({'user_id':'eq.'+uid,'target_kind':'eq.'+kind,'target_id':'eq.'+target}),method='DELETE',token=token)
        return request('/rest/v1/filmgraph_follows?on_conflict=user_id,target_kind,target_id',{'user_id':uid,'target_kind':kind,'target_id':target,'target_name':name,'alerts':True},token=token)
    if action=='delete_account':
        if body.get('confirmation')!='DELETE MY ACCOUNT':raise AccountError('Type DELETE MY ACCOUNT to confirm')
        return gateway({'action':'delete_account','access_token':token})
    raise AccountError('Unknown private operation',404)
