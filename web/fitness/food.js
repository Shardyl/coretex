// Food tracker + Health Connect day view for the Fitness PWA.
//
// Data lives in the main script's globals (foods, foodLog, foodTargets, healthData) and syncs with
// the rest of the document. Nutrition is stored per 100 g and a log row is stamped with the kcal /
// protein it worked out at the time, so editing a food later never rewrites what was eaten.
// Figures come from Open Food Facts / USDA (via the box) or are typed in; nothing is estimated.
(function(){
'use strict';

const MEALS=[['breakfast','Breakfast'],['lunch','Lunch'],['dinner','Dinner'],['snacks','Snacks']];
let foodDay=localDay(new Date());
let histChart=null;

// ---------- helpers ----------
function localDay(d){const z=new Date(d.getTime()-d.getTimezoneOffset()*60000);return z.toISOString().slice(0,10);}
function shiftDay(day,n){const d=new Date(day+'T12:00:00');d.setDate(d.getDate()+n);return localDay(d);}
// Android's installed-app time box only highlights hh/mm and cannot be edited (5 Oct 2026), so every time
// in the Food screens is two plain dropdowns writing into a hidden input with the original id.
function timePick(id,val,allowEmpty){
  const [h,m]=(val||'').split(':');
  const opt=(n,sel)=>Array.from({length:n},(_,i)=>String(i).padStart(2,'0')).map(v=>`<option ${v===sel?'selected':''}>${v}</option>`).join('');
  const upd=`document.getElementById('${id}').value=(document.getElementById('${id}_h').value&&document.getElementById('${id}_m').value)?document.getElementById('${id}_h').value+':'+document.getElementById('${id}_m').value:''`;
  const blank=allowEmpty?'<option value="">--</option>':'';
  return `<span style="display:inline-flex;gap:4px;align-items:center">
    <select id="${id}_h" class="fd-input" style="width:auto;padding:8px 6px" onchange="${upd}">${blank}${opt(24,h)}</select>
    <span style="font-weight:800">:</span>
    <select id="${id}_m" class="fd-input" style="width:auto;padding:8px 6px" onchange="${upd}">${blank}${opt(60,m)}</select>
    <input type="hidden" id="${id}" value="${val||''}"/></span>`;
}
function nowAt(){return foodDay===localDay(new Date())?new Date().toISOString():null;}   // eaten-at for food logged on the day
function newId(p){return p+'_'+Date.now().toString(36)+Math.random().toString(36).slice(2,6);}
function r0(v){return v==null||isNaN(v)?null:Math.round(v);}
function r1(v){return v==null||isNaN(v)?null:Math.round(v*10)/10;}
function fmtN(v){return v==null?'—':Math.round(v).toLocaleString();}
function esc(s){return String(s==null?'':s).replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));}
function dayLabel(day){
  const t=localDay(new Date());
  if(day===t)return'Today';
  if(day===shiftDay(t,-1))return'Yesterday';
  if(day===shiftDay(t,1))return'Tomorrow';
  return new Date(day+'T12:00:00').toLocaleDateString('en-GB',{weekday:'short',day:'numeric',month:'short'});
}
function entriesFor(day){return foodLog.filter(e=>e.date===day);}
function totals(list){
  return list.reduce((a,e)=>({kcal:a.kcal+(+e.kcal||0),protein:a.protein+(+e.protein||0),
    carbs:a.carbs+(+e.carbs||0),fat:a.fat+(+e.fat||0)}),{kcal:0,protein:0,carbs:0,fat:0});
}
function healthDay(day){return (healthData.days||[]).find(d=>d.date===day)||null;}
// Burned = BMR (pro rata through today) + the watch's ACTIVE calories. Health Connect's own TOTAL
// uses a generic body's basal rate, so it is not used. No watch data that day -> no figure.
// Burned = BMR (pro rata through today) + everyday movement (Health Connect active kcal, which on his
// phone EXCLUDES workouts) + watch workouts (Samsung SDK) minus the resting burn inside them, which the
// BMR already counts. Steam room is left out: heat raises heart rate without real energy cost.
function burnedParts(day){
  const h=healthDay(day), today=localDay(new Date());
  if(day>today||!h||(h.activeKcal==null&&h.steps==null))return null;
  const bmr=+foodTargets.bmr||1873;
  let frac=1;
  if(day===today){const n=new Date();frac=(n.getHours()*60+n.getMinutes())/1440;}
  const work=sessionsOn(day).filter(s=>!/steam/i.test(s.title||'')&&s.kcal!=null)
    .reduce((a,s)=>a+Math.max(0,s.kcal-bmr*(s.minutes||0)/1440),0);
  const p={bmr:Math.round(bmr*frac),move:Math.round(+h.activeKcal||0),work:Math.round(work)};
  p.total=p.bmr+p.move+p.work;
  return p;
}
function burnedFor(day){const p=burnedParts(day);return p?p.total:null;}
// Owner's rule: anything under 10 minutes is not a workout (watch false starts, short walks). Ignore it.
const MIN_WORKOUT_MIN=10;
function sessionsOn(day){return (healthData.sessions||[]).filter(s=>s.date===day&&(s.minutes==null||s.minutes>=MIN_WORKOUT_MIN));}
function portion(f,g){
  const k=g/100;
  return {kcal:r0(f.kcal100*k),protein:r1((f.protein100||0)*k),carbs:r1((f.carbs100||0)*k),fat:r1((f.fat100||0)*k)};
}
function lastQty(fid){const e=foodLog.filter(x=>x.foodId===fid&&x.qtyG!=null).sort((a,b)=>((b.at||b.date)>(a.at||a.date)?1:-1))[0];return e?e.qtyG:null;}
function lastUsed(){const m={};foodLog.forEach(e=>{if(e.foodId&&(!m[e.foodId]||e.date>m[e.foodId]))m[e.foodId]=e.date;});return m;}
function sessionName(s){
  if(s.title)return s.title;
  const t=(s.typeName||'').replace(/^EXERCISE_TYPE_/,'').replace(/_/g,' ').toLowerCase();
  return t?t.charAt(0).toUpperCase()+t.slice(1):'Workout';
}

// ---------- styles (scoped to food pages) ----------
const css=`
.fd-daynav{display:flex;align-items:center;justify-content:space-between;margin-bottom:10px}
.fd-daynav button{background:var(--surface);border:0.5px solid var(--border2);border-radius:20px;width:36px;height:32px;font-size:16px;color:var(--ink2);cursor:pointer}
.fd-daynav .fd-day{font-size:15px;font-weight:800}
.fd-big{font-size:34px;font-weight:800;letter-spacing:-1px;line-height:1}
.fd-sub{font-size:11px;color:var(--ink3);font-weight:600;text-transform:uppercase;letter-spacing:0.05em}
.fd-row{display:grid;grid-template-columns:repeat(3,1fr);gap:8px;margin-top:12px}
.fd-cell{background:var(--surface2);border-radius:var(--radius);padding:8px 10px}
.fd-cell .v{font-size:17px;font-weight:800}
.fd-cell .l{font-size:10px;color:var(--ink3);font-weight:700;text-transform:uppercase;letter-spacing:0.04em}
.fd-bar{height:8px;background:var(--surface3);border-radius:4px;overflow:hidden;margin-top:6px}
.fd-bar>div{height:100%;background:var(--teal);border-radius:4px}
.fd-bar.over>div{background:var(--amber)}
.fd-meal{background:var(--surface);border:0.5px solid var(--border);border-radius:var(--radius-lg);padding:12px 14px;margin-bottom:10px}
.fd-meal-h{display:flex;justify-content:space-between;align-items:center;margin-bottom:4px}
.fd-meal-h b{font-size:14px}
.fd-meal-h span{font-size:12px;color:var(--ink3);font-weight:700}
.fd-entry{display:flex;justify-content:space-between;gap:10px;padding:8px 0;border-top:0.5px solid var(--border);cursor:pointer}
.fd-entry .n{font-size:13px;font-weight:600}
.fd-entry .q{font-size:11px;color:var(--ink3)}
.fd-entry .k{font-size:13px;font-weight:800;text-align:right;white-space:nowrap}
.fd-add{width:100%;margin-top:8px;background:none;border:1px dashed var(--border2);border-radius:var(--radius);padding:8px;font-size:12px;font-weight:700;color:var(--teal);cursor:pointer}
.fd-chip{display:inline-block;font-size:9px;font-weight:800;text-transform:uppercase;letter-spacing:0.05em;padding:2px 6px;border-radius:6px;background:var(--surface3);color:var(--ink2);margin-left:6px;vertical-align:middle}
.fd-sheet{position:fixed;inset:0;background:var(--surface2);z-index:9000;display:flex;flex-direction:column;max-width:520px;margin:0 auto}
.fd-sheet-h{display:flex;align-items:center;justify-content:space-between;padding:calc(env(safe-area-inset-top,0px) + 12px) 16px 10px;background:var(--surface);border-bottom:0.5px solid var(--border)}
.fd-sheet-h b{font-size:16px}
.fd-sheet-h button{background:none;border:none;font-size:14px;font-weight:700;color:var(--teal);cursor:pointer}
.fd-tabs{display:flex;background:var(--surface);border-bottom:0.5px solid var(--border)}
.fd-tabs button{flex:1;background:none;border:none;padding:10px 4px;font-size:12px;font-weight:600;color:var(--ink3);border-bottom:2.5px solid transparent;cursor:pointer}
.fd-tabs button.on{color:var(--teal);border-bottom-color:var(--teal)}
.fd-sheet-b{flex:1;overflow-y:auto;padding:14px 16px calc(env(safe-area-inset-bottom,0px) + 20px)}
.fd-item{background:var(--surface);border:0.5px solid var(--border);border-radius:var(--radius);padding:10px 12px;margin-bottom:8px;cursor:pointer}
.fd-item .n{font-size:13px;font-weight:700}
.fd-item .m{font-size:11px;color:var(--ink3);margin-top:2px}
.fd-input{width:100%;font-size:16px;padding:10px 12px;border:0.5px solid var(--border2);border-radius:var(--radius);background:var(--surface);color:var(--ink)}
.fd-btn{width:100%;background:var(--teal);color:#fff;border:none;border-radius:var(--radius);padding:12px;font-size:14px;font-weight:800;cursor:pointer;margin-top:10px}
.fd-btn.ghost{background:none;color:var(--red);border:0.5px solid var(--border2)}
.fd-btn.sec{background:var(--surface);color:var(--ink);border:0.5px solid var(--border2)}
.fd-video{width:100%;border-radius:var(--radius);background:#000;aspect-ratio:4/3;object-fit:cover}
.fd-note{font-size:12px;color:var(--ink3);margin:6px 0 10px;line-height:1.5}
.fd-hrow{display:grid;grid-template-columns:62px 1fr 1fr 1fr 1fr;gap:6px;font-size:12px;padding:7px 0;border-bottom:0.5px solid var(--border);align-items:center}
.fd-hrow.h{font-size:10px;color:var(--ink3);font-weight:800;text-transform:uppercase;letter-spacing:0.04em}
.fd-pos{color:var(--teal);font-weight:700}.fd-neg{color:var(--amber);font-weight:700}
`;
const st=document.createElement('style');st.textContent=css;document.head.appendChild(st);

// ---------- router ----------
window.renderFood=function(sub){
  if(sub==='today')renderToday();
  else if(sub==='history')renderHistory();
  else if(sub==='foods')renderFoodsPage();
  else if(sub==='weight')renderWeight();
  else if(sub==='fasting')renderFasting();
};
window.renderFoodCurrent=function(){
  if(typeof currentSection!=='undefined'&&currentSection==='food')window.renderFood(currentSubpage.food);
};

// ---------- Today ----------
function renderToday(){renderTodayInner();try{drawCgm();}catch(e){}}
function renderTodayInner(){
  const el=document.getElementById('page-food-today');
  const list=entriesFor(foodDay);
  const t=totals(list);
  const tgt=foodTargets.kcal||0, ptgt=foodTargets.protein||0;
  const remain=tgt-t.kcal;
  const h=healthDay(foodDay);
  const isToday=foodDay===localDay(new Date());
  const burned=burnedFor(foodDay);
  const isFuture=foodDay>localDay(new Date());
  const pPct=ptgt?Math.min(100,t.protein/ptgt*100):0;
  const kPct=tgt?Math.min(100,t.kcal/tgt*100):0;
  let balance='';
  if(burned!=null&&list.length){
    const d=burned-t.kcal;
    balance=`<div style="margin-top:10px;font-size:12px;color:var(--ink2)">${isToday?'So far today: ':''}burned ${fmtN(burned)} &minus; eaten ${fmtN(t.kcal)} = <span class="${d>=0?'fd-pos':'fd-neg'}">${d>=0?'deficit':'surplus'} ${fmtN(Math.abs(d))} kcal</span></div>`;
  }
  const sess=sessionsOn(foodDay);
  el.innerHTML=`
    <div class="fd-daynav">
      <button onclick="foodShiftDay(-1)" aria-label="Previous day">&lsaquo;</button>
      <div class="fd-day">${dayLabel(foodDay)}</div>
      <button onclick="foodShiftDay(1)" aria-label="Next day" ${foodDay>=shiftDay(localDay(new Date()),7)?'disabled style="opacity:0.3"':''}>&rsaquo;</button>
    </div>
    <div class="card">
      <div class="fd-sub">${remain>=0?'Remaining':'Over target'}</div>
      <div class="fd-big" style="color:${remain>=0?'var(--teal)':'var(--amber)'}">${fmtN(Math.abs(remain))} <span style="font-size:14px;font-weight:700;color:var(--ink3)">kcal</span></div>
      <div class="fd-bar${t.kcal>tgt?' over':''}"><div style="width:${kPct}%"></div></div>
      <div class="fd-row">
        <div class="fd-cell" onclick="foodEditTargets()" style="cursor:pointer"><div class="v">${fmtN(tgt)}</div><div class="l">Target</div></div>
        <div class="fd-cell"><div class="v">${fmtN(t.kcal)}</div><div class="l">Eaten</div></div>
        <div class="fd-cell"><div class="v">${burned!=null?fmtN(burned):'—'}</div><div class="l">${isFuture?'Planned day':'Burned'+(isToday&&burned!=null?' so far':'')}</div></div>
      </div>
      ${(()=>{const bp=burnedParts(foodDay);return bp?`<div style="margin-top:8px;font-size:11px;color:var(--ink3);text-align:right">Burned: BMR ${fmtN(bp.bmr)} &middot; Movement ${fmtN(bp.move)} &middot; Workouts ${fmtN(bp.work)}</div>`:'';})()}
      <div style="margin-top:12px;display:flex;justify-content:space-between;font-size:12px;font-weight:700">
        <span>Protein</span><span>${fmtN(t.protein)} / ${fmtN(ptgt)} g</span>
      </div>
      <div class="fd-bar"><div style="width:${pPct}%;background:var(--blue)"></div></div>
      <div style="margin-top:6px;font-size:11px;color:var(--ink3)">Carbs ${fmtN(t.carbs)} g &middot; Fat ${fmtN(t.fat)} g</div>
      ${balance}
      ${projectToday()}
    </div>
    ${isFuture?'':fastCard()}
    ${isFuture?'':readingsCard()}
    ${isFuture?'':creatineCard()}
    ${isToday?weighCard():''}
    ${isFuture?'':healthCard(h,sess)}
    ${MEALS.map(([k,label])=>mealCard(k,label,list.filter(e=>(e.meal||'snacks')===k))).join('')}
    <button class="fd-btn sec" onclick="foodCopyDay()">Copy ${dayLabel(shiftDay(foodDay,-1)).toLowerCase()}'s food to ${dayLabel(foodDay).toLowerCase()}</button>
  `;
}
// ---------- fasting (break-fast / close eating window) ----------
function fastDay(day){return fastDays.find(f=>f.date===day)||null;}
function hm(ms){if(ms==null||ms<0)return '—';const m=Math.round(ms/60000);return Math.floor(m/60)+'h '+String(m%60).padStart(2,'0')+'m';}
function tOf(iso){return iso?new Date(iso).toLocaleTimeString('en-GB',{hour:'2-digit',minute:'2-digit'}):'';}
// The last eating-window close before time t, from ANY earlier day, so a 2-3 day fast (no break-fast or
// close on the days in between) is measured from where it really started.
function lastCloseBefore(t){
  let best=null;
  fastDays.forEach(f=>{if(f.closedAt&&new Date(f.closedAt)<t&&(!best||f.closedAt>best))best=f.closedAt;});
  // ...unless he ate after that close: a later break-fast means the fast was already broken
  if(best&&fastDays.some(f=>f.brokeAt&&f.brokeAt>best&&new Date(f.brokeAt)<t))return null;
  return best?new Date(best):null;
}
// The fast that ENDED on `day` with its break-fast.
function fastLength(day){
  const f=fastDay(day);
  if(!f||!f.brokeAt)return null;
  const start=lastCloseBefore(new Date(f.brokeAt));
  return start?new Date(f.brokeAt)-start:null;
}
function longHM(ms){const h=ms/3600000;return h>=24?Math.floor(h/24)+'d '+Math.floor(h%24)+'h '+String(Math.round(ms/60000)%60).padStart(2,'0')+'m':hm(ms);}
function atTime(day,hhmm){const [h,m]=(hhmm||'').split(':').map(Number);const d=new Date(day+'T00:00:00');d.setHours(h||0,m||0,0,0);return d.toISOString();}
function nowHM(){const n=new Date();return String(n.getHours()).padStart(2,'0')+':'+String(n.getMinutes()).padStart(2,'0');}
function fastCard(){
  const f=fastDay(foodDay), prev=fastDay(shiftDay(foodDay,-1)), isToday=foodDay===localDay(new Date());
  if(!f||!f.brokeAt){
    const since=isToday?lastCloseBefore(new Date()):(prev&&prev.closedAt?new Date(prev.closedAt):null);
    const sinceTxt=since?(localDay(since)===shiftDay(localDay(new Date()),-1)?tOf(since.toISOString())+' yesterday':since.toLocaleString('en-GB',{weekday:'short',day:'numeric',month:'short',hour:'2-digit',minute:'2-digit'})):'';
    return `<div class="card" style="padding:12px 16px">
      <div class="fd-sub">Fasting${since&&isToday?' &middot; since '+sinceTxt:''}</div>
      ${since&&isToday?`<div class="fd-big" style="font-size:28px" id="fdFastLive" data-since="${since.toISOString()}">${longHM(Date.now()-since)}</div>
        <div style="font-size:12px;color:var(--ink3);margin-top:2px">${(Math.floor((Date.now()-since)/360000)/10).toFixed(1)} hours total</div>`:`<div class="fd-note" style="margin:4px 0">${since?'':'Tap &quot;Close eating window&quot; tonight so tomorrow&#39;s fast length is known.'}</div>`}
      <div style="display:flex;gap:8px;margin-top:8px">${timePick('fdBreakT',nowHM())}
      <button class="fd-btn" style="width:auto;margin:0;padding:0 16px" onclick="foodFastSet('brokeAt','fdBreakT')">Break fast</button></div></div>`;
  }
  const len=fastLength(foodDay);
  if(!f.closedAt)return `<div class="card" style="padding:12px 16px">
    <div class="fd-sub">Fast ${len!=null?'&middot; '+longHM(len):''} &middot; broken at ${tOf(f.brokeAt)}</div>
    <div style="display:flex;gap:8px;margin-top:8px">${timePick('fdCloseT',nowHM())}
    <button class="fd-btn sec" style="width:auto;margin:0;padding:0 16px" onclick="foodFastSet('closedAt','fdCloseT')">Close eating window</button>
    <button class="btn-sm" onclick="foodFastClear('brokeAt')">Undo</button></div></div>`;
  return `<div class="card" style="display:flex;justify-content:space-between;align-items:center;padding:12px 16px">
    <div><div class="fd-sub">Fast ${len!=null?longHM(len):''}</div>
    <div style="font-size:13px;font-weight:700">Eating window ${tOf(f.brokeAt)} to ${tOf(f.closedAt)} (${hm(new Date(f.closedAt)-new Date(f.brokeAt))})</div></div>
    <button class="btn-sm" onclick="foodFastClear('closedAt')">Reopen</button></div>`;
}
window.foodFastSet=function(field,inputId){
  const v=document.getElementById(inputId).value;if(!v){toast('Pick a time');return;}
  let f=fastDay(foodDay);if(!f){f={date:foodDay,brokeAt:null,closedAt:null};fastDays.push(f);}
  f[field]=atTime(foodDay,v);if(f.cleared)f.cleared=f.cleared.filter(x=>x!==field);
  if(field==='closedAt'&&f.brokeAt&&f.closedAt<f.brokeAt){f.closedAt=atTime(shiftDay(foodDay,1),v);}   // window closed after midnight
  saveFasts();renderToday();toast(field==='brokeAt'?'Fast broken':'Eating window closed');
};
window.foodFastClear=function(field){const f=fastDay(foodDay);if(!f)return;
  if(!confirm(field==='brokeAt'?'Undo the break-fast time? Your fast will show as still running.':'Reopen the eating window?'))return;
  f[field]=null;f.cleared=Array.from(new Set([...(f.cleared||[]),field]));saveFasts();renderToday();};

setInterval(()=>{const el=document.getElementById('fdFastLive');if(el&&el.offsetParent){const ms=Date.now()-new Date(el.dataset.since);el.textContent=longHM(ms);const n=el.nextElementSibling;if(n)n.textContent=(Math.floor(ms/360000)/10).toFixed(1)+' hours total';}},30000);

// ---------- ketone + glucose readings (stored in mmol/L) ----------
// Display unit. Default mg/dL: his finger-prick meter reads in mg/dL (94). Stored values stay mmol/L.
function gUnit(){try{return localStorage.getItem('fitness_glucose_unit')||'mgdl';}catch(e){return 'mgdl';}}
function gShow(mmol){return gUnit()==='mgdl'?Math.round(mmol*18):r1(mmol);}
function cgmOn(day){return (cgmData||[]).filter(x=>localDay(new Date(x.at))===day);}
function drawCgm(){
  const el=document.getElementById('fdCgmChart');if(!el)return;
  const cg=cgmOn(foodDay), cs=getComputedStyle(document.documentElement), c=cs.getPropertyValue('--blue').trim()||'#185FA5', txc=cs.getPropertyValue('--ink3').trim()||'#888';
  if(window._cgmChart)window._cgmChart.destroy();
  window._cgmChart=new Chart(el,{type:'line',data:{labels:cg.map(x=>tOf(x.at)),datasets:[{data:cg.map(x=>gShow(x.mmol)),borderColor:c,borderWidth:2,pointRadius:0,tension:0.3}]},
    options:{responsive:true,maintainAspectRatio:false,plugins:{legend:{display:false}},scales:{x:{ticks:{color:txc,font:{size:9},maxTicksLimit:6},grid:{display:false}},y:{ticks:{color:txc,font:{size:9},maxTicksLimit:4},grid:{display:false}}}}});
}
function readingsOn(day){return readings.filter(x=>localDay(new Date(x.at))===day).sort((a,b)=>a.at<b.at?-1:1);}
// What state was he in when the reading was taken? Worked out from the actual times, not from the
// moment it was saved: before break-fast = fasted (since last night's window close); after = time
// since the last food with an eaten-at time.
function readingState(x){
  const day=localDay(new Date(x.at)), t=new Date(x.at), f=fastDay(day), prev=fastDay(shiftDay(day,-1));
  const ate=foodLog.filter(e=>e.at&&new Date(e.at)<=t&&localDay(new Date(e.at))===day).sort((a,b)=>a.at<b.at?1:-1)[0];
  const broke=f&&f.brokeAt?new Date(f.brokeAt):null;
  if((!broke||t<broke)&&!ate){
    const since=lastCloseBefore(t);
    return {fasted:true,label:since?'fasted '+longHM(t-since):(x.context==='pre break-fast'?'fasted':(x.context||''))};
  }
  if(ate)return {fasted:false,label:hm(t-new Date(ate.at))+' after '+ate.name.split(',')[0].split(' (')[0].toLowerCase()};
  return {fasted:false,label:hm(t-broke)+' after break-fast'};
}
function gki(list){   // glucose-ketone index from a glucose and ketone reading taken within 30 minutes
  const k=list.filter(x=>x.kind==='ketones'), g=list.filter(x=>x.kind==='glucose');
  for(const a of k)for(const b of g)if(Math.abs(new Date(a.at)-new Date(b.at))<=30*60000&&a.value>0)return r1(b.value/a.value);
  return null;
}
function readingsCard(){
  const list=readingsOn(foodDay), f=fastDay(foodDay), g=gki(list), u=gUnit();
  const ctx=!f||!f.brokeAt?'pre break-fast':'after eating';
  const cg=cgmOn(foodDay), last=cg.length?cg[cg.length-1]:null;
  return `<div class="card" style="padding:12px 16px">
    ${last?`<div style="display:flex;justify-content:space-between;align-items:baseline;margin-bottom:6px">
      <div class="fd-sub">Libre sensor</div><div style="font-size:12px;color:var(--ink3)">${tOf(last.at)}${foodDay===localDay(new Date())?' &middot; '+Math.round((Date.now()-new Date(last.at))/60000)+' min ago':''}</div></div>
      <div style="display:flex;gap:16px;align-items:baseline;margin-bottom:6px"><div class="fd-big" style="font-size:28px">${gShow(last.mmol)} <span style="font-size:13px;color:var(--ink3)">${u==='mgdl'?'mg/dL':'mmol/L'}</span></div>
      <div style="font-size:12px;color:var(--ink3)">day ${gShow(Math.min(...cg.map(x=>x.mmol)))}&ndash;${gShow(Math.max(...cg.map(x=>x.mmol)))}, avg ${gShow(cg.reduce((a,x)=>a+x.mmol,0)/cg.length)}</div></div>
      <div style="position:relative;height:90px;margin-bottom:10px"><canvas id="fdCgmChart" role="img" aria-label="Glucose today"></canvas></div>`:''}
    <div class="fd-sub" style="margin-bottom:6px">Ketones and glucose${g!=null?` &middot; GKI ${g}`:''}</div>
    ${list.map(x=>`<div class="fd-entry" style="cursor:default;padding:5px 0"><div class="n" style="font-size:13px">${x.kind==='ketones'?'Ketones '+r1(x.value)+' mmol/L':'Glucose '+gShow(x.value)+(u==='mgdl'?' mg/dL':' mmol/L')}</div>
      <div class="q">${tOf(x.at)} &middot; ${esc(readingState(x).label)}${x.notes?' &middot; '+esc(x.notes):''} <button class="hist-del" onclick="foodDelReading('${x.id}')">&times;</button></div></div>`).join('')}
    <div style="display:grid;grid-template-columns:1fr 1fr;gap:8px;margin-top:6px">
      <div><div style="font-size:10px;color:var(--ink3);font-weight:700">KETONES mmol/L</div><input class="fd-input" type="number" step="0.1" inputmode="decimal" id="fdKet"/></div>
      <div><div style="font-size:10px;color:var(--ink3);font-weight:700">GLUCOSE <a href="#" onclick="foodToggleGUnit();return false" style="color:var(--teal)">${u==='mgdl'?'mg/dL':'mmol/L'}</a></div><input class="fd-input" type="number" step="0.1" inputmode="decimal" id="fdGlu"/></div>
    </div>
    <input class="fd-input" id="fdRdNote" placeholder="Note (optional), e.g. 2 slices biltong 5 min before" style="margin-top:8px;font-size:14px"/>
    <div style="display:flex;gap:8px;margin-top:8px;align-items:center">
      <span style="font-size:11px;color:var(--ink3);font-weight:700;white-space:nowrap">TAKEN AT</span>
      ${timePick('fdRdT',foodDay===localDay(new Date())?nowHM():'',true)}</div>
    <div style="display:flex;gap:8px;margin-top:8px">
      <select id="fdCtx" class="fd-input" style="font-size:14px">${['pre break-fast','after eating','random'].map(c=>`<option ${c===ctx?'selected':''}>${c}</option>`).join('')}</select>
      <button class="fd-btn" style="width:auto;margin:0;padding:0 18px" onclick="foodSaveReadings()">Save</button></div></div>`;
}
window.foodToggleGUnit=function(){try{localStorage.setItem('fitness_glucose_unit',gUnit()==='mgdl'?'mmol':'mgdl');}catch(e){}renderToday();};
window.foodSaveReadings=function(){
  const k=document.getElementById('fdKet').value, g=document.getElementById('fdGlu').value, ctx=document.getElementById('fdCtx').value;
  if(k===''&&g===''){toast('Enter a reading');return;}
  const tv=document.getElementById('fdRdT').value;
  if(!tv){toast('Set the time it was taken');return;}
  const at=atTime(foodDay,tv);
  if(k!==''){const v=+k;if(!(v>=0&&v<15)){toast('Ketones look wrong');return;}readings.push({id:newId('rd'),at,kind:'ketones',value:r1(v),context:ctx,notes:(document.getElementById('fdRdNote').value||'').trim()||null});}
  const note=(document.getElementById('fdRdNote').value||'').trim()||null;
  if(g!==''){let v=+g;
    // A meter in mg/dL typed into mmol/L mode (94 instead of 5.2): no real reading is over 30 mmol/L, so convert.
    if(gUnit()==='mgdl'||v>30){if(gUnit()!=='mgdl')toast(v+' looks like mg/dL: saved as '+r1(v/18.0182)+' mmol/L');v=v/18.0182;}
    if(!(v>1&&v<35)){toast('Glucose looks wrong');return;}
    readings.push({id:newId('rd'),at,kind:'glucose',value:Math.round(v*100)/100,context:ctx,notes:note});}
  saveReadings();renderToday();toast('Reading saved');
};
window.foodDelReading=function(id){const i=readings.findIndex(x=>x.id===id);if(i<0)return;markDeleted('readings',id);readings.splice(i,1);saveReadings();renderToday();};

let fastChart=null;
function renderFasting(){
  const el=document.getElementById('page-food-fasting'), today=localDay(new Date());
  const days=[];for(let i=0;i<30;i++)days.push(shiftDay(today,-i));
  const rows=days.map(d=>{const L=readingsOn(d), pre=L.filter(x=>readingState(x).fasted);
    const kk=pre.find(x=>x.kind==='ketones')||L.find(x=>x.kind==='ketones'), gg=pre.find(x=>x.kind==='glucose')||L.find(x=>x.kind==='glucose');
    return {d,len:fastLength(d),f:fastDay(d),k:kk?kk.value:null,g:gg?gg.value:null,gki:gki(pre.length?pre:L)};});
  const lens=rows.slice(0,7).map(r=>r.len).filter(x=>x!=null);
  const avg=lens.length?lens.reduce((a,b)=>a+b,0)/lens.length:null;
  const u=gUnit();
  el.innerHTML=`<div class="card"><div class="fd-row" style="margin-top:0">
      <div class="fd-cell"><div class="v">${hm(avg)}</div><div class="l">Avg fast, 7 days</div></div>
      <div class="fd-cell"><div class="v">${lens.length}</div><div class="l">Fasts logged</div></div>
      <div class="fd-cell"><div class="v">${hm(rows.map(r=>r.len).filter(x=>x!=null).reduce((a,b)=>Math.max(a,b),0)||null)}</div><div class="l">Longest, 30 days</div></div>
    </div></div>
    <div class="chart-section"><div class="chart-title" style="margin-bottom:8px">Fast length and pre break-fast ketones</div>
      <div style="position:relative;width:100%;height:210px"><canvas id="fdFastChart" role="img" aria-label="Fast length and ketones"></canvas></div></div>
    <div class="card" style="padding:8px 12px">
      <div class="fd-hrow h"><div>Day</div><div>Fast</div><div>Ketones</div><div>Glucose</div><div>GKI</div></div>
      ${rows.map(r=>`<div class="fd-hrow"><div>${dayLabel(r.d).replace('Yesterday','Yest.')}</div><div>${r.len!=null?hm(r.len):'—'}</div><div>${r.k!=null?r1(r.k):'—'}</div><div>${r.g!=null?gShow(r.g):'—'}</div><div>${r.gki??'—'}</div></div>`).join('')}
    </div>
    <div class="card" id="fdLibreCard"><div class="fd-sub">FreeStyle Libre</div><div class="fd-note" style="margin:4px 0">Checking&hellip;</div></div>
    <div class="fd-note">Glucose in ${u==='mgdl'?'mg/dL':'mmol/L'} (tap the unit on Today to switch). GKI = glucose / ketones, both in mmol/L, from readings taken within 30 minutes of each other.</div>`;
  libreCard();
  const cr=rows.slice().reverse(), cs=getComputedStyle(document.documentElement);
  const teal=cs.getPropertyValue('--teal').trim()||'#1D9E75', purple=cs.getPropertyValue('--purple').trim()||'#534AB7', txc=cs.getPropertyValue('--ink3').trim()||'#888', gc=cs.getPropertyValue('--border').trim()||'rgba(0,0,0,0.1)';
  if(fastChart)fastChart.destroy();
  fastChart=new Chart(document.getElementById('fdFastChart'),{type:'bar',data:{labels:cr.map(r=>fmtDate(r.d)),datasets:[
    {type:'bar',label:'Fast (hours)',data:cr.map(r=>r.len!=null?r1(r.len/3600000):null),backgroundColor:teal+'99',borderRadius:3,yAxisID:'y'},
    {type:'line',label:'Ketones mmol/L',data:cr.map(r=>r.k),borderColor:purple,backgroundColor:purple,pointRadius:3,spanGaps:true,yAxisID:'y1'}
  ]},options:{responsive:true,maintainAspectRatio:false,plugins:{legend:{labels:{color:txc,boxWidth:10,font:{size:11}}}},
    scales:{x:{ticks:{color:txc,font:{size:9},maxRotation:60},grid:{display:false}},y:{ticks:{color:txc,font:{size:10}},grid:{color:gc}},
      y1:{position:'right',ticks:{color:txc,font:{size:10}},grid:{display:false}}}}});
}

// ---------- FreeStyle Libre connection (LibreLinkUp follower login, stored server-side only) ----------
async function libreCard(){
  const el=document.getElementById('fdLibreCard');if(!el)return;
  let st=null;try{st=await syncApi('/api/fitness/libre/status');}catch(e){}
  const form=`<div class="fd-note" style="margin:4px 0 8px">Enter the LibreLinkUp <b>follower</b> login you created for Cortex. It is checked with Abbott, stored on the server, and never shown again.</div>
    <input class="fd-input" id="lbE" type="email" autocomplete="off" placeholder="Follower email" style="margin-bottom:8px"/>
    <input class="fd-input" id="lbP" type="password" autocomplete="new-password" placeholder="Follower password"/>
    <button class="fd-btn" onclick="foodLibreConnect()">Connect Libre</button>`;
  if(!st||!st.connected){el.innerHTML='<div class="fd-sub">FreeStyle Libre</div>'+form;return;}
  el.innerHTML=`<div class="fd-sub">FreeStyle Libre &middot; connected</div>
    <div style="font-size:13px;margin-top:4px">${st.latest?`Latest ${gShow(st.latest.mmol)} at ${tOf(st.latest.at)}`:'No readings yet (the sensor needs 60 minutes to warm up)'}</div>
    <div class="fd-note" style="margin:4px 0 0">Follower ${esc(st.email||'')} &middot; checked every 5 minutes${st.last_poll?', last '+new Date(st.last_poll*1000).toLocaleTimeString('en-GB',{hour:'2-digit',minute:'2-digit'}):''}</div>
    <details style="margin-top:8px"><summary style="font-size:12px;color:var(--ink3)">Change login</summary>${form}</details>`;
}
window.foodLibreConnect=async function(){
  const e=document.getElementById('lbE').value.trim(), p=document.getElementById('lbP').value;
  if(!e||!p){toast('Enter email and password');return;}
  toast('Checking with Abbott…');
  try{
    const t=syncToken();
    const r=await fetch(API_BASE+'/api/fitness/libre/connect',{method:'POST',headers:{'Content-Type':'application/json','Authorization':'Bearer '+t},body:JSON.stringify({email:e,password:p})});
    const j=await r.json();
    if(!r.ok){toast(j.detail||'Could not connect');return;}
    toast(j.points!=null?'Connected: '+j.points+' readings pulled':'Connected');
    libreCard();if(typeof syncNow==='function')syncNow(false);
  }catch(err){toast('Could not reach Cortex');}
};

// ---------- creatine (daily dose, default 10 g) ----------
function creatineOn(day){return supplements.filter(x=>x.date===day&&x.name==='creatine');}
function creatineStreak(){
  let n=0,d=localDay(new Date());
  if(!creatineOn(d).length)d=shiftDay(d,-1);           // today not taken yet doesn't break the streak
  while(creatineOn(d).length){n++;d=shiftDay(d,-1);}
  return n;
}
function creatineCard(){
  const taken=creatineOn(foodDay), g=taken.reduce((a,x)=>a+(+x.grams||0),0), dose=+foodTargets.creatine||10;
  const st=creatineStreak();
  if(taken.length)return `<div class="card" style="display:flex;justify-content:space-between;align-items:center;padding:12px 16px">
    <div><div class="fd-sub">Creatine</div><div style="font-size:16px;font-weight:800;color:var(--teal)">&#10003; ${r1(g)} g taken</div>
    <div style="font-size:11px;color:var(--ink3)">${st>1?st+' days in a row':''}</div></div>
    <button class="btn-sm" onclick="foodCreatineUndo()">Undo</button></div>`;
  return `<div class="card" style="padding:12px 16px;border:1px solid var(--amber)">
    <div class="fd-sub" style="margin-bottom:6px;color:var(--amber)">Creatine not taken${foodDay===localDay(new Date())?' yet today':''}</div>
    <div style="display:flex;gap:8px;align-items:center"><input class="fd-input" type="number" step="0.5" inputmode="decimal" id="fdCreIn" value="${dose}" style="max-width:110px"/>
    <span style="font-size:13px;color:var(--ink3)">g</span>
    <button class="fd-btn" style="width:auto;margin:0 0 0 auto;padding:0 18px" onclick="foodCreatineTake()">Taken</button></div>
    ${st?`<div style="font-size:11px;color:var(--ink3);margin-top:6px">${st}-day streak, keep it going</div>`:''}</div>`;
}
window.foodCreatineTake=function(){
  const g=+document.getElementById('fdCreIn').value;
  if(!(g>0&&g<=50)){toast('Enter the grams');return;}
  supplements.push({id:newId('sup'),date:foodDay,name:'creatine',grams:r1(g)});
  if(g!==+foodTargets.creatine){foodTargets.creatine=r1(g);saveTargets();}   // remember the usual dose
  saveSupplements();renderToday();toast('Creatine logged');
};
window.foodCreatineUndo=function(){
  const t=creatineOn(foodDay);if(!t.length)return;
  t.forEach(x=>markDeleted('supplements',x.id));supplements=supplements.filter(x=>!t.includes(x));saveSupplements();renderToday();
};
function weighCard(){
  const t=localDay(new Date()), w=bodyweightLog.find(b=>b.date===t);
  if(w)return `<div class="card" style="display:flex;justify-content:space-between;align-items:center;padding:12px 16px">
    <div><div class="fd-sub">Weigh-in</div><div style="font-size:18px;font-weight:800">${w.kg} kg</div></div>
    <button class="btn-sm" onclick="document.querySelector('.subnav-btn[data-sub=weight]').click()">Trend</button></div>`;
  return `<div class="card" style="padding:12px 16px"><div class="fd-sub" style="margin-bottom:6px">Morning weigh-in</div>
    <div style="display:flex;gap:8px"><input class="fd-input" type="number" step="0.1" inputmode="decimal" id="wtTodayIn" placeholder="kg"/>
    <button class="fd-btn" style="width:auto;margin:0;padding:0 18px" onclick="foodLogWeight(null,'wtTodayIn')">Save</button></div></div>`;
}
function healthCard(h,sess){
  const sync=healthData.lastSync?new Date(healthData.lastSync).toLocaleString('en-GB',{day:'numeric',month:'short',hour:'2-digit',minute:'2-digit'}):null;
  if(!h&&!sess.length){
    return `<div class="card"><div class="fd-sub" style="margin-bottom:4px">Watch</div>
      <div class="fd-note" style="margin:0">${sync?'No Health Connect data for this day yet. Last sync '+sync+'.':'Not connected yet. Install Cortex Health Bridge on your phone to pull steps, calories and workouts from Samsung Health.'}</div></div>`;
  }
  const cells=[];
  if(h){
    if(h.steps!=null)cells.push([fmtN(h.steps),'Steps']);
    if(h.activeKcal!=null)cells.push([fmtN(h.activeKcal),'Active kcal']);
    if(h.restingHR!=null)cells.push([fmtN(h.restingHR),'Resting HR']);
    if(h.sleepMin!=null)cells.push([Math.floor(h.sleepMin/60)+'h '+String(Math.round(h.sleepMin%60)).padStart(2,'0'),'Sleep']);
    if(h.hrvMs!=null)cells.push([fmtN(h.hrvMs),'HRV ms']);
    if(h.distanceM!=null)cells.push([r1(h.distanceM/1000),'km']);
  }
  return `<div class="card"><div class="fd-sub">Watch${sync?` &middot; synced ${sync}`:''}</div>
    ${cells.length?`<div class="fd-row">${cells.map(([v,l])=>`<div class="fd-cell"><div class="v">${v}</div><div class="l">${l}</div></div>`).join('')}</div>`:''}
    ${sess.map(s=>`<div class="fd-entry" style="cursor:default"><div><div class="n">${esc(sessionName(s))}</div>
      <div class="q">${new Date(s.start).toLocaleTimeString('en-GB',{hour:'2-digit',minute:'2-digit'})} &middot; ${fmtN(s.minutes)} min${s.avgHR!=null?` &middot; avg ${fmtN(s.avgHR)} / max ${fmtN(s.maxHR)} bpm`:''}</div></div>
      <div class="k">${s.kcal!=null?fmtN(s.kcal)+' kcal':''}</div></div>`).join('')}
  </div>`;
}
function mealCard(key,label,list){
  const t=totals(list);
  return `<div class="fd-meal">
    <div class="fd-meal-h"><b>${label}</b><span>${list.length?fmtN(t.kcal)+' kcal &middot; '+fmtN(t.protein)+' g P':''}</span></div>
    ${list.map(e=>`<div class="fd-entry" onclick="foodEditEntry('${e.id}')">
      <div><div class="n">${esc(e.name)}</div><div class="q">${e.at?tOf(e.at)+' &middot; ':''}${e.qtyG!=null?fmtN(e.qtyG)+' g':''}${e.protein!=null?(e.qtyG!=null?' &middot; ':'')+r1(e.protein)+' g protein':''}${e.notes==='photo estimate'?' &middot; <span style="color:var(--amber)">photo estimate</span>':''}</div></div>
      <div class="k">${fmtN(e.kcal)}</div></div>`).join('')}
    <button class="fd-add" onclick="foodOpenAdd('${key}')">+ Add to ${label.toLowerCase()}</button>
    ${list.length?`<button class="fd-add" style="border:none;color:var(--ink3);margin-top:2px" onclick="foodSaveAsMeal('${key}')">Save as meal</button>`:''}
  </div>`;
}
window.foodShiftDay=function(n){
  const next=shiftDay(foodDay,n);
  if(next>shiftDay(localDay(new Date()),7))return;     // plan up to a week ahead
  foodDay=next;renderToday();
};
window.foodEditTargets=function(){
  openModal('Daily targets',`
    <div class="form-group"><label class="form-label">Calories (kcal)</label><input type="number" id="fdTgtK" value="${foodTargets.kcal||''}"/></div>
    <div class="form-group"><label class="form-label">Protein (g)</label><input type="number" id="fdTgtP" value="${foodTargets.protein||''}"/></div>
    <div class="form-group"><label class="form-label">BMR, resting burn (kcal/day)</label><input type="number" id="fdTgtB" value="${foodTargets.bmr||1873}"/></div>
    <div class="fd-note">Burned = BMR (counted up to now on today) + active calories from your watch. 1,873 comes from your DEXA lean mass of 69.6 kg; update it after a new scan.</div>`,
  function(){
    const k=+document.getElementById('fdTgtK').value, p=+document.getElementById('fdTgtP').value;
    if(k>0)foodTargets.kcal=Math.round(k);
    if(p>0)foodTargets.protein=Math.round(p);
    const b=+document.getElementById('fdTgtB').value;if(b>0)foodTargets.bmr=Math.round(b);
    saveTargets();closeModal();renderToday();toast('Targets saved');
  });
};
window.foodCopyDay=function(){
  const src=entriesFor(shiftDay(foodDay,-1));
  if(!src.length){toast('Nothing logged the day before');return;}
  src.forEach(e=>foodLog.push(Object.assign({},e,{id:newId('fl'),date:foodDay,at:null})));
  saveFoodLog();renderToday();toast(src.length+' items copied');
};

// ---------- add / edit sheet ----------
let sheet=null, sheetMeal='breakfast', sheetTab='mine', scanStream=null;
function closeSheet(){stopScan();if(sheet){sheet.remove();sheet=null;}}
function openSheet(title,inner){
  closeSheet();
  sheet=document.createElement('div');sheet.className='fd-sheet';
  sheet.innerHTML=`<div class="fd-sheet-h"><b>${title}</b><button onclick="foodCloseSheet()">Close</button></div>${inner}`;
  document.body.appendChild(sheet);
}
window.foodCloseSheet=closeSheet;
window.foodOpenAdd=function(meal){
  sheetMeal=meal||'snacks';
  const label=(MEALS.find(m=>m[0]===sheetMeal)||[,''])[1];
  openSheet('Add to '+label.toLowerCase(),`
    <div class="fd-tabs">
      ${[['mine','My foods'],['meals','Meals'],['search','Search'],['photo','Photo'],['scan','Barcode'],['quick','Quick']].map(([k,l])=>`<button data-t="${k}" class="${k===sheetTab?'on':''}" onclick="foodSheetTab('${k}')">${l}</button>`).join('')}
    </div>
    <div class="fd-sheet-b" id="fdSheetBody"></div>`);
  window.foodSheetTab(sheetTab);
};
window.foodSheetTab=function(k){
  sheetTab=k;stopScan();
  sheet.querySelectorAll('.fd-tabs button').forEach(b=>b.classList.toggle('on',b.dataset.t===k));
  const b=document.getElementById('fdSheetBody');
  if(k==='meals'){
    b.innerHTML=`<input class="fd-input" id="fdMealQ" placeholder="Filter saved meals" oninput="foodRenderMeals()"/><div id="fdMealList" style="margin-top:10px"></div>`;
    window.foodRenderMeals();
  }else if(k==='mine'){
    b.innerHTML=`<input class="fd-input" id="fdMineQ" placeholder="Filter my foods" oninput="foodRenderMine()"/><div id="fdMineList" style="margin-top:10px"></div>`;
    window.foodRenderMine();
  }else if(k==='photo'){
    b.innerHTML=`<div class="fd-note" style="margin-top:0">Take a photo of the meal. Cortex estimates each item; check and adjust before adding. Entries are tagged "photo estimate".</div>
      <input type="file" accept="image/*" capture="environment" id="fdPhotoIn" style="display:none" onchange="foodPhotoPicked(this)"/>
      <button class="fd-btn" onclick="document.getElementById('fdPhotoIn').click()">Take or choose a photo</button>
      <input class="fd-input" id="fdPhotoNote" placeholder="Optional: e.g. ate half the rice, sauce on the side" style="margin-top:10px"/>
      <div id="fdPhotoOut" style="margin-top:12px"></div>`;
  }else if(k==='search'){
    b.innerHTML=`<div style="display:flex;gap:8px"><input class="fd-input" id="fdSearchQ" placeholder="e.g. chicken breast, greek yogurt" onkeydown="if(event.key==='Enter')foodDoSearch()"/>
      <button class="fd-btn" style="width:auto;margin:0;padding:0 16px" onclick="foodDoSearch()">Search</button></div>
      <div class="fd-note">Plain foods come from USDA, packaged products from Open Food Facts. Values are per 100 g.</div>
      <div id="fdSearchList"></div>`;
    setTimeout(()=>document.getElementById('fdSearchQ')?.focus(),50);
  }else if(k==='scan'){
    const can='BarcodeDetector' in window;
    b.innerHTML=`${can?`<video class="fd-video" id="fdVideo" playsinline muted></video><div class="fd-note" id="fdScanMsg">Point the camera at the barcode.</div>`:`<div class="fd-note">This browser cannot scan barcodes. Type the number under the barcode instead.</div>`}
      <div style="display:flex;gap:8px"><input class="fd-input" id="fdCode" inputmode="numeric" placeholder="Barcode number"/>
      <button class="fd-btn" style="width:auto;margin:0;padding:0 16px" onclick="foodLookupCode(document.getElementById('fdCode').value)">Look up</button></div>`;
    if(can)startScan();
  }else{
    b.innerHTML=`
      <div class="form-group"><label class="form-label">What was it</label><input class="fd-input" id="fdQName" placeholder="e.g. Lunch at the club"/></div>
      <div class="two-col">
        <div class="form-group"><label class="form-label">Calories</label><input class="fd-input" type="number" id="fdQK"/></div>
        <div class="form-group"><label class="form-label">Protein (g)</label><input class="fd-input" type="number" id="fdQP"/></div>
      </div>
      <div class="two-col">
        <div class="form-group"><label class="form-label">Carbs (g)</label><input class="fd-input" type="number" id="fdQC"/></div>
        <div class="form-group"><label class="form-label">Fat (g)</label><input class="fd-input" type="number" id="fdQF"/></div>
      </div>
      <button class="fd-btn" onclick="foodSaveQuick()">Add</button>`;
  }
};
function mealTotals(m){return totals(m.items||[]);}
window.foodRenderMeals=function(){
  const q=(document.getElementById('fdMealQ')?.value||'').toLowerCase();
  const list=savedMeals.filter(m=>!q||m.name.toLowerCase().includes(q))
    .sort((a,b)=>(b.favourite?1:0)-(a.favourite?1:0)||a.name.localeCompare(b.name));
  document.getElementById('fdMealList').innerHTML=list.length?list.map(m=>{const t=mealTotals(m);
    return `<div class="fd-item" onclick="foodLogMeal('${m.id}')">
      <div class="n">${m.favourite?'<span style="color:var(--amber)">&#9733;</span> ':''}${esc(m.name)}</div>
      <div class="m">${fmtN(t.kcal)} kcal &middot; P ${fmtN(t.protein)} &middot; C ${fmtN(t.carbs)} &middot; F ${fmtN(t.fat)} g &middot; ${(m.items||[]).length} item${(m.items||[]).length===1?'':'s'}</div>
      <div class="m" style="margin-top:3px">${(m.items||[]).map(i=>esc(i.name)).join(', ')}</div></div>`;}).join('')
    :`<div class="fd-note">${savedMeals.length?'No match.':'No saved meals yet. Log a meal, then tap "Save as meal" under it.'}</div>`;
};
window.foodLogMeal=function(id){
  const m=savedMeals.find(x=>x.id===id);if(!m)return;
  (m.items||[]).forEach(i=>foodLog.push({id:newId('fl'),date:foodDay,at:nowAt(),meal:sheetMeal,foodId:i.foodId||null,name:i.name,
    qtyG:i.qtyG??null,kcal:i.kcal??null,protein:i.protein??null,carbs:i.carbs??null,fat:i.fat??null}));
  saveFoodLog();closeSheet();renderToday();toast(m.name+' added');
};
window.foodSaveAsMeal=function(key){
  const items=entriesFor(foodDay).filter(e=>(e.meal||'snacks')===key);
  if(!items.length)return;
  const label=(MEALS.find(m=>m[0]===key)||[,''])[1];
  openModal('Save as meal',`<div class="form-group"><label class="form-label">Meal name</label>
    <input type="text" id="fdMealName" placeholder="e.g. Salmon and cottage cheese" value="${esc(items.length===1?items[0].name:'')}"/></div>
    <div class="fd-note">${items.length} item${items.length===1?'':'s'} from ${label.toLowerCase()}: ${items.map(i=>esc(i.name)).join(', ')}</div>`,
  function(){
    const name=document.getElementById('fdMealName').value.trim();
    if(!name){toast('Name the meal');return;}
    savedMeals.push({id:newId('meal'),name,source:'app',favourite:false,
      items:items.map(e=>({foodId:e.foodId||null,name:e.name,qtyG:e.qtyG??null,kcal:e.kcal??null,protein:e.protein??null,carbs:e.carbs??null,fat:e.fat??null}))});
    saveMeals();closeModal();toast('Meal saved');
  });
};
window.foodRenderMine=function(){
  const q=(document.getElementById('fdMineQ')?.value||'').toLowerCase();
  const used=lastUsed();
  const list=foods.filter(f=>!q||(f.name+' '+(f.brand||'')).toLowerCase().includes(q))
    .sort((a,b)=>(b.favourite?1:0)-(a.favourite?1:0)||String(used[b.id]||'').localeCompare(String(used[a.id]||''))||a.name.localeCompare(b.name));
  const el=document.getElementById('fdMineList');
  el.innerHTML=list.length?list.map(f=>`<div class="fd-item" onclick="foodPickSaved('${f.id}')">
      <div class="n">${f.favourite?'<span style="color:var(--amber)">&#9733;</span> ':''}${esc(f.name)}${f.brand?` <span class="fd-chip">${esc(f.brand)}</span>`:''}</div>
      <div class="m">${fmtN(f.kcal100)} kcal &middot; ${r1(f.protein100)??'—'} g protein per 100 g${lastQty(f.id)!=null?` &middot; last ${fmtN(lastQty(f.id))} g`:(f.servingG?` &middot; ${esc(f.servingLabel||'serving')} = ${fmtN(f.servingG)} g`:'')}</div></div>`).join('')
    :`<div class="fd-note">${foods.length?'No match.':'Nothing saved yet. Foods you add from Search or Barcode are kept here, so your usual meals are one tap.'}</div>`;
};
window.foodDoSearch=async function(){
  const q=(document.getElementById('fdSearchQ').value||'').trim();
  const el=document.getElementById('fdSearchList');
  if(q.length<2){toast('Type at least 2 letters');return;}
  el.innerHTML='<div class="fd-note">Searching&hellip;</div>';
  try{
    const r=await syncApi('/api/fitness/food/search?q='+encodeURIComponent(q));
    if(!r){el.innerHTML='<div class="fd-note">Not signed in.</div>';return;}
    window._fdResults=r.items||[];
    el.innerHTML=_fdResults.length?_fdResults.map((f,i)=>`<div class="fd-item" onclick="foodPickResult(${i})">
        <div class="n">${esc(f.name)}${f.brand?` <span class="fd-chip">${esc(f.brand)}</span>`:''}<span class="fd-chip">${f.source==='usda'?'USDA':'OFF'}</span></div>
        <div class="m">${fmtN(f.kcal100)} kcal &middot; ${r1(f.protein100)??'—'} g protein per 100 g${f.servingG?` &middot; serving ${fmtN(f.servingG)} g`:''}</div></div>`).join('')
      :'<div class="fd-note">No results. Try a simpler name, or use Quick add.</div>';
  }catch(e){el.innerHTML='<div class="fd-note">Search failed. Check your connection.</div>';}
};
window.foodPickResult=function(i){const f=_fdResults[i];if(f)portionView(Object.assign({},f,{id:null}),null);};
window.foodPickSaved=function(id){const f=foods.find(x=>x.id===id);if(f)portionView(f,null);};
window.foodLookupCode=async function(code){
  code=String(code||'').replace(/\D/g,'');
  if(!code){toast('Enter a barcode');return;}
  const local=foods.find(f=>f.barcode===code);
  if(local){portionView(local,null);return;}
  try{
    const r=await syncApi('/api/fitness/food/barcode/'+code);
    if(r&&r.item){portionView(Object.assign({},r.item,{id:null}),null);return;}
    toast('Not in Open Food Facts');
    foodEditFood(null,{barcode:code});
  }catch(e){toast('Lookup failed');}
};
async function startScan(){
  try{
    const det=new BarcodeDetector({formats:['ean_13','ean_8','upc_a','upc_e','code_128']});
    scanStream=await navigator.mediaDevices.getUserMedia({video:{facingMode:'environment'}});
    const v=document.getElementById('fdVideo');if(!v){stopScan();return;}
    v.srcObject=scanStream;await v.play();
    const tick=async()=>{
      if(!scanStream||!document.getElementById('fdVideo'))return;
      try{const codes=await det.detect(v);if(codes.length){const c=codes[0].rawValue;stopScan();window.foodLookupCode(c);return;}}catch(e){}
      requestAnimationFrame(tick);
    };
    tick();
  }catch(e){const m=document.getElementById('fdScanMsg');if(m)m.textContent='Camera not available. Type the number instead.';}
}
function stopScan(){if(scanStream){scanStream.getTracks().forEach(t=>t.stop());scanStream=null;}}

// Portion screen: pick grams (or servings), see the numbers, add. `entry` = existing log row to edit.
function portionView(food,entry){
  stopScan();
  const sg=+food.servingG||0;
  // New entry: start from the amount he logged LAST time for this food (he usually repeats it).
  const lastQ=!entry&&food.id?(foodLog.filter(e=>e.foodId===food.id&&e.qtyG!=null).sort((a,b)=>((b.at||b.date)>(a.at||a.date)?1:-1))[0]||{}).qtyG:null;
  const grams=entry&&entry.qtyG!=null?entry.qtyG:(lastQ!=null?lastQ:(sg||100));
  const saved=!!(food.id&&foods.find(f=>f.id===food.id));
  const meal=entry?entry.meal:sheetMeal;
  const inner=`<div class="fd-sheet-b">
    <div class="fd-item" style="cursor:default"><div class="n">${esc(food.name)}${food.brand?` <span class="fd-chip">${esc(food.brand)}</span>`:''}</div>
      <div class="m">Per 100 g: ${fmtN(food.kcal100)} kcal &middot; P ${r1(food.protein100)??'—'} &middot; C ${r1(food.carbs100)??'—'} &middot; F ${r1(food.fat100)??'—'}</div></div>
    <div class="form-group"><label class="form-label">Amount (g)</label><input class="fd-input" type="number" id="fdG" value="${r1(grams)}" oninput="foodPortionCalc()"/></div>
    ${sg?`<div style="display:flex;gap:6px;flex-wrap:wrap;margin-bottom:10px">${[0.5,1,1.5,2,3].map(n=>`<button class="btn-sm" onclick="document.getElementById('fdG').value=${r1(sg*n)};foodPortionCalc()">${n} &times; ${esc(food.servingLabel||'serving')}</button>`).join('')}</div>`:''}
    <div class="form-group"><label class="form-label">Eaten at</label>${timePick('fdAtT',entry&&entry.at?tOf(entry.at):(entry?'':(foodDay===localDay(new Date())?nowHM():'')),true)}</div>
    <div class="form-group"><label class="form-label">Meal</label><select id="fdMeal" class="fd-input">${MEALS.map(([k,l])=>`<option value="${k}" ${k===meal?'selected':''}>${l}</option>`).join('')}</select></div>
    <div class="card" id="fdCalc"></div>
    ${saved?'':`<label style="display:flex;gap:8px;align-items:center;font-size:13px;margin:6px 0"><input type="checkbox" id="fdKeep" checked/> Save to My foods</label>`}
    <button class="fd-btn" id="fdAddBtn">${entry?'Save changes':'Add'}</button>
    ${entry?`<button class="fd-btn ghost" onclick="foodDeleteEntry('${entry.id}')">Remove from log</button>`:''}
  </div>`;
  openSheet(entry?'Edit entry':'Amount',inner);
  window._fdFood=food;
  window.foodPortionCalc();
  document.getElementById('fdAddBtn').onclick=function(){
    const g=+document.getElementById('fdG').value;
    if(!(g>0)){toast('Enter an amount');return;}
    let f=food;
    if(!saved&&document.getElementById('fdKeep')?.checked){
      f=Object.assign({},food,{id:newId('food'),favourite:false});
      foods.push(f);saveFoods();
    }
    const p=portion(f,g);
    const tv=document.getElementById('fdAtT').value, dd=entry?entry.date:foodDay;
    const row={date:dd,at:tv?atTime(dd,tv):null,meal:document.getElementById('fdMeal').value,foodId:f.id||null,
      name:f.name+(f.brand?' ('+f.brand+')':''),qtyG:r1(g),kcal:p.kcal,protein:p.protein,carbs:p.carbs,fat:p.fat};
    if(entry)Object.assign(entry,row);else foodLog.push(Object.assign({id:newId('fl')},row));
    saveFoodLog();closeSheet();renderToday();toast(entry?'Updated':'Added');
  };
}
window.foodPortionCalc=function(){
  const f=window._fdFood,g=+document.getElementById('fdG').value||0,p=portion(f,g);
  document.getElementById('fdCalc').innerHTML=`<div class="fd-row" style="margin-top:0">
    <div class="fd-cell"><div class="v">${fmtN(p.kcal)}</div><div class="l">kcal</div></div>
    <div class="fd-cell"><div class="v">${r1(p.protein)}</div><div class="l">Protein g</div></div>
    <div class="fd-cell"><div class="v">${r1(p.carbs)}</div><div class="l">Carbs g</div></div></div>`;
};
window.foodSaveQuick=function(){
  const name=(document.getElementById('fdQName').value||'').trim()||'Quick add';
  const k=+document.getElementById('fdQK').value;
  if(!(k>0)){toast('Enter calories');return;}
  const num=id=>{const v=document.getElementById(id).value;return v===''?null:r1(+v);};
  foodLog.push({id:newId('fl'),date:foodDay,at:nowAt(),meal:sheetMeal,foodId:null,name,qtyG:null,kcal:r0(k),
    protein:num('fdQP'),carbs:num('fdQC'),fat:num('fdQF')});
  saveFoodLog();closeSheet();renderToday();toast('Added');
};
window.foodEditEntry=function(id){
  const e=foodLog.find(x=>x.id===id);if(!e)return;
  const f=e.foodId&&foods.find(x=>x.id===e.foodId);
  if(f&&e.qtyG!=null){portionView(f,e);return;}
  // Quick-add rows (no food behind them): edit the numbers directly
  openModal('Edit entry',`
    <div class="form-group"><label class="form-label">Name</label><input type="text" id="fdEN" value="${esc(e.name)}"/></div>
    <div class="two-col"><div class="form-group"><label class="form-label">Calories</label><input type="number" id="fdEK" value="${e.kcal??''}"/></div>
    <div class="form-group"><label class="form-label">Protein (g)</label><input type="number" id="fdEP" value="${e.protein??''}"/></div></div>
    <div class="form-group"><label class="form-label">Eaten at</label>${timePick('fdET',e.at?tOf(e.at):'',true)}</div>
    <button class="fd-btn ghost" onclick="closeModal();foodDeleteEntry('${e.id}')">Remove from log</button>`,
  function(){
    e.name=document.getElementById('fdEN').value.trim()||e.name;
    const k=document.getElementById('fdEK').value,p=document.getElementById('fdEP').value;
    e.kcal=k===''?null:r0(+k);e.protein=p===''?null:r1(+p);
    const tv=document.getElementById('fdET').value;e.at=tv?atTime(e.date,tv):null;
    saveFoodLog();closeModal();renderToday();
  });
};
window.foodDeleteEntry=function(id){
  const i=foodLog.findIndex(x=>x.id===id);if(i<0)return;
  markDeleted('foodLog',foodLog[i].id);foodLog.splice(i,1);saveFoodLog();closeSheet();renderToday();toast('Removed');
};

// ---------- History ----------
function renderHistory(){
  const el=document.getElementById('page-food-history');
  const today=localDay(new Date());
  const days=[];for(let i=0;i<30;i++)days.push(shiftDay(today,-i));
  const rows=days.map(d=>{const t=totals(entriesFor(d)),h=healthDay(d);
    return {d,logged:entriesFor(d).length>0,kcal:t.kcal,protein:t.protein,burned:burnedFor(d),steps:h?h.steps:null};});
  const avg=(arr,k)=>{const v=arr.filter(r=>r[k]!=null&&(k==='burned'||k==='steps'||r.logged)).map(r=>r[k]);return v.length?v.reduce((a,b)=>a+b,0)/v.length:null;};
  const wk1=rows.slice(1,8), wk2=rows.slice(8,15);   // the last 7 COMPLETE days, and the 7 before
  const weights=(healthData.weights||[]).slice().sort((a,b)=>a.at<b.at?-1:1);
  const lw=weights.length?weights[weights.length-1]:null;
  el.innerHTML=`
    <div class="section-label">Last 7 complete days</div>
    <div class="card"><div class="fd-row" style="margin-top:0">
      <div class="fd-cell"><div class="v">${fmtN(avg(wk1,'kcal'))}</div><div class="l">Avg eaten</div></div>
      <div class="fd-cell"><div class="v">${fmtN(avg(wk1,'burned'))}</div><div class="l">Avg burned</div></div>
      <div class="fd-cell"><div class="v">${fmtN(avg(wk1,'protein'))}</div><div class="l">Avg protein</div></div>
    </div>
    <div style="font-size:11px;color:var(--ink3);margin-top:8px">Week before: eaten ${fmtN(avg(wk2,'kcal'))} &middot; burned ${fmtN(avg(wk2,'burned'))} &middot; steps ${fmtN(avg(wk1,'steps'))} avg this week${lw?` &middot; last weigh-in ${r1(lw.kg)} kg (${fmtDate(lw.date)})`:''}</div></div>
    <div class="chart-section"><div class="chart-title" style="margin-bottom:8px">Eaten vs burned, 30 days</div>
      <div style="position:relative;width:100%;height:210px"><canvas id="fdHistChart" role="img" aria-label="Calories eaten versus burned over 30 days"></canvas></div></div>
    <div class="card" style="padding:8px 12px">
      <div class="fd-hrow h"><div>Day</div><div>Eaten</div><div>Protein</div><div>Burned</div><div>Steps</div></div>
      ${rows.map(r=>`<div class="fd-hrow"><div>${dayLabel(r.d).replace('Yesterday','Yest.')}</div><div>${r.logged?fmtN(r.kcal):'—'}</div><div>${r.logged?fmtN(r.protein):'—'}</div><div>${fmtN(r.burned)}</div><div>${fmtN(r.steps)}</div></div>`).join('')}
    </div>`;
  const chartRows=rows.slice().reverse();
  const cs=getComputedStyle(document.documentElement);
  const teal=cs.getPropertyValue('--teal').trim()||'#1D9E75', amber=cs.getPropertyValue('--amber').trim()||'#BA7517';
  const txc=cs.getPropertyValue('--ink3').trim()||'#888', gc=cs.getPropertyValue('--border').trim()||'rgba(0,0,0,0.1)';
  if(histChart)histChart.destroy();
  histChart=new Chart(document.getElementById('fdHistChart'),{type:'bar',data:{labels:chartRows.map(r=>fmtDate(r.d)),datasets:[
    {type:'bar',label:'Eaten',data:chartRows.map(r=>r.logged?r.kcal:null),backgroundColor:teal+'99',borderRadius:3},
    {type:'line',label:'Burned',data:chartRows.map(r=>r.burned),borderColor:amber,backgroundColor:amber,borderWidth:2,pointRadius:2,tension:0.3,spanGaps:true},
    {type:'line',label:'Target',data:chartRows.map(()=>foodTargets.kcal||null),borderColor:txc,borderDash:[4,4],borderWidth:1,pointRadius:0}
  ]},options:{responsive:true,maintainAspectRatio:false,plugins:{legend:{labels:{color:txc,boxWidth:10,font:{size:11}}}},
    scales:{x:{ticks:{color:txc,font:{size:9},maxRotation:60},grid:{display:false}},y:{ticks:{color:txc,font:{size:10}},grid:{color:gc}}}}});
}

// ---------- 5-week weight projection (7,700 kcal per kg of body fat) ----------
const KCAL_PER_KG=7700, PROJ_DAYS=35;
function baseWeight(){
  const all=(typeof weighIns==='function')?weighIns():[], t=localDay(new Date());
  const v=all.filter(w=>w.date>shiftDay(t,-7)).map(w=>w.kg);
  if(v.length)return v.reduce((a,b)=>a+b,0)/v.length;
  return all.length?all[all.length-1].kg:null;
}
// A WHOLE day's burn: full BMR (not pro rata) + movement and workouts recorded so far.
function fullDayBurn(day){
  const p=burnedParts(day), bmr=+foodTargets.bmr||1873;
  return p?bmr+p.move+p.work:null;
}
function projLine(label,kcalDiffPerDay){
  const b=baseWeight();if(b==null)return '';
  const end=b+kcalDiffPerDay*PROJ_DAYS/KCAL_PER_KG, d=end-b;
  return `<div style="margin-top:10px;padding-top:10px;border-top:0.5px solid var(--border);font-size:13px">${label}
    <b>${r1(end)} kg</b> <span class="${d<=0?'fd-pos':'fd-neg'}">(${d>0?'+':''}${r1(d)} kg)</span></div>`;
}
function projectToday(){
  const t=totals(entriesFor(foodDay));
  if(!t.kcal||foodDay>localDay(new Date()))return '';
  const burn=fullDayBurn(foodDay)??(+foodTargets.bmr||1873);
  return projLine(`If every day were like ${foodDay===localDay(new Date())?'today':'this day'}, in 5 weeks you'd weigh`,t.kcal-burn);
}
function projectWeek(){
  const t=localDay(new Date()), diffs=[];
  for(let i=1;i<=7;i++){const d=shiftDay(t,-i), e=entriesFor(d), b=fullDayBurn(d);if(e.length&&b!=null)diffs.push(totals(e).kcal-b);}
  if(diffs.length<3)return `<div class="fd-note" style="margin:8px 0 0">The 5-week projection appears once 3 of the last 7 days have food and watch data.</div>`;
  return projLine(`At your last ${diffs.length} days' average, in 5 weeks:`,diffs.reduce((a,b)=>a+b,0)/diffs.length);
}

// ---------- Weight (morning weigh-ins) ----------
// Uses the app's existing bodyweight log, the same one that scores pull-ups and dips by the weight
// on each session date, so every weigh-in also keeps those PRs honest. One entry per day.
let wtChart=null, wtRange=90;
function weighIns(){
  const m={};
  (healthData.weights||[]).forEach(w=>{if(w.kg)m[w.date]={date:w.date,kg:r1(w.kg),src:'watch'};});   // Withings etc via Health Connect
  bodyweightLog.forEach(b=>{m[b.date]={date:b.date,kg:+b.kg,id:b.id,src:'log'};});                   // your own entries win
  return Object.values(m).sort((a,b)=>a.date<b.date?-1:1);
}
function avgBetween(list,from,to){const v=list.filter(w=>w.date>=from&&w.date<=to).map(w=>w.kg);return v.length?v.reduce((a,b)=>a+b,0)/v.length:null;}
window.foodLogWeight=function(day,inputId){
  const kg=+document.getElementById(inputId).value;
  if(!(kg>30&&kg<250)){toast('Enter your weight in kg');return;}
  const d=day||localDay(new Date());
  const i=bodyweightLog.findIndex(b=>b.date===d);
  const row={id:'bw_'+d,date:d,kg:Math.round(kg*10)/10,notes:null};
  if(i>=0)bodyweightLog[i]=row;else bodyweightLog.push(row);
  saveBodyweight();
  if(typeof renderBodyweightCard==='function')renderBodyweightCard();
  toast('Weight saved');
  if(currentSubpage.food==='weight')renderWeight();else renderToday();
};
window.foodDelWeight=function(d){
  const i=bodyweightLog.findIndex(b=>b.date===d);if(i<0)return;
  if(!confirm('Delete the weigh-in for '+fmtDate(d)+'?'))return;
  bodyweightLog.splice(i,1);saveBodyweight();renderWeight();
};
window.foodWtRange=function(n){wtRange=n;renderWeight();};
function renderWeight(){
  const el=document.getElementById('page-food-weight');
  const today=localDay(new Date());
  const all=weighIns();
  const last=all.length?all[all.length-1]:null;
  const todays=all.find(w=>w.date===today);
  const a7=avgBetween(all,shiftDay(today,-6),today), p7=avgBetween(all,shiftDay(today,-13),shiftDay(today,-7));
  const a30=avgBetween(all,shiftDay(today,-36),shiftDay(today,-30));
  const diff=(x,y)=>x!=null&&y!=null?r1(x-y):null;
  const chip=v=>v==null?'—':`<span class="${v<=0?'fd-pos':'fd-neg'}">${v>0?'+':''}${v} kg</span>`;
  el.innerHTML=`
    <div class="card">
      <div class="fd-sub">${todays?'Today':'Morning weigh-in'}</div>
      ${todays?`<div class="fd-big">${todays.kg} <span style="font-size:14px;font-weight:700;color:var(--ink3)">kg</span></div>`:''}
      <div style="display:flex;gap:8px;margin-top:10px">
        <input class="fd-input" type="number" step="0.1" inputmode="decimal" id="fdWtIn" placeholder="${last?last.kg:'kg'}" value="${todays?todays.kg:''}"/>
        <button class="fd-btn" style="width:auto;margin:0;padding:0 18px" onclick="foodLogWeight(document.getElementById('fdWtDay').value,'fdWtIn')">${todays?'Update':'Save'}</button>
      </div>
      <input type="date" id="fdWtDay" value="${today}" max="${today}" class="fd-input" style="margin-top:8px;font-size:14px"/>
    </div>
    <div class="card"><div class="fd-row" style="margin-top:0">
      <div class="fd-cell"><div class="v">${a7!=null?r1(a7):'—'}</div><div class="l">7-day avg</div></div>
      <div class="fd-cell"><div class="v">${chip(diff(a7,p7))}</div><div class="l">vs last week</div></div>
      <div class="fd-cell"><div class="v">${chip(diff(a7,a30))}</div><div class="l">vs 30 days ago</div></div>
    </div><div class="fd-note" style="margin:8px 0 0">Daily weight swings with water and food, so judge progress by the 7-day average.</div>${projectWeek()}</div>
    <div class="chart-section">
      <div class="chart-toggle" style="margin-bottom:8px">${[[30,'30d'],[90,'90d'],[365,'1y'],[99999,'All']].map(([n,l])=>`<button class="${wtRange===n?'active':''}" onclick="foodWtRange(${n})">${l}</button>`).join('')}</div>
      <div style="position:relative;width:100%;height:210px"><canvas id="fdWtChart" role="img" aria-label="Weight trend"></canvas></div>
    </div>
    <div class="card" style="padding:8px 12px">
      ${all.slice().reverse().slice(0,30).map(w=>`<div class="fd-entry" style="cursor:default"><div><div class="n">${w.kg} kg</div><div class="q">${fmtDate(w.date)} ${new Date(w.date+'T12:00:00').getFullYear()!==new Date().getFullYear()?new Date(w.date+'T12:00:00').getFullYear():''}${w.src==='watch'?' &middot; from Health Connect':''}</div></div>
        ${w.src==='log'?`<button class="hist-del" onclick="foodDelWeight('${w.date}')">&times;</button>`:''}</div>`).join('')||'<div class="fd-note">No weigh-ins yet.</div>'}
    </div>`;
  const from=shiftDay(today,-wtRange), pts=all.filter(w=>w.date>=from);
  const roll=pts.map(w=>{const v=all.filter(x=>x.date<=w.date&&x.date>=shiftDay(w.date,-6)).map(x=>x.kg);return r1(v.reduce((a,b)=>a+b,0)/v.length);});
  const cs=getComputedStyle(document.documentElement);
  const teal=cs.getPropertyValue('--teal').trim()||'#1D9E75', txc=cs.getPropertyValue('--ink3').trim()||'#888', gc=cs.getPropertyValue('--border').trim()||'rgba(0,0,0,0.1)';
  if(wtChart)wtChart.destroy();
  wtChart=new Chart(document.getElementById('fdWtChart'),{type:'line',data:{labels:pts.map(w=>fmtDate(w.date)),datasets:[
    {label:'Weigh-in',data:pts.map(w=>w.kg),borderColor:txc,backgroundColor:txc,pointRadius:pts.length>120?0:2.5,borderWidth:1,showLine:false},
    {label:'7-day average',data:roll,borderColor:teal,backgroundColor:teal,pointRadius:0,borderWidth:2.5,tension:0.3}
  ]},options:{responsive:true,maintainAspectRatio:false,plugins:{legend:{labels:{color:txc,boxWidth:10,font:{size:11}}}},
    scales:{x:{ticks:{color:txc,font:{size:9},maxRotation:60,autoSkip:true,maxTicksLimit:12},grid:{display:false}},y:{ticks:{color:txc,font:{size:10}},grid:{color:gc}}}}});
}

// ---------- meal photo estimate ----------
function shrinkImage(file,max){return new Promise((res,rej)=>{const img=new Image();img.onload=()=>{
  const k=Math.min(1,max/Math.max(img.width,img.height)),c=document.createElement('canvas');
  c.width=Math.round(img.width*k);c.height=Math.round(img.height*k);c.getContext('2d').drawImage(img,0,0,c.width,c.height);
  res(c.toDataURL('image/jpeg',0.85));};img.onerror=rej;img.src=URL.createObjectURL(file);});}
window.foodPhotoPicked=async function(inp){
  const f=inp.files&&inp.files[0];if(!f)return;
  const out=document.getElementById('fdPhotoOut');
  out.innerHTML='<div class="fd-note">Analysing the photo&hellip; (about 10-20 seconds)</div>';
  try{
    const img=await shrinkImage(f,1600);
    const r=await syncApi('/api/fitness/food/photo',{method:'POST',body:JSON.stringify({image:img,note:document.getElementById('fdPhotoNote').value||''})});
    if(!r||!r.items||!r.items.length){out.innerHTML='<div class="fd-note">Could not read any food in that photo. Try another angle, or use Quick add.</div>';return;}
    window._fdPhoto=r.items;
    out.innerHTML=`<img src="${img}" style="width:100%;border-radius:var(--radius);margin-bottom:10px"/>
      ${r.summary?`<div class="fd-note" style="margin-top:0">${esc(r.summary)}</div>`:''}
      ${r.items.map((it,i)=>`<div class="fd-item" style="cursor:default">
        <div style="display:flex;justify-content:space-between;gap:8px"><div class="n">${esc(it.name)}</div>
        <label style="font-size:11px;color:var(--ink3)"><input type="checkbox" id="fpK${i}" checked/> add</label></div>
        <div class="m">${it.confidence} confidence${it.note?' &middot; '+esc(it.note):''}</div>
        <div style="display:grid;grid-template-columns:repeat(5,1fr);gap:4px;margin-top:6px">
          ${[['grams','g'],['kcal','kcal'],['protein','P'],['carbs','C'],['fat','F']].map(([k,l])=>`<div><div style="font-size:9px;color:var(--ink3);font-weight:700">${l}</div>
          <input type="number" step="any" id="fp_${k}_${i}" value="${it[k]??''}" style="width:100%;font-size:13px;padding:4px"/></div>`).join('')}
        </div></div>`).join('')}
      <button class="fd-btn" onclick="foodPhotoAdd()">Add to ${(MEALS.find(m=>m[0]===sheetMeal)||[,''])[1].toLowerCase()}</button>`;
  }catch(e){out.innerHTML='<div class="fd-note">Photo analysis failed. Check your connection and try again.</div>';}
};
window.foodPhotoAdd=function(){
  const items=window._fdPhoto||[];let n=0;
  const v=(k,i)=>{const x=document.getElementById(`fp_${k}_${i}`).value;return x===''?null:+x;};
  items.forEach((it,i)=>{
    if(!document.getElementById('fpK'+i).checked)return;
    foodLog.push({id:newId('fl'),date:foodDay,at:nowAt(),meal:sheetMeal,foodId:null,name:it.name,qtyG:v('grams',i),
      kcal:r0(v('kcal',i)),protein:r1(v('protein',i)),carbs:r1(v('carbs',i)),fat:r1(v('fat',i)),notes:'photo estimate'});
    n++;
  });
  if(!n){toast('Nothing ticked');return;}
  saveFoodLog();closeSheet();renderToday();toast(n+' item'+(n===1?'':'s')+' added (photo estimate)');
};

// ---------- watch-created cardio entries: check / edit settings ----------
// The server creates cardio entries from Samsung watch workouts. New ones are pre-filled from the
// last session on that preset and wait here for the owner to confirm; history ones carry no settings.
window.editWatchCardio=function(id){
  const r=cardioSessions.find(s=>s.id===id);if(!r)return;
  const opts=cardioExercises.filter(e=>!e.deleted).map(e=>`<option value="${e.id}" ${e.id===r.exerciseId?'selected':''}>${esc(e.name)}</option>`).join('');
  const fieldsFor=exId=>{
    const ex=getCardioExDef(exId), f=(ex&&ex.manualFields)||[];
    return f.map((x,i)=>/distance/i.test(x.label)?'':`<div class="form-group"><label class="form-label">${esc(x.label)}</label>
      <input type="number" step="any" id="wcF${i}" value="${esc((r.extra||{})[i]??'')}"/></div>`).join('')||'<div class="fd-note">This preset has no settings to fill.</div>';
  };
  openModal('Check session',`
    <div class="fd-note" style="margin-top:0">${fmtDate(r.date)} &middot; ${r.duration||''} &middot; avg ${r.avgHR??'—'} / max ${r.maxHR??'—'} bpm${r.calories?' &middot; '+r.calories+' kcal':''} (from your watch)</div>
    <div class="form-group"><label class="form-label">Session</label><select id="wcPreset" onchange="document.getElementById('wcFields').innerHTML=window._wcFields(this.value)">${opts}</select></div>
    <div id="wcFields">${fieldsFor(r.exerciseId)}</div>`,
  function(){
    const exId=document.getElementById('wcPreset').value, ex=getCardioExDef(exId);
    const extra={};
    ((ex&&ex.manualFields)||[]).forEach((x,i)=>{const el=document.getElementById('wcF'+i);if(el&&el.value!=='')extra[i]=el.value;});
    r.exerciseId=exId;r.exerciseName=ex?ex.name:r.exerciseName;r.extra=extra;
    r.watch=Object.assign({},r.watch||{},{confirmed:true,settings:Object.keys(extra).length?'manual':(r.watch&&r.watch.settings)||'not recorded'});
    if(/^From watch/.test(r.notes||''))r.notes='From watch';
    saveCardio();closeModal();
    if(typeof renderCardioHistory==='function')renderCardioHistory();
    toast('Session saved');
  });
  window._wcFields=fieldsFor;
};

// ---------- My foods ----------
function renderFoodsPage(){
  const el=document.getElementById('page-food-foods');
  const used=lastUsed();
  const list=foods.slice().sort((a,b)=>(b.favourite?1:0)-(a.favourite?1:0)||a.name.localeCompare(b.name));
  el.innerHTML=`<div class="section-label">My foods (${foods.length})</div>
    <div class="fd-note" style="margin-top:0">Starred foods sit at the top of the add list. Values are per 100 g.</div>
    ${list.map(f=>`<div class="fd-item" style="display:flex;justify-content:space-between;align-items:center;gap:10px">
      <div onclick="foodEditFood('${f.id}')" style="flex:1"><div class="n">${esc(f.name)}${f.brand?` <span class="fd-chip">${esc(f.brand)}</span>`:''}</div>
      <div class="m">${fmtN(f.kcal100)} kcal &middot; ${r1(f.protein100)??'—'} g P${used[f.id]?' &middot; last eaten '+fmtDate(used[f.id]):''}</div></div>
      <button class="btn-sm" onclick="foodToggleFav('${f.id}')" aria-label="Favourite" style="font-size:16px;color:${f.favourite?'var(--amber)':'var(--ink3)'}">${f.favourite?'&#9733;':'&#9734;'}</button></div>`).join('')}
    <button class="btn-add" onclick="foodEditFood(null)">+ New food</button>
    <div class="section-label" style="margin-top:18px">Saved meals (${savedMeals.length})</div>
    ${savedMeals.slice().sort((a,b)=>a.name.localeCompare(b.name)).map(m=>{const t=mealTotals(m);
      return `<div class="fd-item" style="display:flex;justify-content:space-between;align-items:center;gap:10px">
      <div style="flex:1"><div class="n">${esc(m.name)}</div><div class="m">${fmtN(t.kcal)} kcal &middot; P ${fmtN(t.protein)} g &middot; ${(m.items||[]).map(i=>esc(i.name)).join(', ')}</div></div>
      <button class="btn-sm" onclick="foodToggleMealFav('${m.id}')" style="font-size:16px;color:${m.favourite?'var(--amber)':'var(--ink3)'}">${m.favourite?'&#9733;':'&#9734;'}</button>
      <button class="btn-sm" onclick="foodDeleteMeal('${m.id}')">Delete</button></div>`;}).join('')}`;
}
window.foodToggleMealFav=function(id){const m=savedMeals.find(x=>x.id===id);if(!m)return;m.favourite=!m.favourite;saveMeals();renderFoodsPage();};
window.foodDeleteMeal=function(id){
  const m=savedMeals.find(x=>x.id===id);if(!m)return;
  if(!confirm('Delete the saved meal "'+m.name+'"? Past log entries are kept.'))return;
  markDeleted('meals',m.id);savedMeals.splice(savedMeals.indexOf(m),1);saveMeals();renderFoodsPage();toast('Meal deleted');
};
window.foodToggleFav=function(id){const f=foods.find(x=>x.id===id);if(!f)return;f.favourite=!f.favourite;saveFoods();renderFoodsPage();};
window.foodEditFood=function(id,preset){
  const f=id?foods.find(x=>x.id===id):Object.assign({name:'',brand:'',barcode:'',servingLabel:'',servingG:null,kcal100:null,protein100:null,carbs100:null,fat100:null},preset||{});
  if(!f)return;
  closeSheet();
  const v=x=>x==null?'':x;
  openModal(id?'Edit food':'New food',`
    <div class="form-group"><label class="form-label">Name</label><input type="text" id="ffN" value="${esc(v(f.name))}"/></div>
    <div class="two-col"><div class="form-group"><label class="form-label">Brand</label><input type="text" id="ffB" value="${esc(v(f.brand))}"/></div>
    <div class="form-group"><label class="form-label">Barcode</label><input type="text" id="ffC" value="${esc(v(f.barcode))}"/></div></div>
    <div class="fd-note" style="margin-top:0">Per 100 g, from the label</div>
    <div class="two-col"><div class="form-group"><label class="form-label">Calories</label><input type="number" id="ffK" value="${v(f.kcal100)}"/></div>
    <div class="form-group"><label class="form-label">Protein g</label><input type="number" id="ffP" value="${v(f.protein100)}"/></div></div>
    <div class="two-col"><div class="form-group"><label class="form-label">Carbs g</label><input type="number" id="ffCa" value="${v(f.carbs100)}"/></div>
    <div class="form-group"><label class="form-label">Fat g</label><input type="number" id="ffF" value="${v(f.fat100)}"/></div></div>
    <div class="two-col"><div class="form-group"><label class="form-label">Serving name</label><input type="text" id="ffSL" value="${esc(v(f.servingLabel))}" placeholder="e.g. 1 scoop"/></div>
    <div class="form-group"><label class="form-label">Serving grams</label><input type="number" id="ffSG" value="${v(f.servingG)}"/></div></div>
    ${id?`<button class="fd-btn ghost" onclick="foodDeleteFood('${id}')">Delete food</button>`:''}`,
  function(){
    const g=x=>{const s=document.getElementById(x).value;return s===''?null:+s;};
    const name=document.getElementById('ffN').value.trim();
    if(!name){toast('Name it');return;}
    if(g('ffK')==null){toast('Calories per 100 g needed');return;}
    const row={name,brand:document.getElementById('ffB').value.trim()||null,barcode:document.getElementById('ffC').value.trim()||null,
      kcal100:g('ffK'),protein100:g('ffP'),carbs100:g('ffCa'),fat100:g('ffF'),
      servingLabel:document.getElementById('ffSL').value.trim()||null,servingG:g('ffSG'),source:f.source||'manual'};
    if(id)Object.assign(f,row);else foods.push(Object.assign({id:newId('food'),favourite:false},row));
    saveFoods();closeModal();toast('Saved');
    if(currentSubpage.food==='foods')renderFoodsPage();
  });
};
window.foodDeleteFood=function(id){
  const i=foods.findIndex(x=>x.id===id);if(i<0)return;
  markDeleted('foods',foods[i].id);foods.splice(i,1);saveFoods();closeModal();renderFoodsPage();toast('Deleted (past log entries kept)');
};

// Food is the home screen: init() ran before this file loaded, so draw it now.
try{if(typeof currentSection!=='undefined'&&currentSection==='food')window.renderFood(currentSubpage.food);}catch(e){}
})();
