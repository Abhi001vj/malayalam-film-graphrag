-- Ratings and corrections-with-proof queue (private; written only through the signed gateway).
create table if not exists filmgraph_private.feedback (
 id bigint generated always as identity primary key,
 created_at timestamptz not null default now(),
 kind text not null check (kind in ('rating','correction')),
 rating smallint check (rating in (-1,1)),
 question text not null check (length(question) <= 500),
 intent text, answer_excerpt text, entities jsonb, film_ids jsonb,
 claimed_value text, correction text check (length(correction) <= 2000),
 proof_url text check (proof_url is null or proof_url ~ '^https://'),
 reporter text not null,
 status text not null default 'pending' check (status in ('pending','accepted','rejected','recorded')),
 reviewer_note text
);
create index if not exists feedback_reporter_time on filmgraph_private.feedback (reporter, created_at desc);
create index if not exists feedback_queue on filmgraph_private.feedback (kind, status, created_at desc);
grant all on filmgraph_private.feedback to service_role;

create or replace function public.filmgraph_submit_feedback(p_reporter text, p_item jsonb)
returns jsonb language plpgsql security invoker set search_path='' as $$
declare recent int; new_id bigint; k text := p_item->>'kind';
begin
 if length(p_reporter) > 200 or k not in ('rating','correction') then raise exception 'Invalid feedback'; end if;
 select count(*) into recent from filmgraph_private.feedback where reporter = p_reporter and created_at > now() - interval '1 hour';
 if recent >= 30 then return jsonb_build_object('stored', false, 'error', 'Too much feedback in the last hour'); end if;
 insert into filmgraph_private.feedback(kind, rating, question, intent, answer_excerpt, entities, film_ids, claimed_value, correction, proof_url, reporter, status)
 values (k, nullif(p_item->>'rating','')::smallint, left(p_item->>'question', 500), left(p_item->>'intent', 40), left(p_item->>'answer_excerpt', 600),
         p_item->'entities', p_item->'film_ids', left(p_item->>'claimed_value', 100), left(p_item->>'correction', 2000),
         nullif(p_item->>'proof_url',''), p_reporter, case when k = 'correction' then 'pending' else 'recorded' end)
 returning id into new_id;
 return jsonb_build_object('stored', true, 'id', new_id, 'status', case when k = 'correction' then 'pending' else 'recorded' end);
end $$;
revoke all on function public.filmgraph_submit_feedback(text, jsonb) from public, anon, authenticated;
grant execute on function public.filmgraph_submit_feedback(text, jsonb) to service_role;
