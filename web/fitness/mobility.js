// Mobility tab (owner, 8 Nov 2026): Matthew Smith's 8 flexibility pillars. Each pillar lists its assessment tests
// (his names, from setting fitness_mobility_tests on the server), takes the day's values per side, and keeps the
// owner's videos/photos of each test. Results and media are server-owned (/api/fitness/mobility), not in the sync
// document. Media is private: fetched with the device token and shown from a blob, never a public link.
(function(){
let _mob=null, _mobView='list', _mobDay=null;
const SIDE={L:'Left',R:'Right',both:'Both'};
const esc=s=>String(s==null?'':s).replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
const today=()=>{const d=new Date();return new Date(d.getTime()-d.getTimezoneOffset()*60000).toISOString().slice(0,10);};
const fd=d=>new Date(d+'T12:00:00').toLocaleDateString('en-GB',{day:'numeric',month:'short',year:'numeric'});
const mb=b=>b>1e6?(b/1e6).toFixed(0)+' MB':Math.max(1,Math.round(b/1e3))+' KB';

async function load(){
  try{_mob=await syncApi('/api/fitness/mobility');}catch(e){_mob=null;}
  return _mob;
}
const pillar=id=>(_mob&&_mob.pillars||[]).find(p=>p.id===id);
const resFor=(p,t,side,day)=>(_mob.results||[]).filter(r=>r.pillar===p&&r.test===t&&r.side===side&&(!day||r.date===day));
const mediaFor=(p,t,side,day)=>(_mob.media||[]).filter(m=>m.pillar===p&&m.test===t&&m.side===side&&m.date===day);

window.renderMobility=async function(){
  const el=document.getElementById('page-mobility-pillars');if(!el)return;
  if(!_mob){el.innerHTML='<div class="fd-note" style="padding:20px 0">Loading&hellip;</div>';await load();}
  if(!_mob){el.innerHTML='<div class="fd-note" style="padding:20px 0">Could not reach Cortex.</div>';return;}
  if(_mobView==='list')return renderList(el);
  renderPillar(el,pillar(_mobView));
};

function renderList(el){
  el.innerHTML=`<div class="section-label">Flexibility pillars</div>
    <div style="font-size:12px;color:var(--ink3);margin-bottom:12px">Matthew Smith's eight skills. Each assessment is done cold and repeated at the end of every training block.</div>
    ${_mob.pillars.map(p=>{
      const days=[...new Set((_mob.results||[]).filter(r=>r.pillar===p.id).map(r=>r.date))].sort();
      const ready=(p.tests||[]).length>0;
      return `<div class="card" style="padding:12px 14px;margin-bottom:10px;${ready?'cursor:pointer':'opacity:0.55'}" ${ready?`onclick="mobOpen('${p.id}')"`:''}>
        <div style="display:flex;justify-content:space-between;align-items:center;gap:10px">
          <div><div style="font-size:15px;font-weight:800">${esc(p.name)}</div>
            <div style="font-size:12px;color:var(--ink3);margin-top:2px">${ready?`${p.tests.length} tests &middot; ${days.length?'last assessed '+fd(days[days.length-1]):'not assessed yet'}`:'Tests added when we reach this assessment'}</div></div>
          ${ready?'<div style="font-size:20px;color:var(--ink3)">&rsaquo;</div>':''}</div></div>`;}).join('')}`;
}

window.mobOpen=function(id){_mobView=id;_mobDay=null;renderMobility();window.scrollTo(0,0);};
window.mobBack=function(){_mobView='list';renderMobility();};
window.mobDay=function(v){_mobDay=v||today();renderMobility();};

function inputFor(p,t,side,m,cur){
  const id=`mob_${t.id}_${side}_${m.key}`;
  const v=cur.find(r=>r.measure===m.key);
  if(m.kind==='choice')return `<label class="form-label" style="margin-top:6px">${esc(m.label)}</label>
    <select id="${id}" style="width:100%;font-size:14px"><option value="">Not done</option>${m.options.map(o=>`<option${v&&v.label===o?' selected':''}>${esc(o)}</option>`).join('')}</select>`;
  return `<label class="form-label" style="margin-top:6px">${esc(m.label)}${m.unit?' ('+esc(m.unit)+')':''}</label>
    <input id="${id}" type="number" step="any" inputmode="decimal" class="fd-input" style="font-size:15px" value="${v&&v.value!=null?v.value:''}" placeholder="${m.unit==='deg'?'from the video, or leave blank':''}"/>`;
}

function renderPillar(el,p){
  if(!p){_mobView='list';return renderList(el);}
  const day=_mobDay||today();
  const days=[...new Set((_mob.results||[]).filter(r=>r.pillar===p.id).map(r=>r.date))].sort();
  el.innerHTML=`<button class="btn-sm" onclick="mobBack()" style="margin-bottom:10px">&lsaquo; All pillars</button>
    <div style="font-size:18px;font-weight:800">${esc(p.name)}</div>
    ${p.setup?`<div style="font-size:12px;color:var(--ink2);margin:6px 0 4px;line-height:1.5">${esc(p.setup)}</div>`:''}
    ${p.lesson?`<a href="${esc(p.lesson)}" target="_blank" rel="noopener" style="font-size:12px;color:var(--blue)">Open the lesson video</a>`:''}
    <div style="display:flex;gap:8px;align-items:center;margin:12px 0">
      <label class="form-label" style="margin:0">Assessment date</label>
      <input type="date" value="${day}" max="${today()}" class="fd-input" style="width:auto;font-size:14px" onchange="mobDay(this.value)"/></div>
    ${p.tests.map((t,i)=>`<div class="card" style="padding:12px 14px;margin-bottom:10px">
      <div style="font-size:14px;font-weight:800">${i+1}. ${esc(t.name)}</div>
      ${t.camera?`<div style="font-size:12px;color:var(--ink2);margin-top:4px;padding:6px 8px;border-radius:6px;background:var(--surface2)"><b>Camera:</b> ${esc(t.camera)}</div>`:''}
      ${t.cue?`<div style="font-size:12px;color:var(--ink2);margin-top:4px;line-height:1.45">${esc(t.cue)}</div>`:''}
      ${t.sides.map(s=>{const cur=resFor(p.id,t.id,s,day), med=mediaFor(p.id,t.id,s,day);
        return `<div style="margin-top:10px;padding-top:8px;border-top:0.5px solid var(--border)">
          ${t.sides.length>1?`<div style="font-size:12px;font-weight:800;color:var(--ink2)">${SIDE[s]}</div>`:''}
          ${t.measures.map(m=>inputFor(p,t,s,m,cur)).join('')}
          <div style="display:flex;gap:6px;flex-wrap:wrap;align-items:center;margin-top:8px">
            ${med.map((x,k)=>`<button class="btn-sm" onclick="mobPlay('${x.id}','${x.mime}')">${x.mime.startsWith('video')?'Video':'Photo'} ${k+1} &middot; ${mb(x.bytes)}</button>`).join('')}
            <label class="btn-sm" style="cursor:pointer">+ Video or photo<input type="file" accept="video/*,image/*" style="display:none" onchange="mobUpload(this,'${p.id}','${t.id}','${s}')"/></label>
            <span id="mobup_${t.id}_${s}" style="font-size:11px;color:var(--ink3)"></span></div></div>`;}).join('')}
      <button class="fd-btn" style="margin-top:10px" onclick="mobSave('${p.id}','${t.id}')">Save ${esc(t.name)}</button></div>`).join('')}
    ${days.length?historyBlock(p,days):''}`;
}

function historyBlock(p,days){
  const rows=[];
  p.tests.forEach(t=>t.sides.forEach(s=>t.measures.forEach(m=>{
    const pts=days.map(d=>{const r=resFor(p.id,t.id,s,d).find(x=>x.measure===m.key);return r?{d,r}:null;}).filter(Boolean);
    if(!pts.length)return;
    const show=x=>x.r.label||(x.r.value!=null?x.r.value+(m.unit==='deg'?'°':''):'');
    const first=pts[0], last=pts[pts.length-1];
    rows.push(`<tr><td style="padding:4px 0">${esc(t.name)}${t.sides.length>1?' &middot; '+SIDE[s]:''}<div style="font-size:10px;color:var(--ink3)">${esc(m.label)}</div></td>
      <td style="text-align:right">${esc(show(first))}<div style="font-size:10px;color:var(--ink3)">${fd(first.d)}</div></td>
      <td style="text-align:right;font-weight:800">${pts.length>1?esc(show(last))+`<div style="font-size:10px;color:var(--ink3);font-weight:400">${fd(last.d)}</div>`:'&mdash;'}</td></tr>`);
  })));
  return `<div class="card" style="padding:12px 14px;margin-top:4px"><div style="font-size:13px;font-weight:800;margin-bottom:6px">History</div>
    <table style="width:100%;border-collapse:collapse;font-size:12px"><tr style="font-size:10px;color:var(--ink3)"><td></td><td style="text-align:right">Baseline</td><td style="text-align:right">Latest</td></tr>${rows.join('')}</table></div>`;
}

window.mobSave=async function(pid,tid){
  const p=pillar(pid), t=p.tests.find(x=>x.id===tid), day=_mobDay||today();
  try{
    for(const s of t.sides){
      const values={};t.measures.forEach(m=>{const e=document.getElementById(`mob_${t.id}_${s}_${m.key}`);if(e&&e.value!=='')values[m.key]=m.kind==='number'?+e.value:e.value;});
      await syncApi('/api/fitness/mobility/result',{method:'POST',body:JSON.stringify({date:day,pillar:pid,test:tid,side:s,values})});
    }
    await load();renderMobility();toast('Saved');
  }catch(e){toast('Could not save. Check your connection.');}
};

window.mobUpload=async function(inp,pid,tid,side){
  const f=inp.files&&inp.files[0];if(!f)return;
  const st=document.getElementById(`mobup_${tid}_${side}`), day=_mobDay||today();
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
    ov.innerHTML=(mime.startsWith('video')?`<video src="${url}" controls playsinline autoplay style="max-width:100%;max-height:80vh;border-radius:10px"></video>`
      :`<img src="${url}" style="max-width:100%;max-height:80vh;border-radius:10px"/>`)
      +`<div style="display:flex;gap:10px;margin-top:14px;flex-wrap:wrap;justify-content:center">${hasAngle(id)?`<button class="btn-sm" id="mobMeasure" style="background:var(--teal);color:#fff;border-color:var(--teal)">Measure angle${mime.startsWith('video')?' on this frame':''}</button>`:''}<button class="btn-sm" id="mobDel">Delete</button><button class="btn-sm" id="mobClose">Close</button></div>`
      +(mime.startsWith('video')?'<div style="color:#aaa;font-size:11px;margin-top:8px">Pause on your best position, then tap Measure.</div>':'');
    ov.querySelector('#mobClose').onclick=()=>{URL.revokeObjectURL(url);ov.remove();};
    const mBtn=ov.querySelector('#mobMeasure');
    if(mBtn)mBtn.onclick=()=>{const src=ov.querySelector('video,img');if(src.tagName==='VIDEO')src.pause();mobMeasure(src,id,ov);};
    ov.querySelector('#mobDel').onclick=async()=>{if(!confirm('Remove this clip?'))return;await fetch(API_BASE+'/api/fitness/mobility/media/'+encodeURIComponent(id),{method:'DELETE',headers:{'Authorization':'Bearer '+syncToken()}});ov.remove();await load();renderMobility();};
  }catch(e){ov.innerHTML='<div style="color:#fff">Could not load it.</div>';setTimeout(()=>ov.remove(),1500);}
};
// ---------- Tap to measure (owner, 8 Nov 2026) ----------
// The frame (a paused video or a photo) is drawn on a canvas; he taps three points, the MIDDLE one being the joint,
// and code computes the angle at it (0-180 deg). Tapping near a placed point moves it. Saving writes the angle into
// the test's degree measure for that day and side (keeping the other values already saved) and uploads the marked-up
// frame as a photo, so every number has the picture it was read from.
const angleMeasures=t=>(t&&t.measures||[]).filter(m=>m.kind==='number'&&m.unit==='deg');
function mediaRec(id){return (_mob.media||[]).find(m=>m.id===id);}
function testOf(rec){const p=rec&&pillar(rec.pillar);return p&&p.tests.find(t=>t.id===rec.test);}
function hasAngle(id){return angleMeasures(testOf(mediaRec(id))).length>0;}
function angleAt(a,b,c){const v1=[a.x-b.x,a.y-b.y],v2=[c.x-b.x,c.y-b.y];
  const d=(v1[0]*v2[0]+v1[1]*v2[1])/(Math.hypot(v1[0],v1[1])*Math.hypot(v2[0],v2[1])||1);return Math.acos(Math.max(-1,Math.min(1,d)))*180/Math.PI;}
window.mobAngleAt=angleAt;

window.mobMeasure=function(src,id,ov){
  const rec=mediaRec(id), t=testOf(rec), ms=angleMeasures(t);
  const W=src.videoWidth||src.naturalWidth, H=src.videoHeight||src.naturalHeight;
  if(!W||!H){toast('Could not read this frame');return;}
  const maxW=Math.min(window.innerWidth-32,900), maxH=window.innerHeight*0.66, k=Math.min(maxW/W,maxH/H);
  const cv=document.createElement('canvas');cv.width=W;cv.height=H;
  cv.style.cssText='width:'+Math.round(W*k)+'px;max-width:100%;height:auto;border-radius:8px;touch-action:none;cursor:crosshair';   // never wider than the screen
  const ctx=cv.getContext('2d'), frame=document.createElement('canvas');frame.width=W;frame.height=H;frame.getContext('2d').drawImage(src,0,0,W,H);
  const pts=[], R=Math.max(10,W/90);
  const tip=document.createElement('div');tip.style.cssText='color:#ddd;font-size:12px;margin-bottom:8px;text-align:center';
  tip.textContent='Tap three points along the limb. The middle point is the joint (yellow). Drag a point to adjust it.';
  const out=document.createElement('div');out.style.cssText='color:#fff;font-weight:800;font-size:15px;margin:10px 0 6px';
  const bar=document.createElement('div');bar.style.cssText='display:flex;gap:8px;flex-wrap:wrap;justify-content:center;align-items:center';
  const pick=document.createElement('select');pick.style.cssText='font-size:13px;'+(ms.length>1?'':'display:none');
  ms.forEach(m=>{const o=document.createElement('option');o.value=m.key;o.textContent=m.label;pick.appendChild(o);});
  const save=document.createElement('button');save.className='btn-sm';save.textContent='Save angle';save.style.cssText='background:var(--teal);color:#fff;border-color:var(--teal)';
  const reset=document.createElement('button');reset.className='btn-sm';reset.textContent='Start again';
  const close=document.createElement('button');close.className='btn-sm';close.textContent='Cancel';close.onclick=()=>ov.remove();
  const draw=()=>{ctx.drawImage(frame,0,0);ctx.lineWidth=Math.max(3,W/300);ctx.strokeStyle='#30d5a8';
    if(pts.length>1){ctx.beginPath();ctx.moveTo(pts[0].x,pts[0].y);ctx.lineTo(pts[1].x,pts[1].y);if(pts[2])ctx.lineTo(pts[2].x,pts[2].y);ctx.stroke();}
    pts.forEach((p,i)=>{ctx.beginPath();ctx.arc(p.x,p.y,R,0,7);ctx.fillStyle=i===1?'#ffc53d':'#30d5a8';ctx.fill();});
    const a=pts.length===3?angleAt(pts[0],pts[1],pts[2]):null;
    if(a!=null){ctx.font='bold '+Math.round(W/22)+'px system-ui';ctx.fillStyle='#fff';ctx.strokeStyle='#000';ctx.lineWidth=Math.max(3,W/250);
      const tx=pts[1].x+R*2, ty=pts[1].y-R*2, lab=Math.round(a)+'\u00b0';ctx.strokeText(lab,tx,ty);ctx.fillText(lab,tx,ty);}
    out.textContent=a!=null?'Angle at the joint: '+Math.round(a)+'\u00b0':'Tap point '+(pts.length+1)+' of 3'+(pts.length===1?' (the joint)':'');
    save.disabled=a==null;save.style.opacity=a==null?0.4:1;};
  reset.onclick=()=>{pts.length=0;draw();};
  const pos=e=>{const b=cv.getBoundingClientRect();return{x:(e.clientX-b.left)/b.width*W,y:(e.clientY-b.top)/b.height*H};};
  let drag=-1;
  cv.addEventListener('pointerdown',e=>{e.preventDefault();const p=pos(e);
    const near=pts.findIndex(q=>Math.hypot(q.x-p.x,q.y-p.y)<R*3);
    if(near>=0)drag=near;else if(pts.length<3){pts.push(p);drag=pts.length-1;}draw();});
  cv.addEventListener('pointermove',e=>{if(drag<0)return;pts[drag]=pos(e);draw();});
  cv.addEventListener('pointerup',()=>{drag=-1;});
  cv.addEventListener('pointerleave',()=>{drag=-1;});
  ov.innerHTML='';ov.onclick=null;
  bar.append(pick,save,reset,close);ov.append(tip,cv,out,bar);draw();
  save.onclick=async()=>{
    const a=Math.round(angleAt(pts[0],pts[1],pts[2])*10)/10, key=pick.value||ms[0].key;
    const values={};(_mob.results||[]).filter(r=>r.pillar===rec.pillar&&r.test===rec.test&&r.side===rec.side&&r.date===rec.date)
      .forEach(r=>{values[r.measure]=r.label!=null?r.label:r.value;});
    values[key]=a;
    save.disabled=true;save.textContent='Saving...';
    try{
      await syncApi('/api/fitness/mobility/result',{method:'POST',body:JSON.stringify({date:rec.date,pillar:rec.pillar,test:rec.test,side:rec.side,values})});
      const blob=await new Promise(r=>cv.toBlob(r,'image/jpeg',0.85));
      const st=await syncApi('/api/fitness/mobility/media',{method:'POST',body:JSON.stringify({date:rec.date,pillar:rec.pillar,test:rec.test,side:rec.side,mime:'image/jpeg',bytes:blob.size})});
      if(st&&st.id){await fetch(API_BASE+'/api/fitness/mobility/media/'+st.id+'/0',{method:'PUT',headers:{'Authorization':'Bearer '+syncToken(),'Content-Type':'application/octet-stream'},body:blob});
        await syncApi('/api/fitness/mobility/media/'+st.id+'/finish',{method:'POST'});}
      ov.remove();await load();renderMobility();toast('Saved '+Math.round(a)+'\u00b0 to '+t.name);
    }catch(e){save.disabled=false;save.textContent='Save angle';toast('Could not save. Check your connection.');}
  };
};
})();
