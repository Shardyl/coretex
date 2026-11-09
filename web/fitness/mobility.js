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
  renderPillar(el,pillar(_mobView));
};

function todoBlock(items,max,title){
  if(!items.length)return '';
  return `<div class="card" style="padding:12px 14px;margin-bottom:12px"><div style="font-size:13px;font-weight:800;margin-bottom:6px">${title}</div>
    ${items.slice(0,max).map(x=>`<div onclick="mobOpen('${x.p}','${x.t}')" style="display:flex;align-items:center;gap:8px;padding:6px 0;border-top:0.5px solid var(--border);cursor:pointer;font-size:13px">
      ${icon(ST[x.k].i,ST[x.k].c)}<span style="flex:1">${esc(x.txt)}</span>${icon('arrow','var(--ink3)',14)}</div>`).join('')}
    ${items.length>max?`<div style="font-size:11px;color:var(--ink3);margin-top:4px">and ${items.length-max} more</div>`:''}</div>`;
}

function renderList(el){
  const all=[];_mob.pillars.forEach(p=>all.push(...todos(p)));
  el.innerHTML=`<div class="section-label">Flexibility pillars</div>
    <div style="font-size:12px;color:var(--ink3);margin-bottom:12px">Matthew Smith's eight skills, in our assessment order. Each assessment is done cold and repeated at the end of every training block.</div>
    ${todoBlock(all,6,'Next to do')}
    ${_mob.pillars.map(p=>{
      const s=pillarState(p), ready=s.total>0, g=ready?gapFor(p):null;
      const gtxt=g&&g.any?'Gap '+g.rows.filter(r=>r.gap!=null).map(r=>(g.ta.sides.length>1?r.side+' ':'')+r.gap+'°').join(' &middot; '):'';
      return `<div class="card" style="padding:12px 14px;margin-bottom:10px;${ready?'cursor:pointer':'opacity:0.55'}" ${ready?`onclick="mobOpen('${p.id}')"`:''}>
        <div style="display:flex;align-items:center;gap:12px">
          ${ready?ring(s.done/s.total,s.status==='baseline'?'var(--teal)':'var(--amber)',s.done+'/'+s.total):ring(0,'var(--border)','&ndash;')}
          <div style="flex:1;min-width:0"><div style="display:flex;align-items:center;gap:8px;flex-wrap:wrap"><span style="font-size:15px;font-weight:800">${esc(p.name)}</span>${chip(s.status)}</div>
            <div style="font-size:12px;color:var(--ink3);margin-top:3px">${ready?`${s.total} tests &middot; ${s.last?'last assessed '+fd(s.last):'not assessed yet'}${gtxt?' &middot; <b style="color:var(--ink2)">'+gtxt+'</b>':''}`:'Tests added when we reach this assessment'}</div></div>
          ${ready?icon('arrow','var(--ink3)',18):''}</div></div>`;}).join('')}`;
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

function renderPillar(el,p){
  if(!p){_mobView='list';return renderList(el);}
  const s=pillarState(p);
  const anyDays=[...new Set([...(_mob.results||[]),...(_mob.media||[])].filter(r=>r.pillar===p.id).map(r=>r.date))].sort();
  const day=_mobDay||anyDays[anyDays.length-1]||today();
  const days=[...new Set((_mob.results||[]).filter(r=>r.pillar===p.id).map(r=>r.date))].sort();
  const mediaDay=(t,side)=>clipsOf(p.id,t).filter(m=>m.side===side&&m.date===day);
  el.innerHTML=`<button class="btn-sm" onclick="mobBack()" style="margin-bottom:10px">&lsaquo; All pillars</button>
    <div style="display:flex;align-items:center;gap:8px;flex-wrap:wrap"><span style="font-size:18px;font-weight:800">${esc(p.name)}</span>${chip(s.status)}</div>
    ${gridBlock(p,s)}
    ${gapBlock(p)}
    ${todoBlock(todos(p),8,'Still to do')}
    ${p.setup?`<div style="font-size:12px;color:var(--ink2);margin:6px 0 4px;line-height:1.5">${esc(p.setup)}</div>`:''}
    ${p.lesson?`<a href="${esc(p.lesson)}" target="_blank" rel="noopener" style="font-size:12px;color:var(--blue)">Open the lesson video</a>`:''}
    <div style="display:flex;gap:8px;align-items:center;margin:12px 0">
      <label class="form-label" style="margin:0">Assessment date</label>
      <input type="date" value="${day}" max="${today()}" class="fd-input" style="width:auto;font-size:14px" onchange="mobDay(this.value)"/></div>
    ${p.tests.map((t,i)=>{const both=t.sides.length>1?mediaDay(t.id,'both'):[];
      const prov=t.sides.some(sd=>resFor(p.id,t.id,sd,day).some(r=>r.provisional));
      return `<div class="card" id="mobt_${t.id}" style="padding:12px 14px;margin-bottom:10px;scroll-margin-top:70px">
      <div style="font-size:14px;font-weight:800">${i+1}. ${esc(t.name)}${t.role?` <span style="font-size:10px;font-weight:700;color:var(--ink3)">${t.role==='active'?'ACTIVE':'PASSIVE'}</span>`:''}</div>
      ${t.camera?`<div style="font-size:12px;color:var(--ink2);margin-top:4px;padding:6px 8px;border-radius:6px;background:var(--surface2)"><b>Camera:</b> ${esc(t.camera)}</div>`:''}
      ${t.cue?`<div style="font-size:12px;color:var(--ink2);margin-top:4px;line-height:1.45">${esc(t.cue)}</div>`:''}
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
      +`<div style="display:flex;gap:10px;margin-top:12px;flex-wrap:wrap;justify-content:center">${hasAngle(id)&&!(rec&&rec.source)?`<button class="btn-sm" id="mobMeasure" style="background:var(--teal);color:#fff;border-color:var(--teal)">Measure angle${mime.startsWith('video')?' on this frame':''}</button>`:''}<button class="btn-sm" id="mobDel">Delete</button><button class="btn-sm" id="mobClose">Close</button></div>`
      +(mime.startsWith('video')&&hasAngle(id)?'<div style="color:#aaa;font-size:11px;margin-top:8px">Pause on your best position, then tap Measure.</div>':'');
    ov.querySelector('#mobClose').onclick=()=>{URL.revokeObjectURL(url);ov.remove();};
    const mBtn=ov.querySelector('#mobMeasure');
    if(mBtn)mBtn.onclick=()=>{const src=ov.querySelector('video,img');if(src.tagName==='VIDEO')src.pause();mobMeasure(src,id,ov);};
    ov.querySelector('#mobDel').onclick=async()=>{if(!confirm('Remove this clip?'))return;await fetch(API_BASE+'/api/fitness/mobility/media/'+encodeURIComponent(id),{method:'DELETE',headers:{'Authorization':'Bearer '+syncToken()}});ov.remove();await load();renderMobility();};
  }catch(e){ov.innerHTML='<div style="color:#fff">Could not load it.</div>';setTimeout(()=>ov.remove(),1500);}
};

// ---------- Tap to measure (owner, 8 Nov 2026; per-test angle definitions 9 Nov 2026) ----------
// The frame (a paused video or a photo) is drawn on a canvas and he taps the points the measure's `angle` spec names.
// Code computes the angle by its mode: "inside" = the angle at the middle point; "flex" = 180 minus it (how far the
// joint has folded, so a fold and a leg lift read on one scale); "vertical" = two points, the line's angle from a
// plumb line. Saving writes it into that test's measure for the day and the chosen side (other values kept), with
// the provisional flag, and uploads the marked-up frame as a still carrying the clip and the second it came from.
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

window.mobMeasure=function(src,id,ov){
  const rec=mediaRec(id), t=testOf(rec), ms=angleMeasures(t);
  const W=src.videoWidth||src.naturalWidth, H=src.videoHeight||src.naturalHeight;
  if(!W||!H){toast('Could not read this frame');return;}
  const frameS=src.tagName==='VIDEO'?Math.round(src.currentTime*10)/10:null;
  let m=ms[0], sp=specOf(m);
  const maxW=Math.min(window.innerWidth-32,900), maxH=window.innerHeight*0.6, k=Math.min(maxW/W,maxH/H);
  const cv=document.createElement('canvas');cv.width=W;cv.height=H;
  cv.style.cssText='width:'+Math.round(W*k)+'px;max-width:100%;height:auto;border-radius:8px;touch-action:none;cursor:crosshair';
  const ctx=cv.getContext('2d'), frame=document.createElement('canvas');frame.width=W;frame.height=H;frame.getContext('2d').drawImage(src,0,0,W,H);
  const pts=[], R=Math.max(10,W/90);
  const tip=document.createElement('div');tip.style.cssText='color:#ddd;font-size:12px;margin-bottom:8px;text-align:center;max-width:520px';
  const out=document.createElement('div');out.style.cssText='color:#fff;font-weight:800;font-size:15px;margin:10px 0 6px;text-align:center';
  const bar=document.createElement('div');bar.style.cssText='display:flex;gap:8px;flex-wrap:wrap;justify-content:center;align-items:center';
  const sel=(opts,cur)=>{const s=document.createElement('select');s.style.cssText='font-size:13px';opts.forEach(([v,l])=>{const o=document.createElement('option');o.value=v;o.textContent=l;if(v===cur)o.selected=true;s.appendChild(o);});return s;};
  const pick=sel(ms.map(x=>[x.key,x.label]),m.key);if(ms.length<2)pick.style.display='none';
  const sides=t.sides, side=sel(sides.map(s=>[s,SIDE[s]]),sides.includes(rec.side)?rec.side:sides[0]);if(sides.length<2)side.style.display='none';
  const provL=document.createElement('label');provL.style.cssText='color:#ddd;font-size:12px;display:flex;align-items:center;gap:4px';
  const prov=document.createElement('input');prov.type='checkbox';provL.append(prov,'Provisional');
  const save=document.createElement('button');save.className='btn-sm';save.textContent='Save angle';save.style.cssText='background:var(--teal);color:#fff;border-color:var(--teal)';
  const reset=document.createElement('button');reset.className='btn-sm';reset.textContent='Start again';
  const close=document.createElement('button');close.className='btn-sm';close.textContent='Cancel';close.onclick=()=>ov.remove();
  const setTip=()=>{tip.textContent='Tap '+sp.points.map((p,i)=>i===pivotOf(sp)?p+' (yellow)':p).join(', then ')+'. Drag a point to adjust it.'+(sp.mode==='vertical'?' The dashed line is straight down.':'');};
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
  pick.onchange=()=>{m=ms.find(x=>x.key===pick.value)||ms[0];sp=specOf(m);pts.length=0;setTip();draw();};
  reset.onclick=()=>{pts.length=0;draw();};
  const pos=e=>{const b=cv.getBoundingClientRect();return{x:(e.clientX-b.left)/b.width*W,y:(e.clientY-b.top)/b.height*H};};
  let drag=-1;
  cv.addEventListener('pointerdown',e=>{e.preventDefault();const p=pos(e);
    const near=pts.findIndex(q=>Math.hypot(q.x-p.x,q.y-p.y)<R*3);
    if(near>=0)drag=near;else if(pts.length<nPts(sp)){pts.push(p);drag=pts.length-1;}draw();});
  cv.addEventListener('pointermove',e=>{if(drag<0)return;pts[drag]=pos(e);draw();});
  cv.addEventListener('pointerup',()=>{drag=-1;});
  cv.addEventListener('pointerleave',()=>{drag=-1;});
  ov.innerHTML='';ov.onclick=null;
  bar.append(pick,side,provL,save,reset,close);ov.append(tip,cv,out,bar);setTip();draw();
  save.onclick=async()=>{
    const a=Math.round(readAngle(sp,pts)*10)/10, sd=side.value, key=m.key;
    const values={};let wasProv=false;
    resFor(rec.pillar,rec.test,sd,rec.date).forEach(r=>{values[r.measure]=r.label!=null?r.label:r.value;if(r.provisional)wasProv=true;});
    values[key]=a;
    save.disabled=true;save.textContent='Saving...';
    try{
      await syncApi('/api/fitness/mobility/result',{method:'POST',body:JSON.stringify({date:rec.date,pillar:rec.pillar,test:rec.test,side:sd,values,provisional:prov.checked||wasProv})});
      const blob=await new Promise(r=>cv.toBlob(r,'image/jpeg',0.85));
      const note=m.label+' '+Math.round(a)+'°'+(sides.length>1?', '+SIDE[sd]:'')+(frameS!=null?', read at '+frameS+' s of the clip':'');
      const st=await syncApi('/api/fitness/mobility/media',{method:'POST',body:JSON.stringify({date:rec.date,pillar:rec.pillar,test:rec.test,side:sd,mime:'image/jpeg',bytes:blob.size,note,frame_s:frameS,source:id})});
      if(st&&st.id){await fetch(API_BASE+'/api/fitness/mobility/media/'+st.id+'/0',{method:'PUT',headers:{'Authorization':'Bearer '+syncToken(),'Content-Type':'application/octet-stream'},body:blob});
        await syncApi('/api/fitness/mobility/media/'+st.id+'/finish',{method:'POST'});}
      ov.remove();await load();renderMobility();toast('Saved '+Math.round(a)+'° to '+t.name);
    }catch(e){save.disabled=false;save.textContent='Save angle';toast('Could not save. Check your connection.');}
  };
};
})();
