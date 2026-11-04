/* Smart answers: optional retrieval-augmented generation with Gemma running in the visitor's browser (WebLLM).
   The server still retrieves everything (graph query, cards, passages). Gemma only rewrites those facts as prose,
   and a grounding check withholds any answer that states a number the retrieved facts do not contain. */
'use strict';
window.FilmGraphSmart = (() => {
  const WEBLLM = 'https://esm.sh/@mlc-ai/web-llm@0.2.87';
  const MODELS = {
    'gemma-4-E2B-it-q4f16_1-MLC': 'Gemma 4 E2B · ~2 GB download · recommended',
    'gemma3-1b-it-q4f16_1-MLC': 'Gemma 3 1B · ~0.7 GB · for low-memory devices',
  };
  const KEY = 'filmgraph.smart';
  let engine = null, loading = null, loadedId = null;
  const saved = () => { try { return JSON.parse(localStorage.getItem(KEY) || '{}'); } catch { return {}; } };
  const save = v => localStorage.setItem(KEY, JSON.stringify({...saved(), ...v}));

  const api = {
    MODELS,
    supported: () => !!navigator.gpu,
    enabled: () => !!saved().enabled && !!navigator.gpu,
    modelId: () => (MODELS[saved().model] ? saved().model : 'gemma-4-E2B-it-q4f16_1-MLC'),
    ready: () => !!engine && loadedId === api.modelId(),
    setEnabled(on) { save({enabled: !!on}); },
    setModel(id) { if (MODELS[id]) save({model: id}); },

    async load(onProgress) {
      const id = api.modelId();
      if (api.ready()) return engine;
      if (loading) return loading;
      loading = (async () => {
        if (engine) { await engine.unload(); engine = null; }
        const {CreateMLCEngine} = await import(WEBLLM);
        engine = await CreateMLCEngine(id, {initProgressCallback: p => onProgress && onProgress(p.text, p.progress)});
        loadedId = id;
        return engine;
      })();
      try { return await loading; } finally { loading = null; }
    },

    /* Facts the model may use: the graph answer, up to 8 film cards and the cited passages. */
    context(question, data) {
      const cards = (data.cards || []).slice(0, 8).map((c, i) => {
        const bits = [`${c.title}${c.year ? ' (' + c.year + ')' : ''}`];
        if (c.directors?.length) bits.push('director: ' + c.directors.slice(0, 2).join(', '));
        if (c.cast?.length) bits.push('cast: ' + c.cast.slice(0, 4).join(', '));
        if (c.music?.length) bits.push('music: ' + c.music.slice(0, 2).join(', '));
        return `F${i + 1}. ` + bits.join(' · ');
      });
      const passages = (data.sources || []).slice(0, 5).map(s => `[${s.n}] ${s.film}: ${String(s.text || '').slice(0, 450)}`);
      const graphAnswer = String(data.answer || '').replace(/<[^>]+>/g, '').slice(0, 1800);
      return [`QUESTION: ${question}`, `GRAPH ANSWER (exact, computed from the knowledge graph):\n${graphAnswer}`,
        cards.length ? `FILMS:\n${cards.join('\n')}` : '', passages.length ? `PASSAGES:\n${passages.join('\n')}` : '']
        .filter(Boolean).join('\n\n');
    },

    /* Every number in the generated text must appear in the retrieved facts (counts, years, ranks). */
    check(text, context) {
      const allowed = new Set(context.match(/\d+/g) || []);
      const unknown = [...new Set((text.match(/\d+/g) || []).filter(n => !allowed.has(n)))];
      return {ok: unknown.length === 0, unknown};
    },

    async generate(question, data, onToken) {
      const ctx = api.context(question, data);
      const messages = [
        {role: 'system', content: 'You answer questions about Malayalam cinema using ONLY the facts provided. ' +
          'Write 2-4 natural sentences. Copy every number, year and name exactly from the facts; never add a fact, ' +
          'number, film or person that is not in them. Cite passages as [n] when you use them. ' +
          'If the facts do not answer the question, say so in one sentence.'},
        {role: 'user', content: ctx},
      ];
      const started = performance.now();
      const stream = await engine.chat.completions.create({messages, stream: true, temperature: 0.2, max_tokens: 220});
      let text = '';
      for await (const chunk of stream) {
        text += chunk.choices?.[0]?.delta?.content || '';
        onToken && onToken(text);
      }
      return {text: text.trim(), ms: performance.now() - started, ...api.check(text, ctx), context: ctx};
    },
  };
  return api;
})();
