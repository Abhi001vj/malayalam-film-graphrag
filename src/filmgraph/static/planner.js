/* Optional dictionary NER + intent classification. No model download, no generated SQL. */
'use strict';
window.FilmGraphPlanner={
 entities:[],
 normalize:s=>s.normalize('NFKC').toLocaleLowerCase().replace(/[^\p{L}\p{N}_]+/gu,' ').trim(),
 async initialize(){try{const r=await fetch('/api/entities');if(r.ok)this.entities=(await r.json()).entities}catch{}},
 plan(text){
  const q=this.normalize(text),found=[],ranges=[];
  for(const e of this.entities){const alias=e.alias;let start=q.indexOf(alias);if(start<0)continue;const end=start+alias.length;if((start&&/[\p{L}\p{N}_]/u.test(q[start-1]))||(end<q.length&&/[\p{L}\p{N}_]/u.test(q[end])))continue;
   if(ranges.some(([a,b])=>start<b&&end>a))continue;ranges.push([start,end]);found.push({name:e.name,role:null,start,end,alias});if(found.length>=6)break;
  }
  found.sort((a,b)=>a.start-b.start);
  const words={actor:'actor',acted:'actor',actors:'actor',starring:'actor',director:'director',directed:'director',writer:'writer',wrote:'writer',written:'writer',screenwriter:'screenwriter',producer:'producer',produced:'producer',composer:'composer',music:'composer',singer:'singer',sang:'singer',sung:'singer',lyricist:'lyricist',lyrics:'lyricist',cinematographer:'cinematographer',cinematography:'cinematographer',choreographer:'choreographer',editor:'editor',edited:'editor'};
  const roles=[...q.matchAll(/\b(?:actor|acted|actors|starring|director|directed|writer|wrote|written|screenwriter|producer|produced|composer|music|singer|sang|sung|lyricist|lyrics|cinematographer|cinematography|choreographer|editor|edited)\b/g)];
  for(const match of roles){const start=match.index,end=start+match[0].length;let best=null,dist=Infinity;for(const p of found){const between=q.slice(Math.min(end,p.end),Math.max(start,p.start));const d=Math.min(Math.abs(start-p.end),Math.abs(end-p.start))+(/\b(and|with|or)\b/.test(between)?100:0);if(d<dist){dist=d;best=p}}if(best&&dist<55)best.role=words[match[0]]}
  if(roles.length&&roles.every(r=>words[r[0]]==='actor'))found.forEach(p=>p.role='actor');
  const years=[...q.matchAll(/\b(19|20)\d{2}\b/g)].map(m=>Number(m[0]));
  // Story queries, date ranges and unknown entities use the richer server fallback.
  const stop=new Set('a an and are acted actor actors about based by can director film films find for from had has have i in is it me movie movies of on or plot show story that the there this was were where which who with year how many times did do does work works worked together collaborate collaborated collaboration between starring featuring list all give tell as both also malayalam cinema please'.split(' '));
  let remainder=q;for(const p of found)remainder=remainder.replace(p.alias,' ');const terms=remainder.split(/\s+/).filter(t=>t&&!stop.has(t)&&!words[t]&&!/^\d+$/.test(t));
  if(!found.length||terms.length||years.length>1||/\b(after|before|since|until|from)\b/.test(q))return null;
  return {version:1,intent:/\bhow many|\bcount\b|\btimes\b/.test(q)?'collaboration_count':found.length>1?'shared_films':'person_filmography',people:found.map(({name,role})=>({name,role})),year:years[0]||null,terms:[]};
 }
};
window.FilmGraphPlanner.initialize();
