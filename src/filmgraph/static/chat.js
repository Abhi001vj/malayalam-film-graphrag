'use strict';
// GraphRAG chat: POST /api/chat, render answer + cards + trace, push the answer subgraph into the graph view.
// Loaded after app.js; reuses its globals ($, el, state, addMessage, drawGraph, openFilmById, link, colors).
const legacyQuery = runQuery;
const chatState = {context: null, pending: false};
Object.assign(colors, {music_director: '#bc9bb0', second_unit_director: '#7a9490', editor: '#8baba5', producer: '#a3b1a0',
  entity: '#c49a7a', collaborated: '#90b18b'});

function md(text) {
  const esc = s => s.replace(/[&<>"]/g, c => ({'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;'}[c]));
  const inline = s => esc(s).replace(/\*\*(.+?)\*\*/g, '<strong>$1</strong>').replace(/(^|[\s(])[*_](.+?)[*_](?=[\s).,;:!?]|$)/g, '$1<em>$2</em>')
    .replace(/`([^`]+)`/g, '<code>$1</code>').replace(/&lt;sub&gt;(.+?)&lt;\/sub&gt;/g, '<small>$1</small>').replace(/\[(c\d{3}|\d)\]/g, '<sup>[$1]</sup>');
  const out = []; let list = null;
  for (const line of text.split('\n')) {
    const item = line.match(/^\s*(?:[-*]|\d+\.)\s+(.*)$/);
    if (item) { if (!list) { list = []; out.push(list); } list.push(inline(item[1])); continue; }
    if (list && /^\s{2,}/.test(line)) { list[list.length - 1] += '<br>' + inline(line.trim()); continue; }
    list = null;
    if (line.startsWith('> ')) out.push('<blockquote>' + inline(line.slice(2)) + '</blockquote>');
    else if (line.trim()) out.push('<p>' + inline(line) + '</p>');
  }
  return out.map(x => Array.isArray(x) ? '<ul>' + x.map(i => '<li>' + i + '</li>').join('') + '</ul>' : x).join('');
}

function traceView(data) {
  const box = el('details', 'trace');
  const nlu = data.trace.find(s => s.stage === 'nlu') || {};
  box.append(el('summary', '', `How this was answered · ${data.intent || 'unknown'} · ${data.method} ${Math.round((data.confidence || 0) * 100)}% · ${data.backend} · ${Math.round(data.total_ms)} ms`));
  const chips = el('div', 'entity-chips');
  for (const e of data.entities || []) chips.append(el('span', 'chip chip-' + e.type.toLowerCase(), `${e.type} · ${e.name}${e.roles?.length ? ' · ' + e.roles.join('/') : ''}${e.corrected_from ? ' (from “' + e.corrected_from + '”)' : ''}`));
  for (const [k, v] of Object.entries(data.slots || {})) chips.append(el('span', 'chip', `${k} = ${v}`));
  box.append(chips);
  if (nlu.delexicalised) box.append(el('div', 'trace-line', 'Delexicalised: ' + nlu.delexicalised));
  if (nlu.classifier_top3) box.append(el('div', 'trace-line', 'Classifier: ' + nlu.classifier_top3.map(([k, p]) => `${k} ${p}`).join(' · ')));
  if (nlu.rule) box.append(el('div', 'trace-line', 'Rule fired: ' + nlu.rule));
  if (nlu.llm) box.append(el('div', 'trace-line', `LLM fallback (${nlu.llm.model}, ${nlu.llm.ms} ms): ` + JSON.stringify(nlu.llm.output)));
  for (const s of data.trace) {
    if (s.stage === 'nlu') continue;
    const row = el('div', 'trace-step');
    row.append(el('strong', '', `${s.stage}${s.template ? ' · ' + s.template : ''}${s.mode ? ' · ' + s.mode : ''} — ${s.ms} ms`));
    const facts = Object.entries(s).filter(([k]) => !['stage', 'ms', 'query', 'template', 'mode'].includes(k)).map(([k, v]) => `${k}: ${typeof v === 'object' ? JSON.stringify(v) : v}`).join(' · ');
    if (facts) row.append(el('div', 'trace-line', facts));
    if (s.query) row.append(el('pre', 'query', s.query));
    box.append(row);
  }
  return box;
}

function cardView(card) {
  const c = el('article', 'film-card');
  c.append(el('div', 'film-number', (card.year || 'Year ?') + (card.imdb_rating ? ' · IMDb ' + card.imdb_rating : '')), el('h3', '', card.title));
  if (card.evidence) {
    const ev = el('div', 'evidence ' + (card.evidence.confirmed ? 'ok' : 'weak'),
      (card.evidence.confirmed ? '✓ Confirmed on Wikipedia' : '⚠ Weak evidence') + ' · ' +
      Object.entries(card.evidence.people).map(([n, v]) => `${n}: ${v.roles.join('/') || 'song'} (${v.sources.join(', ') || '?'})`).join(' · '));
    c.append(ev);
  }
  const people = [card.directors?.length ? 'Dir. ' + card.directors.join(', ') : '', card.music?.length ? 'Music ' + card.music.join(', ') : '', card.cast?.length ? card.cast.join(', ') : ''].filter(Boolean);
  c.append(el('div', 'film-people', people.join('\n')));
  if (card.plot) c.append(el('p', 'film-plot', card.plot.slice(0, 180) + (card.plot.length > 180 ? '…' : '')));
  const links = el('div', 'card-links');
  for (const l of card.links || []) { const a = link(l.label, l.url, l.verified ? '' : 'unverified'); a.title = l.verified ? 'Source-linked' : 'Search link (availability not verified)'; links.append(a); }
  c.append(links);
  if (window.FilmGraphAccount) c.append(window.FilmGraphAccount.controls({id: card.id, title: card.title, year: card.year}));
  const open = el('button', 'film-open', 'Credits, songs & sources →'); open.onclick = e => { e.stopPropagation(); openFilmById(card.id); }; c.append(open);
  c.addEventListener('click', e => { if (!e.target.closest('a,button')) { state.network?.selectNodes([card.id].filter(id => state.network.body.data.nodes.get(id))); state.network?.focus(card.id, {scale: 1.1, animation: true}); } });
  return c;
}

async function chat(question, overrides) {
  if (chatState.pending) return;
  chatState.pending = true; $('#send').disabled = true;
  if (!overrides) addMessage(question, 'user');
  const pending = addMessage('Reading the graph…', 'assistant pending');
  try {
    const response = await fetch('/api/chat', {method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({question, context: chatState.context, overrides: overrides || null,
        use_llm: $('#use-llm')?.checked ?? true, backend: $('#backend')?.value || 'auto'})});
    const data = await response.json();
    if (data.quota) $('#quota-status').textContent = data.quota.remaining + ' / ' + data.quota.limit + ' searches left · resets 00:00 UTC';
    if (!response.ok) throw Error(data.error || 'Request failed');
    pending.remove();
    chatState.context = data.context;
    const m = el('div', 'message assistant'); const body = el('div', 'answer'); body.innerHTML = md(data.answer); m.append(body);
    if (data.clarify) {
      const opts = el('div', 'suggestions');
      for (const o of data.clarify.options) { const b = el('button', '', o.name + (o.films ? ` · ${o.films} films` : '')); b.onclick = () => chat(question, {[data.clarify.text]: o.id}); opts.append(b); }
      m.append(opts);
    }
    for (const card of (data.cards || []).slice(0, 3)) {
      const row = el('div', 'chat-film'); const b = el('button', '', card.title + ' · ' + (card.year || '')); b.onclick = () => openFilmById(card.id); row.append(b);
      for (const l of card.links.slice(0, 4)) row.append(link(l.label, l.url, l.verified ? '' : 'unverified'));
      m.append(row);
    }
    if (data.sources?.length) { const s = el('details', 'sources'); s.append(el('summary', '', 'Sources (' + data.sources.length + ')')); for (const src of data.sources) { const p = el('p', '', `[${src.n}] ${src.film}: ${src.text.slice(0, 220)}… `); p.append(link('page', src.url)); s.append(p); } m.append(s); }
    if (data.suggestions?.length) { const sug = el('div', 'suggestions'); for (const q of data.suggestions) { const b = el('button', '', q + ' ↗'); b.onclick = () => chat(q); sug.append(b); } m.append(sug); }
    m.append(traceView(data));
    $('#chat-log').append(m); $('#chat-log').scrollTop = $('#chat-log').scrollHeight;
    if (data.graph?.nodes?.length) {
      state.graph = {...data.graph, note: `${data.graph.nodes.length} nodes from the ${data.intent} answer`};
      $('#query-title').textContent = (data.entities || []).filter(e => ['PERSON', 'FILM', 'PLACE'].includes(e.type)).map(e => e.name).join(' × ') || question;
      $('#query-description').textContent = (data.total != null ? data.total.toLocaleString() + ' films · ' : '') + data.intent.replace(/_/g, ' ') + ' · via ' + data.backend;
      drawGraph();
      const ids = (data.graph.highlight || []).filter(id => state.network?.body.data.nodes.get(id));
      if (ids.length) state.network.selectNodes(ids);
    }
    if (data.cards?.length) {
      $('#results').replaceChildren(...data.cards.map(cardView));
      $('#result-count').textContent = (data.total ?? data.cards.length).toLocaleString();
      $('#load-more').classList.add('hidden');
    }
    const url = new URL(location.href); url.searchParams.set('q', question); history.replaceState(null, '', url);
  } catch (error) { pending.remove(); addMessage(error.message, 'assistant error'); }
  finally { chatState.pending = false; $('#send').disabled = false; }
}

// Chat submissions and suggestion chips go through the GraphRAG pipeline; filters/paging keep the catalogue search.
// Every question (typed, suggestion chip, ?q= link, page load) goes through the GraphRAG pipeline;
// only "Show more films" paging keeps the old catalogue search.
runQuery = (q, opts = {}) => opts.append ? legacyQuery(q, opts) : chat(q || 'help');
const foot = $('.chat-foot');
if (foot) {
  const controls = el('div', 'chat-controls');
  controls.innerHTML = '<label><input type="checkbox" id="use-llm" checked> Local LLM</label> <label>Graph <select id="backend"><option value="auto">auto</option><option value="neo4j">Neo4j</option><option value="sqlite">SQLite mirror</option></select></label> <button type="button" class="quiet" id="new-chat">New chat</button>';
  foot.prepend(controls);
  $('#new-chat').onclick = () => { chatState.context = null; addMessage('Started a new conversation; earlier names will not carry over.', 'assistant'); };
}
