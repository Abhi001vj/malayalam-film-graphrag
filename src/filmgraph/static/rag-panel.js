/* "GraphRAG vs typical RAG" sidebar: turns the answer trace into named GraphRAG steps and sets them beside
   what plain chunk retrieval (BM25 over the same Wikipedia text) returns for the same question. */
'use strict';
window.FilmGraphRAGPanel = (() => {
  let last = null;
  const esc = s => String(s ?? '').replace(/[&<>"']/g, c => ({'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'}[c]));
  const num = v => (typeof v === 'number' ? v : Number(v));

  function steps(data) {
    const out = [];
    const tr = data.trace || [];
    const find = s => tr.filter(t => t.stage === s);
    const nlu = find('nlu')[0];
    if (nlu) {
      const typed = String(data.question || '').toLowerCase().replace(/[^\p{L}\p{N}]+/gu, '');
      const ents = (nlu.entities || []).map(e => {
        const plain = String(e.name || '').toLowerCase().replace(/[^\p{L}\p{N}]+/gu, '');
        const how = e.corrected_from ? ` ← “${e.corrected_from}”`
          : (plain && !typed.includes(plain) ? ' ← expanded from a nickname or spelling in your question' : '');
        return `<b>${esc(e.name)}</b> <span class="rp-tag">${esc(e.type)}</span>${esc(how)}`;
      });
      const slots = Object.entries(nlu.slots || data.slots || {}).filter(([, v]) => v != null && v !== '').map(([k, v]) => `${k}=${v}`);
      out.push({title: 'Query understanding & expansion', concept: 'Entity linking, nickname/spelling expansion, slot filling',
        body: (ents.length ? 'Linked to graph nodes: ' + ents.join(' · ') : 'No named entities found') +
          (slots.length ? `<br>Filters: <code>${esc(slots.join(', '))}</code>` : '')});
      out.push({title: 'Intent routing', concept: 'Rules → classifier → LLM fallback picks the retrieval plan',
        body: `<code>${esc(nlu.intent || data.intent)}</code> via ${esc(nlu.method || data.method)} · confidence ${Math.round(num(nlu.confidence || data.confidence) * 100)}%`});
    }
    const mem = find('dialogue_state')[0];
    if (mem) out.push({title: 'Conversation memory', concept: 'Earlier names and filters carried into this turn',
      body: esc(Object.entries(mem).filter(([k]) => !['stage', 'ms'].includes(k)).map(([k, v]) => `${k}: ${typeof v === 'object' ? JSON.stringify(v) : v}`).join(' · ').slice(0, 300))});
    for (const s of [...find('default_roles'), ...find('attribute_constraints'), ...find('graph_prefilter')]) {
      out.push({title: 'Query planning rule', concept: 'Domain rules shape the graph query',
        body: esc(s.rule || Object.entries(s).filter(([k]) => !['stage', 'ms'].includes(k)).map(([k, v]) => `${k}: ${JSON.stringify(v)}`).join(' · ')).slice(0, 300)});
    }
    const tools = tr.filter(t => ['graph_query', 'retrieval', 'community_search'].includes(t.stage));
    if (tools.length) out.push({title: `Tool calls (${tools.length})`, concept: 'Parameterised graph queries and retrieval, never generated SQL',
      body: tools.map(t => {
        if (t.stage === 'graph_query') return `<div class="rp-call"><b>${esc(t.template || 'graph query')}</b>${t.mode ? ' · ' + esc(t.mode) : ''} → <b>${esc(t.rows)}</b> rows · ${esc(t.backend)} · ${num(t.ms).toFixed(1)} ms` +
          (t.query ? `<details><summary>query</summary><pre>${esc(t.query)}</pre>${t.params ? `<small>params ${esc(JSON.stringify(t.params))}</small>` : ''}</details>` : '') + '</div>';
        return `<div class="rp-call"><b>${esc(t.stage.replace('_', ' '))}</b> · ${esc(Object.entries(t).filter(([k]) => !['stage', 'ms'].includes(k)).map(([k, v]) => `${k}: ${typeof v === 'object' ? JSON.stringify(v).slice(0, 80) : v}`).join(' · ')).slice(0, 220)}</div>`;
      }).join('')});
    const checks = tr.filter(t => ['appearance_check', 'provenance'].includes(t.stage));
    if (checks.length) out.push({title: 'Evidence checks', concept: 'Every credit checked against its source; weak claims set aside',
      body: checks.map(c => c.stage === 'appearance_check'
        ? `On screen together: <b>${esc(c.costar)}</b> full roles, <b>${esc(c.cameo)}</b> cameo/self; excluded voice/narration <b>${esc(c.offscreen)}</b>, unconfirmed <b>${esc(c.unconfirmed)}</b>`
        : `Confirmed by Wikipedia: <b>${esc(c.confirmed)}</b> · weak evidence: <b>${esc(c.weak)}</b>`).join('<br>')});
    const gen = find('generation')[0];
    out.push({title: 'Answer composition', concept: gen ? 'LLM narrates retrieved facts only' : 'Template filled from query results: no LLM, nothing invented',
      body: gen ? esc(Object.entries(gen).filter(([k]) => !['stage', 'ms'].includes(k)).map(([k, v]) => `${k}: ${v}`).join(' · ')).slice(0, 200)
        : `${(data.cards || []).length} film cards and a ${data.graph?.nodes?.length || 0}-node answer subgraph, in ${Math.round(data.total_ms || 0)} ms total`});
    return out;
  }

  function contrast(data) {
    const graphFilms = new Set((data.cards || []).map(c => c.title));
    const base = data.baseline?.passages || [];
    const baseFilms = [...new Set(base.map(p => p.film))];
    const overlap = baseFilms.filter(f => graphFilms.has(f)).length;
    const total = data.confirmed_total ?? data.total;
    const lines = [];
    if (total != null && ['collaboration', 'filmography', 'songs', 'top_collaborators', 'by_attribute'].includes(data.intent))
      lines.push(`GraphRAG counted <b>${esc(total)}</b> results across the whole graph. The typical RAG retriever sees only <b>${base.length}</b> passages about <b>${baseFilms.length}</b> film${baseFilms.length === 1 ? '' : 's'}, so an LLM given those could not produce this count.`);
    if (graphFilms.size) lines.push(`${overlap} of the ${baseFilms.length} films the passages mention appear in the GraphRAG answer.`);
    if (data.intent === 'connection_path') lines.push('A connection path needs multi-hop traversal; isolated passages do not contain the chain.');
    return lines;
  }

  function render(question, data) {
    last = {question, data};
    const box = document.querySelector('#rag-drawer-body');
    if (!box) return;
    const st = steps(data);
    const base = data.baseline?.passages || [];
    box.innerHTML = `
      <p class="rp-q">“${esc(question)}”</p>
      <section class="rp-col graph"><h3>GraphRAG <small>this app</small></h3>
        <ol class="rp-steps">${st.map(s => `<li><div class="rp-step-title">${esc(s.title)}</div><div class="rp-concept">${esc(s.concept)}</div><div class="rp-body">${s.body}</div></li>`).join('')}</ol>
      </section>
      <section class="rp-col plain"><h3>Typical RAG <small>${esc(data.baseline?.method || 'chunk retrieval')}</small></h3>
        ${base.length ? `<ol class="rp-passages">${base.map(p => `<li><b>${esc(p.film)}</b> ${p.year ? '(' + esc(p.year) + ')' : ''} <span class="rp-tag">${esc(p.kind)}</span><br><span>${esc(p.text)}…</span></li>`).join('')}</ol>`
          : '<p class="rp-empty">No passage matched the raw question words.</p>'}
        ${contrast(data).map(l => `<p class="rp-contrast">${l}</p>`).join('')}
      </section>
      <section class="rp-explain"><h3>What changes with GraphRAG</h3>
        <table><thead><tr><th></th><th>Typical RAG</th><th>GraphRAG here</th></tr></thead><tbody>
          <tr><td>Unit retrieved</td><td>Text chunks similar to the question</td><td>Entities, typed relations and the passages linked to them</td></tr>
          <tr><td>Names</td><td>Exact words only</td><td>Linked to graph nodes, with nicknames and misspellings expanded</td></tr>
          <tr><td>Counting, “together”, “most”</td><td>Guessed from a few passages</td><td>Computed over every edge in the graph</td></tr>
          <tr><td>Multi-hop</td><td>Only if one passage holds the chain</td><td>Path queries over the graph</td></tr>
          <tr><td>Evidence</td><td>Passage citations</td><td>Per-credit sources, cameo/voice checks, weak claims set aside</td></tr>
          <tr><td>Follow-ups</td><td>Re-retrieve from scratch</td><td>Memory carries people and filters forward</td></tr>
        </tbody></table>
      </section>
      ${LIMITS}`;
  }

  const LIMITS = `<section class="rp-limits"><h3>Constraints & limitations (hosted demo)</h3><ul>
    <li><b>No server LLM.</b> Answers are templates filled from graph queries, so the demo shows retrieval and reasoning over the graph, not free-form generation. The local build adds Gemma 4 (Ollama) for narration and the NLU fallback; in-browser Gemma is experimental.</li>
    <li><b>Plot search is keyword-only here (BM25).</b> Locally it fuses BM25 with vector search (hit@5 76% → 86% on the 29-question plot set).</li>
    <li><b>Coverage follows Wikipedia/Wikidata.</b> IMDb data is used only locally and never deployed, so hosted counts can be a little lower, and a few spelling variants (e.g. G. Venkitaraman / Venkittaraman) stay separate without IMDb IDs.</li>
    <li><b>Relations from prose are partial.</b> Remakes, adaptations and locations come from hyperlinked sentences only (precision over recall).</li>
    <li><b>Evaluation is a hand-built regression set</b> (50 structured + 29 plot questions), not a public benchmark.</li>
    <li><b>Demo limits:</b> 10 questions/min and 40 distinct questions/day per visitor; no accounts or saved feedback on the public demo.</li>
  </ul></section>`;

  function open() { document.querySelector('#rag-drawer')?.classList.add('open'); if (!last) document.querySelector('#rag-drawer-body').innerHTML = '<p class="rp-empty">Ask a question to see how GraphRAG answers it, step by step, next to typical RAG.</p>'; }
  function close() { document.querySelector('#rag-drawer')?.classList.remove('open'); }

  document.addEventListener('DOMContentLoaded', () => {
    const drawer = document.createElement('aside');
    drawer.id = 'rag-drawer'; drawer.setAttribute('aria-label', 'GraphRAG versus typical RAG');
    drawer.innerHTML = '<div class="rp-head"><div><div class="section-label">UNDER THE HOOD</div><h2>GraphRAG vs typical RAG</h2></div><button class="dialog-close" id="rag-close" aria-label="Close">×</button></div><div id="rag-drawer-body"></div>';
    document.body.append(drawer);
    document.querySelector('#rag-close').onclick = close;
    document.addEventListener('keydown', e => { if (e.key === 'Escape') close(); });
    const heading = document.querySelector('.explorer-heading');
    if (heading) { const b = document.createElement('button'); b.className = 'quiet rp-open'; b.id = 'rag-open'; b.textContent = 'GraphRAG vs RAG ↗'; b.onclick = open; heading.append(b); }
  });
  return {render, open, close};
})();
