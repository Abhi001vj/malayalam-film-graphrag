-- Run only in the dedicated FilmGraph project. User tables are private through RLS.
create schema if not exists filmgraph_private;
revoke all on schema filmgraph_private from public, anon, authenticated;
grant usage on schema filmgraph_private to service_role;

create table if not exists public.filmgraph_library (
 user_id uuid not null references auth.users(id) on delete cascade,
 film_id text not null check (length(film_id)<=300),
 title text not null check (length(title)<=300),
 favorite boolean not null default false,
 watchlisted boolean not null default false,
 watched boolean not null default false,
 rating smallint check (rating between 1 and 10),
 updated_at timestamptz not null default now(),
 primary key(user_id,film_id)
);
create table if not exists public.filmgraph_follows (
 user_id uuid not null references auth.users(id) on delete cascade,
 target_kind text not null check(target_kind in ('person','film','company')),
 target_id text not null check(length(target_id)<=300),
 target_name text not null check(length(target_name)<=300),
 alerts boolean not null default true,
 created_at timestamptz not null default now(),
 primary key(user_id,target_kind,target_id)
);
create table if not exists public.filmgraph_preferences (
 user_id uuid primary key references auth.users(id) on delete cascade,
 personalization boolean not null default false,
 analytics boolean not null default false,
 email_alerts boolean not null default false,
 terms_version text not null default '2026-10-22-draft',
 privacy_version text not null default '2026-10-22-draft',
 consent_at timestamptz not null default now()
);
create table if not exists public.filmgraph_notifications (
 id uuid primary key default gen_random_uuid(),
 user_id uuid not null references auth.users(id) on delete cascade,
 event_key text not null,
 title text not null,
 url text,
 read boolean not null default false,
 created_at timestamptz not null default now(),
 unique(user_id,event_key)
);
create index if not exists filmgraph_notifications_user_date on public.filmgraph_notifications(user_id,created_at desc);

alter table public.filmgraph_library enable row level security;
alter table public.filmgraph_follows enable row level security;
alter table public.filmgraph_preferences enable row level security;
alter table public.filmgraph_notifications enable row level security;

-- Each policy is installed once in a fresh dedicated project; no unrelated policies are changed.
create policy own_library on public.filmgraph_library for all to authenticated
 using((select auth.uid())=user_id) with check((select auth.uid())=user_id);
create policy own_follows on public.filmgraph_follows for all to authenticated
 using((select auth.uid())=user_id) with check((select auth.uid())=user_id);
create policy own_preferences on public.filmgraph_preferences for all to authenticated
 using((select auth.uid())=user_id) with check((select auth.uid())=user_id);
create policy read_own_notifications on public.filmgraph_notifications for select to authenticated
 using((select auth.uid())=user_id);
create policy update_own_notifications on public.filmgraph_notifications for update to authenticated
 using((select auth.uid())=user_id) with check((select auth.uid())=user_id);
create policy delete_own_notifications on public.filmgraph_notifications for delete to authenticated
 using((select auth.uid())=user_id);
revoke all on public.filmgraph_library,public.filmgraph_follows,public.filmgraph_preferences,public.filmgraph_notifications from anon;
grant select,insert,update,delete on public.filmgraph_library,public.filmgraph_follows,public.filmgraph_preferences to authenticated;
grant select,update,delete on public.filmgraph_notifications to authenticated;
grant all on public.filmgraph_library,public.filmgraph_follows,public.filmgraph_preferences,public.filmgraph_notifications to service_role;

create table if not exists filmgraph_private.query_usage (
 subject text not null, day date not null default current_date,
 used integer not null default 0 check(used>=0),
 primary key(subject,day)
);
create table if not exists filmgraph_private.query_seen (
 subject text not null,day date not null default current_date,
 fingerprint text not null,
 primary key(subject,day,fingerprint)
);
grant all on filmgraph_private.query_usage,filmgraph_private.query_seen to service_role;

-- Plans: guest (per IP) 5/min + 20/day, member 20/min + 200/day, paid 60/min and no daily cap.
create table if not exists filmgraph_private.plans (
 user_id uuid primary key references auth.users(id) on delete cascade,
 tier text not null default 'member' check (tier in ('member','paid')),
 updated_at timestamptz not null default now()
);
create table if not exists filmgraph_private.query_minute (
 subject text not null, minute timestamptz not null, used integer not null default 0,
 primary key(subject,minute)
);
grant all on filmgraph_private.plans,filmgraph_private.query_minute to service_role;

-- Only the authenticated server gateway can invoke this atomic, invoker-security function.
-- Every request counts toward the per-minute burst limit; only new questions count toward the daily cap.
-- Old query_minute rows are harmless; prune them with a scheduled job (pg_cron) if they grow.
create or replace function public.filmgraph_consume_quota(p_subject text,p_user_id uuid,p_fingerprint text)
returns jsonb language plpgsql security invoker set search_path='' as $$
declare
 tier text := case when p_user_id is null then 'guest'
   else coalesce((select p.tier from filmgraph_private.plans p where p.user_id=p_user_id),'member') end;
 minute_limit int := case tier when 'guest' then 5 when 'member' then 20 else 60 end;
 daily_limit int := case tier when 'guest' then 20 when 'member' then 200 else null end;
 this_minute timestamptz := date_trunc('minute', now());
 minute_used int; current_used int; is_new boolean;
begin
 if length(p_subject)>200 or p_fingerprint !~ '^[a-f0-9]{64}$' then raise exception 'Invalid quota identity'; end if;
 insert into filmgraph_private.query_minute(subject,minute,used) values(p_subject,this_minute,1)
   on conflict(subject,minute) do update set used=filmgraph_private.query_minute.used+1 returning used into minute_used;
 if minute_used > minute_limit then
  return jsonb_build_object('allowed',false,'reason','minute','tier',tier,'limit_per_minute',minute_limit,'retry_after',60-extract(second from now())::int,
    'limit',daily_limit,'remaining',null,'reset','next minute');
 end if;
 insert into filmgraph_private.query_usage(subject,day) values(p_subject,current_date) on conflict do nothing;
 select used into current_used from filmgraph_private.query_usage where subject=p_subject and day=current_date for update;
 select not exists(select 1 from filmgraph_private.query_seen where subject=p_subject and day=current_date and fingerprint=p_fingerprint) into is_new;
 if is_new and daily_limit is not null and current_used>=daily_limit then
  return jsonb_build_object('allowed',false,'reason','day','tier',tier,'used',current_used,'limit',daily_limit,'remaining',0,'reset','00:00 UTC');
 end if;
 if is_new then
  insert into filmgraph_private.query_seen(subject,day,fingerprint) values(p_subject,current_date,p_fingerprint);
  update filmgraph_private.query_usage set used=used+1 where subject=p_subject and day=current_date returning used into current_used;
 end if;
 return jsonb_build_object('allowed',true,'tier',tier,'used',current_used,'limit',daily_limit,'limit_per_minute',minute_limit,
   'remaining',case when daily_limit is null then null else daily_limit-current_used end,'reset','00:00 UTC');
end $$;
revoke all on function public.filmgraph_consume_quota(text,uuid,text) from public,anon,authenticated;
grant execute on function public.filmgraph_consume_quota(text,uuid,text) to service_role;
