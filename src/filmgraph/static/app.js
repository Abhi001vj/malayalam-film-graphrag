'use strict';
const $=s=>document.querySelector(s), el=(tag,cls,text)=>{const n=document.createElement(tag);if(cls)n.className=cls;if(text!==undefined)n.textContent=text;return n};
const state={query:'Mohanlal and Mammootty worked together',data:null,graph:null,network:null,stats:null,request:0,offset:0,films:new Map()};
const roleLabel=r=>r.replaceAll('_',' '),fmt=n=>Number(n||0).toLocaleString();
function safeLink(url){try{const u=new URL(url);return ['https:','http:'].includes(u.protocol)?u.href:null}catch{return null}}
function link(label,url,cls=''){const a=el('a',cls,label+' ↗');a.href=safeLink(url)||'#';a.target='_blank';a.rel='noopener noreferrer';return a}
function addMessage(text,type='assistant',sub=''){const m=el('div','message '+type);m.append(el('p','',text));if(sub)m.append(el('div','interpretation',sub));$('#chat-log').append(m);$('#chat-log').scrollTop=$('#chat-log').scrollHeight;return m}
function filters(){return Object.fromEntries([['year_from',$('#year-from').value],['year_to',$('#year-to').value],['role',$('#role-filter').value],['technology',$('#technology-filter').value],['location',$('#location-filter').value]].filter(([,v])=>v))}
async function runQuery(q,{chat=true,append=false}={}){
 const seq=++state.request;state.query=q||'all movies';$('#send').disabled=true;
 $('#graph-status').textContent='Finding connections…';if(chat&&!append)addMessage(state.query,'user');
 const pending=chat&&!append?addMessage('Following the credits…','assistant pending'):null;
 try{
  const params=new URLSearchParams({q:state.query,limit:'20',offset:String(append?state.offset:0),...filters()});
  const plan=window.FilmGraphPlanner?.plan(state.query)||null;const response=await fetch('/api/query',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({question:state.query,plan,limit:20,offset:append?state.offset:0,filters:filters()})});const data=await response.json();if(data.quota)$('#quota-status').textContent=data.quota.remaining+' / '+data.quota.limit+' searches left · resets 00:00 UTC';if(!response.ok)throw Error(data.error||'Search failed');if(seq!==state.request)return;
  if(pending)pending.remove();state.data=data;state.offset=data.offset+data.results.length;
  if(chat&&!append){const interpreted=data.parsed.people.map(p=>p.name+(p.role?' · '+roleLabel(p.role):'')).join(' / ');const message=addMessage(data.answer,'assistant',interpreted+(data.parsed.year?' · '+data.parsed.year:'')+(data.parsed.story_terms.length?' · clues: '+data.parsed.story_terms.join(', '):''));for(const film of data.results.slice(0,3)){const row=el('div','chat-film'),button=el('button','',film.title+' · '+(film.year||''));button.onclick=()=>openFilm(film);row.append(button);if(film.wikipedia_url)row.append(link('Wiki',film.wikipedia_url));message.append(row)}}
  $('#query-title').textContent=data.parsed.people.length?data.parsed.people.map(p=>p.name).join(' × '):state.query==='all movies'?'The Malayalam cinema network':state.query;
  $('#query-description').textContent=fmt(data.total)+' films in the collection'+(data.parsed.year?' · '+data.parsed.year:'')+' · select a film to inspect its credits';
  $('#result-count').textContent=fmt(data.total);$('#coverage-note').textContent='Counts reflect imported credits. Some names and titles remain unresolved.';
  renderResults(data,append);if(!append){state.graph=data.graph;drawGraph()}
  $('#load-more').classList.toggle('hidden',state.offset>=data.total);
  const url=new URL(location.href);url.searchParams.set('q',state.query);history.replaceState(null,'',url);
 }catch(error){if(seq!==state.request)return;if(pending)pending.remove();addMessage(error.message,'assistant error');$('#graph-status').textContent='Query unavailable'}
 finally{if(seq===state.request)$('#send').disabled=false}
}
function renderResults(data,append){
 if(!append){$('#results').replaceChildren();state.films.clear()}
 if(!data.results.length){$('#results').append(el('div','empty','No matching films in the imported collection. Try fewer names or filters.'));return}
 data.results.forEach((film,index)=>{
  state.films.set(film.id,film);const card=el('article','film-card');card.dataset.id=film.id;card.tabIndex=0;
  card.append(el('div','film-number',String(data.offset+index+1).padStart(2,'0')+' / FILM'),el('h3','',film.title),el('div','film-meta',[film.year||'Year unknown',film.songs.length?film.songs.length+' songs':''].filter(Boolean).join(' · ')));
  const queried=new Set(data.parsed.people.flatMap(p=>p.ids));const credits=film.credits.filter(c=>queried.has(c.person_id));
  const crew=credits.length?credits:film.credits.filter(c=>['director','actor'].includes(c.role)).slice(0,3);
  card.append(el('div','film-people',[...new Set(crew.map(c=>c.name+' · '+roleLabel(c.role)))].join('\n')));
  const links=el('div','card-links');film.links.slice(0,2).forEach(l=>links.append(link(l.label,l.url)));card.append(links);if(window.FilmGraphAccount)card.append(window.FilmGraphAccount.controls(film));
  const button=el('button','film-open','Credits, songs & sources →');button.addEventListener('click',e=>{e.stopPropagation();openFilm(film)});card.append(button);
  card.addEventListener('click',e=>{if(e.target.closest('a,button'))return;selectFilm(film.id);openFilm(film)});card.addEventListener('keydown',e=>{if(e.key==='Enter'){selectFilm(film.id);openFilm(film)}});$('#results').append(card);
 });
}
const colors={film:'#526b50',focus:'#b6904f',actor:'#90b18b',director:'#7a9490',writer:'#9caaa9',screenwriter:'#9caaa9',story_writer:'#9caaa9',composer:'#bc9bb0',score_composer:'#bc9bb0',singer:'#c8a6b8',lyricist:'#b5a1c3',cinematographer:'#8baba5',location:'#b5b678',technology:'#b9a16e'};
const music=new Set(['composer','score_composer','singer','performer','lyricist']);
function isVisible(n,filter){if(['film','focus'].includes(n.group)||filter==='all')return true;const roles=n.roles||[n.group];if(filter==='music')return roles.some(r=>music.has(r));if(filter==='crew')return roles.some(r=>!music.has(r)&&!['actor','location','technology'].includes(r));return n.group===filter||roles.includes(filter)}
function drawGraph(){
 const graph=state.graph;if(!graph)return;const filter=$('#node-filter').value;let nodes=graph.nodes.filter(n=>isVisible(n,filter));const visible=new Set(nodes.map(n=>n.id));
 let edges=graph.edges.filter(e=>visible.has(e.from)&&visible.has(e.to));const connected=new Set(edges.flatMap(e=>[e.from,e.to]));nodes=nodes.filter(n=>connected.has(n.id)||n.group==='film');
 $('#graph-empty').classList.toggle('hidden',nodes.length>0);$('#graph-status').textContent=fmt(nodes.length)+' nodes · '+fmt(edges.length)+' connections';$('#graph-hint').textContent=graph.note+' · Double-click to explore.';
 if(state.network)state.network.destroy();if(!window.vis){$('#graph-status').textContent='Graph library unavailable';return}
 const fixed=graph.layout==='fixed';showCaption(fixed?graph:null);
 if(fixed){drawExplanation(graph,nodes,edges);return}
 const dataset=new vis.DataSet(nodes.map(n=>({...n,title:n.label+(n.year?' ('+n.year+')':'')+' · '+roleLabel(n.group),
  color:{background:colors[n.group]||'#9ba99a',border:colors[n.group]||'#9ba99a',highlight:{background:'#d1ad66',border:'#496f3e'}},
  shape:n.group==='film'?'dot':n.group==='focus'?'diamond':'dot',size:n.group==='focus'?24:n.group==='film'?11:7,
  font:{face:'system-ui',size:n.group==='focus'?14:n.group==='film'?10:9,color:'#607154',strokeWidth:3,strokeColor:'#fbfcf7'},borderWidth:1})));
 state.network=new vis.Network($('#graph'),{nodes:dataset,edges:new vis.DataSet(edges.map(e=>({...e,color:{color:'#c9d4bd',highlight:'#799862'},width:.6,font:{size:0,face:'system-ui'},selectionWidth:1.5})))},
  {layout:{randomSeed:14,improvedLayout:nodes.length<200},interaction:{hover:true,tooltipDelay:150,hideEdgesOnDrag:true},physics:{solver:'forceAtlas2Based',forceAtlas2Based:{gravitationalConstant:-55,centralGravity:.008,springLength:100,springConstant:.06,damping:.65},stabilization:{iterations:120,fit:true}},nodes:{scaling:{min:6,max:20}},edges:{smooth:false}});
 state.network.once('stabilizationIterationsDone',()=>state.network?.setOptions({physics:false}));
 state.network.on('selectNode',p=>{const id=p.nodes[0];if(state.network.isCluster(id))return;const n=graph.nodes.find(n=>n.id===id);if(!n)return;if(n.group==='film')selectFilm(id,false);$('#graph-status').textContent=n.label+(n.year?' · '+n.year:'')+' · '+roleLabel(n.group)});
 state.network.on('doubleClick',p=>{const id=p.nodes[0];if(!id)return;if(state.network.isCluster(id)){state.network.openCluster(id);return}const n=graph.nodes.find(n=>n.id===id);if(!n)return;if(n.group==='film'){openFilmById(id)}else if(n.group==='location'){$('#location-filter').value=n.label;runQuery('all movies')}else if(n.group==='technology'){$('#technology-filter').value=n.label;runQuery('all movies')}else runQuery(n.label)});
 const cluster=$('#cluster-filter').value;
 if(cluster!=='none'){
  const groups=new Set(nodes.map(n=>cluster==='role'?n.group:(n.group==='film'&&n.year?String(Math.floor(n.year/10)*10):null)).filter(g=>g&&g!=='focus'&&(cluster!=='role'||g!=='film')));
  for(const group of groups){const members=nodes.filter(n=>cluster==='role'?n.group===group:(n.group==='film'&&n.year&&String(Math.floor(n.year/10)*10)===group));if(members.length<3)continue;
   state.network.cluster({joinCondition:n=>members.some(m=>m.id===n.id),clusterNodeProperties:{id:'cluster:'+group,label:roleLabel(group)+(cluster==='decade'?'s':'')+' · '+members.length,color:colors[group]||'#84976f',shape:'dot',size:26,font:{color:'#466139',size:13}}});
  }
 }
}
// Explanation layout for "X and Y together": people fixed on both sides, films between them, one styled edge per credit.
const BUCKET_STYLE={costar:{background:'#355b43',border:'#355b43',font:'#ffffff'},cameo:{background:'#fdf3df',border:'#c79a45',font:'#6d4f17'},offscreen:{background:'#f4f1e8',border:'#b9ae93',font:'#6f6a58'},unconfirmed:{background:'#f3f4ef',border:'#b7bdb0',font:'#7c8378'}};
const EDGE_STYLE={role:{color:'#4c7a52',width:1.6,dashes:false},cameo:{color:'#c79a45',width:1.3,dashes:[7,5]},self:{color:'#c79a45',width:1.3,dashes:[7,5]},voice:{color:'#a99c79',width:1.2,dashes:[7,5]},archive:{color:'#a99c79',width:1.2,dashes:[7,5]},unconfirmed:{color:'#b7bdb0',width:1,dashes:[2,5]}};
function showCaption(graph){const box=$('#graph-caption');if(!box)return;box.classList.toggle('hidden',!graph);$('.graph-legend')?.classList.toggle('hidden',!!graph);if(!graph)return;box.replaceChildren(el('p','',graph.caption));
 if(graph.counts){const row=el('div','caption-legend');for(const [b,label] of [['costar','Full roles (counted)'],['cameo','Cameo / as self (counted)'],['offscreen','Voice or archive only (not counted)'],['unconfirmed','Unconfirmed (not counted)']]){if(!graph.counts[b])continue;const s=el('span','caption-key '+b);s.append(el('i'),label+' · '+graph.counts[b]);row.append(s)}box.append(row)}}
function drawExplanation(graph,nodes,edges){
 const dataset=new vis.DataSet(nodes.map(n=>{if(n.group==='focus')return{...n,title:n.label+' · queried person',shape:'box',margin:{top:12,bottom:12,left:16,right:16},color:{background:'#d1ad66',border:'#9c7a35',highlight:{background:'#d1ad66',border:'#496f3e'}},font:{face:'system-ui',size:17,color:'#2f3a1f',bold:true},borderWidth:2,shapeProperties:{borderRadius:10}};
  const b=BUCKET_STYLE[n.bucket]||BUCKET_STYLE.costar;return{...n,title:n.label,shape:'box',margin:{top:6,bottom:6,left:9,right:9},color:{background:b.background,border:b.border,highlight:{background:b.background,border:'#2a8e68'}},font:{face:'system-ui',size:13,color:b.font},borderWidth:1,shapeProperties:{borderRadius:6,borderDashes:n.bucket==='unconfirmed'?[3,3]:false}}}));
 const lines=new vis.DataSet(edges.map(e=>{const st=EDGE_STYLE[e.kind]||EDGE_STYLE.role;return{...e,label:undefined,color:{color:st.color,opacity:.55,highlight:st.color,hover:st.color},width:st.width,dashes:st.dashes,hoverWidth:1.5,selectionWidth:2,smooth:false}}));
 state.network=new vis.Network($('#graph'),{nodes:dataset,edges:lines},{physics:false,interaction:{hover:true,tooltipDelay:120,dragNodes:false},layout:{improvedLayout:false},edges:{smooth:false}});
 const fit=()=>{state.network.fit({animation:false});state.network.moveTo({scale:state.network.getScale()*.94})};
 state.network.once('afterDrawing',()=>requestAnimationFrame(fit)); // boxes are measured on the first draw; fit to them, with a margin
 state.network.on('hoverNode',p=>{const n=graph.nodes.find(n=>n.id===p.node);if(!n)return;const why=edges.filter(e=>e.to===n.id||e.from===n.id).map(e=>e.title).slice(0,3).join('  ·  ');$('#graph-status').textContent=n.group==='focus'?n.label+' · '+edges.filter(e=>e.from===n.id&&(e.kind==='role')).length+' real roles in this answer':why});
 state.network.on('selectNode',p=>{const n=graph.nodes.find(n=>n.id===p.nodes[0]);if(n?.group==='film')selectFilm(n.id,false)});
 state.network.on('doubleClick',p=>{const n=graph.nodes.find(n=>n.id===p.nodes[0]);if(!n)return;n.group==='film'?openFilmById(n.id):runQuery(n.label)});
}
function selectFilm(id,select=true){document.querySelectorAll('.film-card').forEach(c=>c.classList.toggle('selected',c.dataset.id===id));if(select&&state.network?.body.data.nodes.get(id)){state.network.selectNodes([id]);state.network.focus(id,{scale:1.05,animation:{duration:450}})}}
async function openFilmById(id){if(state.films.has(id))return openFilm(state.films.get(id));try{const r=await fetch('/api/film?id='+encodeURIComponent(id));if(!r.ok)throw Error('Film unavailable');openFilm(await r.json())}catch(e){addMessage(e.message)}}
function openFilm(film){
 const root=$('#film-detail');root.replaceChildren();root.append(el('div','section-label','FILM / '+(film.year||'YEAR UNKNOWN')),el('h2','',film.title));
 const links=el('div','detail-links');film.links.forEach(l=>links.append(link(l.label+(l.verified?'':' · search'),l.url)));root.append(links);if(window.FilmGraphAccount){root.append(window.FilmGraphAccount.controls(film));const follow=el('button','filter-apply','Follow this film');follow.onclick=()=>window.FilmGraphAccount.follow('film',film.id,film.title);root.append(follow)}
 root.append(el('p','connection-label','Video and streaming searches do not confirm current availability. Source-linked availability may change by region and date.'));
 if(window.FilmGraphAccount){for(const f of film.facts.filter(f=>['production_company','production_companies'].includes(f.kind)&&f.value.length<=300)){const b=el('button','quiet','Follow '+f.value);b.onclick=()=>window.FilmGraphAccount.follow('company','company:'+f.value,f.value);root.append(b)}}const plot=film.documents.find(d=>d.kind==='plot')||film.documents.find(d=>d.kind==='lead');if(plot){root.append(el('h3','','The story'),el('div','detail-plot',plot.text),link('Story source · '+plot.license,plot.source_url,'connection-label'))}
 root.append(el('h3','','Cast & crew'));const credits=el('div');const used=new Set();film.credits.forEach(c=>{const key=c.name+':'+c.role;if(used.has(key))return;used.add(key);const chip=el('span','detail-credit',c.name+' · '+roleLabel(c.role)+(c.character_name?' as '+c.character_name:''));chip.append(link('source',c.source_url));if(window.FilmGraphAccount&&c.person_id){const follow=el('button','quiet','Follow');follow.onclick=()=>window.FilmGraphAccount.follow('person',c.person_id,c.name);chip.append(follow)}credits.append(chip)});root.append(credits);
 if(film.songs.length){root.append(el('h3','','The soundtrack'));film.songs.forEach(s=>{const row=el('div','song-row',s.title);row.append(el('small','',s.credits.map(c=>c.name+' · '+roleLabel(c.role)).join(' / ')),link('Source',s.source_url,'connection-label'));root.append(row)})}
 const facts=film.facts.filter(f=>['filming_location','narrative_location','technology_mention','film_format','aspect_ratio','color','filming_evidence','streaming_evidence','imdb_rating','imdb_votes'].includes(f.kind));
 if(facts.length){root.append(el('h3','','Places, craft & evidence'));facts.forEach(f=>{const item=el('div','evidence');item.append(el('strong','',roleLabel(f.kind)+': '),document.createTextNode(f.value));if(f.evidence&&f.evidence!==f.value)item.append(el('div','',f.evidence));item.append(link('Source',f.source_url));root.append(item)})}
 if(!$('#film-dialog').open)$('#film-dialog').showModal();$('#film-dialog').scrollTop=0;
}
async function loadStats(){
 try{const r=await fetch('/api/stats');if(!r.ok)throw Error('Collection unavailable');const s=await r.json();state.stats=s;$('#stats').replaceChildren();for(const [n,label]of[[s.films,'films'],[s.people,'identities'],[s.songs,'songs']]){const item=el('span');item.append(el('strong','',fmt(n)),document.createTextNode(label));$('#stats').append(item)}
  const selected=$('#role-filter').value;$('#role-filter').replaceChildren(new Option('Every role',''));s.roles.forEach(r=>$('#role-filter').add(new Option(roleLabel(r.role)+' ('+fmt(r.films)+')',r.role)));$('#role-filter').value=selected;
  const tech=$('#technology-filter').value;$('#technology-filter').replaceChildren(new Option('Any technology',''));s.technologies.forEach(t=>$('#technology-filter').add(new Option(t,t)));$('#technology-filter').value=tech;
 }catch(error){$('#stats').textContent=error.message}
}
async function showCoverage(){
 await loadStats();const s=state.stats;if(!s)return;const root=$('#coverage-detail');root.replaceChildren();const grid=el('div','coverage-grid');for(const [key,label]of[['films','Film records'],['people','People & unresolved names'],['credits','Credit edges'],['songs','Songs'],['plots','Films with plots'],['imdb_links','IMDb title links'],['locations','Films with locations'],['technical','Films with technical facts']]){const item=el('div','coverage-stat');item.append(el('strong','',fmt(s[key])),document.createTextNode(label));grid.append(item)}root.append(grid,el('p','coverage-copy',s.note));
 const bar=el('div','coverage-bar');const max=Math.max(...s.years.map(y=>y.films));s.years.forEach(y=>{const n=el('span');n.style.height=Math.max(2,y.films/max*55)+'px';n.title=(y.year||'Unknown year')+': '+fmt(y.films)+' films';bar.append(n)});root.append(bar,el('p','connection-label','Film records by release year. Hover over a bar to inspect.'));
 root.append(el('h3','','Source access'));Object.entries(s.source_status).forEach(([name,status])=>root.append(el('p','coverage-copy',name.toUpperCase()+' · '+status)));
 root.append(el('h3','','Import coverage'));const table=el('table','coverage-table');for(const j of s.jobs){const tr=el('tr');[roleLabel(j.source),j.status,fmt(j.count),j.updated_at+' UTC'].forEach(t=>tr.append(el('td','',t)));table.append(tr)}root.append(table);
 root.append(el('h3','','Credits by role'));const rt=el('table','coverage-table');s.roles.forEach(r=>{const tr=el('tr');[roleLabel(r.role),fmt(r.films)+' films',fmt(r.credits)+' credits'].forEach(t=>tr.append(el('td','',t)));rt.append(tr)});root.append(rt);
 if(!$('#coverage-dialog').open)$('#coverage-dialog').showModal();
}
$('#chat-form').addEventListener('submit',e=>{e.preventDefault();const q=$('#question').value.trim();if(q){$('#question').value='';runQuery(q)}});
$('#question').addEventListener('keydown',e=>{if(e.key==='Enter'&&!e.shiftKey){e.preventDefault();$('#chat-form').requestSubmit()}});
document.querySelectorAll('[data-query]').forEach(b=>b.addEventListener('click',()=>runQuery(b.dataset.query)));
$('#filter-form').addEventListener('submit',e=>{e.preventDefault();runQuery(state.query)});
$('#clear-filters').addEventListener('click',()=>{$('#filter-form').reset();runQuery(state.query)});
$('#load-more').addEventListener('click',()=>runQuery(state.query,{chat:false,append:true}));
$('#node-filter').addEventListener('change',drawGraph);$('#cluster-filter').addEventListener('change',drawGraph);$('#fit-graph').addEventListener('click',()=>state.network?.fit({animation:true}));
$('#graph-tab').addEventListener('click',()=>{$('.explorer').classList.remove('show-list');$('#graph-tab').classList.add('active');$('#list-tab').classList.remove('active');state.network?.redraw();state.network?.fit()});
$('#list-tab').addEventListener('click',()=>{$('.explorer').classList.add('show-list');$('#list-tab').classList.add('active');$('#graph-tab').classList.remove('active')});
$('#close-film').addEventListener('click',()=>$('#film-dialog').close());$('#close-coverage').addEventListener('click',()=>$('#coverage-dialog').close());$('#coverage-button').addEventListener('click',showCoverage);
for(const d of document.querySelectorAll('dialog'))d.addEventListener('click',e=>{if(e.target===d){const r=d.getBoundingClientRect();if(e.clientX<r.left||e.clientX>r.right||e.clientY<r.top||e.clientY>r.bottom)d.close()}});
// Deferred scripts all run before DOMContentLoaded, so by then chat.js has routed runQuery through the GraphRAG pipeline.
loadStats();document.addEventListener('DOMContentLoaded',()=>runQuery(new URLSearchParams(location.search).get('q')||state.query));setInterval(loadStats,60000);
