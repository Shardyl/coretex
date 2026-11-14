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
  return `<button class="btn-sm" style="display:inline-flex;align-items:center;gap:5px;font-weight:700" onclick="event.stopPropagation();caliLessonN(${i})"><svg width="13" height="13" viewBox="0 0 24 24" fill="currentColor" style="flex-shrink:0" aria-hidden="true"><path d="M7 4v16l13 -8z"/></svg>${label||'Watch'}</button>`;};

window.renderCalisthenics=async function(sub){
  const el=document.getElementById('page-calisthenics-'+(sub||'program'));if(!el)return;
  if(!_cali){el.innerHTML='<div class="fd-note" style="padding:20px 0">Loading&hellip;</div>';await load();}
  if(!_cali){el.innerHTML='<div class="fd-note" style="padding:20px 0">Could not reach Cortex.</div>';return;}
  if(sub==='tree')return renderTree(el);
  if(sub==='library')return renderLibrary(el);
  if(_caliView==='session')return renderSession(el);
  renderProgram(el);
};
const rerender=()=>{const s=(typeof currentSubpage!=='undefined'&&currentSubpage.calisthenics)||'program';renderCalisthenics(s);};

// ---------- Program ----------
function itemLine(it){const e=ex(it.exercise_id);const bits=[it.reps,it.sets?it.sets+' sets':'',it.rest?'rest '+it.rest:'',it.tempo?'tempo '+it.tempo:''].filter(x=>x&&!/^-+( sets)?$/.test(x)&&x!=='rest -');
  return `<div style="border-top:0.5px solid var(--border);padding:8px 0">
    <div style="display:flex;align-items:center;gap:8px"><span style="font-size:11px;font-weight:800;color:var(--teal);width:24px">${esc(it.code)}</span>
      <div style="flex:1;min-width:0"><div style="font-size:13px;font-weight:700">${esc(e.name)}</div><div style="font-size:11px;color:var(--ink3)">${esc(bits.join(' · '))}</div></div>${watch(e)}</div>
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
  el.innerHTML=`<button class="btn-sm" onclick="caliBack()" style="margin-bottom:10px">&lsaquo; ${esc(p.name)}</button>
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
