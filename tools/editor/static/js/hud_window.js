// HUD windows tab: placed panels/sprites/bars/text, anchored per screen orientation,
// bound to a [[hud_var]], animated by a show/hide slide. Its own file rather than a
// section of app.js -- per-kind field visibility, an element selected for editing, and a
// server-rendered preview refreshed on every save is the same "canvas-preview-heavy and
// stateful" shape that put sprites.js/atlas.js in their own files.

// Which window/element the form is currently editing. `elementIndex===null` means the
// element form is building a NEW element (appended on save), matching
// save_hud_window_element's own index=None convention.
const HW={window:null,elementIndex:null};

function hwLog(msg,bad){
  const el=$('#hwlog');
  el.className=bad===false?'ok':(bad?'bad':'');
  el.textContent=msg||'—';
}
function hweLog(msg,bad){
  const el=$('#hwelog');
  el.className=bad===false?'ok':(bad?'bad':'');
  el.textContent=msg||'—';
}

function drawHudWindowForm(){
  const sel=$('#hwsel');
  bindSelectOptions(sel,(S.data&&S.data.hud_windows)||[],
    {label:w=>w.name, current:HW.window||sel.value});

  // Element-kind dropdowns, from the project's own declared names -- the same lists the
  // real pnx_assets.parse_hud_windows validates against.
  const panels=(S.data&&S.data.nine_slices)||[];
  $('#hwepanel').innerHTML=panels.map(p=>`<option>${p.name}</option>`).join('');
  const sprites=(S.data&&S.data.sprite_names)||[];
  $('#hwesprite').innerHTML=sprites.map(n=>`<option>${n}</option>`).join('');
  const fonts=(S.data&&S.data.fonts)||[];
  $('#hwefont').innerHTML=fonts.map(f=>`<option>${f.name}</option>`).join('');
  const hudVars=(S.data&&S.data.hud_vars)||[];
  $('#hwebarvar').innerHTML=hudVars.filter(v=>v.type==='int')
    .map(v=>`<option>${v.name}</option>`).join('');
  $('#hwetextvar').innerHTML=hudVars.filter(v=>v.type==='text')
    .map(v=>`<option>${v.name}</option>`).join('');

  if(cur) hwLoadWindow(cur); else hwDrawElementList();
}

function hwLoadWindow(name){
  HW.window=name;
  const w=((S.data&&S.data.hud_windows)||[]).find(x=>x.name===name);
  if(!w){ hwDrawElementList(); return }
  $('#hwname').value=w.name;
  $('#hwshow').value=w.show_ms;
  $('#hwhide').value=w.hide_ms;
  $('#hwease').value=w.ease;
  $('#hwslidex').value=w.slide[0];
  $('#hwslidey').value=w.slide[1];
  hwDrawElementList();
  hwRefreshPreview();
}

$('#hwsel').onchange=()=>{
  const name=$('#hwsel').value;
  hwLog('');
  if(!name){ HW.window=null; $('#hwname').value=''; hwDrawElementList(); return }
  hwLoadWindow(name);
};

$('#hwsave').onclick=async()=>{
  const name=$('#hwname').value.trim();
  if(!name){ hwLog('Name the window first.',true); return }
  const body={name, show_ms:+$('#hwshow').value, hide_ms:+$('#hwhide').value,
    ease:$('#hwease').value, slide:[+$('#hwslidex').value, +$('#hwslidey').value]};
  const r=await post('/api/hud_window/save',body);
  if(!r.ok){ hwLog(r.error,true); return }
  await load();
  HW.window=name;
  drawHudWindowForm();
  $('#hwsel').value=name;
  hwLog(`Saved "${name}". Press Build.`,false);
  budget(true);
};

$('#hwdel').onclick=async()=>{
  const name=$('#hwsel').value||$('#hwname').value.trim();
  if(!name){ hwLog('Pick a window to remove.',true); return }
  if(!confirm(`Remove HUD window "${name}" and all of its elements?`)) return;
  const r=await post('/api/hud_window/remove',{name});
  if(!r.ok){ hwLog(r.error,true); return }
  await load();
  HW.window=null;
  drawHudWindowForm();
  $('#hwsel').value='';
  hwLog(`Removed "${name}". Press Build.`,false);
  budget(true);
};

// --------------------------------------------------------------------------- elements

function hwKindFields(){
  const kind=$('#hweknd').value;
  $('#hwefpanel').style.display=kind==='panel'?'':'none';
  $('#hwefsprite').style.display=kind==='sprite'?'':'none';
  $('#hwefbar').style.display=kind==='bar'?'':'none';
  $('#hweftext').style.display=kind==='text'?'':'none';
}
$('#hweknd').onchange=hwKindFields;
hwKindFields();

function hwDrawElementList(){
  const box=$('#hwelemlist');
  const w=((S.data&&S.data.hud_windows)||[]).find(x=>x.name===HW.window);
  const elements=(w&&w.elements)||[];
  if(!elements.length){
    box.innerHTML='<span class="dim">No elements yet.</span>';
    return;
  }
  box.innerHTML=elements.map((e,i)=>{
    const ref=e.panel||e.sprite||e.hud_var||'';
    return `<div class="row" style="justify-content:space-between;margin-bottom:.25rem">`
      +`<span>${i}: ${e.kind} ${ref?`(${ref})`:''} @ ${e.anchor}</span>`
      +`<span><button data-i="${i}" class="hwe-edit">Edit</button> `
      +`<button data-i="${i}" class="hwe-del">✕</button></span></div>`;
  }).join('');
  for(const b of box.querySelectorAll('.hwe-edit'))
    b.onclick=()=>hwEditElement(+b.dataset.i);
  for(const b of box.querySelectorAll('.hwe-del'))
    b.onclick=()=>hwRemoveElement(+b.dataset.i);
}

function hwEditElement(i){
  const w=((S.data&&S.data.hud_windows)||[]).find(x=>x.name===HW.window);
  const e=w&&w.elements[i];
  if(!e) return;
  HW.elementIndex=i;
  $('#hweknd').value=e.kind;
  hwKindFields();
  $('#hweanchor').value=e.anchor||'top_left';
  $('#hweoffx').value=(e.offset&&e.offset[0])||0;
  $('#hweoffy').value=(e.offset&&e.offset[1])||0;
  if(e.kind==='panel'){
    $('#hwepanel').value=e.panel||'';
    $('#hwepw').value=e.w||20;
    $('#hweph').value=e.h||20;
  }else if(e.kind==='sprite'){
    $('#hwesprite').value=e.sprite||'';
    $('#hweframe').value=e.frame||0;
  }else if(e.kind==='bar'){
    $('#hwebarvar').value=e.hud_var||'';
    $('#hwebw').value=e.w||20;
    $('#hwebh').value=e.h||8;
    $('#hwemax').value=e.max||100;
    $('#hweborder').value=e.border!==undefined?e.border:192;
    $('#hwetrack').value=e.track!==undefined?e.track:0;
    $('#hwefill').value=e.fill!==undefined?e.fill:255;
  }else{
    $('#hwetextvar').value=e.hud_var||'';
    $('#hwefont').value=e.font||'';
    $('#hwecolour').value=e.colour!==undefined?e.colour:255;
  }
  hweLog(`Editing element ${i}. Save replaces it in place.`,false);
}

$('#hweclear').onclick=()=>{
  HW.elementIndex=null;
  hweLog('New element -- save appends it.',false);
};

$('#hwesave').onclick=async()=>{
  if(!HW.window){ hweLog('Save the window first.',true); return }
  const kind=$('#hweknd').value;
  const anchor=$('#hweanchor').value;
  const offset=[+$('#hweoffx').value, +$('#hweoffy').value];
  const body={window:HW.window, index:HW.elementIndex, kind, anchor, offset};
  if(kind==='panel'){
    body.panel=$('#hwepanel').value; body.w=+$('#hwepw').value; body.h=+$('#hweph').value;
  }else if(kind==='sprite'){
    body.sprite=$('#hwesprite').value; body.frame=+$('#hweframe').value;
  }else if(kind==='bar'){
    body.hud_var=$('#hwebarvar').value; body.w=+$('#hwebw').value; body.h=+$('#hwebh').value;
    body.max=+$('#hwemax').value; body.border=+$('#hweborder').value;
    body.track=+$('#hwetrack').value; body.fill=+$('#hwefill').value;
  }else{
    body.hud_var=$('#hwetextvar').value; body.font=$('#hwefont').value;
    body.colour=+$('#hwecolour').value;
  }
  const r=await post('/api/hud_window/element/save',body);
  if(!r.ok){ hweLog(r.error,true); return }
  await load();
  drawHudWindowForm();
  $('#hwsel').value=HW.window;
  HW.elementIndex=null;
  hweLog('Saved. Press Build.',false);
  hwRefreshPreview();
  budget(true);
};

async function hwRemoveElement(i){
  if(!confirm(`Remove element ${i}?`)) return;
  const r=await post('/api/hud_window/element/remove',{window:HW.window, index:i});
  if(!r.ok){ hweLog(r.error,true); return }
  await load();
  drawHudWindowForm();
  $('#hwsel').value=HW.window;
  hweLog(`Removed element ${i}. Press Build.`,false);
  hwRefreshPreview();
  budget(true);
}

// --------------------------------------------------------------------------- preview

async function hwRefreshPreview(){
  const img=$('#hwpreviewimg'), log=$('#hwprevlog');
  if(!HW.window){ img.removeAttribute('src'); log.textContent='Save the window to preview it.'; return }
  const r=await post('/api/hud_window/preview',{name:HW.window});
  if(r.error){ log.className='bad'; log.textContent=r.error; return }
  img.src=r.img;
  log.className='dim';
  log.textContent=`${r.w}x${r.h}, resting position (no slide/animation shown).`;
}
