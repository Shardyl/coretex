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
function newId(p){return p+'_'+Date.now().toString(36)+Math.random().toString(36).slice(2,6);}
function r0(v){return v==null||isNaN(v)?null:Math.round(v);}
function r1(v){return v==null||isNaN(v)?null:Math.round(v*10)/10;}
function fmtN(v){return v==null?'—':Math.round(v).toLocaleString();}
function esc(s){return String(s==null?'':s).replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));}
function dayLabel(day){
  const t=localDay(new Date());
  if(day===t)return'Today';
  if(day===shiftDay(t,-1))return'Yesterday';
  return new Date(day+'T12:00:00').toLocaleDateString('en-GB',{weekday:'short',day:'numeric',month:'short'});
}
function entriesFor(day){return foodLog.filter(e=>e.date===day);}
function totals(list){
  return list.reduce((a,e)=>({kcal:a.kcal+(+e.kcal||0),protein:a.protein+(+e.protein||0),
    carbs:a.carbs+(+e.carbs||0),fat:a.fat+(+e.fat||0)}),{kcal:0,protein:0,carbs:0,fat:0});
}
function healthDay(day){return (healthData.days||[]).find(d=>d.date===day)||null;}
function sessionsOn(day){return (healthData.sessions||[]).filter(s=>s.date===day);}
function portion(f,g){
  const k=g/100;
  return {kcal:r0(f.kcal100*k),protein:r1((f.protein100||0)*k),carbs:r1((f.carbs100||0)*k),fat:r1((f.fat100||0)*k)};
}
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
};
window.renderFoodCurrent=function(){
  if(typeof currentSection!=='undefined'&&currentSection==='food')window.renderFood(currentSubpage.food);
};

// ---------- Today ----------
function renderToday(){
  const el=document.getElementById('page-food-today');
  const list=entriesFor(foodDay);
  const t=totals(list);
  const tgt=foodTargets.kcal||0, ptgt=foodTargets.protein||0;
  const remain=tgt-t.kcal;
  const h=healthDay(foodDay);
  const isToday=foodDay===localDay(new Date());
  const burned=h&&h.totalKcal!=null?h.totalKcal:null;
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
      <button onclick="foodShiftDay(1)" aria-label="Next day" ${isToday?'disabled style="opacity:0.3"':''}>&rsaquo;</button>
    </div>
    <div class="card">
      <div class="fd-sub">${remain>=0?'Remaining':'Over target'}</div>
      <div class="fd-big" style="color:${remain>=0?'var(--teal)':'var(--amber)'}">${fmtN(Math.abs(remain))} <span style="font-size:14px;font-weight:700;color:var(--ink3)">kcal</span></div>
      <div class="fd-bar${t.kcal>tgt?' over':''}"><div style="width:${kPct}%"></div></div>
      <div class="fd-row">
        <div class="fd-cell" onclick="foodEditTargets()" style="cursor:pointer"><div class="v">${fmtN(tgt)}</div><div class="l">Target</div></div>
        <div class="fd-cell"><div class="v">${fmtN(t.kcal)}</div><div class="l">Eaten</div></div>
        <div class="fd-cell"><div class="v">${burned!=null?fmtN(burned):'—'}</div><div class="l">Burned${isToday&&burned!=null?' so far':''}</div></div>
      </div>
      <div style="margin-top:12px;display:flex;justify-content:space-between;font-size:12px;font-weight:700">
        <span>Protein</span><span>${fmtN(t.protein)} / ${fmtN(ptgt)} g</span>
      </div>
      <div class="fd-bar"><div style="width:${pPct}%;background:var(--blue)"></div></div>
      <div style="margin-top:6px;font-size:11px;color:var(--ink3)">Carbs ${fmtN(t.carbs)} g &middot; Fat ${fmtN(t.fat)} g</div>
      ${balance}
    </div>
    ${healthCard(h,sess)}
    ${MEALS.map(([k,label])=>mealCard(k,label,list.filter(e=>(e.meal||'snacks')===k))).join('')}
    <button class="fd-btn sec" onclick="foodCopyDay()">Copy ${dayLabel(shiftDay(foodDay,-1)).toLowerCase()}'s food to ${dayLabel(foodDay).toLowerCase()}</button>
  `;
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
      <div><div class="n">${esc(e.name)}</div><div class="q">${e.qtyG!=null?fmtN(e.qtyG)+' g':''}${e.protein!=null?(e.qtyG!=null?' &middot; ':'')+r1(e.protein)+' g protein':''}</div></div>
      <div class="k">${fmtN(e.kcal)}</div></div>`).join('')}
    <button class="fd-add" onclick="foodOpenAdd('${key}')">+ Add to ${label.toLowerCase()}</button>
    ${list.length?`<button class="fd-add" style="border:none;color:var(--ink3);margin-top:2px" onclick="foodSaveAsMeal('${key}')">Save as meal</button>`:''}
  </div>`;
}
window.foodShiftDay=function(n){
  const next=shiftDay(foodDay,n);
  if(next>localDay(new Date()))return;
  foodDay=next;renderToday();
};
window.foodEditTargets=function(){
  openModal('Daily targets',`
    <div class="form-group"><label class="form-label">Calories (kcal)</label><input type="number" id="fdTgtK" value="${foodTargets.kcal||''}"/></div>
    <div class="form-group"><label class="form-label">Protein (g)</label><input type="number" id="fdTgtP" value="${foodTargets.protein||''}"/></div>`,
  function(){
    const k=+document.getElementById('fdTgtK').value, p=+document.getElementById('fdTgtP').value;
    if(k>0)foodTargets.kcal=Math.round(k);
    if(p>0)foodTargets.protein=Math.round(p);
    saveTargets();closeModal();renderToday();toast('Targets saved');
  });
};
window.foodCopyDay=function(){
  const src=entriesFor(shiftDay(foodDay,-1));
  if(!src.length){toast('Nothing logged the day before');return;}
  src.forEach(e=>foodLog.push(Object.assign({},e,{id:newId('fl'),date:foodDay})));
  saveFoodLog();renderToday();toast(src.length+' items copied');
};

// ---------- add / edit sheet ----------
let sheet=null, sheetMeal='breakfast', sheetTab='meals', scanStream=null;
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
      ${[['meals','Meals'],['mine','My foods'],['search','Search'],['scan','Barcode'],['quick','Quick']].map(([k,l])=>`<button data-t="${k}" class="${k===sheetTab?'on':''}" onclick="foodSheetTab('${k}')">${l}</button>`).join('')}
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
  (m.items||[]).forEach(i=>foodLog.push({id:newId('fl'),date:foodDay,meal:sheetMeal,foodId:i.foodId||null,name:i.name,
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
      <div class="m">${fmtN(f.kcal100)} kcal &middot; ${r1(f.protein100)??'—'} g protein per 100 g${f.servingG?` &middot; ${esc(f.servingLabel||'serving')} = ${fmtN(f.servingG)} g`:''}</div></div>`).join('')
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
  const grams=entry&&entry.qtyG!=null?entry.qtyG:(sg||100);
  const saved=!!(food.id&&foods.find(f=>f.id===food.id));
  const meal=entry?entry.meal:sheetMeal;
  const inner=`<div class="fd-sheet-b">
    <div class="fd-item" style="cursor:default"><div class="n">${esc(food.name)}${food.brand?` <span class="fd-chip">${esc(food.brand)}</span>`:''}</div>
      <div class="m">Per 100 g: ${fmtN(food.kcal100)} kcal &middot; P ${r1(food.protein100)??'—'} &middot; C ${r1(food.carbs100)??'—'} &middot; F ${r1(food.fat100)??'—'}</div></div>
    <div class="form-group"><label class="form-label">Amount (g)</label><input class="fd-input" type="number" id="fdG" value="${r1(grams)}" oninput="foodPortionCalc()"/></div>
    ${sg?`<div style="display:flex;gap:6px;flex-wrap:wrap;margin-bottom:10px">${[0.5,1,1.5,2,3].map(n=>`<button class="btn-sm" onclick="document.getElementById('fdG').value=${r1(sg*n)};foodPortionCalc()">${n} &times; ${esc(food.servingLabel||'serving')}</button>`).join('')}</div>`:''}
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
    const row={date:entry?entry.date:foodDay,meal:document.getElementById('fdMeal').value,foodId:f.id||null,
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
  foodLog.push({id:newId('fl'),date:foodDay,meal:sheetMeal,foodId:null,name,qtyG:null,kcal:r0(k),
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
    <button class="fd-btn ghost" onclick="closeModal();foodDeleteEntry('${e.id}')">Remove from log</button>`,
  function(){
    e.name=document.getElementById('fdEN').value.trim()||e.name;
    const k=document.getElementById('fdEK').value,p=document.getElementById('fdEP').value;
    e.kcal=k===''?null:r0(+k);e.protein=p===''?null:r1(+p);
    saveFoodLog();closeModal();renderToday();
  });
};
window.foodDeleteEntry=function(id){
  const i=foodLog.findIndex(x=>x.id===id);if(i<0)return;
  foodLog.splice(i,1);saveFoodLog();closeSheet();renderToday();toast('Removed');
};

// ---------- History ----------
function renderHistory(){
  const el=document.getElementById('page-food-history');
  const today=localDay(new Date());
  const days=[];for(let i=0;i<30;i++)days.push(shiftDay(today,-i));
  const rows=days.map(d=>{const t=totals(entriesFor(d)),h=healthDay(d);
    return {d,logged:entriesFor(d).length>0,kcal:t.kcal,protein:t.protein,burned:h?h.totalKcal:null,steps:h?h.steps:null};});
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
  savedMeals.splice(savedMeals.indexOf(m),1);saveMeals();renderFoodsPage();toast('Meal deleted');
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
  foods.splice(i,1);saveFoods();closeModal();renderFoodsPage();toast('Deleted (past log entries kept)');
};

})();
