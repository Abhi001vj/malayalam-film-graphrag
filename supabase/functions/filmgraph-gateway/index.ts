// The gateway checks a server HMAC before consuming quotas or handling private operations.
// The deployment script injects a private random secret; no service key reaches browsers.
import { createClient } from 'npm:@supabase/supabase-js@2.118.0';
const secret = Deno.env.get('FILMGRAPH_GATEWAY_SECRET') || '__INJECT_AT_DEPLOY__';
const db = createClient(Deno.env.get('SUPABASE_URL')!, Deno.env.get('SUPABASE_SERVICE_ROLE_KEY')!, {auth:{persistSession:false,autoRefreshToken:false}});
const json=(body:unknown,status=200)=>new Response(JSON.stringify(body),{status,headers:{'Content-Type':'application/json','Cache-Control':'no-store'}});
Deno.serve(async req=>{
 try{
  if(secret.length<32)return json({error:'Gateway secret is not configured'},503);
  if(req.method!=='POST')return json({error:'Method not allowed'},405);
  const text=await req.text();if(text.length>16384)return json({error:'Request too large'},413);
  const signature=req.headers.get('x-filmgraph-signature')||'';
  const key=await crypto.subtle.importKey('raw',new TextEncoder().encode(secret),{name:'HMAC',hash:'SHA-256'},false,['verify']);
  if(!/^[a-f0-9]{64}$/.test(signature))return json({error:'Unauthorized gateway'},401);
  const bytes=Uint8Array.from(signature.match(/../g)!.map(x=>parseInt(x,16)));
  if(!await crypto.subtle.verify('HMAC',key,bytes,new TextEncoder().encode(text)))return json({error:'Unauthorized gateway'},401);
  const body=JSON.parse(text);
  if(typeof body.timestamp!=='number'||Math.abs(Date.now()/1000-body.timestamp)>90)return json({error:'Expired request'},401);
  let user=null;
  if(body.access_token){const {data,error}=await db.auth.getUser(body.access_token);if(error||!data.user||data.user.is_anonymous)return json({error:'Sign in again'},401);user=data.user;}
  if(body.action==='identity')return json({user:user?{id:user.id,email:user.email}:null});
  if(body.action==='quota'){
   const subject=user?'user:'+user.id:'guest:'+String(body.guest_key);
   if(!user&&!/^[a-f0-9]{64}$/.test(body.guest_key||''))return json({error:'Invalid guest key'},400);
   if(!/^[a-f0-9]{64}$/.test(body.fingerprint||''))return json({error:'Invalid query fingerprint'},400);
   const {data,error}=await db.rpc('filmgraph_consume_quota',{p_subject:subject,p_user_id:user?.id||null,p_fingerprint:body.fingerprint});
   if(error){console.error(error.code);return json({error:'Quota service unavailable'},503)}
   return json({...data,user:user?{id:user.id,email:user.email}:null});
  }
  if(body.action==='feedback'){
   // Ratings and corrections; the server validated the item. Reporter is pseudonymous (user id or HMAC of IP).
   const reporter=user?'user:'+user.id:'guest:'+String(body.guest_key);
   if(!user&&!/^[a-f0-9]{64}$/.test(body.guest_key||''))return json({error:'Invalid guest key'},400);
   const {data,error}=await db.rpc('filmgraph_submit_feedback',{p_reporter:reporter,p_item:body.item});
   if(error){console.error(error.code);return json({error:'Feedback service unavailable'},503)}
   if(!data.stored)return json({error:data.error},429);
   return json(data);
  }
  if(body.action==='delete_account'){
   if(!user)return json({error:'Sign in first'},401);
   const {error:signoutError}=await db.auth.admin.signOut(body.access_token,'global');
   if(signoutError)return json({error:'Could not revoke account sessions'},503);
   const {error}=await db.auth.admin.deleteUser(user.id);if(error)return json({error:'Could not delete account'},503);
   return json({deleted:true});
  }
  return json({error:'Unknown action'},400);
 }catch(error){console.error(error instanceof Error?error.name:'Gateway error');return json({error:'Gateway request failed'},500)}
});
