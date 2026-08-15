// The Import tab: sheet -> region/offset/carve settings -> slice grid -> tile editor,
// plus the colour-key eyedropper the sheet view uses. Loaded after app.js (which still
// defines the state object `S`, `$`/`post`/`say`, `sheets`, and generic cross-tab
// helpers like `wireImport`/`loadSheets`'s caller) and before main.js.
//
// This used to be three separate, non-contiguously placed chunks of the old single
// inline script -- this block, the colour key, and the tile editor were each filed
// under whatever section comment happened to precede them in the file (one of them
// was filed under "scenes"), which is a smaller version of the same problem the
// .atlasgrid layout in style.css fixes for the screen itself: related things you
// could not see, or find, together. They're one file now.

async function loadSheets(){
  sheets=await (await fetch('/api/sheets')).json();
  $('#sheet').innerHTML=sheets.map(s=>`<option value="${s.path}">${s.name}</option>`).join('');
  analyse(); drawSlice();
}
// Cells the author has dropped, as indices into the region. Reset when the region
// changes, because the indices mean something different the moment it does.
let IMPEX=new Set(), impRegion='';
// The packed-tile view alongside the raw grid: what carve_tiles last returned, so a
// click on a raw cell can open its editor from a value already in hand rather than a
// round trip per click. Refreshed in lockstep with the slice grid.
let CARVE=null;

function impOffset(){ return [+$('#ox').value, +$('#oy').value] }

function impRegionKey(){
  return [$('#sheet').value,$('#tile').value,$('#rx').value,$('#ry').value,
          $('#rw').value,$('#rh').value,$('#ox').value,$('#oy').value].join('/');
}

async function drawSlice(){
  const key=impRegionKey();
  if(key!==impRegion){ IMPEX=new Set(); impRegion=key; closeTileEditor() }
  const region=[+$('#rx').value,+$('#ry').value,+$('#rw').value,+$('#rh').value];
  const base={sheet:$('#sheet').value,tile:+$('#tile').value,region,
    exclude:[...IMPEX], colorkey:KEY, offset:impOffset(), max_tiles:+$('#maxt').value};
  if(!base.sheet) return;

  // In parallel: the raw grid (what's IN) and the packed tiles (what each kept one
  // MEANS). Two different index spaces -- slice_grid's cells carry `packed`, an index
  // into carve_tiles' own array, resolved through a real pack_atlas (SAME max_tiles
  // both places) so it can never disagree about which tile a cell became, or claim one
  // past the cap that carve_tiles never produced.
  const [g,c]=await Promise.all([
    fetch('/api/slice',{method:'POST',headers:{'content-type':'application/json'},
      body:JSON.stringify(base)}).then(r=>r.json()),
    fetch('/api/atlas/tiles',{method:'POST',headers:{'content-type':'application/json'},
      body:JSON.stringify({...base,name:$('#aname').value.trim()||null})}).then(r=>r.json()),
  ]);
  CARVE=c.error?null:c;
  if(g.error){ $('#slnote').textContent=g.error; return }

  const el=$('#slice');
  // Zoom is a tile SIZE, not a CSS transform: the grid stays a grid, the cells stay
  // clickable at their real positions, and the container scrolls. A transform would
  // scale the hit areas out from under the pointer.
  const z=+$('#slzoom').value, cell=16*z;
  el.style.gridTemplateColumns=`repeat(${g.cols}, ${cell}px)`;
  el.style.maxWidth='100%';
  el.innerHTML=g.cells.map(c=>{
    const editable=c.packed!=null&&CARVE;
    return `<button data-i="${c.i}" data-packed="${c.packed==null?'':c.packed}"
      class="${IMPEX.has(c.i)?'off':c.state}"
      title="cell ${c.i} at ${c.x},${c.y} — ${IMPEX.has(c.i)?'dropped':c.state}`
      +`${editable?` — tile ${c.packed} of the packed atlas; click to edit`:''}"
      ><img src="${c.img}" alt=""></button>`;
  }).join('');
  for(const b of el.querySelectorAll('button')){
    // Single click edits the tile this cell packs into; double click drops/restores the
    // cell itself. Two different actions on two different index spaces (a packed tile
    // vs. a raw sheet cell), on one grid, so "what this cell IS" and "what it MEANS" stay
    // one click apart instead of two screens apart.
    b.onclick=()=>{
      const p=b.dataset.packed;
      if(p==='') { say('This cell packs no tile yet — empty, excluded, or a duplicate.'); return }
      openTileEditor(+p);
    };
    b.ondblclick=()=>{
      const i=+b.dataset.i;
      IMPEX.has(i)?IMPEX.delete(i):IMPEX.add(i);
      drawSlice(); analyse();
    };
  }
  const kept=g.cells.filter(c=>c.state==='unique'&&!IMPEX.has(c.i)).length;
  $('#slnote').textContent=`${kept} kept of ${g.cells.length} cells`
    +(IMPEX.size?` · ${IMPEX.size} dropped`:'')
    +(g.capped?` · showing the first ${g.limit}`:'');

  // The open editor, if any, is showing a snapshot from before this carve changed --
  // refresh it from the same response rather than let it drift from what Save would
  // actually write against.
  if(TE.index!=null) reopenTileEditor();
}

$('#slkeepall').onclick=()=>{ IMPEX=new Set(); drawSlice(); analyse() };
$('#sldropdup').onclick=async()=>{
  const g=await (await fetch('/api/slice',{method:'POST',
    headers:{'content-type':'application/json'},
    body:JSON.stringify({sheet:$('#sheet').value,tile:+$('#tile').value,
      region:[+$('#rx').value,+$('#ry').value,+$('#rw').value,+$('#rh').value],
      exclude:[],colorkey:KEY,offset:impOffset()})})).json();
  // Duplicates already cost nothing -- dedup collapses them -- so this is about a
  // shorter grid to read, not about bytes.
  for(const c of g.cells) if(c.state==='dup') IMPEX.add(c.i);
  drawSlice(); analyse();
};
$('#sldropempty').onclick=async()=>{
  const g=await (await fetch('/api/slice',{method:'POST',
    headers:{'content-type':'application/json'},
    body:JSON.stringify({sheet:$('#sheet').value,tile:+$('#tile').value,
      region:[+$('#rx').value,+$('#ry').value,+$('#rw').value,+$('#rh').value],
      exclude:[],colorkey:KEY,offset:impOffset()})})).json();
  for(const c of g.cells) if(c.state==='empty') IMPEX.add(c.i);
  drawSlice(); analyse();
};

// Each analyse is tagged, and a reply that is not the newest is dropped.
//
// The responses used only to paint, so a stale one was a flicker. Now one of them CLAMPS
// THE REGION, and a reply describing the sheet you just switched away from will resize
// the carve to fit a sheet you are no longer looking at -- which is exactly what it did:
// a 16x32 region became 6x1, the shape of the previously selected sheet.
let pending=null, analyseSeq=0;
function analyse(){
  clearTimeout(pending);
  const seq=++analyseSeq;
  pending=setTimeout(async()=>{
    const body={sheet:$('#sheet').value,tile:+$('#tile').value,
      region:[+$('#rx').value,+$('#ry').value,+$('#rw').value,+$('#rh').value],
      max_tiles:+$('#maxt').value,exclude:[...IMPEX],colorkey:KEY,
      ink_threshold:+$('#bwthresh').value,offset:impOffset()};
    if(!body.sheet) return;
    const r=await (await fetch('/api/analyse',{method:'POST',
      headers:{'content-type':'application/json'},body:JSON.stringify(body)})).json();
    if(seq!==analyseSeq) return;          // a newer request is already in flight
    if(r.error){$('#stats').innerHTML=`<div class="warn"><b>!</b><span>${r.error}</span></div>`;return}
    const cell=(v,l,warn)=>`<div class="${warn?'warn':''}"><b>${v}</b><span>${l}</span></div>`;
    // What it costs, and what it costs YOU: the same bytes read very differently
    // against a budget that is already 7% spent or already 97%.
    const B=n=>n.toLocaleString();
    $('#stats').innerHTML=
      cell(r.unique,'unique tiles',r.capped)+
      cell(r.colours,'colours')+
      cell(r.palettes,'palettes')+
      cell(B(r.bytes),'bytes it adds')+
      cell(`${B(r.res_before)} → ${B(r.res_after)}`,
           `resources, ${r.res_pct_after.toFixed(1)}% of the cap`, r.res_over)+
      (r.heap_after!==null&&r.heap_after!==undefined
        ? cell(`${B(r.heap_before)} → ${B(r.heap_after)}`,
               'heap while a scene holds it', r.heap_after<16384)
        : cell('—','heap: build once to measure'))+
      cell(r.repaired,'repaired',r.repaired>0)+
      cell(r.sheet_tiles.join('×'),'sheet tiles');
    $('#sheetimg').src=r.thumb;
    $('#strip').src=r.strip||'';
    $('#bwstrip').src=r.bw_strip||'';
    drawCrop(r.sheet_tiles);
  },180);
}
for(const id of ['sheet','tile','rx','ry','rw','rh','ox','oy','maxt'])
  $('#'+id).addEventListener('input',()=>{ analyse(); drawSlice() });
$('#bwthresh').addEventListener('input',()=>{
  $('#bwthreshv').textContent=$('#bwthresh').value;
  analyse();
});

$('#slzoom').addEventListener('input',()=>{
  $('#slzoomv').textContent=$('#slzoom').value+'×';
  drawSlice();
});

// ------------------------------------------------------------- the colour key
//
// A great deal of tile art is distributed with no alpha channel at all, drawn on a flat
// background -- magenta by convention, but often not exactly magenta. Without a key those
// pixels are ordinary opaque colour: they eat palette entries, they defeat the blitter's
// early-out for transparent pixels, and they draw a rectangle around every sprite.
//
// Picked off the sheet rather than typed, because the value that matters is the one
// actually in the file. The thumbnail is resized with NEAREST, so its pixels are the
// source's pixels and reading one is exact.
let KEY=null;

function keyLabel(){
  const sw=$('#keyswatch');
  sw.classList.toggle('none', !KEY);
  sw.style.background=KEY?`rgb(${KEY[0]},${KEY[1]},${KEY[2]})`:'';
  $('#keyval').textContent=KEY?`${KEY[0]}, ${KEY[1]}, ${KEY[2]}`:'none';
}

$('#keypick').onclick=()=>{
  $('#sheetwrap').classList.toggle('picking');
  $('#cropnote').textContent=$('#sheetwrap').classList.contains('picking')
    ? 'click a pixel on the sheet — usually the background behind a sprite'
    : '';
};

$('#keynone').onclick=()=>{
  // No key is a real answer, not an empty field: art that already has alpha needs none.
  KEY=null; keyLabel(); analyse(); drawSlice();
};

$('#sheetimg').addEventListener('click',e=>{
  if(!$('#sheetwrap').classList.contains('picking')) return;
  const img=e.target, r=img.getBoundingClientRect();
  const x=Math.floor((e.clientX-r.left)/r.width*img.naturalWidth);
  const y=Math.floor((e.clientY-r.top)/r.height*img.naturalHeight);
  const c=document.createElement('canvas');
  c.width=img.naturalWidth; c.height=img.naturalHeight;
  const g=c.getContext('2d', {willReadFrequently:true});
  g.imageSmoothingEnabled=false;
  g.drawImage(img,0,0);
  const p=g.getImageData(Math.max(0,x),Math.max(0,y),1,1).data;
  KEY=[p[0],p[1],p[2]];
  $('#sheetwrap').classList.remove('picking');
  keyLabel(); analyse(); drawSlice();
});

// The region as a box on the sheet. Percentages of the sheet's size in TILES, which is
// the same fraction as pixels and survives the thumbnail being scaled to fit.
// The region cannot be larger than the sheet holds AT THIS TILE SIZE, and the tile size
// changes what the sheet holds: 480x256 is 30x16 tiles at 16px and 15x8 at 32px. Typing a
// region that fits at one size and then changing the size is how a carve ends up running
// off the sheet -- which the pipeline rejects, but only at build time, and only if the
// block already went into the manifest.
//
// Clamped rather than merely flagged, and the inputs' own max is set too, so the spinners
// stop where the art does.
function clampRegion(sx, sy){
  let changed=false;
  const fit=(id, hi)=>{
    const el=$('#'+id);
    el.max=hi;
    if(+el.value > hi){ el.value=hi; changed=true }
    if(+el.value < +el.min){ el.value=el.min; changed=true }
  };
  fit('rx', Math.max(0, sx-1));
  fit('ry', Math.max(0, sy-1));
  fit('rw', Math.max(1, sx - +$('#rx').value));
  fit('rh', Math.max(1, sy - +$('#ry').value));
  return changed;
}

function drawCrop(sheetTiles){
  const box=$('#cropbox');
  if(!sheetTiles || !sheetTiles[0] || !sheetTiles[1]){ box.style.display='none'; return }
  const [sx,sy]=sheetTiles;
  if(clampRegion(sx, sy)){
    // Re-price against what the region actually became, rather than what was typed.
    analyse(); drawSlice();
  }
  const rx=+$('#rx').value, ry=+$('#ry').value,
        rw=+$('#rw').value, rh=+$('#rh').value;
  // 'block', not '': clearing the inline style hands display back to the stylesheet,
  // which sets none -- so the box positioned itself perfectly and stayed invisible.
  box.style.display='block';
  box.style.left  =(100*Math.min(rx,sx)/sx)+'%';
  box.style.top   =(100*Math.min(ry,sy)/sy)+'%';
  box.style.width =(100*Math.min(rw,sx-rx)/sx)+'%';
  box.style.height=(100*Math.min(rh,sy-ry)/sy)+'%';

  // Running off the sheet is the commonest way a carve goes wrong, and the pipeline
  // rejects it at build time; saying so here saves the round trip.
  const over=(rx+rw>sx)||(ry+rh>sy);
  box.style.borderColor=over?'var(--bad)':'var(--accent)';
  $('#cropnote').innerHTML=over
    ? `<b class="bad">the region runs off the sheet</b> — it is ${sx}×${sy} tiles`
    : `${rw}×${rh} of ${sx}×${sy} tiles — ${(100*rw*rh/(sx*sy)).toFixed(0)}% of the sheet`;
}

// -------------------------------------------------------------- the tile editor
//
// What a click in the Slice grid opens: role and collision for ONE packed tile, both
// edited in one panel because they are both "what this tile is", not two screens for
// two questions. Everything here writes through the same real-pipeline-validated
// endpoints save_atlas_collision/save_role already are for other callers -- this is a
// new client, not a new authority.

const TE={index:null,tile:null,mode:0,rect:null,mask:null};
const clamp=(v,a,b)=>v<a?a:v>b?b:v;

function openTileEditor(idx){
  if(!CARVE||!CARVE.tiles[idx]) return;
  TE.index=idx;
  renderTileEditor();
  $('#tileeditor').style.display='';
  $('#tileeditor').scrollIntoView({behavior:'smooth',block:'nearest'});
}

// Called after every drawSlice, so an editor left open through a region/exclude change
// shows the tile it now actually is rather than a snapshot of what it was.
function reopenTileEditor(){
  if(TE.index==null||!CARVE||!CARVE.tiles[TE.index]){ closeTileEditor(); return }
  renderTileEditor();
}

function closeTileEditor(){
  TE.index=null;
  $('#tileeditor').style.display='none';
}
$('#teclose').onclick=closeTileEditor;

function renderTileEditor(){
  const t=CARVE.tiles[TE.index];
  TE.tile=t;
  TE.mode=t.collision.mode;
  TE.rect=t.collision.rect?t.collision.rect.slice():[0,0,CARVE.tile_px,CARVE.tile_px];
  TE.mask=t.collision.mask?t.collision.mask.slice():t.collision.auto_mask.slice();

  $('#tenote').textContent=
    `${t.role?('"'+t.role+'"'):'tile '+t.index} — sheet ${t.origin[0]},${t.origin[1]}`;
  $('#teimg').src=t.img;
  $('#terole').value=t.role||'';
  $('#temode').value=String(TE.mode);
  $('#telog').textContent='';
  updateModeSections();
}

$('#temode').addEventListener('change',()=>{
  TE.mode=+$('#temode').value;
  if(TE.mode===2&&!TE.rect) TE.rect=[0,0,CARVE.tile_px,CARVE.tile_px];
  updateModeSections();
});

function updateModeSections(){
  $('#tescaled').style.display=TE.mode===2?'':'none';
  $('#tecomplex').style.display=TE.mode===3?'':'none';
  if(TE.mode===2) drawRectEditor();
  if(TE.mode===3) drawMaskGrid();
}

// --- SCALED: drag a rect over a zoomed still of the tile.

function drawRectEditor(){
  const T=CARVE.tile_px, zoom=Math.max(4,Math.floor(128/T));
  const img=$('#terectimg');
  img.src=TE.tile.img;
  img.style.width=(T*zoom)+'px';
  img.style.height=(T*zoom)+'px';
  updateRectBox();
}

function updateRectBox(){
  const T=CARVE.tile_px, img=$('#terectimg'), box=$('#terectbox');
  const zoom=img.clientWidth/T;
  const [x,y,w,h]=TE.rect;
  box.style.left=(x*zoom)+'px'; box.style.top=(y*zoom)+'px';
  box.style.width=(w*zoom)+'px'; box.style.height=(h*zoom)+'px';
  box.style.display='';
  $('#terectnote').textContent=`${w}×${h} at ${x},${y} (of ${T}×${T})`;
}

let rectDrag=null;
$('#terectwrap').addEventListener('mousedown',ev=>{
  if(TE.mode!==2||!TE.tile) return;
  const T=CARVE.tile_px, rect=$('#terectimg').getBoundingClientRect();
  const zoom=rect.width/T;
  rectDrag={sx:clamp(Math.floor((ev.clientX-rect.left)/zoom),0,T-1),
            sy:clamp(Math.floor((ev.clientY-rect.top)/zoom),0,T-1)};
  ev.preventDefault();
});
window.addEventListener('mousemove',ev=>{
  if(!rectDrag||TE.mode!==2) return;
  const T=CARVE.tile_px, rect=$('#terectimg').getBoundingClientRect();
  const zoom=rect.width/T;
  const cx=clamp(Math.floor((ev.clientX-rect.left)/zoom),0,T-1);
  const cy=clamp(Math.floor((ev.clientY-rect.top)/zoom),0,T-1);
  const x0=Math.min(rectDrag.sx,cx), x1=Math.max(rectDrag.sx,cx);
  const y0=Math.min(rectDrag.sy,cy), y1=Math.max(rectDrag.sy,cy);
  TE.rect=[x0,y0,x1-x0+1,y1-y0+1];
  updateRectBox();
});
window.addEventListener('mouseup',()=>{ rectDrag=null; maskPaint=null });

// --- COMPLEX: paint the mask a pixel at a time, at the tile's own resolution.

function drawMaskGrid(){
  const T=CARVE.tile_px, el=$('#temaskgrid');
  el.style.gridTemplateColumns=`repeat(${T}, 16px)`;
  el.innerHTML='';
  for(let y=0;y<T;y++)
    for(let x=0;x<T;x++){
      const i=document.createElement('i');
      if(TE.mask[y][x]==='#') i.className='ink';
      i.dataset.x=x; i.dataset.y=y;
      el.appendChild(i);
    }
}

let maskPaint=null;
function maskSetCell(cell,ink){
  const x=+cell.dataset.x, y=+cell.dataset.y;
  const row=TE.mask[y].split(''); row[x]=ink?'#':'.'; TE.mask[y]=row.join('');
  cell.classList.toggle('ink',ink);
}
$('#temaskgrid').addEventListener('mousedown',ev=>{
  const cell=ev.target.closest('i'); if(!cell) return;
  maskPaint=!cell.classList.contains('ink');
  maskSetCell(cell,maskPaint);
  ev.preventDefault();
});
$('#temaskgrid').addEventListener('mouseover',ev=>{
  if(maskPaint===null) return;
  const cell=ev.target.closest('i'); if(!cell) return;
  maskSetCell(cell,maskPaint);
});
$('#temaskreset').onclick=()=>{
  if(!TE.tile) return;
  TE.mask=TE.tile.collision.auto_mask.slice();
  drawMaskGrid();
};

// --- Save/clear. Both write through the same real-pipeline-validated endpoints
// save_atlas_collision/save_role already give every other caller.

async function teSave(){
  if(!TE.tile) return;
  const atlas=$('#aname').value.trim();
  if(!atlas||!atlasNames().includes(atlas)){
    $('#telog').textContent='Add this atlas first (below) -- a tile has nothing to '
      +'save its role or collision INTO until it exists in the manifest.';
    return;
  }
  const t=TE.tile;
  const wantRole=$('#terole').value.trim();
  if(wantRole!==(t.role||'')){
    if(t.role){
      const r=await post('/api/role/remove',{atlas,role:t.role});
      if(!r.ok){ $('#telog').textContent=r.error; return }
    }
    if(wantRole){
      const r=await post('/api/role',{atlas,role:wantRole,index:t.index});
      if(!r.ok){ $('#telog').textContent=r.error; return }
    }
  }

  // The tile is named by role once it has one -- symbolic, survives a recarve -- and by
  // raw index only until it does, same convention [[atlas.collision]] always used.
  const tileRef=wantRole||t.index;
  if(TE.mode===0){
    if(t.collision.mode!==0){
      const r=await post('/api/atlas/collision/remove',{atlas,tile:tileRef});
      if(!r.ok){ $('#telog').textContent=r.error; return }
    }
  } else {
    const body={atlas,tile:tileRef,mode:TE.mode};
    if(TE.mode===2) body.rect=TE.rect;
    if(TE.mode===3) body.mask=TE.mask;
    const r=await post('/api/atlas/collision',body);
    if(!r.ok){ $('#telog').textContent=r.error; return }
  }

  $('#telog').textContent='Saved.';
  await reload();
  drawSlice(); analyse();
}
$('#tesave').onclick=teSave;

$('#teclearcol').onclick=async()=>{
  TE.mode=0;
  $('#temode').value='0';
  updateModeSections();
  await teSave();
};

// An atlas that already exists is edited, not duplicated. The button says which, because
// a button that silently does one of two things is worse than either.
function atlasNames(){ return S.data.atlas_names || [] }

function importBody(){
  return {name:$('#aname').value.trim(), sheet:$('#sheet').value, tile:+$('#tile').value,
          colorkey:KEY, max_tiles:+$('#maxt').value, exclude:[...IMPEX],
          region:[+$('#rx').value,+$('#ry').value,+$('#rw').value,+$('#rh').value],
          offset:impOffset()};
}

function atlasMode(){
  const name=$('#aname').value.trim();
  const exists=atlasNames().includes(name);
  $('#addatlas').textContent=exists?'Update atlas':'Add atlas';
  $('#aload').style.display=exists?'':'none';
  $('#adel').style.display=exists?'':'none';
  $('#atlasnames').innerHTML=atlasNames().map(n=>`<option value="${n}">`).join('');
  return exists;
}
$('#aname').addEventListener('input',atlasMode);

$('#aload').onclick=async()=>{
  const name=$('#aname').value.trim();
  const s=await (await fetch('/api/atlas/spec',{method:'POST',
    headers:{'content-type':'application/json'},
    body:JSON.stringify({name})})).json();
  if(s.error){ $('#log').className='bad'; $('#log').textContent=s.error; return }
  const set=(id,v)=>{ $('#'+id).value=v };
  set('sheet',s.sheet); set('tile',s.tile); set('maxt',s.max_tiles);
  set('rx',s.region[0]); set('ry',s.region[1]); set('rw',s.region[2]); set('rh',s.region[3]);
  const off=s.offset||[0,0]; set('ox',off[0]); set('oy',off[1]);
  set('apick',(s.autopick||[]).join(', '));
  // `metatiles` arrives as written: "auto", a bool, or a fraction that is this atlas's
  // own threshold. The select carries the three named choices; a fraction is left as
  // "auto" in the box and preserved in the file unless it is deliberately changed.
  const mt=s.metatiles;
  set('ameta', (mt===true||mt==='true')?'true'
             : (mt===false||mt==='false')?'false':'auto');
  set('avars',(s.variants||[]).join(', '));
  KEY=s.colorkey||null; keyLabel();
  IMPEX=new Set(s.exclude||[]); impRegion=impRegionKey();
  $('#log').className=''; $('#log').textContent=
    `Loaded "${name}" — change anything and press Update atlas.`;
  analyse(); drawSlice();
};

// Removal asks the server what still uses the atlas BEFORE confirming, so the dialog says
// "the ship map draws with it" rather than offering a delete that will simply be refused.
$('#adel').onclick=async()=>{
  const name=$('#aname').value.trim();
  const log=$('#log');
  const post=(url)=>fetch(url,{method:'POST',
    headers:{'content-type':'application/json'},body:JSON.stringify({name})});

  const u=await (await post('/api/atlas/users')).json();
  if(u.users&&u.users.length){
    log.className='bad';
    log.textContent=`Cannot remove "${name}" — ${u.users.join('; ')}.`;
    return;
  }
  if(!confirm(`Remove atlas "${name}" from the manifest?\n\n`
              +`Nothing references it. The art on disk is left alone.`)) return;

  const r=await (await post('/api/atlas/remove')).json();
  if(r.error){ log.className='bad'; log.textContent=r.error; return }
  log.className='ok'; log.textContent=`Removed "${name}". Press Build.`;
  $('#aname').value='';
  await load();
  atlasMode();
};

$('#addatlas').onclick=async()=>{
  const body=importBody();
  if(!body.name){alert('Name the atlas first.');return}
  const log=$('#log');

  // Validated through the real pipeline BEFORE anything is written. A carve that fails
  // the build used to go into the manifest anyway, and the only sign was Build failing
  // afterwards -- leaving a broken block to remove by hand.
  const v=await (await fetch('/api/atlas/validate',{method:'POST',
    headers:{'content-type':'application/json'},body:JSON.stringify(body)})).json();
  if(!v.ok){
    log.className='bad';
    log.textContent=`Not added — ${v.error}`;
    return;
  }

  const editing=atlasNames().includes(body.name);
  const r=await (await fetch(editing?'/api/atlas/update':'/api/atlas',{method:'POST',
    headers:{'content-type':'application/json'},body:JSON.stringify(body)})).json();
  log.className=r.ok?'ok':'bad';
  if(!r.ok){ log.textContent=r.error; return }

  // Warnings are said, not enforced: a capped carve or a reduced tile is a legitimate
  // choice about someone's own art.
  // The autopick list is written after the block exists, so adding and editing take the
  // same path.
  //
  // An empty box means different things in the two: on a fresh import the scaffold has
  // already written the default three, so empty means "leave the default alone". When
  // EDITING it means "name nothing" -- and it has to be obeyed, because clearing the list
  // is the only way to hand a name like `floor` over to an explicit [atlas.semantic]
  // entry. Skipping it here is what made an autopicked role impossible to override.
  // Metatiles and variants are written after the block exists, for the same reason
  // autopick is: both are decisions about a packed atlas, and the block has to be there
  // to hold them.
  const vars=$('#avars').value.split(',').map(s=>s.trim()).filter(Boolean);
  const x=await post('/api/atlas/extras',
    {name:body.name, metatiles:$('#ameta').value, variants:vars});
  if(!x.ok){ log.className='bad'; log.textContent=x.error; return }

  const picks=$('#apick').value.split(',').map(s=>s.trim()).filter(Boolean);
  if(picks.length||editing){
    const p=await post('/api/autopick',{atlas:body.name,roles:picks});
    if(!p.ok){ log.className='bad'; log.textContent=p.error; return }
  }

  log.textContent=(editing?`Updated [[atlas]] "${body.name}".`
                          :`Added [[atlas]] "${body.name}" to the manifest.`)
    + (v.warnings.length?`\n\n${v.warnings.map(w=>'! '+w).join('\n')}`:'')
    + '\n\nPress Build.';
  await load();
  atlasMode();
};


// The "Tiles kept" strip's 1-bit/colour toggle. drawSlice() (above) keeps both #strip
// and #bwstrip populated regardless of which one is showing, so this is pure display --
// nothing to recompute, just which <img> is visible.
$('#stripbw').addEventListener('change', () => {
  const bw = $('#stripbw').checked;
  $('#strip').style.display = bw ? 'none' : '';
  $('#bwstripwrap').style.display = bw ? '' : 'none';
});
