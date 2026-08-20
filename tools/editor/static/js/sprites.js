// Sprite sheet import: two picker modes over the INTACT sheet image, not split into
// per-cell thumbnails the way the atlas tab's grid slicer works -- seeing a grid or a
// drag-box against the real art is what makes spacing/origin easy to eyeball, which
// cropping the sheet into a grid of buttons would hide.
//
//   GRID mode is for a sheet that IS laid out on a grid, but not necessarily starting at
//   0,0 or filling the whole sheet: fw/fh/ox/oy/gx/gy describe it, drawn as lines over
//   the sheet (shDrawOverlay). Click a cell to pick it as one frame; drag across several
//   to combine them into ONE frame (a sprite bigger than one grid cell).
//
//   FREEFORM mode is for a tightly packed sheet with no grid at all: drag a box directly
//   on the sheet, or type x/y/w/h, then + Frame.
//
// Both feed the SAME ordered SH.frames list -- an array of {x,y,w,h} in commit order,
// which IS the anim frame order (matching the picker this replaces, and pnx_assets.py's
// own "each frames entry stands on its own" -- there is no longer a reason frames must
// come from one grid, so the two modes freely mix within one sprite).

const SH={sheet:null, cells:null, frames:[], mode:'grid', imgW:0, imgH:0};

// Mask colour: the background a Pebble sprite draws transparent (tools/pnx_assets.py's
// to_gcolor8/colorkey), same job and same picked-off-the-sheet-not-typed reasoning as
// atlas.js's own KEY -- the value that matters is the one actually in the file, and the
// declared sprite's save/validate calls already accept it (project/sprites.py's
// save_sprite), only the editor never had a way to set it. Set by #spsel's onchange
// (app.js) from the sprite's own manifest entry, reset there when nothing is selected.
let SPKEY=null;

function spKeyLabel(){
  const sw=$('#spkeyswatch');
  sw.classList.toggle('none', !SPKEY);
  sw.style.background=SPKEY?`rgb(${SPKEY[0]},${SPKEY[1]},${SPKEY[2]})`:'';
  $('#spkeyval').textContent=SPKEY?`${SPKEY[0]}, ${SPKEY[1]}, ${SPKEY[2]}`:'none';
}

$('#spkeypick').onclick=()=>{
  $('#shimgwrap').classList.toggle('picking');
};
$('#spkeynone').onclick=()=>{
  SPKEY=null; spKeyLabel(); shLoadGrid(); shDrawOverlay();
};
// Sampling the sheet's OWN pixels, at natural resolution, regardless of how #shimg is
// scaled up for display (shSetDisplaySize) -- the same client-rect-vs-naturalWidth ratio
// atlas.js's own #sheetimg click handler uses.
$('#shimg').addEventListener('click',e=>{
  if(!$('#shimgwrap').classList.contains('picking')) return;
  const img=e.target, r=img.getBoundingClientRect();
  const x=Math.floor((e.clientX-r.left)/r.width*img.naturalWidth);
  const y=Math.floor((e.clientY-r.top)/r.height*img.naturalHeight);
  const c=document.createElement('canvas');
  c.width=img.naturalWidth; c.height=img.naturalHeight;
  const g=c.getContext('2d', {willReadFrequently:true});
  g.imageSmoothingEnabled=false;
  g.drawImage(img,0,0);
  const p=g.getImageData(Math.max(0,x),Math.max(0,y),1,1).data;
  SPKEY=[p[0],p[1],p[2]];
  $('#shimgwrap').classList.remove('picking');
  spKeyLabel(); shLoadGrid(); shDrawOverlay();
});

function shLog(msg,bad){
  const el=$('#shlog');
  el.className=bad===false?'ok':(bad?'bad':'');
  el.textContent=msg;
}
function shFreeLog(msg,bad){
  const el=$('#shfreelog');
  el.className=bad===false?'ok':(bad?'bad':'');
  el.textContent=msg;
}

// Read by the #spsave handler (app.js) exactly as the old cell-index picker's version
// was -- picked frames win over the vertical-stack fallback, which is still right for
// art painted directly on the canvas rather than picked off a sheet.
function spFrames(){
  if(SH.frames.length) return SH.frames.map(f=>[f.x,f.y,f.w,f.h]);
  const w=+$('#spfw').value, h=+$('#spfh').value, n=+$('#spn').value;
  return Array.from({length:n},(_,i)=>[0,i*h,w,h]);
}

function shZoom(){
  const img=$('#shimg');
  return img.clientWidth ? img.clientWidth/(SH.imgW||1) : 1;
}

// Sprite sheets are routinely tiny (a 16px-wide stack of poses) -- shown at their
// natural size that is a handful of pixels, unusable to click or drag on. Scale up by a
// whole integer (crisp with image-rendering:pixelated) so the sheet is at least ~256px
// on its long edge, capped so a genuinely large sheet is not blown up further.
function shSetDisplaySize(){
  const img=$('#shimg');
  const long=Math.max(SH.imgW,SH.imgH)||1;
  const mult=Math.max(1,Math.min(8,Math.floor(256/long)));
  img.style.width=(SH.imgW*mult)+'px';
  img.style.height='';
}

function shSameRect(a,b){
  return a.x===b.x&&a.y===b.y&&a.w===b.w&&a.h===b.h;
}

async function shLoadGrid(){
  if(!SH.sheet) return;
  const r=await post('/api/sheet/frames',{sheet:SH.sheet, fw:+$('#shfw').value,
    fh:+$('#shfh').value, ox:+$('#shox').value, oy:+$('#shoy').value,
    gx:+$('#shgx').value, gy:+$('#shgy').value, colorkey:SPKEY});
  if(r.error){ shLog(r.error,true); SH.cells=null; return }
  SH.cells=r.cells;
  shLog(`${r.cols}x${r.rows} grid of ${$('#shfw').value}x${$('#shfh').value}`
    +(r.capped?` — showing the first ${r.limit}`:'')
    +'. Click a cell to pick it, or drag across several to combine them.');
}

async function shLoadSheet(sheet){
  if(!sheet){ $('#shimgwrap').style.display='none'; SH.sheet=null; shDrawPicked(); return }
  const r=await post('/api/sheet/image',{sheet});
  if(r.error){ shLog(r.error,true); return }
  SH.sheet=sheet; SH.imgW=r.w; SH.imgH=r.h;
  $('#spsheet').value=sheet;
  const img=$('#shimg');
  await new Promise(res=>{ img.onload=res; img.src=r.img });
  shSetDisplaySize();
  $('#shimgwrap').style.display='';
  await shLoadGrid();
  shDrawOverlay();
}

// After a frame is repainted (#pxsave, app.js) the sheet on disk has new pixels at the
// same geometry -- SH.frames (rects, not indices) survive this untouched.
async function shReloadSheet(){
  if(!SH.sheet) return;
  const r=await post('/api/sheet/image',{sheet:SH.sheet});
  if(r.error) return;
  SH.imgW=r.w; SH.imgH=r.h;
  const img=$('#shimg');
  await new Promise(res=>{ img.onload=res; img.src=r.img });
  shSetDisplaySize();
  await shLoadGrid();
  shDrawOverlay();
}

function shCellAt(px,py){
  if(!SH.cells) return null;
  for(let i=0;i<SH.cells.length;i++){
    const c=SH.cells[i];
    if(px>=c.x&&px<c.x+c.w&&py>=c.y&&py<c.y+c.h) return i;
  }
  return null;
}

// Every grid cell whose rect falls entirely inside `rect` -- how a drag block's bounding
// rect (anchor cell to current cell) turns into "which cells did this actually cover".
function shCoveredCells(rect){
  if(!SH.cells) return [];
  return SH.cells.filter(c=>c.x>=rect.x&&c.y>=rect.y
    &&c.x+c.w<=rect.x+rect.w&&c.y+c.h<=rect.y+rect.h);
}

let shDrag=null; // {sx,sy,cx,cy}: sheet-pixel anchor and current point, while dragging

function shPointerXY(ev){
  const img=$('#shimg'), r=img.getBoundingClientRect(), z=shZoom();
  const px=Math.floor((ev.clientX-r.left)/z), py=Math.floor((ev.clientY-r.top)/z);
  return [Math.max(0,Math.min(SH.imgW-1,px)), Math.max(0,Math.min(SH.imgH-1,py))];
}

function shDragRect(){
  if(SH.mode==='grid'){
    const ai=shCellAt(shDrag.sx,shDrag.sy), ci=shCellAt(shDrag.cx,shDrag.cy);
    if(ai==null||ci==null) return {x:0,y:0,w:0,h:0};
    const a=SH.cells[ai], c=SH.cells[ci];
    const x0=Math.min(a.x,c.x), y0=Math.min(a.y,c.y);
    const x1=Math.max(a.x+a.w,c.x+c.w), y1=Math.max(a.y+a.h,c.y+c.h);
    return {x:x0,y:y0,w:x1-x0,h:y1-y0};
  }
  const x0=Math.min(shDrag.sx,shDrag.cx), y0=Math.min(shDrag.sy,shDrag.cy);
  const x1=Math.max(shDrag.sx,shDrag.cx)+1, y1=Math.max(shDrag.sy,shDrag.cy)+1;
  return {x:x0,y:y0,w:x1-x0,h:y1-y0};
}

function shFreePreviewRect(){
  return {x:+$('#shrx').value||0, y:+$('#shry').value||0,
          w:Math.max(1,+$('#shrw').value||1), h:Math.max(1,+$('#shrh').value||1)};
}

function shDrawOverlay(){
  const cv=$('#shoverlay'), img=$('#shimg');
  const w=img.clientWidth, h=img.clientHeight;
  if(!w||!h) return;
  cv.width=w; cv.height=h;
  const g=cv.getContext('2d');
  g.clearRect(0,0,w,h);
  const z=shZoom();

  if(SH.mode==='grid'&&SH.cells){
    g.lineWidth=1;
    for(const c of SH.cells){
      const picked=SH.frames.some(f=>shSameRect(f,c));
      g.strokeStyle=picked?'#7fd1ff':'rgba(255,255,255,.35)';
      g.strokeRect(c.x*z+.5,c.y*z+.5,c.w*z-1,c.h*z-1);
      if(picked){ g.fillStyle='rgba(127,209,255,.25)'; g.fillRect(c.x*z,c.y*z,c.w*z,c.h*z) }
      else if(c.blank){ g.fillStyle='rgba(255,255,255,.08)'; g.fillRect(c.x*z,c.y*z,c.w*z,c.h*z) }
    }
    if(shDrag){
      const r=shDragRect();
      g.strokeStyle='#ffb454'; g.lineWidth=2;
      g.strokeRect(r.x*z,r.y*z,r.w*z,r.h*z);
    }
  }else if(SH.mode==='free'){
    g.lineWidth=1; g.strokeStyle='rgba(127,209,255,.6)';
    for(const f of SH.frames) g.strokeRect(f.x*z+.5,f.y*z+.5,f.w*z-1,f.h*z-1);
    const r=shDrag?shDragRect():shFreePreviewRect();
    g.strokeStyle='#ffb454'; g.lineWidth=2;
    g.strokeRect(r.x*z,r.y*z,Math.max(1,r.w)*z,Math.max(1,r.h)*z);
  }
}

function shDrawPicked(){
  const box=$('#shpicked');
  box.innerHTML='';
  SH.frames.forEach((f,i)=>{
    const b=document.createElement('button');
    b.className='tile';
    b.title=`${f.x},${f.y} ${f.w}x${f.h} — click to remove, double-click to edit, `
      +`drag into a clip below`;
    b.innerHTML=`<b>${i}: ${f.w}×${f.h}</b>`;
    // A clip drop target (below, in Declare) reads this frame's index the same way
    // #spframes cells do (app.js's spShowFrames) -- both name a position in the SAME
    // ordered frame list spFrames() produces, so a clip built from either drags in the
    // index that will actually be this frame once Save sprite writes it.
    b.draggable=true;
    b.addEventListener('dragstart',ev=>{
      ev.dataTransfer.setData('text/plain', JSON.stringify({frame:i}));
      ev.dataTransfer.effectAllowed='copy';
    });
    b.onclick=()=>{
      SH.frames.splice(i,1);
      shDrawOverlay(); shDrawPicked();
      shLog(SH.frames.length?`${SH.frames.length} frame(s) picked — they become anim `
            +`indices 0..${SH.frames.length-1}.`
            :'Click a cell to pick it, or drag across several to combine them.');
    };
    // Editing one pose directly, the same door the old picker's double-click opened.
    b.ondblclick=async ev=>{
      ev.preventDefault();
      const r=await post('/api/frame/read',{sheet:SH.sheet,x:f.x,y:f.y,w:f.w,h:f.h});
      if(r.error){ shLog(r.error,true); return }
      PX.w=r.w; PX.h=r.h; PX.frames=1; PX.frame=0;
      PX.data=Uint8Array.from(r.pixels); PX.undo=[]; PX.redo=[];
      PX.origin={sheet:SH.sheet,x:f.x,y:f.y};
      $('#pxw').value=r.w; $('#pxh').value=r.h;
      $('#pxtitle').textContent=`Canvas — ${SH.sheet} @ ${f.x},${f.y}`;
      $('#pxnote').textContent='Editing one frame. Save writes it back into the sheet.';
      pxDraw();
      shLog(`Editing frame at ${f.x},${f.y}.`,false);
    };
    box.appendChild(b);
  });
}

function shSetMode(m){
  SH.mode=m; shDrag=null;
  $('#shmodegrid').classList.toggle('on',m==='grid');
  $('#shmodefree').classList.toggle('on',m==='free');
  $('#shgridmode').style.display=m==='grid'?'':'none';
  $('#shfreemode').style.display=m==='free'?'':'none';
  shDrawOverlay();
}
$('#shmodegrid').onclick=()=>shSetMode('grid');
$('#shmodefree').onclick=()=>shSetMode('free');

$('#shsheet').addEventListener('change',()=>{
  SH.frames=[]; shDrawPicked(); shLoadSheet($('#shsheet').value);
});
for(const id of ['shfw','shfh','shox','shoy','shgx','shgy'])
  $('#'+id).addEventListener('change',async()=>{ await shLoadGrid(); shDrawOverlay() });
for(const id of ['shrx','shry','shrw','shrh'])
  $('#'+id).addEventListener('input',shDrawOverlay);
window.addEventListener('resize',shDrawOverlay);

$('#shimgwrap').addEventListener('mousedown',e=>{
  if(!SH.sheet) return;
  if($('#shimgwrap').classList.contains('picking')) return; // mask-colour pick, not a frame drag
  if(SH.mode==='grid'&&!SH.cells) return;
  const [sx,sy]=shPointerXY(e);
  shDrag={sx,sy,cx:sx,cy:sy};
  e.preventDefault();
});
window.addEventListener('mousemove',e=>{
  if(!shDrag) return;
  const [cx,cy]=shPointerXY(e);
  shDrag.cx=cx; shDrag.cy=cy;
  if(SH.mode==='free'){
    const r=shDragRect();
    $('#shrx').value=r.x; $('#shry').value=r.y;
    $('#shrw').value=Math.max(1,r.w); $('#shrh').value=Math.max(1,r.h);
  }
  shDrawOverlay();
});
window.addEventListener('mouseup',()=>{
  if(!shDrag) return;
  if(SH.mode==='grid'){
    const rect=shDragRect();
    const covered=shCoveredCells(rect);
    if(covered.length===1){
      // A plain click, or a drag that never left one cell: toggle that cell.
      const idx=SH.frames.findIndex(f=>shSameRect(f,covered[0]));
      if(idx>=0) SH.frames.splice(idx,1);
      else SH.frames.push({x:covered[0].x,y:covered[0].y,w:covered[0].w,h:covered[0].h});
    }else if(covered.length>1){
      // Several cells, combined into ONE frame -- for a sprite bigger than one cell.
      SH.frames.push({x:rect.x,y:rect.y,w:rect.w,h:rect.h});
    }
    shDrawOverlay(); shDrawPicked();
    shLog(SH.frames.length?`${SH.frames.length} frame(s) picked — they become anim `
          +`indices 0..${SH.frames.length-1}.`
          :'Click a cell to pick it, or drag across several to combine them.');
  }
  shDrag=null;
});

$('#shaddframe').onclick=()=>{
  if(!SH.sheet){ shFreeLog('Pick a sheet first.',true); return }
  const r=shFreePreviewRect();
  if(r.x+r.w>SH.imgW||r.y+r.h>SH.imgH){
    shFreeLog(`${r.w}x${r.h} at ${r.x},${r.y} runs past the sheet `
              +`(${SH.imgW}x${SH.imgH}).`,true);
    return;
  }
  SH.frames.push(r);
  shDrawOverlay(); shDrawPicked();
  shFreeLog(`${SH.frames.length} frame(s) picked.`,false);
};

$('#shclear').onclick=()=>{
  SH.frames=[];
  shDrawOverlay(); shDrawPicked();
  shLog('Picks cleared — the declare form is back to a vertical stack.');
};

// ------------------------------------------------------------- frame collision editor
//
// A frame's own SCALED rect / COMPLEX mask -- the same two shapes an atlas tile authors
// (#tileeditor, atlas.js), keyed by frame instead of tile id since frames are not one
// uniform size (each frame's own w/h drives the zoom and the mask grid, where an atlas
// tile editor has one shared tile_px for all of them). Opened via the small collision
// badge on each #spframes cell (app.js's spShowFrames); that cell's own click still opens
// pixel editing, unchanged -- this is a second, separate door into the same frame.

const SF={sprite:null, cells:null, frame:null, cell:null, mode:0, kind:0, rect:null, mask:null};
const sfClamp=(v,a,b)=>v<a?a:v>b?b:v;

function openSpriteFrameEditor(frame){
  if(!SF.cells||!SF.cells[frame]) return;
  SF.frame=frame;
  renderSpriteFrameEditor();
  $('#sfsave').closest('section').scrollIntoView({behavior:'smooth',block:'nearest'});
}

function renderSpriteFrameEditor(){
  const c=SF.cells[SF.frame];
  SF.cell=c;
  const col=c.collision||{mode:0,kind:0};
  SF.mode=col.mode; SF.kind=col.kind||0;
  SF.rect=col.rect?col.rect.slice():[0,0,c.w,c.h];
  SF.mask=col.mask?col.mask.slice():(col.auto_mask?col.auto_mask.slice():null);

  $('#sfnote').textContent=`frame ${SF.frame} — ${c.w}x${c.h}`;
  $('#sfimg').src=c.img;
  $('#sfmode').value=String(SF.mode);
  $('#sfkind').value=String(SF.kind);
  $('#sflog').textContent='';
  sfUpdateModeSections();
}

$('#sfkind').addEventListener('change',()=>{ SF.kind=+$('#sfkind').value; });
$('#sfmode').addEventListener('change',()=>{
  SF.mode=+$('#sfmode').value;
  if(SF.mode===2&&!SF.rect) SF.rect=[0,0,SF.cell.w,SF.cell.h];
  sfUpdateModeSections();
});

function sfUpdateModeSections(){
  $('#sfscaled').style.display=SF.mode===2?'':'none';
  $('#sfcomplex').style.display=SF.mode===3?'':'none';
  if(SF.mode===2) sfDrawRectEditor();
  if(SF.mode===3) sfDrawMaskGrid();
}

// --- SCALED: drag a rect over a zoomed still of the frame's own art.

function sfDrawRectEditor(){
  const c=SF.cell, zoom=Math.max(2,Math.floor(160/Math.max(c.w,c.h)));
  const img=$('#sfrectimg');
  img.src=c.img;
  img.style.width=(c.w*zoom)+'px';
  img.style.height=(c.h*zoom)+'px';
  sfUpdateRectBox();
}

function sfUpdateRectBox(){
  const c=SF.cell, img=$('#sfrectimg'), box=$('#sfrectbox');
  const zoom=img.clientWidth/c.w;
  const [x,y,w,h]=SF.rect;
  box.style.left=(x*zoom)+'px'; box.style.top=(y*zoom)+'px';
  box.style.width=(w*zoom)+'px'; box.style.height=(h*zoom)+'px';
  box.style.display='';
  $('#sfrectnote').textContent=`${w}×${h} at ${x},${y} (of ${c.w}×${c.h})`;
}

let sfRectDrag=null;
$('#sfrectwrap').addEventListener('mousedown',ev=>{
  if(SF.mode!==2||!SF.cell) return;
  const c=SF.cell, rect=$('#sfrectimg').getBoundingClientRect(), zoom=rect.width/c.w;
  sfRectDrag={sx:sfClamp(Math.floor((ev.clientX-rect.left)/zoom),0,c.w-1),
              sy:sfClamp(Math.floor((ev.clientY-rect.top)/zoom),0,c.h-1)};
  ev.preventDefault();
});
window.addEventListener('mousemove',ev=>{
  if(!sfRectDrag||SF.mode!==2) return;
  const c=SF.cell, rect=$('#sfrectimg').getBoundingClientRect(), zoom=rect.width/c.w;
  const cx=sfClamp(Math.floor((ev.clientX-rect.left)/zoom),0,c.w-1);
  const cy=sfClamp(Math.floor((ev.clientY-rect.top)/zoom),0,c.h-1);
  const x0=Math.min(sfRectDrag.sx,cx), x1=Math.max(sfRectDrag.sx,cx);
  const y0=Math.min(sfRectDrag.sy,cy), y1=Math.max(sfRectDrag.sy,cy);
  SF.rect=[x0,y0,x1-x0+1,y1-y0+1];
  sfUpdateRectBox();
});
window.addEventListener('mouseup',()=>{ sfRectDrag=null; sfMaskPaint=null });

// --- COMPLEX: paint the mask a pixel at a time, directly on the frame's own art -- the
// same zoomed-still-plus-overlay shape sfDrawRectEditor() uses for SCALED.

function sfDrawMaskGrid(){
  const c=SF.cell, zoom=Math.max(2,Math.floor(160/Math.max(c.w,c.h)));
  const img=$('#sfmaskimg'), el=$('#sfmaskgrid');
  img.src=c.img;
  img.style.width=(c.w*zoom)+'px';
  img.style.height=(c.h*zoom)+'px';
  el.style.width=(c.w*zoom)+'px';
  el.style.height=(c.h*zoom)+'px';
  el.style.gridTemplateColumns=`repeat(${c.w}, ${zoom}px)`;
  el.style.gridAutoRows=`${zoom}px`;
  el.innerHTML='';
  for(let y=0;y<c.h;y++)
    for(let x=0;x<c.w;x++){
      const i=document.createElement('i');
      if(SF.mask&&SF.mask[y][x]==='#') i.className='ink';
      i.dataset.x=x; i.dataset.y=y;
      el.appendChild(i);
    }
}

let sfMaskPaint=null;
function sfMaskSetCell(cell,ink){
  const x=+cell.dataset.x, y=+cell.dataset.y;
  const row=SF.mask[y].split(''); row[x]=ink?'#':'.'; SF.mask[y]=row.join('');
  cell.classList.toggle('ink',ink);
}
$('#sfmaskgrid').addEventListener('mousedown',ev=>{
  const cell=ev.target.closest('i'); if(!cell) return;
  sfMaskPaint=!cell.classList.contains('ink');
  sfMaskSetCell(cell,sfMaskPaint);
  ev.preventDefault();
});
$('#sfmaskgrid').addEventListener('mouseover',ev=>{
  if(sfMaskPaint===null) return;
  const cell=ev.target.closest('i'); if(!cell) return;
  sfMaskSetCell(cell,sfMaskPaint);
});
$('#sfmaskreset').onclick=()=>{
  if(!SF.cell||!SF.cell.collision) return;
  SF.mask=(SF.cell.collision.auto_mask||[]).slice();
  sfDrawMaskGrid();
};

// --- Save/clear. Both write through the same real-pipeline-validated endpoints
// save_sprite_collision/remove_sprite_collision give every other caller.

async function sfSave(){
  if(SF.frame==null||!SF.sprite) return;
  const name=SF.sprite;
  if(SF.mode===0){
    if(SF.cell.collision&&SF.cell.collision.mode!==0){
      const r=await post('/api/sprite/collision/remove',{name,frame:SF.frame});
      if(!r.ok){ $('#sflog').textContent=r.error; return }
    }
  }else{
    const body={name,frame:SF.frame,mode:SF.mode,kind:SF.kind};
    if(SF.mode===2) body.rect=SF.rect;
    if(SF.mode===3) body.mask=SF.mask;
    const r=await post('/api/sprite/collision',body);
    if(!r.ok){ $('#sflog').textContent=r.error; return }
  }
  // Order matters: spShowFrames/openSpriteFrameEditor re-render this whole panel from
  // the just-saved manifest (so the badge and the mode/kind fields reflect reality, not
  // a stale in-memory guess), which resets #sflog as part of that -- setting "Saved." has
  // to happen AFTER, or the re-render would erase it before anyone saw it.
  await spShowFrames(name);
  openSpriteFrameEditor(SF.frame);
  $('#sflog').textContent='Saved.';
  $('#sflog').className='ok';
}
$('#sfsave').onclick=sfSave;

$('#sfclearcol').onclick=async()=>{
  SF.mode=0;
  $('#sfmode').value='0';
  sfUpdateModeSections();
  await sfSave();
};

// --------------------------------------------------------------------- anim clip builder
//
// pnx_assets.py's parse_sprite_anim recognises three [sprite.anim] entry shapes: a bare
// int (a POSE, what #spanim above authors), a bare list (a CLIP at default fps/loop), or
// a table (a CLIP with explicit fps/loop/durations). Only the pose form had a UI; a clip
// existed only if someone hand-edited the manifest. This builds the other two, staged in
// CLIPS client-side exactly the way #spanim's names and SH.frames' picks already are --
// nothing reaches disk until Save sprite (app.js's #spsave) merges CLIPS into the same
// `anim` dict poses go into and calls /api/sprite/save.
//
// Frames are ADDED by dragging a chip in from #shpicked (this file) or #spframes
// (app.js's spShowFrames) -- both name a position in the ordered list spFrames()
// produces, which is the same index space a clip's `frames` array indexes into. Existing
// chips are draggable too, for reordering within the clip -- order is the animation.

let CLIPS={};                                    // name -> {frames, fps, loop, durations}
let CLIP={name:null, frames:[], durations:null};  // the one being edited right now

function clipLog(msg,bad){
  const el=$('#cliplog');
  el.className=bad===false?'ok':(bad?'bad':'');
  el.textContent=msg;
}

// Loaded fresh by app.js's #spsel handler, from the very same `s.anim` map #spanim's
// byIndex loader reads -- an entry is a clip here iff it is NOT a plain int (a pose).
function spLoadClips(anim){
  CLIPS={};
  for(const [k,v] of Object.entries(anim||{})){
    if(typeof v==='number') continue;
    if(Array.isArray(v)) CLIPS[k]={frames:v.slice(), fps:8, loop:true, durations:null};
    else CLIPS[k]={frames:(v.frames||[]).slice(), fps:v.fps||8, loop:v.loop!==false,
                   durations:v.durations?v.durations.slice():null};
  }
  drawClipSelect();
  clipReset();
}

function drawClipSelect(){
  const sel=$('#clipsel'), cur=sel.value;
  sel.innerHTML='<option value="">— new —</option>'
    +Object.keys(CLIPS).map(n=>`<option${n===cur?' selected':''}>${n}</option>`).join('');
}

function clipReset(){
  CLIP={name:null, frames:[], durations:null};
  $('#clipsel').value='';
  $('#clipname').value='';
  $('#clipfps').value=8;
  $('#cliploop').checked=true;
  $('#clipdur').checked=false;
  renderClipFrames();
  clipLog(Object.keys(CLIPS).length
    ?`${Object.keys(CLIPS).length} clip(s) declared. Drag frames in to start a new one, `
     +`or pick one above to edit it.`
    :'Drag frames in from the sheet picks or the frames grid to build a clip.');
}

$('#clipsel').addEventListener('change',()=>{
  const name=$('#clipsel').value;
  if(!name){ clipReset(); return }
  const c=CLIPS[name];
  CLIP={name, frames:c.frames.slice(), durations:c.durations?c.durations.slice():null};
  $('#clipname').value=name;
  $('#clipfps').value=c.fps;
  $('#cliploop').checked=c.loop;
  $('#clipdur').checked=!!c.durations;
  renderClipFrames();
  clipLog(`Editing "${name}" — ${c.frames.length} frame(s).`,false);
});

// One frame's thumbnail, from wherever it can be found: the saved sprite's own render
// (SF.cells, once #spframes has loaded it) if there is one, otherwise cropped live off
// the sheet image at natural resolution -- drawImage's source rect always reads the
// image's intrinsic pixels, so this is exact regardless of how #shimg is scaled up for
// display (shSetDisplaySize).
function spFrameThumb(fi){
  if(SF.cells&&SF.cells[fi]) return SF.cells[fi].img;
  const f=SH.frames[fi];
  if(f&&SH.sheet){
    const c=document.createElement('canvas'); c.width=f.w; c.height=f.h;
    const g=c.getContext('2d'); g.imageSmoothingEnabled=false;
    g.drawImage($('#shimg'), f.x,f.y,f.w,f.h, 0,0,f.w,f.h);
    return c.toDataURL();
  }
  return '';
}

function renderClipFrames(){
  const box=$('#clipframes'), withDur=$('#clipdur').checked;
  box.innerHTML='';
  if(!CLIP.frames.length){
    box.innerHTML='<small class="dim">No frames yet.</small>';
    return;
  }
  if(withDur&&!CLIP.durations) CLIP.durations=CLIP.frames.map(()=>1);
  CLIP.frames.forEach((fi,pos)=>{
    const chip=document.createElement('div');
    chip.className='clipchip';
    chip.draggable=true;
    chip.title=`frame ${fi} — drag to reorder`;
    chip.innerHTML=`<img src="${spFrameThumb(fi)}" alt=""><b>${fi}</b>`
      +(withDur?`<input type="number" class="clipdurinput" min="1" max="255"
                  value="${CLIP.durations[pos]}">`:'')
      +`<i class="clipx" title="remove">×</i>`;
    // Reordering an existing chip and dropping a new frame in both land here -- the
    // payload tells them apart (`reorder`: a position already in CLIP.frames, `frame`:
    // a frame index arriving for the first time).
    chip.addEventListener('dragstart',ev=>{
      ev.dataTransfer.setData('text/plain', JSON.stringify({reorder:pos}));
      ev.dataTransfer.effectAllowed='move';
    });
    chip.addEventListener('dragover',ev=>ev.preventDefault());
    chip.addEventListener('drop',ev=>{
      ev.preventDefault(); ev.stopPropagation();
      clipDropAt(ev,pos);
    });
    chip.querySelector('.clipx').onclick=()=>{
      CLIP.frames.splice(pos,1);
      if(CLIP.durations) CLIP.durations.splice(pos,1);
      renderClipFrames();
    };
    const durInput=chip.querySelector('.clipdurinput');
    if(durInput) durInput.addEventListener('change',()=>{
      CLIP.durations[pos]=Math.max(1,Math.min(255,+durInput.value||1));
    });
    box.appendChild(chip);
  });
}

function clipDropAt(ev,pos){
  const raw=ev.dataTransfer.getData('text/plain');
  if(!raw) return;
  let payload;
  try{ payload=JSON.parse(raw) }catch{ return }
  if(payload.reorder!=null){
    if(payload.reorder===pos) return;
    const [moved]=CLIP.frames.splice(payload.reorder,1);
    let dest=pos; if(payload.reorder<pos) dest-=1;
    CLIP.frames.splice(dest,0,moved);
    if(CLIP.durations){
      const [d]=CLIP.durations.splice(payload.reorder,1);
      CLIP.durations.splice(dest,0,d);
    }
  }else if(payload.frame!=null){
    CLIP.frames.splice(pos,0,payload.frame);
    if(CLIP.durations) CLIP.durations.splice(pos,0,1);
  }
  renderClipFrames();
}

$('#clipdrop').addEventListener('dragover',ev=>{
  ev.preventDefault();
  $('#clipdrop').classList.add('over');
});
$('#clipdrop').addEventListener('dragleave',()=>$('#clipdrop').classList.remove('over'));
$('#clipdrop').addEventListener('drop',ev=>{
  ev.preventDefault();
  $('#clipdrop').classList.remove('over');
  clipDropAt(ev, CLIP.frames.length);          // dropped on empty space -- append
});

$('#clipdur').addEventListener('change',()=>{
  if($('#clipdur').checked&&!CLIP.durations) CLIP.durations=CLIP.frames.map(()=>1);
  if(!$('#clipdur').checked) CLIP.durations=null;
  renderClipFrames();
});

$('#clipsave').onclick=()=>{
  const name=$('#clipname').value.trim();
  if(!name){ clipLog('Name the clip first.',true); return }
  if(!/^[a-z][a-z0-9_]*$/.test(name)){
    clipLog('Clip name must be lowercase letters, digits and underscores.',true); return;
  }
  if(!CLIP.frames.length){ clipLog('Drag at least one frame in first.',true); return }
  const poseNames=$('#spanim').value.split(',').map(s=>s.trim()).filter(Boolean);
  if(poseNames.includes(name)){
    clipLog(`"${name}" is already a pose name in Anim above.`,true); return;
  }
  if(CLIP.name&&CLIP.name!==name) delete CLIPS[CLIP.name];  // renamed: drop the old key
  CLIPS[name]={frames:CLIP.frames.slice(), fps:Math.max(1,Math.min(255,+$('#clipfps').value||8)),
              loop:$('#cliploop').checked,
              durations:$('#clipdur').checked?CLIP.durations.slice():null};
  CLIP.name=name;
  drawClipSelect();
  $('#clipsel').value=name;
  clipLog(`"${name}" staged — ${CLIPS[name].frames.length} frame(s). `
    +`Press Save sprite to write it.`,false);
};

$('#clipdel').onclick=()=>{
  const name=$('#clipsel').value||CLIP.name;
  if(!name||!CLIPS[name]){ clipLog('Pick a clip to delete.',true); return }
  delete CLIPS[name];
  drawClipSelect();
  clipReset();
  clipLog(`"${name}" removed. Press Save sprite to write it.`,false);
};
