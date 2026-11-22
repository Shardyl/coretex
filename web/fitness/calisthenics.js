// Calisthenics tab (owner, 14 Nov 2026): its own section, separate from Mobility. Starts with Matthew Smith's
// Handstand Toolkit: his program sheets (Starter to one arm prep) as data from /api/fitness/cali, the skill tree's
// milestones, his wrist warm-up library, and the lessons playing inside the app (mobLesson, course "handstand").
// Handstands are non-linear (Smith): progress is judged by milestones; a session shows last time and best only.
(function(){
let _cali=null, _caliView='program', _caliPick=false;
const esc=s=>String(s==null?'':s).replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
const today=()=>{const d=new Date();return new Date(d.getTime()-d.getTimezoneOffset()*60000).toISOString().slice(0,10);};
const fd=d=>new Date(d+'T12:00:00').toLocaleDateString('en-GB',{day:'numeric',month:'short',year:'numeric'});
const ICON={check:'<path d="M5 12l5 5l10 -10"/>',lock:'<rect x="5" y="11" width="14" height="10" rx="2"/><path d="M8 11v-4a4 4 0 1 1 8 0v4"/>',
  target:'<circle cx="12" cy="12" r="8"/><circle cx="12" cy="12" r="3"/>',arrow:'<path d="M9 6l6 6l-6 6"/>'};
const icon=(k,c,s)=>`<svg width="${s||16}" height="${s||16}" viewBox="0 0 24 24" fill="none" stroke="${c||'currentColor'}" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" style="flex-shrink:0" aria-hidden="true">${ICON[k]}</svg>`;

async function load(){try{_cali=await syncApi('/api/fitness/cali');}catch(e){_cali=null;}return _cali;}
const D=()=>(_cali&&_cali.data)||{};
const ex=id=>(D().exercises||[]).find(x=>x.id===id)||{id,name:String(id||'').replace(/_/g,' ')};
const prog=id=>(_cali.programs||[]).find(p=>p.id===id);
const msOf=id=>(D().milestones||[]).find(m=>m.id===id);
const msDone=id=>(_cali.milestones||[]).filter(m=>m.milestone===id);
// the lesson player lives in mobility.js; handstand lessons use course "handstand"
const _L=[];window.caliLessonN=i=>{const x=_L[i];if(x&&window.mobLesson)mobLesson(x.c,x.s,x.y,x.n,x.v);};
const watch=(x,label)=>{if(!x||(!x.lesson_slug&&!x.youtube&&!x.vimeo))return '';const i=_L.push({c:x.lesson_slug?'handstand':'',s:x.lesson_slug||'',y:x.youtube||'',v:x.vimeo||'',n:x.name})-1;
  return `<button class="btn-sm" style="display:inline-flex;align-items:center;gap:7px;font-weight:800;padding:4px 12px 4px 4px;border-radius:999px;border:1px solid var(--teal);color:var(--teal);background:transparent" onclick="event.stopPropagation();caliLessonN(${i})"><span style="display:inline-flex;align-items:center;justify-content:center;width:24px;height:24px;border-radius:50%;background:var(--teal);flex-shrink:0"><svg width="12" height="12" viewBox="0 0 24 24" fill="#fff" aria-hidden="true" style="margin-left:2px"><path d="M7 4v16l13 -8z"/></svg></span>${label||'Watch'}</button>`;};

window.renderCalisthenics=async function(sub){
  const el=document.getElementById('page-calisthenics-'+(sub||'program'));if(!el)return;
  if(!_cali){el.innerHTML='<div class="fd-note" style="padding:20px 0">Loading&hellip;</div>';await load();}
  if(!_cali){el.innerHTML='<div class="fd-note" style="padding:20px 0">Could not reach Cortex.</div>';return;}
  if(sub==='goals')return renderGoalsView(el);
  if(sub==='tree')return renderTree(el);
  if(sub==='library')return renderLibrary(el);
  if(_caliView==='session')return renderSession(el);
  renderProgram(el);
};
const rerender=()=>{const s=(typeof currentSubpage!=='undefined'&&currentSubpage.calisthenics)||'program';renderCalisthenics(s);};

// ---------- Program ----------
function itemLine(it){const e=ex(it.exercise_id);const bits=[it.reps,it.sets?it.sets+' sets':'',it.rest?'rest '+it.rest:''].filter(x=>x&&!/^-+( sets)?$/.test(x)&&x!=='rest -');
  return `<div style="border-top:0.5px solid var(--border);padding:8px 0">
    <div style="display:flex;align-items:center;gap:8px"><span style="font-size:11px;font-weight:800;color:var(--teal);width:24px">${esc(it.code)}</span>
      <div style="flex:1;min-width:0"><div style="font-size:13px;font-weight:700">${esc(e.name)}</div><div style="font-size:11px;color:var(--ink3)">${esc(bits.join(' · '))}</div>${it.tempo&&!/^-+$/.test(String(it.tempo))?tempoGraphic(it.tempo):''}</div>${watch(e)}</div>
    ${(it.notes||[]).length?`<div style="margin:3px 0 0 32px">${it.notes.map(n=>`<div style="font-size:11.5px;color:var(--ink2)">&bull; ${esc(n)}</div>`).join('')}</div>`:''}
    ${e.id==='wrist_warm_up'||/wrist/.test(e.id||'')?wristLine():''}</div>`;}
function wristLine(){const ids=_cali.wrist||[], lib=D().wrist_library||[];
  const mine=lib.filter(w=>ids.includes(w.id));
  return `<div style="margin:4px 0 0 32px;font-size:11.5px;color:var(--ink3)">${mine.length?'Your routine: '+esc(mine.map(w=>w.name).join(', ')):'Pick your wrist routine in Library (5-10 reps of each).'}</div>`;}
function lastFor(pid,code){const s=(_cali.sessions||[]).find(x=>x.program===pid&&x.items.some(i=>i.code===code));return s?{date:s.date,it:s.items.find(i=>i.code===code)}:null;}
function renderProgram(el){
  const p=prog(_cali.current);
  if(!p){el.innerHTML='<div class="fd-note">No programs loaded.</div>';return;}
  const mo=p.move_on||{}, m=mo.milestone_id?msOf(mo.milestone_id):null, got=m?msDone(m.id):[];
  const nxt=_cali.next?prog(_cali.next):null;
  const sess=(_cali.sessions||[]).filter(s=>s.program===p.id);
  el.innerHTML=`<div class="section-label">Handstand</div>
    <div style="font-size:12px;color:var(--ink3);margin-bottom:10px">Matthew Smith's Handstand Toolkit. Train handstands before your other training, on the floor (not parallettes), and keep shoulder flexibility sessions at least 2 days away.</div>
    ${nxt?`<div class="card" style="padding:12px 14px;margin-bottom:10px;border:1px solid var(--teal)"><div style="font-size:13px;font-weight:800">Milestone hit: ${esc(m?m.name:'')}</div>
      <div style="font-size:12px;color:var(--ink3);margin:2px 0 8px">Ready for ${esc(nxt.name)}.</div><button class="fd-btn" onclick="caliSetCurrent('${nxt.id}')">Move to ${esc(nxt.name)}</button></div>`:''}
    <div class="card" style="padding:12px 14px;margin-bottom:10px">
      <div style="font-size:11px;font-weight:800;color:var(--ink3);text-transform:uppercase;letter-spacing:0.06em">${esc(p.stage_name||'')}</div>
      <div style="display:flex;justify-content:space-between;align-items:center;gap:8px"><div style="font-size:16px;font-weight:800">${esc(p.name)}</div><button class="btn-sm" onclick="caliPick()">Change</button></div>
      ${p.purpose?`<div style="font-size:12px;color:var(--ink2);margin-top:4px">${esc(p.purpose)}</div>`:''}
      ${p.frequency?`<div style="font-size:12px;margin-top:4px"><b>Frequency:</b> ${esc(p.frequency)}</div>`:''}
      ${(p.items||[]).map(itemLine).join('')}
      ${mo.text?`<div style="font-size:12px;margin-top:8px;padding:8px;border-radius:6px;background:var(--surface2)"><b>Move on when:</b> ${esc(mo.text)}${got.length?` &middot; <span style="color:var(--teal);font-weight:800">done ${fd(got[got.length-1].date)}</span>`:''}${m&&!got.length?`<div style="margin-top:6px"><button class="btn-sm" onclick="caliMilestone('${m.id}')">I hit it</button></div>`:''}</div>`:''}
      ${p.film?'<div style="font-size:11px;color:var(--ink3);margin-top:6px">Film your sets from the side: video is your best teacher here.</div>':''}
      <button class="fd-btn" style="margin-top:10px" onclick="caliSessionOpen()">Log a session</button></div>
    ${_caliPick?pickBlock():''}
    ${sess.length?`<div class="card" style="padding:12px 14px"><div style="font-size:13px;font-weight:800;margin-bottom:4px">Sessions on this program (${sess.length})</div>
      ${sess.slice(0,15).map(s=>`<div style="border-top:0.5px solid var(--border);padding:6px 0;font-size:12px"><div style="display:flex;justify-content:space-between"><b>${fd(s.date)}</b><button class="btn-sm" onclick="caliSessionDel('${s.id}')">Remove</button></div>
        ${s.items.map(i=>`<div style="color:var(--ink2)">${esc(i.code)} ${esc(ex((p.items.find(x=>x.code===i.code)||{}).exercise_id||i.exercise_id).name)}: ${esc(i.sets.map(z=>z.hold_s!=null?z.hold_s+'s':z.reps+'').join(', '))}${i.note?' &middot; '+esc(i.note):''}</div>`).join('')}
        ${s.note?`<div style="color:var(--ink3)">${esc(s.note)}</div>`:''}</div>`).join('')}</div>`:''}`;
}
function pickBlock(){
  return `<div class="card" style="padding:12px 14px;margin-bottom:10px"><div style="font-size:13px;font-weight:800;margin-bottom:6px">Choose a program</div>
    ${(_cali.programs||[]).map(p=>`<div onclick="caliSetCurrent('${p.id}')" style="display:flex;align-items:center;gap:8px;padding:7px 0;border-top:0.5px solid var(--border);cursor:pointer">
      <div style="flex:1"><div style="font-size:13px;font-weight:${p.id===_cali.current?800:600}">${esc(p.name)}</div><div style="font-size:11px;color:var(--ink3)">${esc(p.stage_name||'')}</div></div>${p.id===_cali.current?icon('check','var(--teal)'):icon('arrow','var(--ink3)')}</div>`).join('')}</div>`;}
window.caliPick=()=>{_caliPick=!_caliPick;rerender();};
window.caliSetCurrent=async function(id){try{await syncApi('/api/fitness/cali/current',{method:'POST',body:JSON.stringify({program:id})});_caliPick=false;await load();rerender();toast('Program set');}catch(e){toast('Could not change it.');}};
window.caliSessionDel=async function(id){if(!confirm('Remove this session?'))return;await syncApi('/api/fitness/cali/session/'+id,{method:'DELETE'});await load();rerender();};

// ---------- Session ----------
const nInt=x=>{const m=String(x==null?'':x).match(/[0-9]+/);return m?+m[0]:null;};
const isHold=it=>{const e=ex(it.exercise_id);return e.kind==='hold'||/\bs\b|hold|sec/.test(String(it.reps||''));};
window.caliSessionOpen=()=>{_caliView='session';rerender();window.scrollTo(0,0);};
function renderSession(el){
  const p=prog(_cali.current);if(!p){_caliView='program';return renderProgram(el);}
  el.innerHTML=`${bigBack('caliBack()',esc(p.name))}
    <div style="font-size:18px;font-weight:800">Log a session</div>
    <div style="display:flex;gap:8px;align-items:center;margin:8px 0 10px"><label class="form-label" style="margin:0">Date</label><input id="caliDay" type="date" value="${today()}" max="${today()}" class="fd-input" style="width:auto;font-size:14px"/></div>
    ${(p.items||[]).map((it,k)=>{const e=ex(it.exercise_id), hold=isHold(it), n=Math.max(1,Math.min(12,nInt(it.sets)||1)), last=lastFor(p.id,it.code);
      const warm=/wrist/.test(e.id||'');
      return `<div class="card" style="padding:12px 14px;margin-bottom:10px">
        <div style="display:flex;align-items:center;gap:8px"><span style="font-size:12px;font-weight:800;color:var(--teal)">${esc(it.code)}</span><div style="flex:1;font-size:14px;font-weight:800">${esc(e.name)}</div>${watch(e)}</div>
        ${warm?'':`<div style="font-size:12px;margin-top:4px"><b>Target:</b> ${esc([it.reps,it.sets?it.sets+' sets':'',it.rest?'rest '+it.rest:''].filter(x=>x&&!/^-+( sets)?$/.test(x)&&x!=='rest -').join(' · '))}</div>`}
        ${last?`<div style="font-size:11px;color:var(--ink3);margin-top:2px">Last time (${fd(last.date)}): ${esc(last.it.sets.map(z=>z.hold_s!=null?z.hold_s+'s':z.reps+'').join(', '))}</div>`:''}
        ${warm?wristTicks(k):`<div style="display:grid;grid-template-columns:repeat(auto-fill,minmax(64px,1fr));gap:6px;margin-top:6px">${Array.from({length:n},(_,i)=>`<input id="cali_${k}_${i}" type="number" inputmode="decimal" class="fd-input" style="font-size:14px;padding:6px" placeholder="${hold?'s':'reps'} ${i+1}"/>`).join('')}</div>`}
        <input id="cali_${k}_note" class="fd-input" style="font-size:13px;margin-top:6px" placeholder="${warm?'Done? Any note':'Note (optional)'}"/>
        ${it.rest&&nInt(it.rest)&&window.mobRest?`<button class="btn-sm" style="margin-top:6px" onclick="mobRest(${nInt(it.rest)})">Rest timer ${nInt(it.rest)} s</button>`:''}</div>`;}).join('')}
    <input id="caliNote" class="fd-input" style="font-size:13px;margin-bottom:8px" placeholder="Session note (wrists, energy, falls)"/>
    <button class="fd-btn" onclick="caliSave()">Save session</button>`;
}
function wristTicks(k){const ids=_cali.wrist||[], lib=(D().wrist_library||[]).filter(w=>ids.includes(w.id));
  if(!lib.length)return `<div style="font-size:12px;color:var(--amber);margin-top:6px">No wrist routine picked yet: tick yours in Library. Until then, warm up with wrist circles and leans, 5-10 reps each, building towards bodyweight.</div><label style="display:flex;gap:8px;align-items:center;font-size:12.5px;margin-top:6px"><input type="checkbox" class="caliw_${k}"/>Wrists warmed up</label>`;
  return `<div style="font-size:11px;color:var(--ink3);margin-top:4px">5-10 reps of each, load building towards bodyweight. Take as long as you need.</div>${lib.map(w=>`<label style="display:flex;gap:8px;align-items:center;font-size:12.5px;padding:3px 0"><input type="checkbox" class="caliw_${k}"/>${esc(w.name)} ${watch(w,'Demo')}</label>`).join('')}`;}
window.caliBack=()=>{_caliView='program';rerender();};
window.caliSave=async function(){
  const p=prog(_cali.current);
  const items=p.items.map((it,k)=>{const hold=isHold(it), sets=[];
    for(let i=0;i<12;i++){const e=document.getElementById(`cali_${k}_${i}`);if(e&&e.value!=='')sets.push(hold?{hold_s:+e.value}:{reps:+e.value});}
    const nt=document.getElementById(`cali_${k}_note`), w=document.querySelectorAll(`.caliw_${k}`);
    const wn=w.length?[...w].filter(e=>e.checked).length:0;
    return {code:it.code,exercise_id:it.exercise_id,sets,note:(w.length?`wrists ${wn}/${w.length} done`+(nt&&nt.value?'; ':''):'')+(nt?nt.value:''),_w:[wn,w.length]};});
  const wu=items.find(i=>i._w&&i._w[1]);if(wu&&wu._w[0]<wu._w[1]&&!confirm('Wrist warm-up not finished. Save anyway?'))return;
  items.forEach(i=>delete i._w);
  try{await syncApi('/api/fitness/cali/session',{method:'POST',body:JSON.stringify({program:p.id,date:document.getElementById('caliDay').value||today(),items,note:document.getElementById('caliNote').value||''})});
    _caliView='program';await load();rerender();window.scrollTo(0,0);toast('Session saved');}
  catch(e){toast('Log at least one set or note, and check your connection.');}
};

// ---------- Skill tree ----------
window.caliMilestone=async function(id){
  const m=msOf(id);if(!m)return;const t=m.target||{};
  let v=null;if(t.kind&&t.kind!=='bool'){const a=prompt(`${m.name}: your ${t.kind==='hold'?'hold in seconds':'reps'}${t.value?` (target ${t.value}${t.unit==='s'?' s':''})`:''}`);if(a===null)return;v=a===''?null:+a;}
  else if(!confirm(`Mark "${m.name}" as achieved today?`))return;
  try{await syncApi('/api/fitness/cali/milestone',{method:'POST',body:JSON.stringify({milestone:id,date:today(),value:v})});await load();rerender();toast('Milestone logged');}catch(e){toast('Could not save.');}
};
window.caliMilestoneDel=async function(uid){if(!confirm('Remove this milestone entry?'))return;await syncApi('/api/fitness/cali/milestone/'+uid,{method:'DELETE'});await load();rerender();};
function renderTree(el){
  const ms=D().milestones||[], stages=(D().stages||[]).slice().sort((a,b)=>(a.order||0)-(b.order||0));
  const done=id=>msDone(id).length>0, open=m=>(m.requires||[]).every(done);
  const nDone=ms.filter(m=>done(m.id)).length;
  el.innerHTML=`<div class="section-label">Skill tree</div>
    <div style="font-size:12px;color:var(--ink3);margin-bottom:10px">Smith's milestones, ${nDone} of ${ms.length} done. Each unlocks the next; film your attempts.</div>
    ${stages.map(st=>{const mine=ms.filter(m=>m.stage===st.id);if(!mine.length)return '';
      return `<div class="card" style="padding:12px 14px;margin-bottom:10px"><div style="font-size:14px;font-weight:800;margin-bottom:4px">${esc(st.name)}</div>
        ${mine.map(m=>{const d=done(m.id), o=open(m), g=msDone(m.id), t=m.target||{};const c=d?'var(--teal)':o?'var(--amber)':'var(--ink3)';
          return `<div style="display:flex;align-items:center;gap:8px;padding:7px 0;border-top:0.5px solid var(--border)">${icon(d?'check':o?'target':'lock',c,18)}
            <div style="flex:1;min-width:0"><div style="font-size:13px;font-weight:700;color:${d||o?'var(--ink)':'var(--ink3)'}">${esc(m.name)}</div>
              <div style="font-size:11px;color:var(--ink3)">${t.value?esc(t.value+(t.unit==='s'?' s':' '+(t.unit||''))+(t.sides==='each'?' each side':'')):''}${!o&&!d?(t.value?' · ':'')+'needs '+esc((m.requires||[]).map(r=>(msOf(r)||{}).name||r).join(', ')):''}${d?' · done '+fd(g[g.length-1].date)+(g[g.length-1].value!=null?' ('+g[g.length-1].value+')':''):''}</div></div>
            ${watch(m,'Watch')}${d?`<button class="btn-sm" onclick="caliMilestoneDel('${g[g.length-1].id}')">Undo</button>`:o?`<button class="btn-sm" onclick="caliMilestone('${m.id}')">Log</button>`:''}</div>`;}).join('')}</div>`;}).join('')}`;
}

// ---------- Skill ladders (owner, 15 Nov 2026) ----------
// Five skills, each a ladder of rungs with pass marks (data: goals). A rung passes only when every pass mark is
// logged at or above its number, on every side, all sets where it has sets, and FILMED (server: ladders()).
// Logging pass tests is also the placement test. Guide images are AI images of him, private on the box.
let _goalView='home', _passOpen=null;
const G=()=>(_cali&&_cali.goals)||{};
const skillOf=id=>(G().skills||[]).find(s=>s.id===id);
const lad=id=>((_cali&&_cali.ladders)||{})[id]||{passed:[],current:null,total:0};
const _cgCache={};
async function loadCg(){for(const im of document.querySelectorAll('img[data-cg]')){const n=im.dataset.cg;
  try{if(!_cgCache[n]){const r=await fetch(API_BASE+'/api/fitness/cali/img/'+encodeURIComponent(n),{cache:'no-cache',headers:{'Authorization':'Bearer '+syncToken()}});
      if(!r.ok){_cgCache[n]='none';}else _cgCache[n]=URL.createObjectURL(await r.blob());}
    if(_cgCache[n]==='none'){im.style.display='none';const ph=im.nextElementSibling;if(ph&&ph.dataset.cgph)ph.style.display='flex';}else im.src=_cgCache[n];}catch(e){}}}
const cgImg=(sid,n,small)=>`<img data-cg="cg_${sid}_${n}.jpg" alt="" style="width:100%;aspect-ratio:4/3;object-fit:cover;border-radius:${small?8:10}px;background:var(--surface2);display:block"/><div data-cgph="1" style="display:none;width:100%;aspect-ratio:4/3;border-radius:10px;background:var(--surface2);align-items:center;justify-content:center;font-size:12px;color:var(--ink3)">Guide image coming</div>`;
const fmtVal=(p,v)=>v==null?'':(p.kind==='hold'?v+' s':v+' reps');
const markText=p=>`${p.kind==='hold'?p.value+' s':(p.sets>1?p.sets+' x '+p.value:p.value)+(p.kind==='reps'?' reps':'')}${p.sides==='each'?' each side':''}`;
const nSets=d=>{const m=String(d||'').match(/^(\d+)\s*x/);return m?Math.max(1,Math.min(10,+m[1])):1;};
function bestFor(sid,p,sd){const ts=(_cali.goal_tests||[]).filter(t=>t.skill===sid&&t.test===p.id&&t.side===sd&&t.value!=null);
  if(!ts.length)return null;return ts.reduce((a,b)=>b.value>a.value?b:a);}
function markMet(sid,p){const sides=p.sides==='each'?['L','R']:['both'];
  return sides.every(sd=>(_cali.goal_tests||[]).some(t=>t.skill===sid&&t.test===p.id&&t.side===sd&&t.filmed&&t.value>=p.value&&(!(p.sets>1)||t.all_sets)));}
function rungState(sid,n){const L=lad(sid);return L.passed.includes(n)?'passed':L.current===n?'current':'ahead';}

const goalN=s=>{const rs=s.rungs||[];const g=rs.find(x=>x.goal)||rs[rs.length-1];return g?g.n:1;};
function renderGoalsView(el){
  if(_goalView.startsWith('skill:'))return renderSkill(el,_goalView.slice(6));
  if(_goalView.startsWith('day:'))return renderLadderDay(el,_goalView.slice(4));
  if(_goalView.startsWith('routine:'))return renderRoutine(el,_goalView.slice(8));
  const g=G(), sk=g.skills||[];
  if(!sk.length){el.innerHTML='<div class="fd-note">No skill ladders loaded.</div>';return;}
  const anyTests=(_cali.goal_tests||[]).length>0;
  const recent=(_cali.sessions||[]).filter(s=>/^(ladder|routine):/.test(String(s.program))).slice(0,5);
  el.innerHTML=`<div class="section-label">Skill goals</div>
    <div style="font-size:12px;color:var(--ink3);margin-bottom:10px">Five skills, each a ladder. Pass every mark on a rung, filmed, and you move up. Form first: holds in seconds, strict reps.</div>
    ${anyTests?'':`<div class="card" style="padding:12px 14px;margin-bottom:10px;border:1px solid var(--teal)"><div style="font-size:13px;font-weight:800">Placement</div>
      <div style="font-size:12px;color:var(--ink2);margin-top:3px">Open each skill and log the pass marks you can already hit, starting at rung 1. Each ladder starts you at the first rung you have not passed. Film every pass test.</div></div>`}
    ${sk.map(s=>{const L=lad(s.id), cur=L.current, r=(s.rungs||[]).find(x=>x.n===cur)||(s.rungs||[])[s.rungs.length-1], done=cur==null;
      return `<div class="card" onclick="caliGoal('skill:${s.id}')" style="padding:12px 14px;margin-bottom:10px;cursor:pointer">
        <div style="display:flex;gap:12px;align-items:center">
          <div style="width:110px;flex-shrink:0;position:relative">${cgImg(s.id,goalN(s),true)}<span style="position:absolute;left:5px;top:5px;font-size:9.5px;font-weight:800;letter-spacing:0.06em;color:#fff;background:rgba(0,0,0,0.55);border-radius:999px;padding:1px 7px">GOAL</span></div>
          <div style="flex:1;min-width:0">
            <div style="font-size:15px;font-weight:800">${esc(s.name)}</div>
            <div style="font-size:11.5px;color:var(--ink3)">Goal: ${esc(s.goal)}</div>
            <div style="font-size:12.5px;font-weight:700;margin-top:4px;color:${done?'var(--teal)':'var(--ink)'}">${done?'Goal reached':`Rung ${cur} of ${L.total}: ${esc(r.name)}`}</div>
            <div style="display:flex;gap:4px;margin-top:6px">${(s.rungs||[]).map(x=>{const st=rungState(s.id,x.n);return `<span style="flex:1;height:6px;border-radius:3px;background:${st==='passed'?'var(--teal)':st==='current'?'var(--amber)':'var(--border)'}"></span>`;}).join('')}</div>
          </div>${icon('arrow','var(--ink3)')}</div></div>`;}).join('')}
    ${(g.routines||[]).map(r=>`<div class="card" style="padding:12px 14px;margin-bottom:10px;border:1px solid var(--teal)">
      <div style="display:flex;align-items:center;gap:8px"><div style="flex:1;min-width:0"><div style="font-size:15px;font-weight:800">${esc(r.name)}</div>
        <div style="font-size:11.5px;color:var(--ink3)">${r.items.length} exercises &middot; ${esc(r.note||'')}</div></div>
        <button class="btn-sm" onclick="caliGoal('routine:${r.id}')">Start</button></div></div>`).join('')}
    <div class="card" style="padding:12px 14px;margin-bottom:10px">
      <div style="font-size:13px;font-weight:800">Training week (draft, January)</div>
      <div style="font-size:11.5px;color:var(--ink3);margin:2px 0 6px">${esc(g.week_note||'')}</div>
      ${(g.week||[]).map(w=>`<div style="display:flex;align-items:center;gap:8px;padding:7px 0;border-top:0.5px solid var(--border)">
        <span style="font-size:12px;font-weight:800;color:var(--teal);width:32px">${esc(w.day)}</span>
        <div style="flex:1;min-width:0"><div style="font-size:13px;font-weight:700">${esc(w.name)}</div><div style="font-size:11px;color:var(--ink3)">${esc(w.skills.map(id=>(skillOf(id)||{}).name||id).join(', '))}${(w.extra||[]).length?' + '+w.extra.length+' extras':''}</div></div>
        <button class="btn-sm" onclick="caliGoal('day:${w.day}')">Start</button></div>`).join('')}</div>
    ${recent.length?`<div class="card" style="padding:12px 14px;margin-bottom:10px"><div style="font-size:13px;font-weight:800;margin-bottom:4px">Recent sessions</div>
      ${recent.map(s=>`<div style="border-top:0.5px solid var(--border);padding:6px 0;font-size:12px"><div style="display:flex;justify-content:space-between"><b>${fd(s.date)} &middot; ${esc(sessLabel(s.program))}</b><button class="btn-sm" onclick="caliSessionDel('${s.id}')">Remove</button></div>
        ${s.items.map(i=>`<div style="color:var(--ink2)">${esc(i.exercise_id)}${i.sets.length?': '+esc(i.sets.map(z=>z.hold_s!=null?z.hold_s+'s':z.reps+'').join(', ')):''}${i.note?' &middot; '+esc(i.note):''}</div>`).join('')}</div>`).join('')}</div>`:''}
    <div class="card" style="padding:12px 14px;margin-bottom:10px"><div style="font-size:13px;font-weight:800;margin-bottom:4px">Flexibility end goals</div>
      ${(g.mobility_goals||[]).map(m=>`<div style="border-top:0.5px solid var(--border);padding:6px 0"><div style="font-size:13px;font-weight:700">${esc(m.name)}</div><div style="font-size:12px;color:var(--ink2)">${esc(m.goal)}</div></div>`).join('')}</div>
    <div class="card" style="padding:12px 14px;margin-bottom:10px"><div style="font-size:13px;font-weight:800;margin-bottom:4px">Next tier ${icon('lock','var(--ink3)',14)}</div>
      <div style="font-size:11.5px;color:var(--ink3);margin-bottom:4px">After the five ladders.</div>
      ${(g.next_tier||[]).map(t=>`<div style="border-top:0.5px solid var(--border);padding:6px 0"><div style="font-size:13px;font-weight:700;color:var(--ink2)">${esc(t.name)}</div><div style="font-size:11.5px;color:var(--ink3)">${esc(t.gate)}</div></div>`).join('')}</div>`;
  loadCg();
}
window.caliGoal=v=>{_goalView=v||'home';_passOpen=null;rerender();window.scrollTo(0,0);};

function drillRow(dr){return `<div style="border-top:0.5px solid var(--border);padding:7px 0;display:flex;align-items:center;gap:8px">
  <div style="flex:1;min-width:0"><div style="font-size:13px;font-weight:700">${esc(dr.name)}</div>
    <div style="font-size:11.5px;color:var(--ink3)">${esc(dr.dose)}${dr.rest_s?' · rest '+dr.rest_s+' s':''}</div>${dr.tempo?tempoGraphic(dr.tempo):''}</div>${watch(dr)}</div>`;}
function rungCard(s,r){
  const st=rungState(s.id,r.n), open=_passOpen===`${s.id}:${r.n}`;
  const chip=st==='passed'?['Passed','var(--teal)']:st==='current'?['You are here','var(--amber)']:['Ahead','var(--ink3)'];
  const hist=(_cali.goal_tests||[]).filter(t=>t.skill===s.id&&t.rung===r.n).slice(-6).reverse();
  return `<div ${st==='current'?'data-cur="1"':''} style="flex:0 0 88%;scroll-snap-align:center;box-sizing:border-box" class="card">
    <div style="padding:10px 12px">
      <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:6px"><span style="font-size:11px;font-weight:800;color:var(--ink3);text-transform:uppercase;letter-spacing:0.06em">${r.goal?'Goal':'Rung '+r.n}</span>
        <span style="font-size:11px;font-weight:800;color:${chip[1]};border:1px solid ${chip[1]};border-radius:999px;padding:1px 8px">${chip[0]}</span></div>
      ${cgImg(s.id,r.n)}
      <div style="font-size:16px;font-weight:800;margin-top:8px">${esc(r.name)}</div>
      <div style="font-size:12px;font-weight:800;color:var(--ink3);margin-top:8px;text-transform:uppercase">Pass marks${s.film?' · film '+esc(s.film.toLowerCase()):''}</div>
      ${(r.pass||[]).map(p=>{const met=markMet(s.id,p), sides=p.sides==='each'?['L','R']:['both'];
        const best=sides.map(sd=>{const b=bestFor(s.id,p,sd);return b?(sides.length>1?sd+' ':'')+fmtVal(p,b.value)+(b.filmed?'':' (not filmed)'):null;}).filter(Boolean);
        return `<div style="display:flex;align-items:center;gap:8px;padding:5px 0">${icon(met?'check':'target',met?'var(--teal)':'var(--ink3)',16)}
          <div style="flex:1;min-width:0"><div style="font-size:13px;font-weight:700">${esc(p.name)}: ${esc(markText(p))}</div>
          <div style="font-size:11px;color:var(--ink3)">${best.length?'Best: '+esc(best.join(', ')):'Not logged yet'}${p.smith?' · Smith milestone':''}</div></div></div>`;}).join('')}
      <button class="fd-btn" style="margin-top:6px" onclick="caliPassOpen('${s.id}',${r.n})">${open?'Close':'Log a pass test'}</button>
      ${open?passForm(s,r):''}
      ${(r.cues||[]).length?`<div style="font-size:12px;font-weight:800;color:var(--ink3);margin-top:10px;text-transform:uppercase">Form</div>${r.cues.map(c=>`<div style="font-size:12.5px;color:var(--ink2)">&bull; ${esc(c)}</div>`).join('')}`:''}
      <div style="font-size:12px;font-weight:800;color:var(--ink3);margin-top:10px;text-transform:uppercase">Drills for this rung</div>
      ${(r.drills||[]).map(drillRow).join('')}
      <div style="font-size:12px;margin-top:8px">${r.youtube?watch({youtube:r.youtube,name:r.name},'Video for this rung'):'<span style="color:var(--ink3)">Video for this rung: link to come.</span>'}</div>
      ${hist.length?`<div style="font-size:12px;font-weight:800;color:var(--ink3);margin-top:10px;text-transform:uppercase">Logged</div>${hist.map(t=>{const p=(r.pass||[]).find(x=>x.id===t.test)||{name:t.test,kind:'reps'};
        return `<div style="display:flex;justify-content:space-between;gap:6px;font-size:12px;padding:3px 0;border-top:0.5px solid var(--border)"><span>${fd(t.date)} · ${esc(p.name)}${t.side!=='both'?' '+t.side:''}: <b>${esc(fmtVal(p,t.value))}</b>${t.all_sets?' · all sets':''}${t.filmed?' · filmed':' · not filmed'}</span><button class="btn-sm" onclick="caliPassDel('${t.id}')">Undo</button></div>`;}).join('')}`:''}
    </div></div>`;}
function passForm(s,r){
  return `<div style="margin-top:8px;padding:10px;border-radius:8px;background:var(--surface2)">
    <div style="display:flex;gap:8px;align-items:center;margin-bottom:6px"><label class="form-label" style="margin:0">Date</label><input id="cgDay" type="date" value="${today()}" max="${today()}" class="fd-input" style="width:auto;font-size:14px"/></div>
    ${(r.pass||[]).map(p=>{const sides=p.sides==='each'?['L','R']:['both'];
      return `<div style="margin-bottom:8px"><div style="font-size:12.5px;font-weight:800">${esc(p.name)} <span style="color:var(--ink3);font-weight:600">(mark ${esc(markText(p))})</span></div>
        <div style="display:flex;gap:6px;margin-top:4px">${sides.map(sd=>`<input id="cg_${p.id}_${sd}" type="number" inputmode="decimal" class="fd-input" style="font-size:14px;padding:6px" placeholder="${(sd!=='both'?(sd==='L'?'Left ':'Right '):'')+(p.kind==='hold'?'seconds':(p.sets>1?'reps, worst set':'reps'))}"/>`).join('')}</div>
        ${p.sets>1?`<label style="display:flex;gap:8px;align-items:center;font-size:12px;margin-top:4px"><input type="checkbox" id="cg_${p.id}_all"/>All ${p.sets} sets hit that number</label>`:''}</div>`;}).join('')}
    <label style="display:flex;gap:8px;align-items:center;font-size:12.5px;font-weight:700"><input type="checkbox" id="cgFilmed"/>Filmed, form checked</label>
    <div style="font-size:11px;color:var(--ink3);margin:2px 0 6px">A rung only passes on video. Unfilmed attempts are kept as practice bests.</div>
    <input id="cgNote" class="fd-input" style="font-size:13px" placeholder="Note (optional)"/>
    <button class="fd-btn" style="margin-top:8px" onclick="caliPassSave('${s.id}',${r.n})">Save</button></div>`;}
window.caliPassOpen=(sid,n)=>{_passOpen=_passOpen===`${sid}:${n}`?null:`${sid}:${n}`;const k=`${sid}:${n}`;rerender();
  setTimeout(()=>{const c=[...document.querySelectorAll('#page-calisthenics-goals [data-rung]')].find(x=>x.dataset.rung===k);if(c)c.scrollIntoView({inline:'center',block:'nearest'});},60);};
window.caliPassSave=async function(sid,n){
  const s=skillOf(sid), r=(s.rungs||[]).find(x=>x.n===n);const day=(document.getElementById('cgDay')||{}).value||today();
  const filmed=!!(document.getElementById('cgFilmed')||{}).checked, note=(document.getElementById('cgNote')||{}).value||'';
  const posts=[];
  (r.pass||[]).forEach(p=>{(p.sides==='each'?['L','R']:['both']).forEach(sd=>{const e=document.getElementById(`cg_${p.id}_${sd}`);
    if(e&&e.value!=='')posts.push({skill:sid,rung:n,test:p.id,side:sd,value:+e.value,date:day,filmed,note,all_sets:!!(document.getElementById(`cg_${p.id}_all`)||{}).checked});});});
  if(!posts.length){toast('Nothing filled in, so nothing was saved');return;}
  const before=lad(sid).current;
  try{for(const b of posts)await syncApi('/api/fitness/cali/goal-test',{method:'POST',body:JSON.stringify(b)});
    _passOpen=null;await load();rerender();
    const after=lad(sid).current;
    toast(after!==before?(after==null?'Goal reached!':'Rung passed: now on rung '+after):filmed?'Saved':'Saved as practice: not filmed, so it does not pass the rung');}
  catch(e){toast('Could not save. Check the values.');}
};
window.caliPassDel=async function(uid){if(!confirm('Remove this entry?'))return;await syncApi('/api/fitness/cali/goal-test/'+uid,{method:'DELETE'});await load();rerender();};
function renderSkill(el,sid){
  const s=skillOf(sid);if(!s){_goalView='home';return renderGoalsView(el);}
  const L=lad(sid);
  el.innerHTML=`${bigBack("caliGoal('home')",'All goals')}
    <div style="font-size:18px;font-weight:800">${esc(s.name)}</div>
    <div style="font-size:12.5px;font-weight:700;color:var(--teal)">Goal: ${esc(s.goal)}</div>
    <div style="font-size:12px;color:var(--ink2);margin-top:4px">${esc(s.why||'')}</div>
    <div style="font-size:11.5px;color:var(--ink3);margin-top:2px">Kit: ${esc(s.equipment||'')} · ${L.passed.length} of ${L.total} rungs passed</div>
    <div style="display:flex;gap:6px;margin:10px 0 6px;flex-wrap:wrap">${(s.rungs||[]).map(r=>{const st=rungState(sid,r.n);
      const c=st==='current'?'var(--amber)':st==='passed'?'var(--teal)':'';
      return `<button class="btn-sm" data-rb="${sid}:${r.n}" data-c="${c}" onclick="caliRungJump('${sid}:${r.n}')" style="font-weight:800;${c?'border-color:'+c+';color:'+c:''}">${r.goal?'Goal':r.n}</button>`;}).join('')}</div>
    <div style="font-size:11px;color:var(--ink3);margin-bottom:6px">Swipe through every rung, including the ones ahead.</div>
    <div id="cgCarousel" style="display:flex;gap:10px;overflow-x:auto;scroll-snap-type:x mandatory;padding-bottom:8px;-webkit-overflow-scrolling:touch">
      ${(s.rungs||[]).map(r=>rungCard(s,r).replace('<div ','<div data-rung="'+sid+':'+r.n+'" ')).join('')}</div>`;
  loadCg();
  setTimeout(()=>{const k=_passOpen||(L.current!=null?sid+':'+L.current:sid+':'+(s.rungs[s.rungs.length-1].n));caliRungJump(k,true);rungInView();},40);
  const box=document.getElementById('cgCarousel');let raf=0;
  if(box)box.addEventListener('scroll',()=>{clearTimeout(raf);raf=setTimeout(rungInView,60);},{passive:true});
}
// highlight the button of the rung card nearest the middle of the carousel: filled = the one you are looking at
function rungInView(){const box=document.getElementById('cgCarousel');if(!box)return;
  const mid=box.scrollLeft+box.clientWidth/2;let best=null,bd=1e9;
  box.querySelectorAll('[data-rung]').forEach(c=>{const d=Math.abs(c.offsetLeft-box.offsetLeft+c.clientWidth/2-mid);if(d<bd){bd=d;best=c.dataset.rung;}});
  document.querySelectorAll('[data-rb]').forEach(b=>{const on=b.dataset.rb===best, c=b.dataset.c;
    b.style.background=on?'var(--teal)':'';b.style.color=on?'#fff':c;b.style.borderColor=on?'var(--teal)':c;});}
window.caliRungJump=(k,instant)=>{const c=[...document.querySelectorAll('#cgCarousel [data-rung]')].find(x=>x.dataset.rung===k);
  const box=document.getElementById('cgCarousel');if(c&&box)box.scrollTo({left:c.offsetLeft-box.offsetLeft-(box.clientWidth-c.clientWidth)/2,behavior:instant?'auto':'smooth'});};

// a session from the draft week: warm-ups, then each skill's CURRENT rung drills, then the day's extras
function lastDrill(name){for(const s of (_cali.sessions||[]))if(/^(ladder|routine):/.test(String(s.program))){const i=s.items.find(x=>x.exercise_id===name&&x.sets.length);if(i)return {date:s.date,it:i};}return null;}
function setInputs(key,dr){const n=nSets(dr.dose), hold=dr.kind==='hold', last=lastDrill(dr.name);
  return `${last?`<div style="font-size:11px;color:var(--ink3);margin-top:2px">Last time (${fd(last.date)}): ${esc(last.it.sets.map(z=>z.hold_s!=null?z.hold_s+'s':z.reps+'').join(', '))}</div>`:''}
    <div style="display:grid;grid-template-columns:repeat(auto-fill,minmax(64px,1fr));gap:6px;margin-top:6px">${Array.from({length:n},(_,i)=>`<input data-cgset="${key}" data-hold="${hold?1:0}" type="number" inputmode="decimal" class="fd-input" style="font-size:14px;padding:6px" placeholder="${hold?'s':'reps'} ${i+1}"/>`).join('')}</div>
    ${dr.rest_s&&window.mobRest?`<button class="btn-sm" style="margin-top:6px" onclick="mobRest(${dr.rest_s})">Rest timer ${dr.rest_s} s</button>`:''}`;}
const _dayItems=[];
function renderLadderDay(el,day){
  const g=G(), w=(g.week||[]).find(x=>x.day===day);if(!w){_goalView='home';return renderGoalsView(el);}
  _dayItems.length=0;
  const block=(title,inner)=>`<div class="card" style="padding:12px 14px;margin-bottom:10px"><div style="font-size:14px;font-weight:800;margin-bottom:4px">${title}</div>${inner}</div>`;
  const warm=(g.warmups||[]).filter(x=>x.id==='wrist'||w.name.startsWith('Upper')).map(wu=>block(esc(wu.name),
    `<div style="font-size:11.5px;color:var(--ink3);margin-bottom:4px">${esc(wu.note||'')}</div>${wu.items.map(it=>`<label style="display:flex;gap:8px;align-items:center;font-size:12.5px;padding:3px 0"><input type="checkbox" class="cgw_${wu.id}"/><span style="flex:1">${esc(it.name)} <span style="color:var(--ink3)">${esc(it.dose)}</span></span>${watch(it,'Demo')}</label>`).join('')}
    ${wu.id==='wrist'?'<div style="display:flex;gap:8px;align-items:center;margin-top:6px"><label class="form-label" style="margin:0">Wrist pain 0-10</label><input id="cgWristPain" type="number" min="0" max="10" inputmode="numeric" class="fd-input" style="width:70px;font-size:14px;padding:6px"/></div>':''}`)).join('');
  const skillsHtml=w.skills.map(id=>{const s=skillOf(id);if(!s)return '';const L=lad(id), r=(s.rungs||[]).find(x=>x.n===L.current)||s.rungs[s.rungs.length-1];
    return block(`${esc(s.name)} <span style="font-size:12px;color:var(--ink3);font-weight:600">· ${r.goal?'Goal':'rung '+r.n}: ${esc(r.name)}</span>`,
      `<div style="width:120px;margin:4px 0 6px">${cgImg(s.id,r.n,true)}</div>${(r.drills||[]).map(dr=>{const k=_dayItems.push({code:`${id}:${r.n}`,exercise_id:dr.name})-1;
        return `<div style="border-top:0.5px solid var(--border);padding:8px 0"><div style="display:flex;align-items:center;gap:8px"><div style="flex:1;font-size:13px;font-weight:800">${esc(dr.name)}</div>${watch(dr)}</div>
          <div style="font-size:11.5px;color:var(--ink3)">${esc(dr.dose)}</div>${dr.tempo?tempoGraphic(dr.tempo):''}${setInputs(k,dr)}</div>`;}).join('')}
      <div style="font-size:11.5px;margin-top:6px"><a href="#" onclick="event.preventDefault();caliGoal('skill:${id}')" style="color:var(--teal);font-weight:700">Hit a pass mark? Log it on the ladder</a></div>`);}).join('');
  const extras=(w.extra||[]).length?block('Extras',(w.extra||[]).map(dr=>{const k=_dayItems.push({code:'extra',exercise_id:dr.name})-1;
    return `<div style="border-top:0.5px solid var(--border);padding:8px 0"><div style="display:flex;align-items:center;gap:8px"><div style="flex:1;font-size:13px;font-weight:800">${esc(dr.name)}</div>${watch(dr)}</div>
      <div style="font-size:11.5px;color:var(--ink3)">${esc(dr.dose)}</div>${dr.tempo?tempoGraphic(dr.tempo):''}${setInputs(k,dr)}</div>`;}).join('')):'';
  el.innerHTML=`${bigBack("caliGoal('home')",'All goals')}
    <div style="font-size:18px;font-weight:800">${esc(w.day)} · ${esc(w.name)}</div>
    <div style="display:flex;gap:8px;align-items:center;margin:8px 0 10px"><label class="form-label" style="margin:0">Date</label><input id="cgSessDay" type="date" value="${today()}" max="${today()}" class="fd-input" style="width:auto;font-size:14px"/></div>
    ${warm}${skillsHtml}${extras}
    <input id="cgSessNote" class="fd-input" style="font-size:13px;margin-bottom:8px" placeholder="Session note (energy, form, anything that hurt)"/>
    <button class="fd-btn" onclick="caliLadderSave('${w.day}')">Save session</button>`;
  loadCg();
}
// a ROUTINE (owner, 19 Nov 2026): a fixed list of exercises with their own guide images, logged like a week day
const routineOf=id=>(G().routines||[]).find(r=>r.id===id);
const sessLabel=p=>{p=String(p);if(p.startsWith('routine:')){const r=routineOf(p.slice(8));return r?r.name:p.slice(8);}return p.slice(7);};
function renderRoutine(el,id){
  const g=G(), r=routineOf(id);if(!r){_goalView='home';return renderGoalsView(el);}
  _dayItems.length=0;
  const block=(title,inner,bd)=>`<div class="card" style="padding:12px 14px;margin-bottom:10px${bd?';border:1px solid var(--amber)':''}"><div style="font-size:14px;font-weight:800;margin-bottom:4px">${title}</div>${inner}</div>`;
  const warm=(g.warmups||[]).filter(x=>(r.warmups||[]).includes(x.id)).map(wu=>block(esc(wu.name),
    `<div style="font-size:11.5px;color:var(--ink3);margin-bottom:4px">${esc(wu.note||'')}</div>${wu.items.map(it=>`<label style="display:flex;gap:8px;align-items:center;font-size:12.5px;padding:3px 0"><input type="checkbox" class="cgw_${wu.id}"/><span style="flex:1">${esc(it.name)} <span style="color:var(--ink3)">${esc(it.dose)}</span></span>${watch(it,'Demo')}</label>`).join('')}
    ${wu.id==='wrist'?'<div style="display:flex;gap:8px;align-items:center;margin-top:6px"><label class="form-label" style="margin:0">Wrist pain 0-10</label><input id="cgWristPain" type="number" min="0" max="10" inputmode="numeric" class="fd-input" style="width:70px;font-size:14px;padding:6px"/></div>':''}`)).join('');
  const items=r.items.map((it,i)=>{const k=_dayItems.push({code:`${r.id}:${it.n}`,exercise_id:it.name})-1, prev=r.items[i-1];
    const ss=it.superset||(prev&&prev.superset);
    return block(`${it.n}. ${esc(it.name)}`,`<div style="display:flex;gap:10px;align-items:flex-start;margin:4px 0 6px">
        <div style="width:120px;flex-shrink:0">${cgImg(r.id,it.n,true)}</div>
        <div style="flex:1;min-width:0"><div style="font-size:12.5px;font-weight:700">${esc(it.dose)}${it.rest_s?' · rest '+(it.rest_s>=60&&it.rest_s%60===0?(it.rest_s/60)+' min':it.rest_s+' s'):' · no rest, straight into the next'}</div>
          ${it.tempo?tempoGraphic(it.tempo):''}<div style="margin-top:6px">${watch(it)}</div></div></div>
      ${(it.cues||[]).map(c=>`<div style="font-size:12px;color:var(--ink2)">&bull; ${esc(c)}</div>`).join('')}
      ${it.progression?`<div style="font-size:11.5px;color:var(--ink3);margin-top:4px">Next: ${esc(it.progression)}</div>`:''}
      ${setInputs(k,it)}`,ss);}).join('');
  el.innerHTML=`${bigBack("caliGoal('home')",'All goals')}
    <div style="font-size:18px;font-weight:800">${esc(r.name)}</div>
    <div style="font-size:12px;color:var(--ink3);margin:2px 0 4px">${esc(r.note||'')}</div>
    <div style="display:flex;gap:8px;align-items:center;margin:8px 0 10px"><label class="form-label" style="margin:0">Date</label><input id="cgSessDay" type="date" value="${today()}" max="${today()}" class="fd-input" style="width:auto;font-size:14px"/></div>
    ${warm}${items}
    <input id="cgSessNote" class="fd-input" style="font-size:13px;margin-bottom:8px" placeholder="Session note (energy, form, anything that hurt)"/>
    <button class="fd-btn" onclick="caliLadderSave('routine:${r.id}')">Save session</button>
    <div style="font-size:10.5px;color:var(--ink3);margin-top:8px">${esc(r.source||'')}</div>`;
  loadCg();
}
window.caliLadderSave=async function(day){
  const items=_dayItems.map((x,k)=>{const sets=[...document.querySelectorAll(`[data-cgset="${k}"]`)].filter(e=>e.value!=='').map(e=>e.dataset.hold==='1'?{hold_s:+e.value}:{reps:+e.value});return {...x,sets,note:''};});
  const warm=[];(G().warmups||[]).forEach(wu=>{const b=document.querySelectorAll('.cgw_'+wu.id);if(b.length)warm.push(`${wu.name} ${[...b].filter(e=>e.checked).length}/${b.length}`);});
  const pain=(document.getElementById('cgWristPain')||{}).value;
  if(warm.length)items.unshift({code:'warmup',exercise_id:'Warm-up',sets:[],note:warm.join('; ')+(pain!==''&&pain!=null?'; wrist pain '+pain+'/10':'')});
  if(!items.some(i=>i.sets.length)){toast('Log at least one set first');return;}
  const wu=(G().warmups||[]).some(w=>{const b=document.querySelectorAll('.cgw_'+w.id);return b.length&&[...b].some(e=>!e.checked);});
  if(wu&&!confirm('Warm-up not finished. Save anyway?'))return;
  try{await syncApi('/api/fitness/cali/ladder-session',{method:'POST',body:JSON.stringify({week_day:day,date:document.getElementById('cgSessDay').value||today(),items,note:document.getElementById('cgSessNote').value||''})});
    _goalView='home';await load();rerender();window.scrollTo(0,0);toast('Session saved');}
  catch(e){toast('Could not save the session.');}
};

// ---------- Library ----------
window.caliWristToggle=async function(id,on){const cur=new Set(_cali.wrist||[]);on?cur.add(id):cur.delete(id);_cali.wrist=[...cur];
  try{await syncApi('/api/fitness/cali/current',{method:'POST',body:JSON.stringify({wrist:_cali.wrist})});}catch(e){toast('Could not save your routine.');}};
function renderLibrary(el){
  const lib=D().wrist_library||[], exs=D().exercises||[], fof=D().fear_of_falling||[], sl=D().shape_levels||[];
  const list=(t,xs)=>xs&&xs.length?`<div style="font-size:11px;font-weight:800;color:var(--ink3);margin-top:6px;text-transform:uppercase">${t}</div>${xs.map(x=>`<div style="font-size:12px">&bull; ${esc(x)}</div>`).join('')}`:'';
  el.innerHTML=`<div class="section-label">Library</div>
    <details class="card" style="padding:12px 14px;margin-bottom:10px" open><summary style="cursor:pointer;font-size:13px;font-weight:800">Wrist warm-up library &middot; tick your routine</summary>
      <div style="font-size:11px;color:var(--ink3);margin:4px 0">Wrist warm-ups are individual: pick what feels good, 5-10 reps each, build the load towards bodyweight.</div>
      ${lib.map(w=>`<div style="display:flex;align-items:center;gap:8px;padding:6px 0;border-top:0.5px solid var(--border)"><input type="checkbox" ${(_cali.wrist||[]).includes(w.id)?'checked':''} onchange="caliWristToggle('${w.id}',this.checked)"/>
        <div style="flex:1;min-width:0"><div style="font-size:13px;font-weight:700">${esc(w.name)}</div><div style="font-size:11px;color:var(--ink3)">${esc((w.cues||[]).join(' · '))}</div></div>${watch(w)}</div>`).join('')}</details>
    ${fof.length?`<details class="card" style="padding:12px 14px;margin-bottom:10px"><summary style="cursor:pointer;font-size:13px;font-weight:800">Overcoming the fear of falling &middot; ${fof.length} steps</summary>
      ${fof.map(f=>`<div style="padding:6px 0;border-top:0.5px solid var(--border)"><div style="font-size:13px;font-weight:700">${f.step}. ${esc(f.name)}</div><div style="font-size:12px;color:var(--ink2)">${esc(f.what||'')}</div>${f.measure?`<div style="font-size:11px;color:var(--ink3)">Track: ${esc(f.measure)}</div>`:''}</div>`).join('')}</details>`:''}
    ${sl.length?`<details class="card" style="padding:12px 14px;margin-bottom:10px"><summary style="cursor:pointer;font-size:13px;font-weight:800">Shape change levels</summary>
      ${sl.map(l=>`<div style="padding:6px 0;border-top:0.5px solid var(--border)"><div style="font-size:12px;font-weight:800">Level ${l.level}</div><div style="font-size:12px;color:var(--ink2)">${esc((l.items||[]).join(' · '))}</div></div>`).join('')}</details>`:''}
    <details class="card" style="padding:12px 14px;margin-bottom:10px"><summary style="cursor:pointer;font-size:13px;font-weight:800">Every exercise &middot; ${exs.length}</summary>
      ${exs.map(e=>`<details style="border-top:0.5px solid var(--border);padding:6px 0"><summary style="cursor:pointer;list-style:none;display:flex;align-items:center;gap:8px"><div style="flex:1;font-size:13px;font-weight:700">${esc(e.name)}</div>${watch(e)}</summary>
        ${list('Cues',e.cues)}${list('Common mistakes',e.mistakes)}</details>`).join('')}</details>`;
}
})();
