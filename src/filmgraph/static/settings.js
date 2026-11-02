'use strict';
// Settings dialog (Settings button next to My collection; deep link /#settings).
// Every value starts at the env/built-in default; only values that differ are saved to data/settings.json.
(()=>{
const q=s=>document.querySelector(s);
const make=(tag,cls,text)=>{const n=document.createElement(tag);if(cls)n.className=cls;if(text!==undefined)n.textContent=text;return n};
const fmt=n=>Number(n||0).toLocaleString();
const same=(a,b)=>JSON.stringify(a)===JSON.stringify(b);

const TABS=[
 {id:'paths',label:'Data',intro:'Where the catalogue, vectors and local logs live. Relative paths start from the folder the server runs in.'},
 {id:'models',label:'Models',intro:'Everything runs locally by default. Passwords and API keys stay in .env and are never shown here.'},
 {id:'sources',label:'Sources',intro:'Choose which sources a credit may come from, and which ones are trusted enough to mark an answer ✓ confirmed.'},
 {id:'relations',label:'Relations',intro:'Choose which links in the graph answers may use.'}];
const FIELDS={
 paths:[['db','Catalogue database','SQLite file with the kg_* graph tables',true],['vectors_dir','Vector index','Folder with chunk_ids.npy and chunk_vectors.npy',true],
  ['feedback_db','Feedback queue','Ratings and suggested corrections',true],['accounts_db','Local accounts and quota','Local only, never deployed',true],
  ['traces_log','Answer traces','One JSON line per answer',true],['handover_log','Human handover queue','Tickets for questions passed to a curator',true]],
 models:[['llm_provider','LLM provider','“none” answers from rules, classifier and graph only'],['chat_model','Chat model','NLU fallback, narration and triple relabelling'],
  ['embed_model','Embedding model','Plot search vectors'],['ollama_host','Ollama host','',true],['llm_base_url','OpenAI-compatible URL','Used only when the provider is “openai”',true],
  ['graph_backend','Graph backend','“auto” falls back to the SQLite mirror when Neo4j is empty or down'],['neo4j_uri','Neo4j URI','',true],
  ['neo4j_username','Neo4j user',''],['neo4j_database','Neo4j database','']]};
const SOURCES={wikipedia:['Wikipedia','Cast and crew from each film’s own page'],wikidata:['Wikidata','Structured claims (CC0); often adds cameos and voice roles'],imdb:['IMDb','Non-commercial datasets; local research only, never deployed']};
const RELATION_GROUPS=[
 ['Story and lineage',{REMAKE_OF:'Remake of',REMADE_AS:'Remade as',SEQUEL_TO:'Sequel to',HAS_SEQUEL:'Has sequel',BASED_ON:'Based on',INSPIRED_BY:'Inspired by',REFERENCES:'References'}],
 ['Places',{SHOT_AT:'Shot at',SET_IN:'Set in'}],
 ['Industry',{PRODUCED_BY:'Produced by',DISTRIBUTED_BY:'Distributed by',SCREENED_AT:'Screened at'}],
 ['Awards and careers',{WON_AWARD:'Won award',NOMINATED_FOR:'Nominated for',DEBUTED_IN:'Debut'}],
 ['Weak links',{MENTIONS:'Mentioned in article'}]];
const label=v=>v.replaceAll('_',' ').replace(/^./,c=>c.toUpperCase());

const S=window.FilmGraphSettings={info:null,draft:null,tab:'paths',
 async load(){const r=await fetch('/api/settings');if(!r.ok)throw Error('Settings unavailable');this.info=await r.json();this.applyToChat();return this.info},
 applyToChat(){const m=this.info?.current?.models;if(!m)return;const b=q('#backend');if(b)b.value=m.graph_backend;const l=q('#use-llm');if(l&&m.llm_provider==='none')l.checked=false},
 async open(tab){const d=q('#settings-dialog');if(!d.open)d.showModal();if(!location.hash.startsWith('#settings'))history.replaceState(null,'','#settings');
  try{await this.load();this.draft=structuredClone(this.info.current);if(TABS.some(t=>t.id===tab))this.tab=tab;this.render();q('#settings-body').focus()}catch(e){this.status(e.message,'error')}},
 close(){if(this.dirty().length&&!confirm('Discard unsaved changes?'))return;q('#settings-dialog').close()},
 // keys whose draft value differs from what is currently in effect
 dirty(){const out=[];for(const g of Object.keys(this.draft||{}))for(const k of Object.keys(this.draft[g]))if(!same(this.draft[g][k],this.info.current[g][k]))out.push(g+'.'+k);return out},
 changedFromDefault(g,k){const v=this.draft[g][k];return !same(v,this.info.defaults[g][k])&&v!==''},
 tabGroups(id){return id==='relations'?['relations']:[id]},
 status(text,kind=''){const n=q('#settings-message');n.textContent=text;n.className='settings-status '+kind},
 render(){
  const i=this.info,tabs=q('#settings-tabs'),body=q('#settings-body');
  q('#settings-file').textContent=i.writable?`Changes apply from the next question. Only values that differ from the defaults are saved, to ${i.file}.`:'Read-only on the hosted deployment. Run the app locally to change settings.';
  tabs.replaceChildren(...TABS.map(t=>{const b=make('button',t.id===this.tab?'active':'');b.type='button';b.setAttribute('role','tab');b.setAttribute('aria-selected',t.id===this.tab);
   const n=Object.keys(this.draft[t.id]).filter(k=>this.changedFromDefault(t.id,k)).length;b.append(t.label);if(n){const c=make('span','tab-count',String(n));c.title=n+' changed from default';b.append(c)}
   b.onclick=()=>{this.tab=t.id;this.render()};return b}));
  const tab=TABS.find(t=>t.id===this.tab);body.replaceChildren(make('p','coverage-copy settings-tab-intro',tab.intro));
  if(this.tab==='paths'||this.tab==='models')body.append(...FIELDS[this.tab].map(f=>this.field(this.tab,...f)));
  if(this.tab==='sources')body.append(this.sources());
  if(this.tab==='relations')body.append(...this.relations());
  this.footer()},
 field(g,k,title,hint,mono){
  const i=this.info,key=g+'.'+k,def=i.defaults[g][k],row=make('div','setting-row'+(this.changedFromDefault(g,k)?' changed':''));
  const text=make('div','setting-text');const lab=make('label','setting-label',title);lab.htmlFor='set-'+k;text.append(lab);if(hint)text.append(make('p','setting-hint',hint));
  const ctl=i.options[key]?make('select'):make('input');ctl.id='set-'+k;ctl.className='setting-input'+(mono?' mono':'');
  if(i.options[key])ctl.append(...i.options[key].map(o=>{const op=make('option','',o);op.value=o;return op}));else{ctl.placeholder=def||'Not set';ctl.spellcheck=false;ctl.autocomplete='off'}
  ctl.value=this.draft[g][k];ctl.disabled=!i.writable;
  const meta=make('div','setting-meta'),d=make('span');meta.append(d);
  const describe=()=>{d.replaceChildren();if(!this.changedFromDefault(g,k)){d.textContent=def?'✓ Default':'Not set';return}d.append(def?'Default: ':'Default: not set');if(def)d.append(make('code','',def))};describe();
  const reset=make('button','link-button','Use default');reset.type='button';reset.hidden=!this.changedFromDefault(g,k)||!i.writable;
  reset.onclick=()=>{ctl.value=def;ctl.oninput();ctl.focus()};meta.append(reset);
  ctl.oninput=()=>{this.draft[g][k]=ctl.value.trim();const c=this.changedFromDefault(g,k);row.classList.toggle('changed',c);reset.hidden=!c||!i.writable;describe();this.renderTabCounts();this.footer()};
  const control=make('div','setting-control');control.append(ctl,meta);row.append(text,control);return row},
 renderTabCounts(){q('#settings-tabs').querySelectorAll('button').forEach((b,n)=>{const t=TABS[n],c=Object.keys(this.draft[t.id]).filter(k=>this.changedFromDefault(t.id,k)).length;let s=b.querySelector('.tab-count');if(!c){s?.remove();return}if(!s){s=make('span','tab-count');b.append(s)}s.textContent=c})},
 toggle(g,k,value,on){const list=new Set(this.draft[g][k]);on?list.add(value):list.delete(value);this.draft[g][k]=this.info.options[g+'.'+k].filter(v=>list.has(v));this.render()},
 switchControl(text,checked,onchange){const l=make('label','switch');const c=make('input');c.type='checkbox';c.checked=checked;c.disabled=!this.info.writable;c.onchange=()=>onchange(c.checked);l.append(c,make('span','switch-track'),make('span','switch-label',text));return l},
 sources(){const i=this.info,wrap=make('div','source-cards'),counts=i.counts.sources||{};
  for(const src of i.options['sources.enabled']){const [name,desc]=SOURCES[src]||[src,''],used=this.draft.sources.enabled.includes(src),conf=this.draft.sources.confirming.includes(src);
   const card=make('div','source-card'+(used?'':' off'));
   const head=make('div','source-head');head.append(make('strong','',name),make('span','result-count',fmt(counts[src])+' credits'));
   const changed=used!==i.defaults.sources.enabled.includes(src)||conf!==i.defaults.sources.confirming.includes(src);if(changed)card.classList.add('changed');
   card.append(head,make('p','setting-hint',desc),this.switchControl('Use these credits',used,on=>this.toggle('sources','enabled',src,on)),
    this.switchControl('Counts as ✓ confirmed',conf,on=>this.toggle('sources','confirming',src,on)));wrap.append(card)}
  return wrap},
 chips(g,k,names,counts){const box=make('div','chip-row');for(const [value,text] of names){const on=this.draft[g][k].includes(value),def=this.info.defaults[g][k].includes(value);
   const b=make('button','setting-chip'+(on?' on':'')+(on!==def?' changed':''));b.type='button';b.setAttribute('aria-pressed',on);b.disabled=!this.info.writable;
   b.append(make('span','chip-check',on?'✓':'+'),make('span','',text),make('span','chip-count',fmt(counts[value])));b.title=value+(on!==def?' · changed from default':'');
   b.onclick=()=>this.toggle(g,k,value,!on);box.append(b)}return box},
 relations(){const i=this.info,counts=i.counts.relations||{},out=[];
  const head=(t,copy)=>{const h=make('div','settings-subhead');h.append(make('h3','',t),make('p','setting-hint',copy));return h};
  out.push(head('Relations from Wikipedia text','Triples extracted from linked phrases in film articles. Turned-off relations are left out of relation answers and place or company filters. The number is how many triples the graph has.'));
  for(const [group,names] of RELATION_GROUPS){const sec=make('div','chip-group');sec.append(make('div','section-label',group.toUpperCase()),this.chips('relations','enabled',Object.entries(names),counts));out.push(sec)}
  out.push(head('Credits for connection paths','“How is X connected to Y?” only walks through these credits. The number is how many credits of that kind exist.'));
  const roles=i.options['relations.path_roles'].map(r=>[r,label(r)]);const sec=make('div','chip-group');sec.append(this.chips('relations','path_roles',roles,i.counts.path_roles||{}));out.push(sec);
  return out},
 footer(){const n=this.dirty().length,saved=Object.keys(this.info.saved||{}).length;q('#save-settings').disabled=!n||!this.info.writable;q('#discard-settings').disabled=!n;q('#reset-settings').disabled=!this.info.writable||!saved;
  if(n)this.status(n+' unsaved change'+(n>1?'s':''),'pending');else this.status(saved?'Saved. Some values differ from the defaults.':'Using the defaults.','')},
 payload(){const v={};for(const g of Object.keys(this.draft))v[g]={...this.draft[g]};return v},
 async post(body){const r=await fetch('/api/settings',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});const data=await r.json();if(!r.ok)throw Error(data.error||'Could not save settings');
  this.info=data;this.draft=structuredClone(data.current);this.applyToChat();this.render()},
 async save(){q('#save-settings').disabled=true;try{await this.post({values:this.payload()});this.status('Saved. The next question uses these settings.','ok')}catch(e){this.status(e.message,'error');q('#save-settings').disabled=false}},
 discard(){this.draft=structuredClone(this.info.current);this.render()},
 async reset(){if(!confirm('Reset every setting to its default?'))return;try{await this.post({reset:true});this.status('Reset. Every value is back to its default.','ok')}catch(e){this.status(e.message,'error')}}};

q('#settings-button').onclick=()=>S.open();
q('#close-settings').onclick=()=>S.close();
q('#settings-dialog').addEventListener('cancel',e=>{e.preventDefault();S.close()});
q('#settings-dialog').addEventListener('close',()=>{if(location.hash.startsWith('#settings'))history.replaceState(null,'',location.pathname+location.search)});
q('#save-settings').onclick=()=>S.save();
q('#discard-settings').onclick=()=>S.discard();
q('#reset-settings').onclick=()=>S.reset();
S.load().catch(()=>{});
if(location.hash.startsWith('#settings'))S.open(location.hash.split('/')[1]);
})();
