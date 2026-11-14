// Mobility tab (owner, 8 Nov 2026): Matthew Smith's 8 flexibility pillars. Each pillar lists its assessment tests
// (his names, from setting fitness_mobility_tests on the server), takes the day's values per side, and keeps the
// owner's videos/photos of each test. Results and media are server-owned (/api/fitness/mobility), not in the sync
// document. Media is private: fetched with the device token and shown from a blob, never a public link.
// 9 Nov 2026 (owner: "I can't see what's been done and what's not been done"): the home shows a progress ring and a
// status per pillar plus a "Next to do" list; a pillar opens on a checklist grid (test x side) and its active vs
// passive gap. Everything is computed from the tests in the setting, so a new pillar's tests show up with no code.
(function(){
let _mob=null, _mobView='list', _mobDay=null;
const SIDE={L:'Left',R:'Right',both:'Both'};
const UNIT={deg:'°',cm:' cm',s:' s'};
const esc=s=>String(s==null?'':s).replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
const today=()=>{const d=new Date();return new Date(d.getTime()-d.getTimezoneOffset()*60000).toISOString().slice(0,10);};
const fd=d=>new Date(d+'T12:00:00').toLocaleDateString('en-GB',{day:'numeric',month:'short',year:'numeric'});
const mb=b=>b>1e6?(b/1e6).toFixed(0)+' MB':Math.max(1,Math.round(b/1e3))+' KB';
const r1=v=>Math.round(v*10)/10;

// Tabler-style line icons (no emoji in product UI)
const ICON={check:'<path d="M5 12l5 5l10 -10"/>',empty:'<circle cx="12" cy="12" r="8"/>',
  camera:'<path d="M5 7h1a2 2 0 0 0 2 -2a1 1 0 0 1 1 -1h6a1 1 0 0 1 1 1a2 2 0 0 0 2 2h1a2 2 0 0 1 2 2v9a2 2 0 0 1 -2 2h-14a2 2 0 0 1 -2 -2v-9a2 2 0 0 1 2 -2"/><circle cx="12" cy="13" r="3"/>',
  alert:'<circle cx="12" cy="12" r="9"/><path d="M12 8v4"/><path d="M12 16h.01"/>',
  half:'<circle cx="12" cy="12" r="9"/><path d="M12 3a9 9 0 0 1 0 18z" fill="currentColor"/>',
  arrow:'<path d="M9 6l6 6l-6 6"/>'};
const icon=(k,c,s)=>`<svg width="${s||16}" height="${s||16}" viewBox="0 0 24 24" fill="none" stroke="${c||'currentColor'}" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" style="color:${c||'currentColor'};flex-shrink:0" aria-hidden="true">${ICON[k]}</svg>`;
const ST={done:{c:'var(--teal)',i:'check',t:'Measured'},prov:{c:'var(--amber)',i:'alert',t:'Provisional'},
  partial:{c:'var(--amber)',i:'half',t:'Part measured'},filmed:{c:'var(--blue)',i:'camera',t:'Filmed, not measured'},
  empty:{c:'var(--ink3)',i:'empty',t:'Not done'}};

async function load(){
  try{_mob=await syncApi('/api/fitness/mobility');}catch(e){_mob=null;}
  return _mob;
}
const pillar=id=>(_mob&&_mob.pillars||[]).find(p=>p.id===id);
const resFor=(p,t,side,day)=>(_mob.results||[]).filter(r=>r.pillar===p&&r.test===t&&r.side===side&&(!day||r.date===day));
const clipsOf=(p,t)=>(_mob.media||[]).filter(m=>m.pillar===p&&m.test===t);
const fmtV=(m,r)=>!r?'':(r.label!=null?r.label:(r.value!=null?r1(r.value)+(UNIT[m.unit]||''):''));

// ---------- status, computed from the tests in the setting ----------
function cellState(p,t,side){
  const days=[...new Set(resFor(p.id,t.id,side).map(r=>r.date))].sort(), d=days[days.length-1]||null;
  const rs=d?resFor(p.id,t.id,side,d):[];
  const have=t.measures.filter(m=>rs.some(r=>r.measure===m.key)), missing=t.measures.filter(m=>!have.includes(m));
  const clips=clipsOf(p.id,t.id).filter(x=>!x.source&&(x.side===side||x.side==='both'));
  const prov=rs.some(r=>r.provisional);
  const st=have.length&&!missing.length?(prov?'prov':'done'):have.length?(prov?'prov':'partial'):clips.length?'filmed':'empty';
  const first=have[0]?fmtV(have[0],rs.find(r=>r.measure===have[0].key)):'';
  return {st,d,rs,have,missing,clips,prov,first};
}
function testState(p,t){
  const cells=t.sides.map(s=>({side:s,...cellState(p,t,s)}));
  return {cells,done:cells.every(c=>c.st==='done'),touched:cells.some(c=>c.st!=='empty')};
}
function pillarState(p){
  const tests=(p.tests||[]).map(t=>({t,...testState(p,t)}));
  const done=tests.filter(x=>x.done).length, total=tests.length;
  const days=[...new Set((_mob.results||[]).filter(r=>r.pillar===p.id).map(r=>r.date))].sort();
  const status=!total?'none':done===total?'baseline':tests.some(x=>x.touched)?'progress':'start';
  return {tests,done,total,status,last:days[days.length-1]||null};
}
const sidesTxt=ss=>ss.length===2?'L and R':ss.map(s=>s==='both'?'':s).join('');
function todos(p){
  const out=[];
  pillarState(p).tests.forEach(({t,cells})=>{
    const by=k=>cells.filter(c=>c.st===k).map(c=>c.side), lr=s=>t.sides.length>1&&s.length?' ('+sidesTxt(s)+')':'';
    const e=by('empty'), f=by('filmed'), pa=cells.filter(c=>c.st==='partial'), pr=by('prov');
    if(e.length)out.push({p:p.id,t:t.id,k:'empty',txt:`Film ${t.name}${lr(e)}`});
    if(f.length)out.push({p:p.id,t:t.id,k:'filmed',txt:`Measure ${t.name}${lr(f)}, clip uploaded`});
    if(pa.length)out.push({p:p.id,t:t.id,k:'partial',txt:`Measure ${t.name}: ${pa[0].missing.map(m=>m.label.toLowerCase()).join(', ')}${lr(pa.map(c=>c.side))}${pa.some(c=>c.clips.length)?', clip uploaded':''}`});
    if(pr.length)out.push({p:p.id,t:t.id,k:'prov',txt:`Retest ${t.name}${lr(pr)}, provisional`});
  });
  return out;
}
// active vs passive: the same shape reached with help (passive) and held by strength (active)
function gapFor(p){
  const g=p.gap;if(!g)return null;
  const tp=(p.tests||[]).find(t=>t.id===g.passive.test), ta=(p.tests||[]).find(t=>t.id===g.active.test);
  if(!tp||!ta)return null;
  const val=(t,side,key)=>{const c=cellState(p,t,side);const r=c.rs.find(x=>x.measure===key);return r&&r.value!=null?{v:r.value,prov:!!r.provisional}:null;};
  const rows=ta.sides.map(s=>{
    const a=val(ta,s,g.active.measure), pv=val(tp,tp.sides.includes(s)?s:tp.sides[0],g.passive.measure);
    return {side:s,a,pv,gap:a&&pv?Math.round(pv.v-a.v):null};});
  return {tp,ta,rows,any:rows.some(r=>r.gap!=null)};
}

function ring(f,c,txt){const r=15,C=2*Math.PI*r;
  return `<div style="position:relative;width:40px;height:40px;flex-shrink:0"><svg width="40" height="40" viewBox="0 0 40 40" aria-hidden="true"><circle cx="20" cy="20" r="${r}" fill="none" stroke="var(--border)" stroke-width="4"/>${f>0?`<circle cx="20" cy="20" r="${r}" fill="none" stroke="${c}" stroke-width="4" stroke-linecap="round" stroke-dasharray="${(f*C).toFixed(1)} ${C.toFixed(1)}" transform="rotate(-90 20 20)"/>`:''}</svg>
    <div style="position:absolute;inset:0;display:flex;align-items:center;justify-content:center;font-size:10.5px;font-weight:800">${txt}</div></div>`;}
const CHIP={none:['Tests not added yet','var(--ink3)'],start:['Not started','var(--ink3)'],progress:['In progress','var(--amber)'],baseline:['Baseline done','var(--teal)']};
const chip=k=>`<span style="font-size:10px;font-weight:800;letter-spacing:0.06em;text-transform:uppercase;color:${CHIP[k][1]};border:1px solid ${CHIP[k][1]};border-radius:10px;padding:1px 7px;white-space:nowrap">${CHIP[k][0]}</span>`;

window.renderMobility=async function(){
  const el=document.getElementById('page-mobility-pillars');if(!el)return;
  if(!_mob){el.innerHTML='<div class="fd-note" style="padding:20px 0">Loading&hellip;</div>';await load();}
  if(!_mob){el.innerHTML='<div class="fd-note" style="padding:20px 0">Could not reach Cortex.</div>';return;}
  if(_mobView==='list')return renderList(el);
  if(String(_mobView).startsWith('session:'))return renderSession(el,String(_mobView).slice(8));
  renderPillar(el,pillar(_mobView));
};

function todoBlock(items,max,title){
  if(!items.length)return '';
  return `<div class="card" style="padding:12px 14px;margin-bottom:12px"><div style="font-size:13px;font-weight:800;margin-bottom:6px">${title}</div>
    ${items.slice(0,max).map(x=>`<div onclick="${x.on||`mobOpen('${x.p}','${x.t||''}')`}" style="display:flex;align-items:center;gap:8px;padding:6px 0;border-top:0.5px solid var(--border);cursor:pointer;font-size:13px">
      ${icon(ST[x.k].i,ST[x.k].c)}<span style="flex:1">${esc(x.txt)}</span>${icon('arrow','var(--ink3)',14)}</div>`).join('')}
    ${items.length>max?`<div style="font-size:11px;color:var(--ink3);margin-top:4px">and ${items.length-max} more</div>`:''}</div>`;
}

// focus pillars (owner, 14 Nov 2026): Next to do shows only these, plus any pillar with a running program
const focusIds=()=>{const f=new Set(_mob.focus||['pancake','shoulder_flexion']);(_mob.programs||[]).forEach(x=>{if(x.status==='active')f.add(x.pillar);});return f;};
function sessionTodos(p){const a=(_mob.programs||[]).find(x=>x.pillar===p.id&&x.status==='active');if(!a)return [];
  const n=a.sessions_done||0, N=a.sessions_per_phase||6;
  return [a.retest_due?{p:p.id,k:'prov',txt:`${p.name}: phase done, deload then retest`}
    :{p:p.id,k:'done',txt:`${p.name}: session ${n+1} of ${N}`,on:`mobSessionOpen('${a.id}')`}];}
window.mobFocus=async function(id,on){const f=new Set(_mob.focus||['pancake','shoulder_flexion']);on?f.add(id):f.delete(id);_mob.focus=[...f];
  try{await syncApi('/api/fitness/mobility/focus',{method:'POST',body:JSON.stringify({pillars:_mob.focus})});}catch(e){toast('Could not save.');}renderMobility();};
function renderList(el){
  const F=focusIds(), all=[];
  _mob.pillars.filter(p=>F.has(p.id)).forEach(p=>all.push(...sessionTodos(p)));
  _mob.pillars.filter(p=>F.has(p.id)).forEach(p=>all.push(...todos(p)));
  const order=[..._mob.pillars.filter(p=>F.has(p.id)),..._mob.pillars.filter(p=>!F.has(p.id))];
  el.innerHTML=`<div class="section-label">Flexibility pillars</div>
    <div style="font-size:12px;color:var(--ink3);margin-bottom:12px">Matthew Smith's eight skills. Your focus pillars are at the top; Next to do covers only those. Change focus inside any pillar.</div>
    ${todoBlock(all,6,'Next to do')}
    ${(_mob.general_lessons||[]).length?`<details class="card" style="padding:12px 14px;margin-bottom:12px"><summary style="cursor:pointer;font-size:13px;font-weight:800">Key lessons &middot; ${_mob.general_lessons.length}</summary>
      <div style="display:flex;gap:6px;flex-wrap:wrap;margin-top:8px">${_mob.general_lessons.map(x=>lessonBtn(x,x.name)).join('')}</div></details>`:''}
    ${order.map((p,i)=>(i===F.size&&F.size<order.length?'<div class="section-label" style="margin-top:14px">Other pillars</div>':'')+(()=>{
      const s=pillarState(p), ready=s.total>0, g=ready?gapFor(p):null;
      const gtxt=g&&g.any?'Gap '+g.rows.filter(r=>r.gap!=null).map(r=>(g.ta.sides.length>1?r.side+' ':'')+r.gap+'°').join(' &middot; '):'';
      return `<div class="card" style="padding:12px 14px;margin-bottom:10px;${ready?'cursor:pointer':'opacity:0.55'}" ${ready?`onclick="mobOpen('${p.id}')"`:''}>
        <div style="display:flex;align-items:center;gap:12px">
          ${ready?ring(s.done/s.total,s.status==='baseline'?'var(--teal)':'var(--amber)',s.done+'/'+s.total):ring(0,'var(--border)','&ndash;')}
          <div style="flex:1;min-width:0"><div style="display:flex;align-items:center;gap:8px;flex-wrap:wrap"><span style="font-size:15px;font-weight:800">${esc(p.name)}</span>${chip(s.status)}</div>
            <div style="font-size:12px;color:var(--ink3);margin-top:3px">${ready?`${s.total} tests &middot; ${s.last?'last assessed '+fd(s.last):'not assessed yet'}${gtxt?' &middot; <b style="color:var(--ink2)">'+gtxt+'</b>':''}`:'Tests added when we reach this assessment'}</div></div>
          ${ready?icon('arrow','var(--ink3)',18):''}</div></div>`;})()).join('')}`;
}

window.mobOpen=function(id,tid){_mobView=id;_mobDay=null;renderMobility();
  setTimeout(()=>{const e=tid&&document.getElementById('mobt_'+tid);if(e)e.scrollIntoView({behavior:'smooth',block:'start'});else window.scrollTo(0,0);},30);};
window.mobBack=function(){_mobView='list';renderMobility();};
window.mobDay=function(v){_mobDay=v||today();renderMobility();};

function inputFor(p,t,side,m,cur){
  const id=`mob_${t.id}_${side}_${m.key}`;
  const v=cur.find(r=>r.measure===m.key);
  if(m.kind==='choice')return `<label class="form-label" style="margin-top:6px">${esc(m.label)}</label>
    <select id="${id}" style="width:100%;font-size:14px"><option value="">Not done</option>${m.options.map(o=>`<option${v&&v.label===o?' selected':''}>${esc(o)}</option>`).join('')}</select>`;
  const u=m.unit==='deg'?'degrees':m.unit==='s'?'seconds':m.unit;
  return `<label class="form-label" style="margin-top:6px">${esc(m.label)}${u?' ('+esc(u)+')':''}</label>
    <input id="${id}" type="number" step="any" inputmode="decimal" class="fd-input" style="font-size:15px" value="${v&&v.value!=null?v.value:''}" placeholder="${m.angle?'measure it on the video, or type it':''}"/>`;
}

function gridBlock(p,s){
  const two=p.tests.some(t=>t.sides.length>1);
  const cell=(c,span)=>{const k=ST[c.st];
    return `<td ${span?'colspan="2"':''} style="padding:6px 4px;text-align:center;vertical-align:middle">
      <div title="${esc(k.t)}" style="display:flex;flex-direction:column;align-items:center;gap:2px">${icon(k.i,k.c,18)}
      ${c.first?`<div style="font-size:10px;font-weight:700;color:${k.c};max-width:${span?150:70}px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">${esc(c.first)}</div>`:''}</div></td>`;};
  return `<div class="card" style="padding:12px 14px;margin:12px 0">
    <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:6px"><div style="font-size:13px;font-weight:800">Assessment checklist</div>${ring(s.done/Math.max(1,s.total),s.status==='baseline'?'var(--teal)':'var(--amber)',s.done+'/'+s.total)}</div>
    <table style="width:100%;border-collapse:collapse;font-size:12px">
      ${two?'<tr style="font-size:10px;color:var(--ink3);font-weight:800"><td></td><td style="text-align:center;width:22%">LEFT</td><td style="text-align:center;width:22%">RIGHT</td></tr>':''}
      ${s.tests.map(({t,cells},i)=>`<tr onclick="document.getElementById('mobt_${t.id}').scrollIntoView({behavior:'smooth'})" style="border-top:0.5px solid var(--border);cursor:pointer">
        <td style="padding:6px 0;font-weight:700">${i+1}. ${esc(t.name)}${t.role?`<div style="font-size:10px;color:var(--ink3);font-weight:600">${t.role==='active'?'Active':'Passive'}</div>`:''}</td>
        ${t.sides.length>1?cells.map(c=>cell(c)).join(''):cell(cells[0],two)}</tr>`).join('')}
    </table>
    <div style="display:flex;flex-wrap:wrap;gap:10px;margin-top:8px;font-size:10.5px;color:var(--ink3)">${['done','filmed','partial','prov','empty'].map(k=>`<span style="display:flex;align-items:center;gap:3px">${icon(ST[k].i,ST[k].c,12)}${ST[k].t}</span>`).join('')}</div></div>`;
}

function gapBlock(p){
  const g=gapFor(p);if(!g)return '';
  const unit=(g.ta.measures.find(m=>m.key===p.gap.active.measure)||{}).unit, U=UNIT[unit]||'';
  const bar=(v,c,lab)=>`<div style="display:flex;align-items:center;gap:6px;font-size:11px;margin-top:3px"><span style="width:52px;color:var(--ink3)">${lab}</span>
    <div style="flex:1;height:9px;border-radius:5px;background:var(--surface2);overflow:hidden">${v?`<div style="width:${Math.max(2,Math.min(100,v.v/180*100))}%;height:100%;background:${c};opacity:${v.prov?0.55:1}"></div>`:''}</div>
    <span style="width:44px;text-align:right;font-weight:800">${v?r1(v.v)+U+(v.prov?'*':''):'&ndash;'}</span></div>`;
  return `<div class="card" style="padding:12px 14px;margin-bottom:12px">
    <div style="font-size:13px;font-weight:800">Active vs passive</div>
    <div style="font-size:11px;color:var(--ink3);margin:2px 0 6px">${esc(g.tp.name)} (reached with help) against ${esc(g.ta.name)} (held by strength). The gap is the range you own passively but cannot yet hold.</div>
    ${g.rows.map(r=>`<div style="padding-top:6px;margin-top:4px;border-top:0.5px solid var(--border)">
      ${g.ta.sides.length>1?`<div style="display:flex;justify-content:space-between;font-size:12px;font-weight:800"><span>${SIDE[r.side]}</span><span style="color:var(--amber)">${r.gap!=null?'gap '+r.gap+U:''}</span></div>`:''}
      ${bar(r.pv,'var(--ink3)','Passive')}${bar(r.a,'var(--teal)','Active')}</div>`).join('')}
    ${g.rows.some(r=>(r.a&&r.a.prov)||(r.pv&&r.pv.prov))?'<div style="font-size:10.5px;color:var(--ink3);margin-top:6px">* provisional, counts after the retest</div>':''}
    ${!g.any?`<div style="font-size:11px;color:var(--ink3);margin-top:6px">Needs the ${esc(g.tp.name.toLowerCase())} and ${esc(g.ta.name.toLowerCase())} angles measured.</div>`:''}</div>`;
}

function clipBtns(list){
  return list.map((x,k)=>`<button class="btn-sm" title="${esc(x.note||'')}" onclick="mobPlay('${x.id}','${x.mime}')">${x.source?'Measured still':(x.mime.startsWith('video')?'Video':'Photo')+' '+(k+1)}${x.side==='both'?' (both sides)':''} &middot; ${mb(x.bytes)}</button>`).join('');
}
const noteLines=list=>list.filter(x=>x.note&&!x.source).map(x=>`<div style="font-size:11px;color:var(--ink3);margin-top:4px">Clip note: ${esc(x.note)}</div>`).join('');

// ---------- Program + sessions (owner, 14 Nov 2026) ----------
// A program is built by CODE on the server from his latest results (pancake: Smith's phase 1 tree; shoulders: our
// rules from the notes), shown as a draft, and only used once he starts it. Each session logs per drill the sets,
// the depth marker and whether he went deeper; the server returns next session's target (Smith's progression ladder).
let _mobDraft=null;
const progOf=pid=>(_mob.programs||[]).find(x=>x.pillar===pid&&x.status==='active');
const draftOf=pid=>(_mob.programs||[]).find(x=>x.pillar===pid&&x.status==='draft');
const setTxt=z=>(z.side?z.side+' ':'')+(z.reps!=null?z.reps+'r':'')+(z.hold_s!=null?z.hold_s+'s':'')+(z.kg?' @'+z.kg+'kg':'');
function doseLine(it,t){const d=it.dose||{}, sets=t&&t.sets||d.sets, reps=t&&t.reps!=null?t.reps:d.reps, hold=t&&t.hold_s!=null?t.hold_s:d.hold_s;
  const a=[];if(d.by_feel)a.push('by feel');else if(sets)a.push(sets+(String(sets)==='1'?' set':' sets'));if(reps)a.push(reps+(d.cr?' rounds':' reps'));if(hold)a.push(hold+' s hold');
  if(d.tempo)a.push('tempo '+d.tempo);if(d.cr)a.push(d.cr);if(d.rest_s)a.push(d.rest_s+' s rest');
  return esc(a.join(' · '))+(it.unilateral?' &middot; each side':'');}
function itemRow(it,showTarget){const t=it.target;
  return `<div style="border-top:0.5px solid var(--border);padding:8px 0">
    <div style="display:flex;align-items:center;gap:8px"><span style="font-size:11px;font-weight:800;color:var(--teal);width:22px">${esc(it.code)}</span>
      <div style="flex:1;min-width:0"><div style="font-size:13px;font-weight:700">${esc(it.name)}${it.optional?' <span style="font-size:10px;color:var(--ink3)">optional</span>':''}</div>
      <div style="font-size:11px;color:var(--ink3)">${doseLine(it,showTarget?t:null)}</div></div>${lessonBtn(it)}</div>
    ${showTarget&&t?`<div style="font-size:12px;margin:4px 0 0 30px;color:var(--ink2)">${esc(t.text)}</div>`:''}
    ${!showTarget&&it.why?`<div style="font-size:11px;margin:3px 0 0 30px;color:var(--ink3)">${esc(it.why)}</div>`:''}
    ${(it.dose&&it.dose.filled&&it.dose.filled.length)?`<div style="font-size:10.5px;margin:2px 0 0 30px;color:var(--ink3)">${esc(it.dose.filled.join(', '))} from Smith's program design rules (his note gives none)</div>`:''}</div>`;}
function programBlock(p){
  if(!(_mob.program_pillars||['pancake','shoulder_flexion']).includes(p.id))return '';
  const a=progOf(p.id), d=draftOf(p.id);
  if(a){const sp=a.spec, n=a.sessions_done||0, N=a.sessions_per_phase||6;
    return `<div class="card" style="padding:12px 14px;margin:12px 0">
      <div style="display:flex;justify-content:space-between;align-items:center;gap:8px"><div><div style="font-size:14px;font-weight:800">Phase ${a.phase} program</div>
        <div style="font-size:11px;color:var(--ink3)">Started ${a.started?fd(a.started):''} &middot; ${esc(sp.rules||'')}</div></div>${ring(Math.min(1,n/N),n>=N?'var(--teal)':'var(--amber)',n+'/'+N)}</div>
      ${a.retest_due?`<div style="font-size:12px;margin-top:8px;padding:8px;border-radius:6px;background:var(--surface2)"><b>Phase done.</b> Take a deload week, then retest (film the assessment) and build the next phase.</div>`:''}
      ${sp.items.map(it=>itemRow(it,true)).join('')}
      ${(sp.notes||[]).map(x=>`<div style="font-size:11px;color:var(--ink3);margin-top:6px">${esc(x)}</div>`).join('')}
      <button class="fd-btn" style="margin-top:10px" onclick="mobSessionOpen('${a.id}')">Log a session</button>
      ${sessionsList(a)}</div>`;}
  if(d)return draftCard(d.spec,d.id,p);
  if(_mobDraft&&_mobDraft.pillar===p.id)return draftCard(_mobDraft,null,p);
  return `<div class="card" style="padding:12px 14px;margin:12px 0"><div style="font-size:14px;font-weight:800">Program</div>
    <div style="font-size:12px;color:var(--ink3);margin:4px 0 8px">Built by code from your latest assessment: ${p.id==='pancake'?"Smith's pancake phase 1 decision tree":'our shoulder rules, written from the course notes'}. You see it as a draft first.</div>
    <button class="fd-btn" onclick="mobBuild('${p.id}')">Build my phase 1 program</button></div>`;
}
function draftCard(sp,id,p){
  if(!sp.ready){const qs=(p.questions||[]);
    return `<div class="card" style="padding:12px 14px;margin:12px 0"><div style="font-size:14px;font-weight:800">Program needs a bit more</div>
      ${(sp.missing||[]).map(m=>`<div style="font-size:12px;margin-top:4px">&bull; ${esc(m.why)}</div>`).join('')}
      ${qs.map(q=>`<label class="form-label" style="margin-top:8px">${esc(q.name)}</label><select id="mobq_${q.id}" style="width:100%;font-size:14px"><option value="">Choose</option>${q.options.map(o=>`<option value="${esc(o.value)}"${(sp.answers||{})[q.id]===o.value?' selected':''}>${esc(o.label)}</option>`).join('')}</select>`).join('')}
      <div style="font-size:11px;color:var(--ink3);margin-top:6px">Measure the tests above, answer any question here, then build again.</div>
      <button class="fd-btn" style="margin-top:8px" onclick="mobBuild('${p.id}')">Build again</button>
      <button class="btn-sm" style="margin-top:6px" onclick="mobDraftClear()">Close</button></div>`;}
  return `<div class="card" style="padding:12px 14px;margin:12px 0;border:1px solid var(--amber)">
    <div style="font-size:14px;font-weight:800">Draft: phase 1 program</div><div style="font-size:11px;color:var(--ink3)">${esc(sp.rules||'')} &middot; ${esc(sp.frequency||'')}</div>
    ${sp.items.map(it=>itemRow(it,false)).join('')}
    ${(sp.notes||[]).map(x=>`<div style="font-size:11px;color:var(--ink3);margin-top:6px">${esc(x)}</div>`).join('')}
    <div style="display:flex;gap:8px;flex-wrap:wrap;margin-top:10px">
      <button class="fd-btn" style="flex:1" onclick="mobStart('${p.id}',${id?`'${id}'`:'null'})">Start this program</button>
      ${id?`<button class="btn-sm" onclick="mobDiscard('${id}')">Discard</button>`:`<button class="btn-sm" onclick="mobKeepDraft('${p.id}')">Save as draft</button><button class="btn-sm" onclick="mobDraftClear()">Close</button>`}</div></div>`;
}
function sessionsList(a){const ss=(_mob.sessions||[]).filter(s=>s.program===a.id).slice().reverse();if(!ss.length)return '';
  return `<details style="margin-top:10px"><summary style="cursor:pointer;font-size:12px;font-weight:800">Sessions logged (${ss.length})</summary>
    ${ss.map(s=>`<div style="border-top:0.5px solid var(--border);padding:6px 0;font-size:12px"><div style="display:flex;justify-content:space-between"><b>${fd(s.date)}</b><button class="btn-sm" onclick="mobSessionDel('${s.id}')">Remove</button></div>
      ${s.items.filter(x=>x.code!=='_blocks').map(x=>`<div style="color:var(--ink2)">${esc(x.code)} ${esc(x.sets.map(setTxt).join(', '))}${x.marker?' &middot; '+esc(x.marker):''}${x.deeper==='yes'?' &middot; deeper':''}${x.warmup&&x.warmup.of?' &middot; warm-up '+x.warmup.sets_done+'/'+x.warmup.of:''}</div>`).join('')}
      ${s.note?`<div style="color:var(--ink3)">${esc(s.note)}</div>`:''}</div>`).join('')}</details>`;}
window.mobDraftClear=()=>{_mobDraft=null;renderMobility();};
window.mobBuild=async function(pid){
  const p=pillar(pid), answers={};(p.questions||[]).forEach(q=>{const e=document.getElementById('mobq_'+q.id);if(e&&e.value)answers[q.id]=e.value;});
  try{_mobDraft=await syncApi('/api/fitness/mobility/program/build',{method:'POST',body:JSON.stringify({pillar:pid,answers})});renderMobility();}
  catch(e){toast('Could not build it. Check your connection.');}
};
window.mobKeepDraft=async function(pid){try{await syncApi('/api/fitness/mobility/program',{method:'POST',body:JSON.stringify({pillar:pid,spec:_mobDraft,activate:false})});_mobDraft=null;await load();renderMobility();toast('Saved as a draft');}catch(e){toast('Could not save.');}};
window.mobStart=async function(pid,id){
  if(!confirm('Start this program? Your sessions will be logged against it.'))return;
  try{if(id)await syncApi('/api/fitness/mobility/program/'+id+'/start',{method:'POST'});
    else await syncApi('/api/fitness/mobility/program',{method:'POST',body:JSON.stringify({pillar:pid,spec:_mobDraft,activate:true})});
    _mobDraft=null;await load();renderMobility();toast('Program started');}catch(e){toast('Could not start it.');}
};
window.mobDiscard=async function(id){if(!confirm('Discard this draft?'))return;await syncApi('/api/fitness/mobility/program/'+id,{method:'DELETE'});await load();renderMobility();};
window.mobSessionDel=async function(id){if(!confirm('Remove this logged session?'))return;await syncApi('/api/fitness/mobility/session/'+id,{method:'DELETE'});await load();renderMobility();};
window.mobSessionOpen=function(id){_mobView='session:'+id;renderMobility();window.scrollTo(0,0);};

// the session screen: one card per drill, target first, then what he did
const nInt=x=>{const m=String(x==null?'':x).match(/[0-9]+/);return m?+m[0]:null;};
const isHold=(it)=>{const t=it.target||{}, d=it.dose||{};return t.hold_s!=null||(!d.reps&&!!d.hold_s);};
// warm-ups (lesson 06 cheatsheet): a ladder per drill to tick, then "reached ~85% of last session?"; bridge adds blocks
function warmBlock(it,k){const rows=it.warmup||[];if(!rows.length)return '';
  return `<div style="margin-top:8px;padding:8px;border-radius:6px;background:var(--surface2)">
    <div style="font-size:11px;font-weight:800;color:var(--ink3);text-transform:uppercase;letter-spacing:0.05em">Warm-up first &middot; same set-up and tempo</div>
    ${rows.map((r,i)=>`<label style="display:flex;align-items:flex-start;gap:8px;padding:4px 0;font-size:12.5px;cursor:pointer"><input type="checkbox" id="mobw_${k}_${i}" style="margin-top:2px"/>
      <span><b>${esc(r.label)}:</b> ${esc(r.do)} <span style="color:var(--ink3)">&middot; rest ${esc(r.rest)}</span></span></label>`).join('')}
    <div style="font-size:12px;margin-top:4px">Reached about 85% of last session's depth?</div>
    <div style="display:flex;gap:6px;margin-top:4px">${[['yes','Yes'],['no','Not near it']].map(([v,l])=>`<label class="btn-sm" style="cursor:pointer;display:flex;gap:4px;align-items:center"><input type="radio" name="mobw_${k}_r" value="${v}" onchange="mobWarmCheck(${k},this.value)"/>${l}</label>`).join('')}</div>
    <div id="mobw_${k}_msg"></div></div>`;}
window.mobWarmCheck=function(k,v){const m=document.getElementById(`mobw_${k}_msg`);if(!m)return;
  m.innerHTML=v==='no'?`<div style="font-size:12px;margin-top:6px;color:var(--amber)">${k<=1?"Smith: if you can't get near last session's range in the warm-up, you're not recovered. Take an extra rest day and come back.":'Fine to carry on lighter; note it.'}</div>`:'';};
function blockCards(pid,when){const bs=((_mob.blocks||{})[pid]||{})[when]||[];
  return bs.map(b=>`<div class="card" style="padding:12px 14px;margin-bottom:10px"><div style="display:flex;align-items:center;gap:8px"><div style="flex:1;font-size:14px;font-weight:800">${esc(b.name)}</div>${lessonBtn(b)}</div>
    <div style="font-size:12px;color:var(--ink3);margin:2px 0 4px">${esc(b.how)}</div>
    ${b.options.map((o,i)=>{const nm=typeof o==='string'?o:o.name;return `<label style="display:flex;gap:8px;align-items:center;font-size:12.5px;padding:3px 0;cursor:pointer"><input type="checkbox" data-blk="${esc(b.id)}" value="${esc(nm)}"/><span style="flex:1">${esc(nm)}</span>${typeof o==='object'?lessonBtn(o,'Demo'):''}</label>`;}).join('')}</div>`).join('');}

function renderSession(el,id){
  const a=(_mob.programs||[]).find(x=>x.id===id);if(!a){_mobView='list';return renderList(el);}
  const p=pillar(a.pillar);
  el.innerHTML=`<button class="btn-sm" onclick="mobOpen('${a.pillar}')" style="margin-bottom:10px">&lsaquo; ${esc(p?p.name:'Back')}</button>
    <div style="font-size:18px;font-weight:800">Session ${(a.sessions_done||0)+1} of ${a.sessions_per_phase||6}</div>
    <div style="font-size:12px;color:var(--ink3);margin:2px 0 8px">Warm up each drill first: 2-5 lighter sets until you reach 80-90% of last session's depth. If you can't get near it, rest another day.</div>
    <div style="display:flex;gap:8px;align-items:center;margin-bottom:10px"><label class="form-label" style="margin:0">Date</label><input id="mobSDay" type="date" value="${today()}" max="${today()}" class="fd-input" style="width:auto;font-size:14px"/></div>
    <div style="margin-bottom:10px">${lessonBtn(_mob.warmup_lesson||{},'How to warm up (lesson)')}</div>
    ${blockCards(a.pillar,'before')}
    <div id="mobWarnRest"></div>
    ${a.spec.items.map((it,k)=>{const t=it.target||{}, d=it.dose||{}, nS=Math.max(1,Math.min(6,t.sets||nInt(d.sets)||2));
      const hold=isHold(it), w=it.load&&it.load.uses_weight, sides=it.unilateral?['L','R']:[''], last=it.last;
      return `<div class="card" style="padding:12px 14px;margin-bottom:10px">
        <div style="display:flex;align-items:center;gap:8px"><span style="font-size:12px;font-weight:800;color:var(--teal)">${esc(it.code)}</span><div style="flex:1;font-size:14px;font-weight:800">${esc(it.name)}</div>${lessonBtn(it)}</div>
        <div style="font-size:12px;margin-top:4px"><b>Today:</b> ${doseLine(it,t)}</div>
        ${t.text?`<div style="font-size:12px;color:var(--ink2);margin-top:2px">${esc(t.text)}</div>`:''}
        ${last?`<div style="font-size:11px;color:var(--ink3);margin-top:4px">Last time: ${esc(last.sets.map(setTxt).join(', '))}${last.marker?' &middot; <b>'+esc(last.marker)+'</b>':''}</div>`:''}
        ${warmBlock(it,k)}
        <div style="font-size:11px;font-weight:800;color:var(--ink3);margin-top:8px;text-transform:uppercase;letter-spacing:0.05em">Working sets</div>
        <table style="width:100%;border-collapse:collapse;margin-top:6px;font-size:12px"><tr style="font-size:10px;color:var(--ink3)"><td style="width:28px">Set</td>${sides.map(sd=>`<td>${sd?SIDE[sd]+' ':''}${hold?'hold s':'reps'}</td>${w?`<td>${sd?sd+' ':''}kg</td>`:''}`).join('')}</tr>
          ${Array.from({length:nS},(_,i)=>`<tr><td style="padding:2px 0;color:var(--ink3)">${i+1}</td>${sides.map(sd=>`<td style="padding:2px 4px 2px 0"><input id="mobs_${k}_${i}_${sd||'x'}_v" type="number" inputmode="decimal" class="fd-input" style="font-size:14px;padding:6px" placeholder="${esc(hold?(t.hold_s==null?'':t.hold_s):(t.reps==null?'':t.reps))}"/></td>${w?`<td style="padding:2px 4px 2px 0"><input id="mobs_${k}_${i}_${sd||'x'}_kg" type="number" inputmode="decimal" class="fd-input" style="font-size:14px;padding:6px"/></td>`:''}`).join('')}</tr>`).join('')}</table>
        ${it.method!=='strength'?`<label class="form-label" style="margin-top:6px">Depth marker${it.marker&&it.marker.label?': '+esc(it.marker.label):''}</label>
        <input id="mobs_${k}_marker" class="fd-input" style="font-size:14px" placeholder="${last&&last.marker?'last: '+esc(last.marker):'e.g. 2 fingers, 3 blocks, 42 cm'}"/>
        <div style="display:flex;gap:6px;margin-top:6px">${[['yes','Deeper than last time'],['no','Not deeper']].map(([v,l])=>`<label class="btn-sm" style="cursor:pointer;display:flex;gap:4px;align-items:center"><input type="radio" name="mobs_${k}_deeper" value="${v}"/>${l}</label>`).join('')}</div>`:''}
        <input id="mobs_${k}_note" class="fd-input" style="font-size:13px;margin-top:6px" placeholder="Note (optional)"/>
        ${d.rest_s?`<button class="btn-sm" style="margin-top:6px" onclick="mobRest(${nInt(d.rest_s)||60})">Rest timer ${nInt(d.rest_s)||60} s</button>`:''}</div>`;}).join('')}
    ${blockCards(a.pillar,'after')}
    <input id="mobSNote" class="fd-input" style="font-size:13px;margin-bottom:8px" placeholder="Session note (how it felt, sleep, soreness)"/>
    <button class="fd-btn" onclick="mobSessionSave('${a.id}')">Save session</button>`;
}
window.mobSessionSave=async function(id){
  const a=(_mob.programs||[]).find(x=>x.id===id);if(!a)return;
  const items=a.spec.items.map((it,k)=>{const sides=it.unilateral?['L','R']:['x'], sets=[], hold=isHold(it);
    for(let i=0;i<8;i++)sides.forEach(sd=>{const v=document.getElementById(`mobs_${k}_${i}_${sd}_v`);if(!v||v.value==='')return;
      const kg=document.getElementById(`mobs_${k}_${i}_${sd}_kg`), row=hold?{hold_s:+v.value}:{reps:+v.value};
      if(kg&&kg.value!=='')row.kg=+kg.value;if(sd!=='x')row.side=sd;sets.push(row);});
    const mk=document.getElementById(`mobs_${k}_marker`), dp=document.querySelector(`input[name="mobs_${k}_deeper"]:checked`), nt=document.getElementById(`mobs_${k}_note`);
    const of=(it.warmup||[]).length, done=Array.from({length:of},(_,i)=>document.getElementById(`mobw_${k}_${i}`)).filter(e=>e&&e.checked).length;
    const rr=document.querySelector(`input[name="mobw_${k}_r"]:checked`);
    return {code:it.code,drill_id:it.drill_id,sets,marker:mk?mk.value:'',deeper:dp?dp.value:null,note:nt?nt.value:'',
      warmup:{sets_done:done,of,reached:rr?rr.value:null}};});
  const blocks=[];document.querySelectorAll('input[data-blk]').forEach(e=>{if(!e.checked)return;let b=blocks.find(x=>x.id===e.dataset.blk);if(!b)blocks.push(b={id:e.dataset.blk,picked:[]});b.picked.push(e.value);});
  const skipped=items.filter(x=>x.sets.length&&x.warmup.of&&x.warmup.sets_done<x.warmup.of).map(x=>x.code);
  if(skipped.length&&!confirm('Warm-up not completed for '+skipped.join(', ')+'. Save anyway?'))return;
  try{await syncApi('/api/fitness/mobility/session',{method:'POST',body:JSON.stringify({program:id,date:document.getElementById('mobSDay').value||today(),items,blocks,note:document.getElementById('mobSNote').value||''})});
    await load();_mobView=a.pillar;renderMobility();window.scrollTo(0,0);toast('Session saved');}
  catch(e){toast('Could not save. Log at least one set or marker, and check your connection.');}
};
window.mobRest=function(sec){
  const ov=document.createElement('div');ov.style.cssText='position:fixed;inset:0;z-index:10000;background:rgba(0,0,0,0.9);display:flex;flex-direction:column;align-items:center;justify-content:center;color:#fff';
  let left=sec;ov.innerHTML=`<div style="font-size:13px;color:#aaa">Rest</div><div id="mobRT" style="font-size:72px;font-weight:800">${left}</div><button class="btn-sm" id="mobRX" style="margin-top:16px">Done</button>`;
  document.body.appendChild(ov);
  const beep=()=>{try{const c=new (window.AudioContext||window.webkitAudioContext)(),o=c.createOscillator();o.connect(c.destination);o.frequency.value=880;o.start();setTimeout(()=>{o.stop();c.close();},400);}catch(e){}};
  const iv=setInterval(()=>{left--;const e=ov.querySelector('#mobRT');if(e)e.textContent=Math.max(0,left);if(left<=0){clearInterval(iv);beep();if(navigator.vibrate)navigator.vibrate(300);setTimeout(()=>ov.remove(),1200);}},1000);
  ov.querySelector('#mobRX').onclick=()=>{clearInterval(iv);ov.remove();};
};

// ---------- Drill library (owner, 14 Nov 2026) ----------
// Every drill for the pillar from the course notes, grouped in Smith's session order (technique, isolated, main,
// attribute). Doses show only where his notes state them; each opens its lesson video and our cues in the app.
const SLOTS=[['technique','Technique','1 set, only where position matters'],['isolated','Isolated','2-3 sets, the weak link'],
  ['main','Main (compound)','3-4 sets, brings it together'],['attribute','Specific attribute','2-3 sets: active, isometric, contract-relax']];
const METHOD={passive:'Passive',loaded:'Loaded',isometric:'Isometric',contract_relax:'Contract-relax',active:'Active',
  ballistic:'Ballistic',technique:'Technique',soft_tissue:'Soft tissue',strength:'Strength'};
function doseTxt(d){if(!d)return '';const a=[];
  if(d.sets)a.push(d.sets+' sets');if(d.reps)a.push(d.reps+' reps');if(d.hold_s)a.push(d.hold_s+' s hold');
  if(d.tempo)a.push('tempo '+d.tempo);if(d.cr)a.push(d.cr);if(d.rest_s)a.push(d.rest_s+' s rest');return a.join(' &middot; ');}
const listHtml=(t,xs)=>xs&&xs.length?`<div style="font-size:11px;font-weight:800;color:var(--ink3);margin-top:6px;text-transform:uppercase;letter-spacing:0.05em">${t}</div>${xs.map(x=>`<div style="font-size:12px;margin-top:2px">&bull; ${esc(x)}</div>`).join('')}`:'';
function drillRow(x){const ds=doseTxt(x.dose);
  return `<details style="border-top:0.5px solid var(--border);padding:7px 0"><summary style="cursor:pointer;list-style:none;display:flex;align-items:center;gap:8px">
      <div style="flex:1;min-width:0"><div style="font-size:13px;font-weight:700">${esc(x.name)}${x.unilateral?' <span style="font-size:10px;color:var(--ink3)">each side</span>':''}</div>
        <div style="font-size:11px;color:var(--ink3)">${esc(METHOD[x.method]||'Method not stated')}${ds?' &middot; '+ds:''}</div></div>
      ${lessonBtn(x)}</summary>
    <div style="padding:4px 0 2px 2px">
      ${x.marker&&x.marker.label?`<div style="font-size:12px;margin-top:4px;padding:6px 8px;border-radius:6px;background:var(--surface2)"><b>Log each session:</b> ${esc(x.marker.label)}</div>`:''}
      ${x.load&&x.load.uses_weight?`<div style="font-size:12px;margin-top:4px"><b>Weight:</b> ${esc(x.load.start||'not stated')}${x.load.how_to_choose?' &middot; '+esc(x.load.how_to_choose):''}</div>`:''}
      ${listHtml('Cues',x.cues)}${listHtml('Common mistakes',x.mistakes)}${listHtml('Progress by',x.progressions)}${listHtml('Make it easier',x.regressions)}
      ${!ds?'<div style="font-size:11px;color:var(--ink3);margin-top:6px">His notes give no sets or reps for this drill; the program sets them from the design rules.</div>':''}</div></details>`;}
function drillBlock(p){
  const ds=(_mob.drills||[]).filter(x=>(x.pillars||[]).includes(p.id));if(!ds.length)return '';
  return `<details class="card" style="padding:12px 14px;margin-bottom:12px"><summary style="cursor:pointer;font-size:13px;font-weight:800">Drill library &middot; ${ds.length} drills</summary>
    <div style="font-size:11px;color:var(--ink3);margin:4px 0 2px">In session order. Tap a drill for cues; Watch plays the lesson.</div>
    ${SLOTS.map(([k,n,h])=>{const xs=ds.filter(x=>x.slot===k);return xs.length?`<div style="margin-top:10px"><div style="font-size:12px;font-weight:800">${n} <span style="font-weight:400;color:var(--ink3)">${h}</span></div>${xs.map(drillRow).join('')}</div>`:'';}).join('')}</details>`;
}

function renderPillar(el,p){
  if(!p){_mobView='list';return renderList(el);}
  const s=pillarState(p);
  const anyDays=[...new Set([...(_mob.results||[]),...(_mob.media||[])].filter(r=>r.pillar===p.id).map(r=>r.date))].sort();
  const day=_mobDay||anyDays[anyDays.length-1]||today();
  const days=[...new Set((_mob.results||[]).filter(r=>r.pillar===p.id).map(r=>r.date))].sort();
  const mediaDay=(t,side)=>clipsOf(p.id,t).filter(m=>m.side===side&&m.date===day);
  el.innerHTML=`<button class="btn-sm" onclick="mobBack()" style="margin-bottom:10px">&lsaquo; All pillars</button>
    <div style="display:flex;align-items:center;gap:8px;flex-wrap:wrap"><span style="font-size:18px;font-weight:800">${esc(p.name)}</span>${chip(s.status)}
      <label style="margin-left:auto;display:flex;align-items:center;gap:5px;font-size:12px;color:var(--ink2)"><input type="checkbox" ${focusIds().has(p.id)?'checked':''} onchange="mobFocus('${p.id}',this.checked)"/>Focus pillar</label></div>
    ${programBlock(p)}
    ${gridBlock(p,s)}
    ${gapBlock(p)}
    ${todoBlock(todos(p),8,'Still to do')}
    ${drillBlock(p)}
    ${p.setup?`<div style="font-size:12px;color:var(--ink2);margin:6px 0 4px;line-height:1.5">${esc(p.setup)}</div>`:''}
    <div style="display:flex;gap:8px;flex-wrap:wrap;align-items:center;margin-top:6px">${lessonBtn({name:p.name+' assessment',lesson_slug:p.lesson_slug,lesson_course:p.lesson_course,youtube:p.youtube},'Watch the assessment lesson')}
      ${p.lesson?`<a href="${esc(p.lesson)}" target="_blank" rel="noopener" style="font-size:12px;color:var(--blue)">On Smith's site</a>`:''}</div>
    ${(p.extra_lessons||[]).length?`<div style="display:flex;gap:6px;flex-wrap:wrap;margin-top:6px">${p.extra_lessons.map(x=>lessonBtn(x,x.name)).join('')}</div>`:''}
    <div style="display:flex;gap:8px;align-items:center;margin:12px 0">
      <label class="form-label" style="margin:0">Assessment date</label>
      <input type="date" value="${day}" max="${today()}" class="fd-input" style="width:auto;font-size:14px" onchange="mobDay(this.value)"/></div>
    ${p.tests.map((t,i)=>{const both=t.sides.length>1?mediaDay(t.id,'both'):[];
      const prov=t.sides.some(sd=>resFor(p.id,t.id,sd,day).some(r=>r.provisional));
      return `<div class="card" id="mobt_${t.id}" style="padding:12px 14px;margin-bottom:10px;scroll-margin-top:70px">
      <div style="font-size:14px;font-weight:800">${i+1}. ${esc(t.name)}${t.role?` <span style="font-size:10px;font-weight:700;color:var(--ink3)">${t.role==='active'?'ACTIVE':'PASSIVE'}</span>`:''}</div>
      ${t.camera?`<div style="font-size:12px;color:var(--ink2);margin-top:4px;padding:6px 8px;border-radius:6px;background:var(--surface2)"><b>Camera:</b> ${esc(t.camera)}<div style="margin-top:4px;color:var(--ink3)">${CAM_TIP}</div></div>`:''}
      ${holdOf(t)?`<div style="font-size:13px;font-weight:800;margin-top:6px;color:var(--teal)">Hold ${esc(holdOf(t))} for it to count</div>`:''}
      ${t.benchmark?`<div style="font-size:12px;margin-top:4px"><b>Benchmark:</b> ${esc(t.benchmark)}</div>`:''}
      ${t.cue?`<div style="font-size:12px;color:var(--ink2);margin-top:4px;line-height:1.45">${esc(t.cue)}</div>`:''}
      ${t.lesson_slug||t.youtube||(t.extra_lessons||[]).length?`<div style="margin-top:6px;display:flex;gap:6px;flex-wrap:wrap">${lessonBtn({name:t.name,lesson_slug:t.lesson_slug,lesson_course:t.lesson_course,youtube:t.youtube},'Watch how')}${(t.extra_lessons||[]).map(x=>lessonBtn(x,x.name)).join('')}</div>`:''}
      ${t.sides.length>1?`<div style="display:flex;gap:6px;flex-wrap:wrap;align-items:center;margin-top:8px">${clipBtns(both)}
        <label class="btn-sm" style="cursor:pointer">+ Both sides in one clip<input type="file" accept="video/*,image/*" style="display:none" onchange="mobUpload(this,'${p.id}','${t.id}','both')"/></label>
        <span id="mobup_${t.id}_both" style="font-size:11px;color:var(--ink3)"></span></div>${noteLines(both)}`:''}
      ${t.sides.map(sd=>{const cur=resFor(p.id,t.id,sd,day), med=mediaDay(t.id,sd);
        return `<div style="margin-top:10px;padding-top:8px;border-top:0.5px solid var(--border)">
          ${t.sides.length>1?`<div style="font-size:12px;font-weight:800;color:var(--ink2)">${SIDE[sd]}</div>`:''}
          ${t.measures.map(m=>inputFor(p,t,sd,m,cur)).join('')}
          <div style="display:flex;gap:6px;flex-wrap:wrap;align-items:center;margin-top:8px">${clipBtns(med)}
            <label class="btn-sm" style="cursor:pointer">+ Video or photo<input type="file" accept="video/*,image/*" style="display:none" onchange="mobUpload(this,'${p.id}','${t.id}','${sd}')"/></label>
            <span id="mobup_${t.id}_${sd}" style="font-size:11px;color:var(--ink3)"></span></div>${noteLines(med)}</div>`;}).join('')}
      <label style="display:flex;align-items:center;gap:6px;font-size:12px;color:var(--ink2);margin-top:10px"><input type="checkbox" id="mobprov_${t.id}"${prov?' checked':''}/> Provisional (needs a proper retest)</label>
      <button class="fd-btn" style="margin-top:8px" onclick="mobSave('${p.id}','${t.id}')">Save ${esc(t.name)}</button></div>`;}).join('')}
    ${days.length?historyBlock(p,days):''}`;
}

function spark(vals){
  if(vals.length<2)return '';
  const lo=Math.min(...vals), hi=Math.max(...vals), W=56, H=16, sp=hi-lo||1;
  const pts=vals.map((v,i)=>`${(i/(vals.length-1)*W).toFixed(1)},${(H-2-(v-lo)/sp*(H-4)).toFixed(1)}`).join(' ');
  return `<svg width="${W}" height="${H}" viewBox="0 0 ${W} ${H}" aria-hidden="true"><polyline points="${pts}" fill="none" stroke="var(--teal)" stroke-width="1.8" stroke-linejoin="round" stroke-linecap="round"/></svg>`;
}
function historyBlock(p,days){
  const rows=[];
  p.tests.forEach(t=>t.sides.forEach(s=>t.measures.forEach(m=>{
    const pts=days.map(d=>{const r=resFor(p.id,t.id,s,d).find(x=>x.measure===m.key);return r?{d,r}:null;}).filter(Boolean);
    if(!pts.length)return;
    const show=x=>fmtV(m,x.r)+(x.r.provisional?'*':'');
    const first=pts[0], last=pts[pts.length-1];
    rows.push(`<tr style="border-top:0.5px solid var(--border)"><td style="padding:4px 0">${esc(t.name)}${t.sides.length>1?' &middot; '+SIDE[s]:''}<div style="font-size:10px;color:var(--ink3)">${esc(m.label)}</div></td>
      <td style="text-align:right">${esc(show(first))}<div style="font-size:10px;color:var(--ink3)">${fd(first.d)}</div></td>
      <td style="text-align:right;font-weight:800">${pts.length>1?esc(show(last))+`<div style="font-size:10px;color:var(--ink3);font-weight:400">${fd(last.d)}</div>`:'&mdash;'}</td>
      <td style="text-align:right;width:60px">${spark(pts.map(x=>x.r.value).filter(v=>v!=null))}</td></tr>`);
  })));
  return `<div class="card" style="padding:12px 14px;margin-top:4px"><div style="font-size:13px;font-weight:800;margin-bottom:6px">History</div>
    <table style="width:100%;border-collapse:collapse;font-size:12px"><tr style="font-size:10px;color:var(--ink3)"><td></td><td style="text-align:right">Baseline</td><td style="text-align:right">Latest</td><td style="text-align:right">Trend</td></tr>${rows.join('')}</table>
    ${rows.some(r=>r.includes('*'))?'<div style="font-size:10.5px;color:var(--ink3);margin-top:6px">* provisional</div>':''}</div>`;
}

window.mobSave=async function(pid,tid){
  const p=pillar(pid), t=p.tests.find(x=>x.id===tid);
  const anyDays=[...new Set([...(_mob.results||[]),...(_mob.media||[])].filter(r=>r.pillar===pid).map(r=>r.date))].sort();
  const day=_mobDay||anyDays[anyDays.length-1]||today();
  const pe=document.getElementById('mobprov_'+tid), provisional=!!(pe&&pe.checked);
  try{
    for(const s of t.sides){
      const values={};t.measures.forEach(m=>{const e=document.getElementById(`mob_${t.id}_${s}_${m.key}`);if(e&&e.value!=='')values[m.key]=m.kind==='number'?+e.value:e.value;});
      await syncApi('/api/fitness/mobility/result',{method:'POST',body:JSON.stringify({date:day,pillar:pid,test:tid,side:s,values,provisional})});
    }
    await load();renderMobility();toast('Saved');
  }catch(e){toast('Could not save. Check your connection.');}
};

window.mobUpload=async function(inp,pid,tid,side){
  const f=inp.files&&inp.files[0];if(!f)return;
  const st=document.getElementById(`mobup_${tid}_${side}`);
  const anyDays=[...new Set([...(_mob.results||[]),...(_mob.media||[])].filter(r=>r.pillar===pid).map(r=>r.date))].sort();
  const day=_mobDay||anyDays[anyDays.length-1]||today();
  const say=t=>{if(st)st.textContent=t;};
  try{
    say('Starting...');
    const start=await syncApi('/api/fitness/mobility/media',{method:'POST',body:JSON.stringify({date:day,pillar:pid,test:tid,side,mime:f.type||'video/mp4',bytes:f.size})});
    if(!start||!start.id)throw new Error('start');
    const step=start.chunk||8000000;
    for(let i=0,off=0;off<f.size;i++,off+=step){
      const r=await fetch(API_BASE+'/api/fitness/mobility/media/'+start.id+'/'+i,{method:'PUT',headers:{'Authorization':'Bearer '+syncToken(),'Content-Type':'application/octet-stream'},body:f.slice(off,off+step)});
      if(!r.ok)throw new Error('chunk '+i);
      say('Uploading '+Math.min(100,Math.round((off+step)/f.size*100))+'%');
    }
    await syncApi('/api/fitness/mobility/media/'+start.id+'/finish',{method:'POST'});
    await load();renderMobility();toast('Saved to Cortex');
  }catch(e){say('Upload failed');toast('Upload failed. Try again on wifi.');}
  inp.value='';
};

window.mobPlay=async function(id,mime){
  const ov=document.createElement('div');
  ov.style.cssText='position:fixed;inset:0;z-index:10000;background:rgba(0,0,0,0.92);display:flex;flex-direction:column;align-items:center;justify-content:center;padding:16px;box-sizing:border-box';
  ov.innerHTML='<div style="color:#fff;font-size:13px">Loading&hellip;</div>';
  ov.onclick=e=>{if(e.target===ov){ov.remove();}};
  document.body.appendChild(ov);
  try{
    const r=await fetch(API_BASE+'/api/fitness/mobility/media/'+encodeURIComponent(id),{headers:{'Authorization':'Bearer '+syncToken()}});
    if(!r.ok)throw new Error('http');
    const url=URL.createObjectURL(await r.blob());
    const rec=mediaRec(id);
    ov.innerHTML=(mime.startsWith('video')?`<video src="${url}" controls playsinline autoplay style="max-width:100%;max-height:78vh;border-radius:10px"></video>`
      :`<img src="${url}" style="max-width:100%;max-height:78vh;border-radius:10px"/>`)
      +(rec&&rec.note?`<div style="color:#ccc;font-size:12px;margin-top:8px;text-align:center;max-width:520px">${esc(rec.note)}</div>`:'')
      +`<div style="display:flex;gap:10px;margin-top:12px;flex-wrap:wrap;justify-content:center">${hasAngle(id)&&!(rec&&rec.source)?`${mime.startsWith('video')&&hasPose(id)?`<button class="btn-sm" id="mobAuto" style="background:var(--teal);color:#fff;border-color:var(--teal)">Auto-measure</button>`:''}<button class="btn-sm" id="mobMeasure"${mime.startsWith('video')&&hasPose(id)?'':' style="background:var(--teal);color:#fff;border-color:var(--teal)"'}>Measure ${mime.startsWith('video')?'this frame by hand':'angle'}</button>`:''}<button class="btn-sm" id="mobDel">Delete</button><button class="btn-sm" id="mobClose">Close</button></div>`
      +(mime.startsWith('video')&&hasAngle(id)?'<div id="mobAutoMsg" style="color:#aaa;font-size:11px;margin-top:8px;text-align:center;max-width:520px">'+(hasPose(id)?'Auto-measure finds your end range and reads the angle. Or pause on your best position and measure by hand.':'Pause on your best position, then tap Measure.')+'</div>':'');
    ov.querySelector('#mobClose').onclick=()=>{URL.revokeObjectURL(url);ov.remove();};
    const mBtn=ov.querySelector('#mobMeasure');
    if(mBtn)mBtn.onclick=()=>{const src=ov.querySelector('video,img');if(src.tagName==='VIDEO')src.pause();mobMeasure(src,id,ov);};
    const aBtn=ov.querySelector('#mobAuto');
    if(aBtn)aBtn.onclick=()=>mobAutoMeasure(ov.querySelector('video'),id,ov,aBtn,ov.querySelector('#mobAutoMsg'));
    ov.querySelector('#mobDel').onclick=async()=>{if(!confirm('Remove this clip?'))return;await fetch(API_BASE+'/api/fitness/mobility/media/'+encodeURIComponent(id),{method:'DELETE',headers:{'Authorization':'Bearer '+syncToken()}});ov.remove();await load();renderMobility();};
  }catch(e){console.warn('mobPlay',e);ov.innerHTML='<div style="color:#fff">Could not load it.</div>';setTimeout(()=>ov.remove(),1500);}
};

// ---------- Lesson viewer (owner, 14 Nov 2026) ----------
// A drill or test names its lesson as {course, slug} (our private copy on Cortex, streamed through a short-lived
// signed link) and/or {youtube} (played from YouTube). Our note for the lesson shows under the video.
function noteHtml(md){
  return String(md||'').split('\n').filter(l=>!/^Library:/.test(l)).map(l=>{
    if(/^# /.test(l))return `<div style="font-size:14px;font-weight:800;margin:4px 0 6px">${esc(l.slice(2))}</div>`;
    if(!l.trim())return '<div style="height:6px"></div>';
    const s=esc(l).replace(/^App:/,'<b>App:</b>').replace(/^- /,'&bull; ');
    return `<div style="margin:2px 0">${s}</div>`;}).join('');
}
window.mobLesson=async function(course,slug,yt,title,vimeo){
  const ov=document.createElement('div');
  ov.style.cssText='position:fixed;inset:0;z-index:10000;background:rgba(0,0,0,0.94);overflow-y:auto;padding:16px;box-sizing:border-box';
  ov.innerHTML='<div style="color:#fff;font-size:13px;text-align:center;margin-top:40vh">Loading&hellip;</div>';
  document.body.appendChild(ov);
  const close=()=>{const v=ov.querySelector('video');if(v){v.pause();v.removeAttribute('src');v.load();}ov.remove();};
  let L=null;
  if(course&&slug){try{L=await syncApi('/api/fitness/course/'+encodeURIComponent(course)+'/'+encodeURIComponent(slug));}catch(e){L=null;}}
  const vid=L&&L.video_url?`<video src="${API_BASE+L.video_url}" controls playsinline preload="metadata" style="width:100%;max-height:60vh;border-radius:10px;background:#000"></video>`
    :vimeo?`<div style="position:relative;width:100%;padding-top:56.25%"><iframe src="https://player.vimeo.com/video/${encodeURIComponent(vimeo)}?dnt=1" allow="autoplay; fullscreen; picture-in-picture" allowfullscreen style="position:absolute;inset:0;width:100%;height:100%;border:0;border-radius:10px"></iframe></div>`
    :yt?`<div style="position:relative;width:100%;padding-top:56.25%"><iframe src="https://www.youtube-nocookie.com/embed/${encodeURIComponent(yt)}?rel=0&playsinline=1" allow="autoplay; encrypted-media; picture-in-picture; fullscreen" allowfullscreen style="position:absolute;inset:0;width:100%;height:100%;border:0;border-radius:10px"></iframe></div>`
    :'<div style="color:#aaa;font-size:13px;padding:20px 0;text-align:center">No video for this one yet.</div>';
  ov.innerHTML=`<div style="max-width:760px;margin:0 auto">
    <div style="display:flex;justify-content:space-between;align-items:center;gap:10px;margin-bottom:10px">
      <div style="color:#fff;font-size:15px;font-weight:800">${esc(title||'Lesson')}</div><button class="btn-sm" id="mobLClose">Close</button></div>
    ${vid}
    ${L&&L.note?`<div style="background:var(--surface);color:var(--ink);border-radius:10px;padding:12px 14px;margin-top:12px;font-size:12.5px;line-height:1.5">${noteHtml(L.note)}</div>`:''}</div>`;
  ov.querySelector('#mobLClose').onclick=close;
};
// one button for any item that carries a lesson
// (registered by index, so a name with an apostrophe cannot break the onclick)
const _lessons=[];
window.mobLessonN=i=>{const x=_lessons[i];if(x)mobLesson(x.c,x.s,x.y,x.n,x.v);};
const lessonBtn=(x,label)=>{const s=x.lesson_slug||'', y=x.youtube||'', v=x.vimeo||'';
  if(!s&&!y&&!v)return '';
  const i=_lessons.push({c:s?(x.lesson_course||'mftk'):'',s,y,v,n:x.name})-1;
  return `<button class="btn-sm" onclick="event.stopPropagation();mobLessonN(${i})">${label||'Watch'}</button>`;};

// ---------- Tap to measure (owner, 8 Nov 2026; per-test angle definitions 9 Nov 2026) ----------
// The frame (a paused video or a photo) is drawn on a canvas and he taps the points the measure's `angle` spec names.
// Code computes the angle by its mode: "inside" = the angle at the middle point; "flex" = 180 minus it (how far the
// joint has folded, so a fold and a leg lift read on one scale); "vertical" = two points, the line's angle from a
// plumb line. Saving writes it into that test's measure for the day and the chosen side (other values kept), with
// the provisional flag, and uploads the marked-up frame as a still carrying the clip and the second it came from.
const CLEAN_NOTE='Clean frame, no angle lines';
// the hold time pulled out of the cue so it cannot be missed (owner, 14 Nov 2026: it was buried at the end)
const holdOf=t=>{const m=String(t&&t.cue||'').match(/hold(?: (?:it|each|the [a-z ]+?))?(?: for)? ([0-9]+(?:-[0-9]+)? ?(?:s|sec|seconds))/i);return m?m[1].replace(/ ?(sec|seconds)$/,' s'):'';};
// Distance and lens (owner, 14 Nov 2026): Smith gives no distance, only 'whole body in frame, same spot every time';
// this is OUR guidance from camera geometry, labelled as such in the app.
const CAM_TIP='Our guide (Smith gives no distance): about 2.5 to 3 m away so your whole body fits with space around it; normal 1x lens, not ultra-wide (it bends lines and skews angles); square on to the movement with the joint you measure near the middle of the frame; tape the phone spot and your spot so retests match.';
const angleMeasures=t=>(t&&t.measures||[]).filter(m=>m.kind==='number'&&m.unit==='deg');
function mediaRec(id){return (_mob.media||[]).find(m=>m.id===id);}
function testOf(rec){const p=rec&&pillar(rec.pillar);return p&&p.tests.find(t=>t.id===rec.test);}
function hasAngle(id){return angleMeasures(testOf(mediaRec(id))).length>0;}
function angleAt(a,b,c){const v1=[a.x-b.x,a.y-b.y],v2=[c.x-b.x,c.y-b.y];
  const d=(v1[0]*v2[0]+v1[1]*v2[1])/(Math.hypot(v1[0],v1[1])*Math.hypot(v2[0],v2[1])||1);return Math.acos(Math.max(-1,Math.min(1,d)))*180/Math.PI;}
function fromVertical(a,b){const dx=b.x-a.x,dy=b.y-a.y,l=Math.hypot(dx,dy)||1;return Math.acos(Math.max(-1,Math.min(1,dy/l)))*180/Math.PI;}
const specOf=m=>(m&&m.angle)||{mode:'inside',points:['First point','The joint','Last point']};
const nPts=sp=>sp.mode==='vertical'?2:3;
const pivotOf=sp=>sp.mode==='vertical'?0:1;
function readAngle(sp,pts){
  if(pts.length<nPts(sp))return null;
  if(sp.mode==='vertical')return fromVertical(pts[0],pts[1]);
  const a=angleAt(pts[0],pts[1],pts[2]);return sp.mode==='flex'?180-a:a;
}
window.mobAngleAt=angleAt;window.mobReadAngle=readAngle;

// ---------- Auto-measure (owner, 9 Nov 2026) ----------
// A pose model (MediaPipe Pose Landmarker) runs IN THE BROWSER: the clip never leaves the phone. It finds the joints
// in every frame, code reads the angle with the same readAngle the tap tool uses, and the end-range frame per side
// is chosen by code (median of 5 neighbouring frames, so one jittery frame cannot win). Which joints and which side
// is the measure's `angle.pose` spec, which is data: {joints:[...], side:"visible"|"each"|"lifted", best:"max"|"min"}.
// "other:ankle" = the ankle of the other leg. He then accepts the points or drags one; the number is never a guess.
const MP_VER='0.10.14', MP_BASE='https://cdn.jsdelivr.net/npm/@mediapipe/tasks-vision@'+MP_VER;
const MP_MODEL='https://storage.googleapis.com/mediapipe-models/pose_landmarker/pose_landmarker_full/float16/latest/pose_landmarker_full.task';
const LMI={shoulder:[11,12],elbow:[13,14],wrist:[15,16],hip:[23,24],knee:[25,26],ankle:[27,28],heel:[29,30],toe:[31,32],ear:[7,8]};
const VIS_MIN=0.35;
let _poseP=null, _poseTs=0;
function poseLoader(){
  if(_poseP)return _poseP;
  _poseP=(async()=>{const v=await import(MP_BASE+'/vision_bundle.mjs');const fs=await v.FilesetResolver.forVisionTasks(MP_BASE+'/wasm');
    const o=d=>({baseOptions:{modelAssetPath:MP_MODEL,delegate:d},runningMode:'IMAGE',numPoses:1});
    try{return await v.PoseLandmarker.createFromOptions(fs,o('GPU'));}catch(e){return await v.PoseLandmarker.createFromOptions(fs,o('CPU'));}})();
  _poseP.catch(()=>{_poseP=null;});
  return _poseP;
}
const poseSpecOf=t=>{const m=angleMeasures(t).find(x=>x.angle&&x.angle.pose);return m?{m,sp:specOf(m)}:null;};
function hasPose(id){return !!poseSpecOf(testOf(mediaRec(id)));}
function poseCandidates(P,L){
  // "other:" = the other side's joint; "mid:" = halfway between both; "high:"/"low:" = whichever of the pair is higher
  // or lower in the frame. Seen side-on the model often swaps left and right, so the leg lift is read by geometry.
  // "level:A@B" = a point level with joint A, under joint B: a line parallel to the floor (pancake, internal rotation)
  const get=(n,s)=>{if(n.startsWith('level:')){const [a2,b2]=n.slice(6).split('@'), pa=get(a2,s), pb=get(b2,s);
      return{x:pb.x,y:pa.y,visibility:Math.min(pa.visibility??1,pb.visibility??1)};}
    const [pre,j]=n.includes(':')?n.split(':'):['',n], A=L[LMI[j][0]], B=L[LMI[j][1]];
    if(pre==='mid')return{x:(A.x+B.x)/2,y:(A.y+B.y)/2,visibility:Math.min(A.visibility??1,B.visibility??1)};
    if(pre==='high')return A.y<B.y?A:B; if(pre==='low')return A.y<B.y?B:A;
    return L[LMI[j][pre==='other'?1-s:s]];};
  const cand=s=>{const pts=P.joints.map(n=>get(n,s));return{pts,vis:Math.min(...pts.map(p=>p.visibility==null?1:p.visibility))};};
  const sd=s=>s===0?'L':'R';   // the model's left is HIS left, whichever way he faces the camera
  if(P.side==='each')return[0,1].map(s=>({side:sd(s),...cand(s)}));
  if(P.side==='lifted'){const s=L[LMI.ankle[0]].y<L[LMI.ankle[1]].y?0:1;return[{side:sd(s),...cand(s)}];}
  const a=cand(0),b=cand(1);return[{side:'both',...(a.vis>=b.vis?a:b)}];
}
const seekTo=(v,t)=>new Promise(res=>{let done=false;const fin=()=>{if(done)return;done=true;v.removeEventListener('seeked',fin);if(v.requestVideoFrameCallback){let ok=false;v.requestVideoFrameCallback(()=>{ok=true;res();});setTimeout(()=>{if(!ok)res();},400);}else res();};
  v.addEventListener('seeked',fin);setTimeout(fin,2000);v.currentTime=Math.min(t,Math.max(0,v.duration-0.05));});
// ---------- position + hold (owner, 14 Nov 2026: it measured him getting into position, not the held position) ----------
// posture: 'upright' = trunk within 35 deg of vertical; 'lying' = trunk within 30 deg of horizontal and the knees NOT
// well below the hips (lying, knees bent); 'all_fours' = trunk near horizontal and the knees well below the hips.
function postureOf(L){
  const m=(a,b)=>({x:(L[a].x+L[b].x)/2,y:(L[a].y+L[b].y)/2}), sh=m(11,12), hp=m(23,24), kn=m(25,26);
  const dx=sh.x-hp.x, dy=sh.y-hp.y, len=Math.hypot(dx,dy)||1e-6, fromVert=Math.acos(Math.min(1,Math.abs(dy)/len))*180/Math.PI;
  const kneeBelow=(kn.y-hp.y)/len;
  return {upright:fromVert<=35, horizontal:fromVert>=60, kneeBelow};
}
function postureOK(kind,L){if(!kind)return true;const p=postureOf(L);
  if(kind==='upright')return p.upright;
  if(kind==='lying')return p.horizontal&&p.kneeBelow<0.3;
  if(kind==='all_fours')return p.horizontal&&p.kneeBelow>=0.3;
  return true;}
// the best HELD position: a run of frames lasting the test's hold time, all in the right posture, the angle steady
// (spread <= 8 deg); its value is the median of the run (Smith: the average position held), its dots the frame nearest
function bestHeld(arr,best,holdS,step){
  const ok=arr.filter(x=>x.vis>=VIS_MIN&&x.a!=null&&x.pos!==false);
  const n=Math.max(2,Math.round(holdS/step));let win=null;
  for(let i=0;i+n<=ok.length;i++){const w=ok.slice(i,i+n);
    if(w[w.length-1].t-w[0].t>holdS+step*2.5)continue;      // a gap in the run (frames dropped): not continuous
    const as=w.map(x=>x.a), spread=Math.max(...as)-Math.min(...as);if(spread>8)continue;
    const med=as.slice().sort((a,b)=>a-b)[Math.floor(as.length/2)];
    if(!win||(best==='min'?med<win.med:med>win.med))win={med,w};}
  if(!win)return null;
  const pick=win.w.reduce((a,b)=>Math.abs(b.a-win.med)<Math.abs(a.a-win.med)?b:a);
  return {...pick,held:[win.w[0].t,win.w[win.w.length-1].t],med:Math.round(win.med*10)/10};
}
function bestOf(arr,best){
  const ok=arr.filter(x=>x.vis>=VIS_MIN&&x.a!=null);if(ok.length<3)return null;
  const sm=ok.map((x,i)=>{const w=ok.slice(Math.max(0,i-2),i+3).map(y=>y.a).sort((a,b)=>a-b);return w[Math.floor(w.length/2)];});
  let bi=0;sm.forEach((v,i)=>{if(best==='min'?v<sm[bi]:v>sm[bi])bi=i;});
  let pick=bi;for(let i=Math.max(0,bi-2);i<=Math.min(ok.length-1,bi+2);i++)if(Math.abs(ok[i].a-sm[bi])<Math.abs(ok[pick].a-sm[bi]))pick=i;
  return ok[pick];
}
async function poseScan(video,sp,prog,holdS){
  const lm=await poseLoader(), P=sp.pose, dur=video.duration, step=dur>40?0.25:0.2, W=video.videoWidth, H=video.videoHeight;
  const series={};video.pause();
  const _cv=document.createElement('canvas');_cv.width=W;_cv.height=H;
  for(let k=0;k*step<dur;k++){const t=Math.round(k*step*1000)/1000;
    await seekTo(video,t);
    _cv.getContext('2d').drawImage(video,0,0,W,H);
    const r=lm.detect(_cv), L=r&&r.landmarks&&r.landmarks[0];
    if(L){const pos=postureOK(P.posture,L);poseCandidates(P,L).forEach(c=>{const pts=c.pts.map(q=>({x:q.x*W,y:q.y*H}));(series[c.side]=series[c.side]||[]).push({t:Math.round(t*10)/10,a:readAngle(sp,pts),pts,vis:c.vis,pos});});}
    prog(t/dur);
  }
  const out={};Object.keys(series).forEach(s=>{const b=bestHeld(series[s],P.best,holdS||3,step);if(b)out[s]=b;});
  return out;
}
window.mobPoseBest=bestOf;window.mobPoseCandidates=poseCandidates;
window.mobAutoMeasure=async function(video,id,ov,btn,msg){
  const ps=poseSpecOf(testOf(mediaRec(id)));if(!ps||!video)return;
  btn.disabled=true;btn.style.opacity=0.5;const say=t=>{if(msg)msg.textContent=t;};
  try{
    say('Loading the pose model (first time only)...');
    if(!video.duration||!isFinite(video.duration))await new Promise(r=>video.addEventListener('loadedmetadata',r,{once:true}));
    const hs=parseFloat(holdOf(testOf(mediaRec(id))))||3;
    const res=await poseScan(video,ps.sp,f=>say('Finding your held position... '+Math.round(f*100)+'%'),hs);
    // a one-sided test filmed one side per clip: the visible-side result belongs to the clip's own side
    {const rec=mediaRec(id), t=testOf(rec);if(res.both&&t&&!t.sides.includes('both')&&rec&&t.sides.includes(rec.side)){res[rec.side]=res.both;delete res.both;}}
    const sides=Object.keys(res);
    if(!sides.length){say('No steady '+hs+' s hold in the test position found in this clip. Check the set-up and hold, refilm, or pause on your held position and measure by hand.');btn.disabled=false;btn.style.opacity=1;return;}
    await seekTo(video,res[sides[0]].t);
    mobMeasure(video,id,ov,{m:ps.m,res});
  }catch(e){say('Auto-measure could not start ('+(e&&e.message||'error')+'). Measure by hand instead.');btn.disabled=false;btn.style.opacity=1;}
};

window.mobMeasure=function(src,id,ov,auto){
  const rec=mediaRec(id), t=testOf(rec), ms=angleMeasures(t);
  const W=src.videoWidth||src.naturalWidth, H=src.videoHeight||src.naturalHeight;
  if(!W||!H){toast('Could not read this frame');return;}
  let frameS=src.tagName==='VIDEO'?Math.round(src.currentTime*10)/10:null;
  let m=auto?auto.m:ms[0], sp=specOf(m), moved=false;
  const maxW=Math.min(window.innerWidth-32,900), maxH=window.innerHeight*0.6, k=Math.min(maxW/W,maxH/H);
  const cv=document.createElement('canvas');cv.width=W;cv.height=H;
  cv.style.cssText='width:'+Math.round(W*k)+'px;max-width:100%;height:auto;border-radius:8px;touch-action:none;cursor:crosshair';
  const ctx=cv.getContext('2d'), frame=document.createElement('canvas');frame.width=W;frame.height=H;frame.getContext('2d').drawImage(src,0,0,W,H);
  const pts=[], R=Math.max(10,W/90);
  const tip=document.createElement('div');tip.style.cssText='color:#ddd;font-size:12px;margin-bottom:8px;text-align:center;max-width:520px';
  const out=document.createElement('div');out.style.cssText='color:#fff;font-weight:800;font-size:15px;margin:10px 0 6px;text-align:center';
  const bar=document.createElement('div');bar.style.cssText='display:flex;gap:8px;flex-wrap:wrap;justify-content:center;align-items:center';
  const sel=(opts,cur)=>{const s=document.createElement('select');s.style.cssText='font-size:13px;width:auto';opts.forEach(([v,l])=>{const o=document.createElement('option');o.value=v;o.textContent=l;if(v===cur)o.selected=true;s.appendChild(o);});return s;};
  const pick=sel(ms.map(x=>[x.key,x.label]),m.key);if(ms.length<2)pick.style.display='none';
  const autoSides=auto?Object.keys(auto.res).filter(s=>t.sides.includes(s)):[];
  const sides=t.sides, side=sel(sides.map(s=>[s,SIDE[s]]),autoSides[0]||(sides.includes(rec.side)?rec.side:sides[0]));if(sides.length<2)side.style.display='none';
  const loadAuto=async()=>{const r=auto&&auto.res[side.value];if(!r)return false;
    if(src.tagName==='VIDEO'&&Math.abs(src.currentTime-r.t)>0.05){await seekTo(src,r.t);frame.getContext('2d').drawImage(src,0,0,W,H);}
    frameS=r.t;pts.length=0;r.pts.forEach(q=>pts.push({x:q.x,y:q.y}));moved=false;return true;};
  const provL=document.createElement('label');provL.style.cssText='color:#ddd;font-size:12px;display:flex;align-items:center;gap:4px';
  const prov=document.createElement('input');prov.type='checkbox';provL.append(prov,'Provisional');
  const save=document.createElement('button');save.className='btn-sm';save.textContent='Save angle';save.style.cssText='background:var(--teal);color:#fff;border-color:var(--teal)';
  const reset=document.createElement('button');reset.className='btn-sm';reset.textContent='Start again';
  const close=document.createElement('button');close.className='btn-sm';close.textContent='Cancel';close.onclick=()=>ov.remove();
  const setTip=()=>{if(auto&&auto.res[side.value]){const rr=auto.res[side.value];tip.textContent='Held '+rr.held[0]+' to '+rr.held[1]+' s (median of the hold '+Math.round(rr.med)+'°); shown at '+frameS+' s. Check the dots sit on your '+sp.points.join(', ').toLowerCase()+'; drag any that are off, then save.';return;}
    tip.textContent='Tap '+sp.points.map((p,i)=>i===pivotOf(sp)?p+' (yellow)':p).join(', then ')+'. Drag a point to adjust it.'+(sp.mode==='vertical'?' The dashed line is straight down.':'');};
  const draw=()=>{ctx.drawImage(frame,0,0);ctx.lineWidth=Math.max(3,W/300);ctx.strokeStyle='#30d5a8';
    if(pts.length>1){ctx.beginPath();ctx.moveTo(pts[0].x,pts[0].y);for(let i=1;i<pts.length;i++)ctx.lineTo(pts[i].x,pts[i].y);ctx.stroke();}
    if(sp.mode==='vertical'&&pts.length){const L=pts[1]?Math.hypot(pts[1].x-pts[0].x,pts[1].y-pts[0].y):H/4;
      ctx.save();ctx.setLineDash([R,R*0.8]);ctx.strokeStyle='#ffffff';ctx.beginPath();ctx.moveTo(pts[0].x,pts[0].y);ctx.lineTo(pts[0].x,pts[0].y+L);ctx.stroke();ctx.restore();}
    pts.forEach((p,i)=>{ctx.beginPath();ctx.arc(p.x,p.y,R,0,7);ctx.fillStyle=i===pivotOf(sp)?'#ffc53d':'#30d5a8';ctx.fill();});
    const a=readAngle(sp,pts);
    if(a!=null){ctx.font='bold '+Math.round(W/22)+'px system-ui';ctx.fillStyle='#fff';ctx.strokeStyle='#000';ctx.lineWidth=Math.max(3,W/250);
      const pv=pts[pivotOf(sp)], tx=pv.x+R*2, ty=pv.y-R*2, lab=Math.round(a)+'°';ctx.strokeText(lab,tx,ty);ctx.fillText(lab,tx,ty);}
    out.textContent=a!=null?m.label+': '+Math.round(a)+'°':'Tap '+sp.points[pts.length]+' ('+(pts.length+1)+' of '+nPts(sp)+')';
    save.disabled=a==null;save.style.opacity=a==null?0.4:1;};
  side.onchange=async()=>{if(await loadAuto()){setTip();draw();}};
  pick.onchange=()=>{m=ms.find(x=>x.key===pick.value)||ms[0];sp=specOf(m);pts.length=0;setTip();draw();};
  reset.onclick=()=>{pts.length=0;draw();};
  const pos=e=>{const b=cv.getBoundingClientRect();return{x:(e.clientX-b.left)/b.width*W,y:(e.clientY-b.top)/b.height*H};};
  let drag=-1;
  cv.addEventListener('pointerdown',e=>{e.preventDefault();const p=pos(e);
    const near=pts.findIndex(q=>Math.hypot(q.x-p.x,q.y-p.y)<R*3);
    if(near>=0)drag=near;else if(pts.length<nPts(sp)){pts.push(p);drag=pts.length-1;}draw();});
  cv.addEventListener('pointermove',e=>{if(drag<0)return;pts[drag]=pos(e);moved=true;draw();});
  cv.addEventListener('pointerup',()=>{drag=-1;});
  cv.addEventListener('pointerleave',()=>{drag=-1;});
  ov.innerHTML='';ov.onclick=null;
  bar.append(pick,side,provL,save,reset,close);ov.append(tip,cv,out,bar);
  if(auto){const found=autoSides.length>1?'Found '+autoSides.map(s=>SIDE[s]+' '+Math.round(auto.res[s].a)+'°').join(', ')+'. Switch side to check each.':'';
    if(found){const o=document.createElement('div');o.style.cssText='color:#aaa;font-size:11px;margin-top:6px;text-align:center';o.textContent=found;ov.append(o);}
    loadAuto().then(()=>{setTip();draw();});}else{setTip();draw();}
  save.onclick=async()=>{
    const a=Math.round(readAngle(sp,pts)*10)/10, sd=side.value, key=m.key;
    const values={};let wasProv=false;
    resFor(rec.pillar,rec.test,sd,rec.date).forEach(r=>{values[r.measure]=r.label!=null?r.label:r.value;if(r.provisional)wasProv=true;});
    values[key]=a;
    save.disabled=true;save.textContent='Saving...';
    try{
      await syncApi('/api/fitness/mobility/result',{method:'POST',body:JSON.stringify({date:rec.date,pillar:rec.pillar,test:rec.test,side:sd,values,provisional:prov.checked||wasProv})});
      const blob=await new Promise(r=>cv.toBlob(r,'image/jpeg',0.85));
      const how=auto&&auto.res[sd]?(moved?', auto-detected then adjusted by hand':', auto-detected'):'';
      const note=m.label+' '+Math.round(a)+'°'+(sides.length>1?', '+SIDE[sd]:'')+(frameS!=null?', read at '+frameS+' s of the clip':'')+how;
      // Smith's program builder wants each test twice: with and without the angle lines. So the same frame is also
      // saved clean (the frame canvas, before any drawing), marked by its note.
      const clean=await new Promise(r=>frame.toBlob(r,'image/jpeg',0.9));
      const put=async(b,n)=>{const st=await syncApi('/api/fitness/mobility/media',{method:'POST',body:JSON.stringify({date:rec.date,pillar:rec.pillar,test:rec.test,side:sd,mime:'image/jpeg',bytes:b.size,note:n,frame_s:frameS,source:id})});
        if(st&&st.id){await fetch(API_BASE+'/api/fitness/mobility/media/'+st.id+'/0',{method:'PUT',headers:{'Authorization':'Bearer '+syncToken(),'Content-Type':'application/octet-stream'},body:b});
          await syncApi('/api/fitness/mobility/media/'+st.id+'/finish',{method:'POST'});}};
      await put(blob,note);
      await put(clean,CLEAN_NOTE+(sides.length>1?', '+SIDE[sd]:'')+(frameS!=null?', '+frameS+' s of the clip':''));
      ov.remove();await load();renderMobility();toast('Saved '+Math.round(a)+'° to '+t.name);
    }catch(e){save.disabled=false;save.textContent='Save angle';toast('Could not save. Check your connection.');}
  };
};
})();
