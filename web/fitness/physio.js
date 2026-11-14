// Physio tab (owner, 14 Nov 2026): old injuries and niggles, ranked by him. Each issue (data from /api/fitness/physio)
// has what it is, its tests (logged per day and side, trend shown against the first result), its drills, red flags
// and questions for the physio. A session ticks the drills done; the week's count shows against the target.
// The knee-to-wall test lives in Mobility (Squat pillar, auto-measure) and is read from there.
(function(){
let _ph=null, _phView='issues';
const esc=s=>String(s==null?'':s).replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
const today=()=>{const d=new Date();return new Date(d.getTime()-d.getTimezoneOffset()*60000).toISOString().slice(0,10);};
const fd=d=>new Date(d+'T12:00:00').toLocaleDateString('en-GB',{day:'numeric',month:'short'});
const SIDE={L:'Left',R:'Right',both:'Both'};
const ICON={check:'<path d="M5 12l5 5l10 -10"/>',up:'<path d="M12 19v-14M6 11l6 -6l6 6"/>',down:'<path d="M12 5v14M6 13l6 6l6 -6"/>',
  flat:'<path d="M5 12h14"/>',alert:'<path d="M12 9v4"/><path d="M12 16v.01"/><path d="M10.4 3.9l-8 13.9a1.8 1.8 0 0 0 1.6 2.7h16a1.8 1.8 0 0 0 1.6 -2.7l-8 -13.9a1.8 1.8 0 0 0 -3.2 0z"/>'};
const icon=(k,c,s)=>`<svg width="${s||16}" height="${s||16}" viewBox="0 0 24 24" fill="none" stroke="${c||'currentColor'}" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" style="flex-shrink:0" aria-hidden="true">${ICON[k]}</svg>`;

async function load(){try{_ph=await syncApi('/api/fitness/physio');}catch(e){_ph=null;}return _ph;}
const items=()=>((_ph&&_ph.data&&_ph.data.items)||[]).slice().sort((a,b)=>(a.rank||99)-(b.rank||99));
const isResolved=id=>((_ph.status||{})[id]||{}).status==='resolved';
const active=()=>items().filter(i=>!isResolved(i.id));
const _L=[];window.physioLessonN=i=>{const x=_L[i];if(x&&window.mobLesson)mobLesson(x.c,x.s,'',x.n,'');};
const watch=d=>{if(!d.lesson)return '';const i=_L.push({c:d.lesson.course,s:d.lesson.slug,n:d.name})-1;
  return `<button class="btn-sm" style="display:inline-flex;align-items:center;gap:6px;font-weight:800;padding:3px 10px 3px 3px;border-radius:999px;border:1px solid var(--teal);color:var(--teal);background:transparent" onclick="event.stopPropagation();physioLessonN(${i})"><span style="display:inline-flex;align-items:center;justify-content:center;width:20px;height:20px;border-radius:50%;background:var(--teal)"><svg width="10" height="10" viewBox="0 0 24 24" fill="#fff" style="margin-left:1px" aria-hidden="true"><path d="M7 4v16l13 -8z"/></svg></span>Watch</button>`;};

window.renderPhysio=async function(sub){
  const el=document.getElementById('page-physio-'+(sub||'issues'));if(!el)return;
  if(!_ph){el.innerHTML='<div class="fd-note" style="padding:20px 0">Loading&hellip;</div>';await load();}
  if(!_ph){el.innerHTML='<div class="fd-note" style="padding:20px 0">Could not reach Cortex.</div>';return;}
  if(sub==='session')return renderSession(el);
  if(sub==='history')return renderHistory(el);
  renderIssues(el);
};
const rerender=()=>{const s=(typeof currentSubpage!=='undefined'&&currentSubpage.physio)||'issues';renderPhysio(s);};

// ---------- results ----------
const resOf=(item,test,side,measure)=>(_ph.results||[]).filter(r=>r.item===item&&r.test===test&&r.side===side&&(measure?r.measure===measure:!r.measure));
function fmt(t,v,measure){if(v==null)return '';if(t.kind==='yesno')return v?'Yes':'No';
  if(t.kind==='mobility')return Math.round(v)+(measure==='angle'?'°':' cm');
  return (Math.round(v*10)/10)+(t.kind==='scale'?'/10':t.unit?' '+t.unit:'');}
// trend: first result vs latest, coloured by which way is better for this test
function trend(t,rows){if(rows.length<2)return '';const a=rows[0].value, b=rows[rows.length-1].value;if(a==null||b==null)return '';
  if(t.kind==='yesno'){const good=t.better==='yes'?b===1:b===0;return a===b?'':`<span style="font-size:11px;font-weight:800;color:${good?'var(--teal)':'var(--red,#e05050)'}">${good?'improved':'worse'} since ${fd(rows[0].date)}</span>`;}
  const d=b-a;if(Math.abs(d)<1e-9)return `<span style="font-size:11px;color:var(--ink3);display:inline-flex;align-items:center;gap:3px">${icon('flat','var(--ink3)',12)}same as ${fd(rows[0].date)}</span>`;
  const good=t.better==='lower'?d<0:d>0, c=good?'var(--teal)':'var(--red,#e05050)';
  return `<span style="font-size:11px;font-weight:800;color:${c};display:inline-flex;align-items:center;gap:3px">${icon(d>0?'up':'down',c,12)}${d>0?'+':''}${Math.round(d*10)/10} since ${fd(rows[0].date)}</span>`;}
// left vs right: the weaker side as a percentage of the stronger (foot lift: the left is the benchmark)
function gapLine(t,it){if(t.kind!=='number'||t.sides.length!==2)return '';
  const l=resOf(it.id,t.id,'L').slice(-1)[0], r=resOf(it.id,t.id,'R').slice(-1)[0];if(!l||!r||!l.value||!r.value)return '';
  const lo=Math.min(l.value,r.value), hi=Math.max(l.value,r.value), weak=l.value<r.value?'Left':'Right';
  return lo===hi?'<div style="font-size:12px;color:var(--teal);font-weight:800;margin-top:2px">Sides match</div>':`<div style="font-size:12px;margin-top:2px"><b>${weak}</b> is ${Math.round(lo/hi*100)}% of the other side</div>`;}

function testBlock(it,t){
  if(t.kind==='mobility'){
    const rows=t.sides.map(sd=>{const ms=(t.measures||['angle']).map(m=>{const rs=resOf(it.id,t.id,sd,m);const last=rs.slice(-1)[0];
        return last?`<span style="font-weight:800">${esc(fmt(t,last.value,m))}</span> ${trend({...t,kind:'number'},rs)}`:'';}).filter(Boolean);
      return `<div style="font-size:12.5px;padding:3px 0"><b>${SIDE[sd]}:</b> ${ms.length?ms.join(' &middot; '):'<span style="color:var(--ink3)">not measured yet</span>'}</div>`;}).join('');
    return `<div style="padding:8px 0;border-top:0.5px solid var(--border)"><div style="font-size:13px;font-weight:800">${esc(t.name)}</div>
      <div style="font-size:11.5px;color:var(--ink3);margin:2px 0 4px">${esc(t.cue||'')}</div>${rows}
      <button class="btn-sm" style="margin-top:4px" onclick="physioToMobility('${esc(t.pillar)}','${esc(t.test)}')">Measure in Mobility</button></div>`;}
  const inputs=t.sides.map(sd=>{const rs=resOf(it.id,t.id,sd), last=rs.slice(-1)[0], k=`ph_${it.id}_${t.id}_${sd}`;
    const field=t.kind==='yesno'?`<select id="${k}" class="fd-input" style="font-size:14px;padding:6px;width:auto"><option value="">&ndash;</option><option value="1">Yes</option><option value="0">No</option></select>`
      :t.kind==='scale'?`<select id="${k}" class="fd-input" style="font-size:14px;padding:6px;width:auto"><option value="">&ndash;</option>${Array.from({length:11},(_,i)=>`<option value="${i}">${i}</option>`).join('')}</select>`
      :`<input id="${k}" type="number" inputmode="decimal" class="fd-input" style="font-size:14px;padding:6px;width:90px" placeholder="${esc(t.unit||'')}"/>`;
    return `<div style="display:flex;align-items:center;gap:8px;flex-wrap:wrap;padding:4px 0">
      ${t.sides.length>1||t.sides[0]!=='both'?`<span style="font-size:12px;font-weight:800;width:42px">${SIDE[sd]}</span>`:''}${field}
      <span style="font-size:12px;color:var(--ink2)">${last?`Last ${esc(fmt(t,last.value))} (${fd(last.date)})`:'No result yet'}</span>${trend(t,rs)}</div>`;}).join('');
  return `<div style="padding:8px 0;border-top:0.5px solid var(--border)"><div style="font-size:13px;font-weight:800">${esc(t.name)}</div>
    <div style="font-size:11.5px;color:var(--ink3);margin:2px 0 4px">${esc(t.cue||'')}</div>${inputs}${gapLine(t,it)}
    <button class="btn-sm" style="margin-top:4px" onclick="physioSaveTest('${it.id}','${t.id}')">Save result</button></div>`;}

function drillLine(d){
  return `<div style="padding:7px 0;border-top:0.5px solid var(--border)"><div style="display:flex;align-items:center;gap:8px">
      <div style="flex:1;min-width:0"><div style="font-size:13px;font-weight:700">${esc(d.name)} <span style="font-size:10px;font-weight:800;color:var(--ink3)">${d.source==='smith'?'SMITH':'OURS'}</span></div>
        <div style="font-size:12px;color:var(--teal);font-weight:700">${esc(d.dose||'')}</div></div>${watch(d)}</div>
    ${(d.cues||[]).map(c=>`<div style="font-size:11.5px;color:var(--ink2)">&bull; ${esc(c)}</div>`).join('')}</div>`;}

function weekCard(){const w=(_ph.data.weekly_target)||{}, n=_ph.week_sessions||0, N=w.sessions||3;
  return `<div class="card" style="padding:12px 14px;margin-bottom:10px;display:flex;align-items:center;gap:12px">
    <div style="font-size:24px;font-weight:800;color:${n>=N?'var(--teal)':'var(--ink)'}">${n}/${N}</div>
    <div style="flex:1;font-size:12px;color:var(--ink2)">Physio sessions this week (from ${fd(_ph.week_start)}). Target ${N} a week, ${esc(w.minutes||'')} minutes, ${esc(w.when||'')}.</div>
    <button class="fd-btn" style="width:auto;padding:8px 14px" onclick="physioGo('session')">Log</button></div>`;}

function issueCard(it){
  const res=isResolved(it.id), wk=(_ph.weeks_logged||{})[it.id];
  const scan=it.scan_after_weeks&&wk!=null&&wk>=it.scan_after_weeks;
  return `<div class="card" id="ph_${it.id}" style="padding:12px 14px;margin-bottom:10px;${res?'opacity:0.6':''}">
    <div style="display:flex;align-items:center;gap:8px"><span style="font-size:18px;font-weight:800;color:var(--teal);width:22px">${it.rank}</span>
      <div style="flex:1;min-width:0"><div style="font-size:15px;font-weight:800">${esc(it.name)}</div><div style="font-size:11.5px;color:var(--ink3)">${esc(it.area||'')}</div></div>
      ${res?'<span style="font-size:10px;font-weight:800;color:var(--teal);border:1px solid var(--teal);border-radius:10px;padding:1px 7px">RESOLVED</span>':''}</div>
    ${it.caution&&!res?`<div style="display:flex;gap:6px;align-items:flex-start;font-size:12px;margin-top:8px;padding:8px;border-radius:6px;background:rgba(255,176,32,0.12);color:var(--ink)">${icon('alert','var(--amber)',15)}<span>${esc(it.caution)}</span></div>`:''}
    ${scan?`<div style="font-size:12px;margin-top:8px;padding:8px;border-radius:6px;background:rgba(255,176,32,0.12)">${wk} weeks logged: if it has not settled, ask for the scan.</div>`:''}
    <div style="font-size:12.5px;margin-top:8px;line-height:1.45"><b>Goal:</b> ${esc(it.goal||'')}</div>
    ${it.progression?`<div style="font-size:12px;color:var(--ink2);margin-top:3px">${esc(it.progression)}</div>`:''}
    <details style="margin-top:6px"><summary style="cursor:pointer;font-size:12px;font-weight:800;color:var(--ink2)">What it is</summary>
      <div style="font-size:12px;color:var(--ink2);margin-top:4px;line-height:1.45">${esc(it.summary||'')}</div>
      <div style="font-size:12px;margin-top:4px;line-height:1.45"><b>Likely:</b> ${esc(it.likely||'')}</div></details>
    <div style="font-size:11px;font-weight:800;color:var(--ink3);text-transform:uppercase;letter-spacing:0.06em;margin-top:10px">Tests</div>
    ${(it.tests||[]).map(t=>testBlock(it,t)).join('')}
    <div style="font-size:11px;font-weight:800;color:var(--ink3);text-transform:uppercase;letter-spacing:0.06em;margin-top:10px">Drills</div>
    ${(it.drills||[]).map(drillLine).join('')}
    ${(it.tips||[]).length?`<div style="margin-top:6px">${it.tips.map(x=>`<div style="font-size:12px;color:var(--ink2)">&bull; ${esc(x)}</div>`).join('')}</div>`:''}
    ${(it.red_flags||[]).length?`<details style="margin-top:8px"><summary style="cursor:pointer;font-size:12px;font-weight:800;color:var(--amber)">See the physio or GP if</summary>${it.red_flags.map(x=>`<div style="font-size:12px;color:var(--ink2)">&bull; ${esc(x)}</div>`).join('')}</details>`:''}
    ${(it.physio_questions||[]).length?`<details style="margin-top:6px"><summary style="cursor:pointer;font-size:12px;font-weight:800;color:var(--ink2)">Ask the physio</summary>${it.physio_questions.map(x=>`<div style="font-size:12px;color:var(--ink2)">&bull; ${esc(x)}</div>`).join('')}</details>`:''}
    <button style="background:none;border:none;padding:8px 0 0;color:var(--ink3);font-size:12px;text-decoration:underline;cursor:pointer" onclick="physioStatus('${it.id}','${res?'active':'resolved'}')">${res?'Reopen this issue':'Mark resolved'}</button></div>`;}

function renderIssues(el){
  const act=active(), done=items().filter(i=>isResolved(i.id));
  el.innerHTML=`<div class="section-label">Physio</div>
    <div style="font-size:12px;color:var(--ink3);margin-bottom:10px">${esc(_ph.data.intro||'')}</div>
    ${weekCard()}
    ${act.map(issueCard).join('')}
    ${done.length?`<div class="section-label" style="margin-top:14px">Resolved</div>${done.map(issueCard).join('')}`:''}`;
}

window.physioSaveTest=async function(item,test){
  const it=items().find(i=>i.id===item), t=(it.tests||[]).find(x=>x.id===test);let n=0;
  try{for(const sd of t.sides){const e=document.getElementById(`ph_${item}_${test}_${sd}`);if(!e||e.value==='')continue;
      await syncApi('/api/fitness/physio/result',{method:'POST',body:JSON.stringify({date:today(),item,test,side:sd,value:+e.value})});n++;}
    if(!n){toast('Enter a result first');return;}
    await load();rerender();setTimeout(()=>{const c=document.getElementById('ph_'+item);if(c)c.scrollIntoView({block:'start'});},40);toast('Saved');}
  catch(e){toast('Could not save that result.');}
};
window.physioStatus=async function(item,st){try{await syncApi('/api/fitness/physio/status',{method:'POST',body:JSON.stringify({item,status:st})});await load();rerender();}catch(e){toast('Could not change it.');}};
window.physioToMobility=function(pillar,test){if(typeof showSection==='function')showSection('mobility');if(window.mobOpen)setTimeout(()=>mobOpen(pillar,test),60);};
window.physioGo=function(sub){if(typeof currentSubpage!=='undefined'){currentSubpage.physio=sub;if(typeof showSection==='function')showSection('physio');}};

// ---------- session ----------
function renderSession(el){
  const act=active();
  el.innerHTML=`<div class="section-label">Log a physio session</div>
    <div style="font-size:12px;color:var(--ink3);margin-bottom:8px">Tick what you did. Heat first (steam room or a hot shower) helps the calf and foot work.</div>
    <div style="display:flex;gap:8px;align-items:center;margin-bottom:10px;flex-wrap:wrap"><label class="form-label" style="margin:0">Date</label><input id="phDay" type="date" value="${today()}" max="${today()}" class="fd-input" style="width:auto;font-size:14px"/>
      <label class="form-label" style="margin:0">Minutes</label><input id="phMin" type="number" inputmode="numeric" class="fd-input" style="width:80px;font-size:14px"/></div>
    ${act.map(it=>`<div class="card" style="padding:12px 14px;margin-bottom:10px"><div style="font-size:14px;font-weight:800;margin-bottom:4px">${it.rank}. ${esc(it.name)}</div>
      ${(it.drills||[]).map(d=>`<label style="display:flex;gap:10px;align-items:flex-start;padding:7px 0;border-top:0.5px solid var(--border);cursor:pointer">
        <input type="checkbox" class="phd" data-item="${it.id}" data-drill="${d.id}" style="margin-top:3px"/>
        <div style="flex:1;min-width:0"><div style="font-size:13px;font-weight:700">${esc(d.name)}</div><div style="font-size:12px;color:var(--teal)">${esc(d.dose||'')}</div></div>${watch(d)}</label>`).join('')}</div>`).join('')}
    <input id="phNote" class="fd-input" style="font-size:13px;margin-bottom:8px" placeholder="Note (how it felt, anything flared)"/>
    <button class="fd-btn" onclick="physioSaveSession()">Save session</button>`;
}
window.physioSaveSession=async function(){
  const drills=[...document.querySelectorAll('.phd')].filter(e=>e.checked).map(e=>({item:e.dataset.item,drill:e.dataset.drill}));
  if(!drills.length){toast('Tick at least one drill');return;}
  const m=document.getElementById('phMin').value;
  try{await syncApi('/api/fitness/physio/session',{method:'POST',body:JSON.stringify({date:document.getElementById('phDay').value||today(),drills,minutes:m===''?null:+m,note:document.getElementById('phNote').value||''})});
    await load();physioGo('issues');window.scrollTo(0,0);toast('Session saved');}
  catch(e){toast('Could not save the session.');}
};

// ---------- history ----------
function renderHistory(el){
  const name=(i,d)=>{const it=items().find(x=>x.id===i);const dr=it&&(it.drills||[]).find(x=>x.id===d);return dr?dr.name:d;};
  const ss=_ph.sessions||[], rs=(_ph.results||[]).filter(r=>r.id).slice().reverse();
  const tname=(i,t)=>{const it=items().find(x=>x.id===i);const tt=it&&(it.tests||[]).find(x=>x.id===t);return tt||{name:t,kind:'number'};};
  el.innerHTML=`<div class="section-label">Sessions</div>
    ${ss.length?ss.slice(0,40).map(s=>`<div class="card" style="padding:10px 14px;margin-bottom:8px"><div style="display:flex;justify-content:space-between;align-items:center"><b style="font-size:13px">${fd(s.date)}${s.minutes?' &middot; '+s.minutes+' min':''}</b><button class="btn-sm" onclick="physioDelSession('${s.id}')">Remove</button></div>
      <div style="font-size:12px;color:var(--ink2);margin-top:3px">${esc(s.drills.map(x=>name(x.item,x.drill)).join(' · '))}</div>${s.note?`<div style="font-size:12px;color:var(--ink3)">${esc(s.note)}</div>`:''}</div>`).join(''):'<div class="fd-note">No sessions yet.</div>'}
    <div class="section-label" style="margin-top:14px">Test results</div>
    ${rs.length?`<div class="card" style="padding:6px 14px">${rs.slice(0,80).map(r=>{const t=tname(r.item,r.test);return `<div style="display:flex;align-items:center;gap:8px;padding:6px 0;border-top:0.5px solid var(--border);font-size:12.5px">
      <span style="width:52px;color:var(--ink3)">${fd(r.date)}</span><span style="flex:1">${esc(t.name)}${r.side!=='both'?' &middot; '+SIDE[r.side]:''}</span><b>${esc(fmt(t,r.value))}</b>
      <button class="btn-sm" onclick="physioDelResult('${r.id}')">Remove</button></div>`;}).join('')}</div>`:'<div class="fd-note">No results yet.</div>'}`;
}
window.physioDelSession=async function(id){if(!confirm('Remove this session?'))return;await syncApi('/api/fitness/physio/session/'+id,{method:'DELETE'});await load();rerender();};
window.physioDelResult=async function(id){if(!confirm('Remove this result?'))return;await syncApi('/api/fitness/physio/result/'+id,{method:'DELETE'});await load();rerender();};
})();
