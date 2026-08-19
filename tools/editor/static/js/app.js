const S={data:null,map:null,ch:null,mode:'paint',dirty:false,img:{},T:32};
const $=s=>document.querySelector(s);

// One JSON POST and one line of output. Both existed inline in a dozen handlers; the
// legend and flag editors add enough more of them that the repetition stopped paying.
const post=async(url,body)=>(await fetch(url,{method:'POST',
  headers:{'content-type':'application/json'},
  body:JSON.stringify(body||{})})).json();

function say(text,bad){
  const log=$('#log');
  log.className=bad===false?'ok':(bad===undefined?'':'bad');
  log.textContent=text;
}

// Re-read the manifest after the server has written to it, keeping the map being edited
// selected. The legend is project-wide state, so a character minted from the picker has
// to come back through /api/state rather than being invented in the page -- otherwise
// the palette shows a tile the manifest does not have.
async function reload(){
  const keep=S.map&&S.map.name, dirty=S.dirty;
  // Cells, not rows: painting writes the cell grid now, so preserving `rows` across a
  // reload would restore the state the map had before the last brush stroke.
  const cells=S.map&&S.map.cells, tiles=S.map&&S.map.tiles;
  const start=S.map&&S.map.start, warps=S.map&&S.map.warps;
  const sets=S.map&&S.map.atlases;
  S.data=await (await fetch('/api/state')).json();
  const i=Math.max(0,S.data.maps.findIndex(m=>m.name===keep));
  $('#mapsel').value=i;
  selectMap(i);
  // Unsaved painting survives the round trip. Reloading state to pick up one new legend
  // character would otherwise throw away every edit made since the last save, which is
  // the kind of loss that teaches people not to touch a feature.
  if(dirty&&cells&&haveMap()){
    // The tile table is merged rather than replaced: a character just minted through the
    // picker arrives from the server as a NEW entry, and dropping the server's copy would
    // throw the new tile away the moment it was added.
    if(tiles&&S.map.tiles&&S.map.tiles.length>=tiles.length) {
      tiles.forEach((old,i)=>{ if(S.map.tiles[i]) Object.assign(S.map.tiles[i],old) });
    }
    S.map.cells=cells; S.map.start=start; S.map.warps=warps;
    if(sets){ S.map.atlases=sets; S.map.atlas=sets[0] }
    S.dirty=true; mark(); drawLegend(); draw(); info();
  }
}

async function load(){
  S.data=await (await fetch('/api/state')).json();

  // Launched with no project -- from a dock, say. Go straight to Project, which is
  // where opening and creating actually live, rather than showing empty authoring tabs.
  const authoring=['tabmaps','tabimport','tabfonts','tabpixel','tabcode'];
  if(S.data.no_project){
    for(const id of authoring) $('#'+id).disabled=true;
    showTab('project');
    return;
  }
  for(const id of authoring) $('#'+id).disabled=false;

  $('#mapsel').innerHTML=S.data.maps.map((m,i)=>`<option value="${i}">${m.name}</option>`).join('');
  drawPalettes(); platformSelector(); budget(); statusbar(); orientation(); atlasMode();
  // Once, a moment after the editor is usable: an update check is never
  // worth delaying the first paint for.
  setTimeout(()=>updCheck(), 1500);
  startHeartbeat();
  selectMap(0);
}

// The atlases this map draws from, in the order that fixes its tile id space. A map is
// not "drawn with an atlas" -- it is drawn with a LIST, and the first one is only the
// default for characters that do not name their own.
function mapAtlases(){
  const want=(S.map&&S.map.atlases)||[];
  const found=want.map(n=>S.data.atlases.find(a=>a.name===n)).filter(Boolean);
  return found.length?found:(S.data.atlases[0]?[S.data.atlases[0]]:[]);
}
function atlas(name){
  const list=mapAtlases();
  if(!name) return list[0];
  return list.find(a=>a.name===name)||null;
}

// A legend character names a tile in ONE of the map's atlases -- the one it pins, or the
// map's first if it pins none. It used to resolve against the map's first atlas always,
// which is why a character belonging to a second tileset showed as missing art even
// though the same manifest built and previewed correctly.
//
// The tile is either a role the atlas defines or a raw index into it. Roles are the
// better thing to write, but an atlas has hundreds of tiles and only a handful are worth
// naming, so an index is how the rest get painted at all.
// The legend this map actually paints with: the project table overlaid with the map's
// own, which is exactly what the pipeline builds. Merged in the page rather than on the
// server because WHICH table an entry lives in decides what editing it changes -- a
// project character changes every map, a map's own changes this one -- so the page needs
// both halves, not the answer.
//
// The overlay is what makes a tileset paintable at all. One character per cell caps a map
// at the printable set, about ninety; project-wide, that ninety was shared by every map
// and every atlas in the game, so most of a carved tileset could never be placed.
function LEG(){
  return Object.assign({}, S.data.legend, (S.map&&S.map.legend)||{});
}
function scopeOf(ch){
  return (S.map&&S.map.legend&&S.map.legend[ch])?'map':'project';
}

function resolve(ch){
  const e=LEG()[ch];
  if(!e) return null;
  const a=atlas(e.atlas);
  if(!a) return null;
  const byIndex=typeof e.tile==='number';
  const idx=byIndex?e.tile:a.roles[String(e.tile).toLowerCase()];
  if(idx===undefined||idx<0||idx>=a.tiles.length) return null;
  return {uri:a.tiles[idx], index:idx, atlas:a.name, role:byIndex?null:e.tile,
          flags:e.flags||[], flip:e.flip||[], rotate:!!e.rotate};
}

// Why a character does not resolve, in the words that say what to do about it. "missing"
// with no reason is the failure this replaced: the answer was always in the manifest and
// never on the screen.
function whyMissing(ch){
  const e=LEG()[ch], want=e.atlas;
  if(want && !mapAtlases().some(a=>a.name===want))
    return `${ch} draws from ${want}, which this map does not use`;
  const a=atlas(want);
  if(!a) return `${ch} has no atlas to draw from`;
  if(typeof e.tile==='number')
    return `${ch} names tile ${e.tile} of ${a.name}, which packed ${a.tiles.length}`;
  return `${ch} names role "${e.tile}", which ${a.name} does not define`;
}

// CSS transforms, so a mirrored or rotated tile costs no second image: the same data URI
// is drawn turned. The pipeline stores flip and MAP_ROTATE as bits on the cell for
// exactly the same reason, which is what makes this an honest preview rather than a
// lookalike -- draw() below applies the identical transform to the map canvas.
//
// Once rotate is on, flip_x/flip_y do NOT mean "mirror this axis of the swapped image" --
// pnx_gfx.c's rotate path reads the FLIP_X bit from the destination ROW and FLIP_Y from
// the destination COLUMN, which after a transpose swaps which checkbox controls which
// visual axis. See pack_atlas's transpose() comment in tools/pnx_assets.py for the full
// derivation; matrix(0,1,1,0,0,0) is CSS's plain axis swap, applied (rightmost function
// in the list runs first) BEFORE the scale, same order the engine uses.
function flipCss(flip,rotate){
  if(!flip||!flip.length){
    if(!rotate) return '';
    return 'transform:matrix(0,1,1,0,0,0)';
  }
  const fx=flip.includes('x'), fy=flip.includes('y');
  const sx=(rotate?fy:fx)?-1:1, sy=(rotate?fx:fy)?-1:1;
  return `transform:scale(${sx},${sy})${rotate?' matrix(0,1,1,0,0,0)':''}`;
}

function drawLegend(){
  const el=$('#legend'); el.innerHTML=''; S.img={};
  const list=mapAtlases();
  if(!list.length){ el.innerHTML='<small>No atlas built yet — press Build.</small>'; return }

  // The palette is the map's TILE TABLE, not the project legend -- which is what lets one
  // canvas draw both authoring formats. A `rows` map's entries carry the character they
  // came from and show it; a `.pnxmap`'s show their index, because there is no character
  // and above ~90 tiles there could not be one.
  //
  // Grouped by atlas, because with several tilesets in one map an ungrouped strip is just
  // a pile: which tileset a tile came from is the thing you are choosing between.
  const tiles=(S.map&&S.map.tiles)||[];
  const usable=[], missing=[];
  tiles.forEach((t,i)=>{ (resolveTile(t)?usable:missing).push(i) });
  const groups=new Map(list.map(a=>[a.name,[]]));
  for(const i of usable){
    const r=resolveTile(tiles[i]);
    if(groups.has(r.atlas)) groups.get(r.atlas).push(i);
  }

  for(const [name,members] of groups){
    if(list.length>1){
      const h=document.createElement('div');
      h.className='palgroup'; h.textContent=name;
      el.appendChild(h);
    }
    if(!members.length){
      const p=document.createElement('small');
      p.className='dim'; p.textContent='no tiles yet';
      el.appendChild(p);
    }
    for(const i of members){
      const t=tiles[i], r=resolveTile(t);
      const img=new Image(); img.src=r.uri; S.img[i]=img;
      img.onload=draw;

      const label=t.ch!==undefined?t.ch:String(i);
      const b=document.createElement('button');
      b.className='tile'+(S.ti===i?' sel':'');
      b.title=`${label} → ${r.role?r.role:'tile '+r.index} of ${r.atlas}`
        +(r.flip.length?` flipped ${r.flip.join('')}`:'')
        +(r.flags.length?` [${r.flags.join(' ')}]`:'')
        +(t.ch!==undefined?(scopeOf(t.ch)==='map'?' — this map only':' — project-wide'):'');
      b.innerHTML=`<img src="${r.uri}" alt="${label}" style="${flipCss(r.flip,r.rotate)}">`
        +`<b>${label}</b>`
        +(r.flags.length?`<i class="fmark">${flagMark(r.flags)}</i>`:'');
      b.onclick=()=>{ selectTile(i) };
      el.appendChild(b);
    }
  }
  // Re-derived every time, not only when the index has gone stale. `ti` can stay valid
  // across a change that makes `ch` wrong -- converting a map to a file keeps index 1 and
  // takes its character away -- and a leftover `ch` sends the tile panel down the legend
  // path for a map that has no legend.
  if(!usable.length||S.img[S.ti]===undefined) selectTile(usable.length?usable[0]:null,true);
  else selectTile(S.ti,true);

  $('#painthint').innerHTML = missing.length
    ? `<span style="color:var(--bad)">${missing.map(i=>whyTileMissing(tiles[i],i))
        .join('; ')}.</span>`
    : 'Click to paint. <kbd>W</kbd> sets a warp, <kbd>S</kbd> the start.';
  tileInfo();
}

// One selection, two names. `ti` is what paints; `ch` is the legend character behind it,
// which only a `rows` map has and which the legend sidebar still edits. Keeping both in
// step here is what stops the two halves of the Maps tab disagreeing about what is
// selected.
function selectTile(i,quiet){
  S.ti=i;
  const t=(S.map&&S.map.tiles&&i!=null)?S.map.tiles[i]:null;
  S.ch=(t&&t.ch!==undefined)?t.ch:null;
  if(quiet) return;
  S.mode='paint'; drawLegend(); tool(); tileInfo();
}

// A tile table entry resolved against the built atlases: the same job resolve(ch) does
// for a legend character, minus the character.
function resolveTile(t){
  if(!t) return null;
  const a=atlas(t.atlas);
  if(!a) return null;
  const byIndex=typeof t.index==='number';
  const idx=byIndex?t.index:a.roles[String(t.index).toLowerCase()];
  if(idx===undefined||idx<0||idx>=a.tiles.length) return null;
  const flip=[...((t.flip)||'')];
  return {uri:a.tiles[idx], index:idx, atlas:a.name, role:byIndex?null:t.index,
          flags:t.flag_names||[], flip, rotate:!!t.rotate};
}

function whyTileMissing(t,i){
  const label=t.ch!==undefined?`'${t.ch}'`:`tile ${i}`;
  if(t.missing) return `${label} has no legend entry`;
  if(t.atlas && !mapAtlases().some(a=>a.name===t.atlas))
    return `${label} draws from ${t.atlas}, which this map does not use`;
  const a=atlas(t.atlas);
  if(!a) return `${label} has no atlas to draw from`;
  if(typeof t.index==='number')
    return `${label} names tile ${t.index} of ${a.name}, which packed ${a.tiles.length}`;
  return `${label} names role "${t.index}", which ${a.name} does not define`;
}

// One glyph per flag, so the palette shows behaviour without a tooltip. Solid and warp
// get the two shapes everyone already reads; a project's own flags get their initial.
function flagMark(flags){
  return flags.map(f=>f==='solid'?'▪':f==='warp'?'⇢':f[0].toUpperCase()).join('');
}

// ------------------------------------------------------------------ the tile panel
//
// The selected character, and every property of it that the manifest carries. Editing
// here writes the manifest immediately rather than waiting for Save map: the legend is
// project-wide, and a flag change means something to every map that paints the character,
// not just the one on screen. Save map saves the ROWS; this is not part of them.

// The tile panel for a map whose cells live in a file. Everything here edits the map's
// own tile table and is saved with the map -- no legend, no project-wide effect, and no
// character to run out of.
function tileInfoSource(){
  const box=$('#tileinfo');
  const t=S.map.tiles[S.ti], r=resolveTile(t);
  if(!r){ box.innerHTML='<small class="dim">no tile selected</small>'; return }
  const known=S.data.flags||{solid:1,warp:2};
  box.innerHTML='';

  const head=document.createElement('div');
  head.className='mini';
  head.innerHTML=`<img src="${r.uri}" style="width:32px;height:32px;`
    +`image-rendering:pixelated;${flipCss(r.flip,r.rotate)}">`
    +`<small><b>#${S.ti}</b> → ${r.role?'role "'+r.role+'"':'tile '+r.index}`
    +`<br><span class="dim">${r.atlas}</span></small>`;
  box.appendChild(head);

  const flags=document.createElement('div');
  flags.style.margin='.4rem 0';
  for(const name of Object.keys(known)){
    const on=(t.flag_names||[]).includes(name);
    const l=document.createElement('label');
    l.className='mini';
    l.innerHTML=`<input type="checkbox" ${on?'checked':''}> ${name}`
      +` <span class="dim">0x${known[name].toString(16).padStart(2,'0')}</span>`;
    l.querySelector('input').onchange=ev=>{
      const next=(t.flag_names||[]).filter(f=>f!==name);
      if(ev.target.checked) next.push(name);
      t.flag_names=next;
      // The byte is what the file stores; the names are what the page shows. Both are
      // kept so a reload does not have to re-derive one from the other.
      t.flags=next.reduce((b,n)=>b|(known[n]||0),0);
      S.dirty=true; mark(); drawLegend(); draw(); budget();
    };
    flags.appendChild(l);
  }
  box.appendChild(flags);

  const a=atlas(r.atlas);
  if(!(a&&a.metatiled)){
    const flip=document.createElement('div');
    for(const axis of ['x','y']){
      const on=[...(t.flip||'')].includes(axis);
      const l=document.createElement('label');
      l.className='mini';
      l.innerHTML=`<input type="checkbox" ${on?'checked':''}> flip ${axis.toUpperCase()}`;
      l.querySelector('input').onchange=ev=>{
        const set=new Set([...(t.flip||'')]);
        ev.target.checked?set.add(axis):set.delete(axis);
        t.flip=[...set].sort().join('');
        S.dirty=true; mark(); drawLegend(); draw();
      };
      flip.appendChild(l);
    }
    const rl=document.createElement('label');
    rl.className='mini';
    rl.innerHTML=`<input type="checkbox" ${t.rotate?'checked':''}> rotate`;
    rl.querySelector('input').onchange=ev=>{
      t.rotate=ev.target.checked;
      S.dirty=true; mark(); drawLegend(); draw();
    };
    flip.appendChild(rl);
    box.appendChild(flip);
  }

  const foot=document.createElement('small');
  foot.className='dim';
  foot.textContent='this map only — saved into '+(S.map.source||'its map file');
  box.appendChild(foot);
}

function tileInfo(){
  const box=$('#tileinfo'), ch=S.ch;
  // A `.pnxmap` has no legend characters, so its tiles are edited on the table entry
  // itself and saved with the map. A `rows` map keeps going through the legend, because
  // there the character IS the thing and it is shared with other maps.
  if(S.ti!=null && !ch && S.map && S.map.format==='source'){ tileInfoSource(); return }

  const r=ch?resolve(ch):null;
  if(!r){ box.innerHTML='<small class="dim">no tile selected</small>'; return }

  const e=LEG()[ch];
  const known=S.data.flags||{solid:1,warp:2};
  box.innerHTML='';

  const head=document.createElement('div');
  head.className='mini';
  head.innerHTML=`<img src="${r.uri}" style="width:32px;height:32px;`
    +`image-rendering:pixelated;${flipCss(r.flip,r.rotate)}">`
    +`<small><b>${ch}</b> → ${r.role?'role "'+r.role+'"':'tile '+r.index}`
    +`<br><span class="dim">${r.atlas}</span></small>`;
  box.appendChild(head);

  // Flags. A checkbox each, including the project's own, because the difference between
  // solid and walkable is the difference between a wall and a floor and it should not
  // take a text editor to say which one this is.
  const flags=document.createElement('div');
  flags.style.margin='.4rem 0';
  for(const name of Object.keys(known)){
    const on=(e.flags||[]).includes(name);
    const l=document.createElement('label');
    l.className='mini';
    l.innerHTML=`<input type="checkbox" ${on?'checked':''}> ${name}`
      +` <span class="dim">0x${known[name].toString(16).padStart(2,'0')}</span>`;
    l.querySelector('input').onchange=ev=>{
      const next=(e.flags||[]).filter(f=>f!==name);
      if(ev.target.checked) next.push(name);
      writeLegend(ch,{flags:next});
    };
    flags.appendChild(l);
  }
  box.appendChild(flags);

  // Mirroring, hidden for a metatiled atlas rather than offered and then refused: the
  // runtime does not flip composed tiles, so the choice does not exist there.
  const a=atlas(r.atlas);
  if(a&&!a.metatiled){
    const flip=document.createElement('div');
    for(const axis of ['x','y']){
      const on=r.flip.includes(axis);
      const l=document.createElement('label');
      l.className='mini';
      l.innerHTML=`<input type="checkbox" ${on?'checked':''}> flip ${axis.toUpperCase()}`;
      l.querySelector('input').onchange=ev=>{
        const next=r.flip.filter(f=>f!==axis);
        if(ev.target.checked) next.push(axis);
        writeLegend(ch,{flip:next});
      };
      flip.appendChild(l);
    }
    const rl=document.createElement('label');
    rl.className='mini';
    rl.innerHTML=`<input type="checkbox" ${r.rotate?'checked':''}> rotate`;
    rl.querySelector('input').onchange=ev=>{
      writeLegend(ch,{rotate:ev.target.checked});
    };
    flip.appendChild(rl);
    box.appendChild(flip);
  }

  const foot=document.createElement('div');
  foot.className='mini';
  const del=document.createElement('button');
  del.textContent='Remove';
  del.title='remove this legend character';
  const own=scopeOf(ch)==='map';
  del.onclick=async()=>{
    // Removed from the table it lives in. A project character deleted while another map
    // still paints it is refused by the server, which is why the scope is sent rather
    // than guessed from the map currently open.
    const r2=await post('/api/legend/remove',{char:ch, map:own?S.map.name:null});
    if(!r2.ok){ say(r2.error); return }
    S.ti=null; S.ch=null; await reload();
    say(`Removed ${ch} from ${own?`map "${S.map.name}"`:'the project legend'}.`,false);
  };
  // Which table this character lives in, said plainly next to the button that deletes it.
  // The two look identical on the canvas and behave completely differently: editing a
  // project character changes every map that paints it.
  const scope=document.createElement('small');
  scope.className='dim';
  scope.style.marginLeft='.4rem';
  scope.textContent=own?`this map only`:`project-wide — every map`;
  foot.appendChild(del);
  foot.appendChild(scope);
  box.appendChild(foot);

  // Defining a flag is rare enough to sit behind a click, and common enough that it
  // cannot only live in the manifest.
  const add=document.createElement('div');
  add.className='mini';
  const nf=document.createElement('button');
  nf.textContent='＋ flag';
  nf.title='name a new tile flag the game can test';
  nf.onclick=async()=>{
    const name=prompt('Name the flag (lowercase; it becomes TILE_FLAG_… in the header):');
    if(!name) return;
    const r2=await post('/api/flag',{name:name.trim()});
    if(!r2.ok){ say(r2.error); return }
    await reload();
    say(`${r2.name} is bit 0x${r2.bit.toString(16)} — test it with `
        +`TILE_FLAG_${r2.name.toUpperCase()}.`,false);
  };
  add.appendChild(nf);
  box.appendChild(add);
}

// One legend character, rewritten with some fields changed and the rest kept. Everything
// the manifest holds for it has to be resent, because the endpoint replaces the entry
// rather than patching it -- a partial write would silently drop the flags when you
// changed the flip.
async function writeLegend(ch,changes){
  const e=LEG()[ch], r=resolve(ch);
  // Rewritten in the table it already lives in. Sending no scope would move a map's own
  // character into the project table, which silently changes every other map.
  const body={char:ch, tile:e.tile, atlas:e.atlas||(r?r.atlas:null),
              flags:e.flags||[], flip:e.flip||[], rotate:!!e.rotate,
              map:scopeOf(ch)==='map'?S.map.name:null, ...changes};
  const res=await post('/api/legend',body);
  if(!res.ok){ say(res.error); tileInfo(); return }
  await reload();
  const at=(S.map.tiles||[]).findIndex(t=>t.ch===ch);
  selectTile(at>=0?at:S.ti, true);
  drawLegend(); draw();
}

// ------------------------------------------------------------------- the tile picker
//
// Every tile of every tileset the map draws from. Clicking one paints with it -- binding
// a legend character to it first if it does not have one yet, because "which tile" and
// "what does this tile mean" are the same decision and splitting them across two screens
// is what made the other 197 tiles of an atlas unreachable.

// Characters worth spending on a tile, in the order they get spent. Punctuation and
// digits first because they read as terrain in a rows block; letters after, where the
// case still distinguishes them. Space is excluded -- a rows block would not survive it.
const PICK_CHARS =
  ".,:;'\"!?*+-=/\\|<>()[]{}~^&%$@#0123456789"
  +"abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ";

function freeChar(){
  const taken=new Set(Object.keys(LEG()));
  return [...PICK_CHARS].find(c=>!taken.has(c))||null;
}

// The legend character already bound to this exact tile, flips AND rotate included. Two
// characters for one tile is legal and sometimes wanted -- the same slab as scenery and
// as a door -- but the picker should reuse rather than mint a duplicate nobody asked for.
function charFor(name,index,flip,rotate){
  const key=[...flip].sort().join('');
  return Object.keys(LEG()).find(ch=>{
    const r=resolve(ch);
    return r&&r.atlas===name&&r.index===index&&!!r.rotate===!!rotate
      &&[...r.flip].sort().join('')===key;
  })||null;
}

function pickFlip(){
  return ['x','y'].filter(a=>$('#pickflip'+a).checked);
}

function pickRotate(){
  return $('#pickrotate').checked;
}

// Where a packed tile came from in its own sheet -- the answer to "which of these 151
// near-identical green blobs is the one I'm looking for", which the picker otherwise has
// no way to say: it draws from the compiled blob, which carries pixels and nothing about
// where they were carved from. Fetched once per atlas and cached for the picker's open
// lifetime, not folded into mapAtlases()/reload(), which fire on nearly every edit and
// would each pay a full sheet re-read for a box nobody is currently hovering.
let ORIGIN_CACHE={};
async function showOrigin(atlasName,index){
  // Cached as the in-flight PROMISE, not its resolved value -- so two hovers landing
  // before the first fetch returns share one request instead of firing a second.
  if(!ORIGIN_CACHE[atlasName])
    ORIGIN_CACHE[atlasName]=post('/api/atlas/origin',{name:atlasName});
  const o=await ORIGIN_CACHE[atlasName];
  const wrap=$('#pickorigin');
  if(o.error||!o.origin||!o.origin[index]){ wrap.style.display='none'; return }
  const [px,py]=o.origin[index], [W,H]=o.sheet_size, T=o.tile_px;
  $('#originimg').src=o.thumb;
  const box=$('#originbox');
  box.style.left  =(100*px/W)+'%';
  box.style.top   =(100*py/H)+'%';
  box.style.width =(100*T/W)+'%';
  box.style.height=(100*T/H)+'%';
  $('#originnote').textContent=`${atlasName} — sheet position ${px},${py}px`;
  wrap.style.display='flex';
}

function drawTilePicker(){
  const body=$('#pickbody'); body.innerHTML='';
  $('#pickorigin').style.display='none';
  const list=mapAtlases();
  const flip=pickFlip(), rotate=pickRotate();
  if(!list.length){ body.innerHTML='<small>No atlas built yet — press Build.</small>'; return }

  for(const a of list){
    const h=document.createElement('div');
    h.className='palgroup';
    h.textContent=`${a.name} — ${a.tiles.length} tiles`
      +(a.metatiled?' (metatiled: cannot be flipped or rotated)':'');
    body.appendChild(h);

    const strip=document.createElement('div');
    strip.className='tiles';
    // A metatiled atlas cannot be drawn mirrored or rotated -- the runtime skips both for
    // composed tiles rather than mirroring/turning the quadrant order -- so its tiles are
    // shown upright whatever the checkboxes say, instead of previewing a build that fails.
    const use=a.metatiled?[]:flip, useRotate=a.metatiled?false:rotate;
    a.tiles.forEach((uri,i)=>{
      const bound=charFor(a.name,i,use,useRotate);
      const b=document.createElement('button');
      b.className='tile'+(bound&&bound===S.ch?' sel':'')+(bound?' used':'');
      const role=Object.keys(a.roles||{}).find(r=>a.roles[r]===i);
      b.title=`tile ${i} of ${a.name}`+(role?` — role "${role}"`:'')
        +(bound?` — painted as ${bound}`:' — click to give it a character');
      b.innerHTML=`<img src="${uri}" style="${flipCss(use,useRotate)}">`
        +`<b>${bound||(role?role.slice(0,4):i)}</b>`;
      b.onclick=()=>bindTile(a.name,i,use,useRotate,bound);
      // Right-click names the tile. A role is what game code calls it -- painting only
      // needs the index, but a door the game has to FIND needs a name, and that used to
      // mean hand-writing an [atlas.semantic] table.
      b.oncontextmenu=ev=>{ ev.preventDefault(); nameTile(a.name,i,role) };
      b.onmouseenter=()=>showOrigin(a.name,i);
      strip.appendChild(b);
    });
    body.appendChild(strip);
  }

  const free=freeChar();
  const turned=[flip.length?'mirrored '+flip.join(''):'',rotate?'rotated':'']
    .filter(Boolean).join(', ');
  $('#pickhint').innerHTML=free
    ? `click a tile to paint with it${turned?' ('+turned+')':''}`
      +` · <b>right-click</b> to name it for game code`
    : 'this map has used all 92 legend characters — free one up in the sidebar, or move '
      +'a character that only this map paints out of the project legend';
}

// A role, written into [atlas.semantic]. Named tiles are how C refers to a tile at all:
// TILE_DOOR rather than the number 47, which changes the next time the sheet is recarved.
async function nameTile(atlasName,index,current){
  const role=prompt(
    `Name tile ${index} of ${atlasName}.\n\n`
    +`Game code will call it ${atlasName.replace(/[^A-Za-z0-9]/g,'_').toUpperCase()}`
    +`_TILE_<NAME>. Lowercase letters, digits and underscores.`,
    current||'');
  if(role===null) return;
  const want=role.trim();
  if(!want){
    if(!current) return;
    const r=await post('/api/role/remove',{atlas:atlasName,role:current});
    if(!r.ok){ say(r.error); return }
    await reload(); drawTilePicker();
    say(`${current} is no longer a name in ${atlasName}.`,false);
    return;
  }
  const r=await post('/api/role',{atlas:atlasName,role:want,index});
  if(!r.ok){ say(r.error); return }
  await reload(); drawTilePicker();
  // Pinning an autopicked name is allowed -- it is how a prototype becomes chosen art --
  // but it moves the tile every map drawing through that role uses, so it is said out
  // loud rather than left to be noticed after the next build.
  say(r.pinned
    ? `"${want}" was autopicked and is now pinned to tile ${index} of ${atlasName}.`
      +` Every map drawing through it moves. Build to update the header.`
    : `Tile ${index} of ${atlasName} is now "${want}". Build to update the header.`,
    false);
}

// Clicking a tile either selects the character that already draws it, or mints one. The
// minting is the point: it is what makes a tile with no role paintable, and it writes the
// manifest rather than holding the binding in the page, so what you painted is what
// builds.
async function bindTile(name,index,flip,rotate,bound){
  if(bound){
    // Already in this map's table: select it rather than adding a second entry for the
    // same tile, which would paint identically and read as a duplicate in the palette.
    const at=(S.map.tiles||[]).findIndex(t=>t.ch===bound);
    selectTile(at>=0?at:S.ti); drawTilePicker(); return;
  }

  const ch=freeChar();
  if(!ch){ say('every legend character is taken.'); return }

  // Minted into THIS MAP's legend, not the project one. A character costs a slot out of
  // the printable set, and painting a decorative tile is a decision about one map -- so
  // spending a project-wide slot on it used up the same ninety for every other map in the
  // game. The project table keeps the characters that mean the same thing everywhere.
  //
  // The atlas is still named explicitly even when it is the map's first, because a
  // character that resolves by default would resolve against a different tileset if the
  // map's atlas list is ever reordered, and mean a different tile.
  const r=await post('/api/legend',
    {char:ch, tile:index, atlas:name, flags:[], flip:flip, rotate:rotate,
     map:S.map.name});
  if(!r.ok){ say(r.error); return }
  await reload();
  // The new character arrives in the map's tile table via reload(); select it there.
  const at=(S.map.tiles||[]).findIndex(t=>t.ch===ch);
  selectTile(at>=0?at:null);
  drawTilePicker();
  say(`${ch} now paints tile ${index} of ${name}.`);
}

// ---------------------------------------------------------------- the tileset list
//
// A map's `atlases` list, which the editor could never edit: the toolbar had one select,
// so a map drawing from three tilesets could only ever be told about the first.

function drawSets(){
  const body=$('#setbody'); body.innerHTML='';
  const chosen=(S.map.atlases||[]).slice();

  chosen.forEach((name,i)=>{
    const row=document.createElement('div');
    row.className='setrow';
    row.innerHTML=`<b>${name}</b><span class="grow dim">${i===0?'default':''}</span>`;
    const up=document.createElement('button');
    up.textContent='↑'; up.title='earlier in the id space'; up.disabled=i===0;
    up.onclick=()=>{ chosen.splice(i-1,0,chosen.splice(i,1)[0]); setAtlases(chosen) };
    const rm=document.createElement('button');
    rm.textContent='✕'; rm.title='stop drawing from this tileset';
    rm.disabled=chosen.length<2;
    rm.onclick=()=>{ chosen.splice(i,1); setAtlases(chosen) };
    row.append(up,rm);
    body.appendChild(row);
  });

  const rest=S.data.atlases.filter(a=>!chosen.includes(a.name));
  if(rest.length){
    const add=document.createElement('div');
    add.className='setrow';
    add.innerHTML='<span class="grow dim">add</span>';
    const sel=document.createElement('select');
    sel.innerHTML=rest.map(a=>`<option>${a.name}</option>`).join('');
    const go=document.createElement('button');
    go.textContent='＋';
    go.onclick=()=>setAtlases(chosen.concat([sel.value]));
    add.append(sel,go);
    body.appendChild(add);
  }

  // The order is not cosmetic and the note says so, because reordering silently
  // renumbers every cell in the map when it is rebuilt.
  $('#setnote').innerHTML=
    'Order fixes this map\'s tile id space, and the first tileset is what a legend '
    +'character with no <code>atlas</code> of its own resolves against. '
    +'Each one is a pool slot on the watch.';
}

function setAtlases(list){
  if(!list.length) return;
  S.map.atlases=list; S.map.atlas=list[0];
  S.dirty=true; mark();
  drawSets(); drawLegend(); info(); draw(); budget();
}
function drawPalettes(){
  $('#pals').innerHTML=S.data.palettes.map(p=>'<div class="pal">'+p.map(c=>
    c==='transparent'?'<div class="sw clear"></div>':
    `<div class="sw" style="background:${c}" title="${c}"></div>`).join('')+'</div>').join('');
}
// Budget is recomputed from UNSAVED editor state, not from the last build. Finding out
// a map blew the cap after six hours of work is the failure this exists to prevent, so
// the number has to move while the map is being painted.
let estTimer=null, estLast=null;

// The canvas the author is working on, and the reason it is that shape. Dimensions come
// from the server rather than being derived here, so there is one place that knows the
// display is 200x228 and which way round a landscape project turns it.
function orientation(){
  const o=S.data.orientation==='buttons_right'?'portrait':S.data.orientation;
  $('#orient').value=o;
  const [w,h]=S.data.screen;
  $('#orientnote').textContent=o==='portrait'
    ? `${w}×${h} canvas. Content is baked as drawn.`
    : `${w}×${h} canvas. Art, maps and glyphs are baked turned, so the watch draws them `
      +`the ordinary way round.`;
  const plate=$('#fscenetitle');
  if(plate) plate.innerHTML=`On the watch — ${w}&times;${h}`;
}

// Options come from the server (state().platforms), not a list typed into the page, so
// the seven never drift from PLATFORMS in pnx_editor.py. Built once: state() sends the
// same catalog on every reload, and rebuilding the dropdown's options would drop
// whatever an author had picked mid-session.
function platformSelector(){
  const sel=$('#checkplatform');
  if(sel.options.length<=1){
    for(const name of Object.keys(S.data.platforms)){
      const p=S.data.platforms[name], o=document.createElement('option');
      o.value=name;
      o.textContent=`${name} — ${p.w}×${p.h}${p.round?' round':''}, `
                   +(p.bw?'1-bit':'colour');
      sel.appendChild(o);
    }
  }
  sel.value=S.checkPlatform||'';
  platformNote();
}

// This is a VIEW into what is already built, not the project's own screen -- so it reads
// S.data.platforms rather than S.data.screen/orientation, which describe the project's
// actual default build and do not change when this selector does.
function platformNote(){
  const name=S.checkPlatform, note=$('#platformnote');
  if(!name){
    note.textContent='Prices what is on disk for the project’s own default build.';
    return;
  }
  const p=S.data.platforms[name];
  note.textContent=`${p.w}×${p.h}${p.round?' round':''} · `
    +`${p.bw?'1-bit (ships its ‘~bw’ blobs where built)':'colour'} · `
    +`${KB(p.resources)} resources · ${KB(p.ram)} RAM`;
}

function budget(now){
  clearTimeout(estTimer);
  const go=async()=>{
    // Maps are sent as they currently stand in the editor, including edits not yet
    // saved to the manifest.
    const maps={};
    for(const m of (S.data.maps||[])) maps[m.name]=m.rows.join('\n');
    let e;
    try{
      e=await (await fetch('/api/estimate',{method:'POST',
        headers:{'content-type':'application/json'},
        body:JSON.stringify({maps,platform:S.checkPlatform||undefined})})).json();
    }catch(_){ return }
    if(e.error) return;
    estLast=e;
    paintBudget(e);
  };
  if(now) go(); else estTimer=setTimeout(go,220);
}

// Exact bytes in the headline, rounded KB only in the supporting note. Every one of these
// ceilings is a cliff rather than a slope -- 65,535 is a uint16, not a guideline -- and
// "64 KB / 64 KB" cannot tell you whether you are 2 bytes under it or 40 over.
const B=n=>n.toLocaleString();
const KB=n=>n>=10240?`${(n/1024).toFixed(0)} KB`:`${n.toLocaleString()} B`;

// One cell of the strip. `pct` null means nothing has measured it -- which is drawn as a
// striped bar rather than an empty one, because an empty bar reads as "plenty of room"
// when what it means is "nobody knows".
function paintCell(id, value, pct, note, state){
  const cell=$('#bc-'+id), bar=$('#bm-'+id);
  cell.classList.toggle('over', state==='over');
  cell.classList.toggle('warnb', state==='warn');
  cell.classList.toggle('stale', state==='stale'||pct===null);
  bar.parentElement.classList.toggle('unknown', pct===null);
  bar.style.width=(pct===null?100:Math.min(100,pct))+'%';
  // Cleared rather than set when nothing is known, so the stripe defined in CSS shows
  // through: an inline colour would win, and a full solid bar reads as "100% spent",
  // which is the opposite of what an unmeasured cell means.
  bar.style.background=pct===null?'':
    (state==='over'?'var(--bad)':(state==='warn'?'#d08b2c':'var(--accent)'));
  $('#bv-'+id).textContent=value;
  $('#bn-'+id).innerHTML=note;
}

function paintBudget(e){
  // --- resources. The only one of the four that moves as you paint, which is why the
  //     estimate is recomputed from unsaved rows rather than read off disk.
  paintCell('res', `${B(e.total)} / ${B(e.budget)} B`, e.pct,
    `${e.pct.toFixed(1)}% of ${e.platform?e.platform+"'s":'the'} appstore cap`
    + (e.exact?'':' · <b>some assets not built yet</b>'),
    e.over?'over':(e.warn?'warn':''));

  const a=e.app||{};
  if(!a.known){
    paintCell('app', 'not measured', null,
      `${a.why||'no build yet'} · ceiling 65,535 B`, '');
    paintCell('ram', 'not measured', null, 'measured from the linked binary', '');
  }else{
    paintCell('app', `${B(a.used)} / ${B(a.limit)} B`, a.pct,
      a.stale
        ? '<b>stale — sources or assets changed since this build</b>'
        : `${a.pct.toFixed(1)}%`
          + ((a.modules||[])[0]
             ? ` · largest ${a.modules[0].name} ${KB(a.modules[0].bytes)}` : ''),
      a.over?'over':(a.warn?'warn':(a.stale?'stale':'')));

    // RAM is the heap left over, not the static size again: code, rodata, statics and
    // the heap all come out of one slot, so every byte of binary is a byte an arena
    // cannot have. That is the number an author sizes a scene against.
    if(a.slot){
      const usedPct=100*a.used/a.slot;
      paintCell('ram', `${B(a.heap)} B free`, usedPct,
        `heap left of the ${KB(a.slot)} ${a.platform} slot · ${B(a.mutable)} B `
        + `mutable statics`,
        a.heap<16384?'warn':'');
    }else{
      paintCell('ram', KB(a.mutable), null, 'mutable statics; slot size unknown', '');
    }
  }

  const s=e.save||{};
  paintCell('save', s.known?`${B(s.used)} / ${B(s.limit)} B`:'—',
    s.known?s.pct:null, s.known?'persisted per launch':(s.why||'not built yet'), '');

  // The status bar carries whichever ceiling is in the most trouble, since it is the one
  // signal that survives a collapsed strip and there is room for one number.
  const appPct=a.known?a.pct:0;
  const worst=appPct>e.pct
    ? {label:'app', used:a.used, pct:appPct, over:a.over, warn:a.warn}
    : {label:'resources', used:e.total, pct:e.pct, over:e.over, warn:e.warn};

  const st=$('#stbudget');
  if(st){
    st.textContent=`${worst.label} ${KB(worst.used)} (${worst.pct.toFixed(1)}%)`
      + (worst.over?' OVER BUDGET':'');
    st.style.fontWeight=worst.over?'700':'';
  }
  const bar=$('#statusbar');
  if(bar) bar.style.background=worst.over?'var(--bad)':
    (worst.warn?'#a8701f':'var(--accent)');
}
function tool(){
  const lbl=(S.map&&S.map.tiles&&S.ti!=null&&S.map.tiles[S.ti])
    ?(S.map.tiles[S.ti].ch!==undefined?S.map.tiles[S.ti].ch:'#'+S.ti):'—';
  $('#tool').innerHTML=S.mode==='paint'?`painting <kbd>${lbl}</kbd>`:
    S.mode==='warp'?'<kbd>click a door to add/remove a warp</kbd>':'<kbd>click to set start</kbd>';
}
function selectMap(i){
  // A project with no maps is an ordinary state -- a fresh one, or an example that is
  // only about audio -- and this used to throw on it: JSON.parse(JSON.stringify(undefined))
  // raises, at the last line of load(), leaving S.map null with every Maps control still
  // bound to it. The window came up and nothing in it did anything, which reads as a dead
  // editor rather than as an empty project.
  if(!S.data.maps.length || !S.data.maps[i]){ noMaps(); return }
  S.map=JSON.parse(JSON.stringify(S.data.maps[i]));
  // Normalised once, here, so nothing downstream has to know that `atlas` and `atlases`
  // are the same key spelled for one tileset or many.
  if(!S.map.atlases||!S.map.atlases.length)
    S.map.atlases=S.map.atlas?[S.map.atlas]:[];
  // The frame starts where the player does, which is the section an author is most
  // likely to want to look at first.
  if(!S.cam) S.cam={on:$('#camon').checked, x:0, y:0};
  const r=camRect();
  S.cam.x=Math.max(0,(S.map.start[0]+0.5)*S.T-r.w/2);
  S.cam.y=Math.max(0,(S.map.start[1]+0.5)*S.T-r.h/2);
  for(const id of ['#tilesets','#pick','#save']) $(id).disabled=false;
  S.dirty=false; mark(); drawLegend(); renderWarps(); warpForm(null); info(); draw();
  camInfo(); drawMapProps();
}
// What the Maps view shows when there is nothing to show. Says which of the two things is
// missing, because "add a map" is useless advice to a project with no tileset to draw one
// with -- the server refuses that, and refusing without saying so is how the old dead
// window felt.
function noMaps(){
  S.map=null; S.ch=null; S.ti=null; S.dirty=false; mark();
  const cv=$('#cv'); cv.width=cv.height=0;
  $('#legend').innerHTML='';
  $('#warps').innerHTML='<small>—</small>';
  $('#tileinfo').innerHTML='<small class="dim">—</small>';
  $('#mapinfo').innerHTML='<small class="dim">no maps yet</small>';
  $('#caminfo').textContent='—';
  $('#painthint').innerHTML = S.data.atlases.length
    ? 'This project has no maps. Name one below and press <b>＋ Map</b>.'
    : 'This project has no tilesets yet. Import a sheet on the <b>Atlas</b> tab, press '
      +'<b>Build</b>, then come back and add a map.';
  for(const id of ['#tilesets','#pick','#save']) $(id).disabled=true;
  $('#tool').textContent='';
}

// Every Maps control needs a map. They are re-enabled here rather than at each call site
// so a control added later cannot forget.
// Every Maps control needs a map, and a map is now cells rather than rows -- a
// `.pnxmap` has no rows at all, so testing for them would have declared every source map
// missing and greyed out the whole tab.
function haveMap(){ return !!(S.map && S.map.cells && S.map.cells.length) }

$('#camon').onchange=e=>{
  if(!S.cam) S.cam={on:true,x:0,y:0};
  S.cam.on=e.target.checked; if(haveMap()){ draw(); camInfo() }
};
$('#tilesets').onclick=()=>{ if(!haveMap())return; drawSets();
  $('#setwrap').style.display='flex' };
$('#setclose').onclick=()=>{ $('#setwrap').style.display='none' };
$('#pick').onclick=()=>{ if(!haveMap())return;
  ORIGIN_CACHE={};   // stale after a rebuild -- reset once per open, not per redraw
  drawTilePicker();
  $('#pickwrap').style.display='flex' };
$('#pickclose').onclick=()=>{ $('#pickwrap').style.display='none' };
$('#pickflipx').onchange=drawTilePicker;
$('#pickflipy').onchange=drawTilePicker;
$('#pickrotate').onchange=drawTilePicker;
// Clicking the backdrop closes; clicking the sheet must not. Overlays that swallow a
// misplaced click are the ones people stop trusting.
for(const id of ['#pickwrap','#setwrap'])
  $(id).onclick=e=>{ if(e.target===$(id)) $(id).style.display='none' };
function mark(){$('#dirty').textContent=S.dirty?'● unsaved':''}
// A warp needs a destination map and tile, so the form appears once a source tile is
// picked rather than asking through a chain of prompts.
function warpForm(at){
  const box=$('#warpfrom');
  if(!at){ box.textContent='pick a tile'; return }
  const others=S.data.maps.map(o=>o.name);
  box.innerHTML=`from (${at[0]},${at[1]}) →
    <select id="wto">${others.map(n=>`<option>${n}</option>`).join('')}</select>
    <input id="wx" type="number" value="1" min="0" title="destination x">
    <input id="wy" type="number" value="1" min="0" title="destination y">
    <button id="wadd">Add</button>`;
  $('#wadd').onclick=()=>{
    S.map.warps.push({at,to:[$('#wto').value,+$('#wx').value,+$('#wy').value]});
    S.dirty=true; S.mode='paint'; mark(); renderWarps(); info(); tool(); draw();
    warpForm(null);
  };
}

function renderWarps(){
  if(!haveMap()) return;
  const m=S.map;
  $('#warps').innerHTML = m.warps.length ? m.warps.map((w,i)=>
    `<div class="warp">(${w.at[0]},${w.at[1]}) → <b>${w.to[0]}</b> (${w.to[1]},${w.to[2]})
     <button data-i="${i}" title="remove">✕</button></div>`).join('')
    : '<small>none</small>';
  for(const b of document.querySelectorAll('#warps button'))
    b.onclick=()=>{S.map.warps.splice(+b.dataset.i,1);S.dirty=true;mark();
      renderWarps();info();draw()};
}

function info(){
  if(!haveMap()) return;
  const m=S.map;
  const sets=(m.atlases&&m.atlases.length?m.atlases:[m.atlas]).filter(Boolean);
  $('#mapinfo').innerHTML=`<small>${m.w}×${m.h} · `+
    `${sets.length>1?'tilesets':'tileset'} <b>${sets.join(', ')||'—'}</b> · `+
    `start (${m.start})<br>`+
    (m.warps.length?m.warps.map(w=>`warp (${w.at}) → ${w.to[0]} (${w.to[1]},${w.to[2]})`).join('<br>'):'no warps')+'</small>';
}

function draw(){
  // Tile images call this from onload, which can land after the view has moved to a
  // project or a state with no map.
  if(!haveMap()) return;
  const m=S.map,T=S.T,cv=$('#cv'),g=cv.getContext('2d');
  cv.width=m.w*T; cv.height=m.h*T;
  g.imageSmoothingEnabled=false;
  g.fillStyle='#000'; g.fillRect(0,0,cv.width,cv.height);
  for(let y=0;y<m.h;y++)for(let x=0;x<m.w;x++){
    const ti=m.cells[y*m.w+x], im=S.img[ti];
    if(!im||!im.complete) continue;
    // A flipped or rotated tile has to be drawn that way HERE too. The watch mirrors and
    // transposes it from bits in the cell, so an editor that drew it upright would be
    // showing a map that does not exist -- and a turned tile is placed precisely because
    // the turn is what you are looking at.
    const tt=m.tiles[ti]||{}, flip=[...(tt.flip||'')], rotate=!!tt.rotate;
    if(!flip.length&&!rotate){ g.drawImage(im,x*T,y*T,T,T); continue }
    const fx=flip.includes('x'), fy=flip.includes('y');
    // Once rotate is on, FLIP_X mirrors what is now the vertical axis and FLIP_Y the
    // horizontal -- see flipCss's own comment (and pack_atlas's transpose() in
    // tools/pnx_assets.py) for why the axes swap once the tile is transposed.
    const sx=(rotate?fy:fx)?-1:1, sy=(rotate?fx:fy)?-1:1;
    g.save();
    g.translate(x*T+(sx<0?T:0), y*T+(sy<0?T:0));
    g.scale(sx,sy);
    // Transpose LAST, so it is the first thing applied to the drawn image -- matching
    // pnx_gfx.c's rotate branch, which swaps rows/columns BEFORE flip_x/flip_y.
    if(rotate) g.transform(0,1,1,0,0,0);
    g.drawImage(im,0,0,T,T);
    g.restore();
  }
  // start marker and warps, drawn over the map so placement is checkable at a glance
  g.strokeStyle='#55aaff'; g.lineWidth=2;
  g.strokeRect(m.start[0]*T+1,m.start[1]*T+1,T-2,T-2);
  g.strokeStyle='#e0913f';
  for(const w of m.warps) g.strokeRect(w.at[0]*T+1,w.at[1]*T+1,T-2,T-2);
  drawCamera(g,cv);
}

// The device viewport, drawn over the map.
//
// A map is authored at whatever zoom fits the window; the watch shows 200x228 pixels of
// it. Those are not the same picture, and the gap is where "this room feels open" turns
// into a room the player sees a fifth of. Everything outside the frame is dimmed rather
// than hidden, because the point is to judge a section against its surroundings.
function camRect(){
  const a=atlas(), tile=(a&&a.tile)||16, scale=S.T/tile;
  const [sw,sh]=(S.data.screen||[200,228]);
  return {w:sw*scale, h:sh*scale, tile, scale};
}

function drawCamera(g,cv){
  if(!S.cam||!S.cam.on) return;
  const r=camRect();
  const x=Math.max(0,Math.min(S.cam.x, Math.max(0,cv.width-r.w)));
  const y=Math.max(0,Math.min(S.cam.y, Math.max(0,cv.height-r.h)));
  S.cam.x=x; S.cam.y=y;

  g.save();
  g.fillStyle='rgba(0,0,0,.55)';
  g.fillRect(0,0,cv.width,y);
  g.fillRect(0,y+r.h,cv.width,cv.height-(y+r.h));
  g.fillRect(0,y,x,r.h);
  g.fillRect(x+r.w,y,cv.width-(x+r.w),r.h);

  g.strokeStyle='#7fd1ff'; g.lineWidth=2;
  g.strokeRect(x+1,y+1,r.w-2,r.h-2);
  // Grab handle, so dragging the frame and painting inside it stay different gestures.
  g.fillStyle='#7fd1ff';
  g.fillRect(x,y,CAM_GRIP,CAM_GRIP);
  g.restore();
}

const CAM_GRIP=14;

function camHit(px,py){
  if(!S.cam||!S.cam.on) return false;
  return px>=S.cam.x && px<=S.cam.x+CAM_GRIP && py>=S.cam.y && py<=S.cam.y+CAM_GRIP;
}

function camInfo(){
  const r=camRect(), [sw,sh]=(S.data.screen||[200,228]);
  const tx=(S.cam.x/S.T), ty=(S.cam.y/S.T);
  $('#caminfo').innerHTML=S.cam.on
    ? `${sw}×${sh} px — ${(r.w/S.T).toFixed(1)}×${(r.h/S.T).toFixed(1)} tiles at `
      +`${r.tile}px · top-left tile ${tx.toFixed(1)}, ${ty.toFixed(1)}`
    : 'hidden';
}

// The camera grip is checked before painting: a drag that starts on it moves the frame,
// and anything else paints. Two gestures on one canvas, told apart by where the drag
// STARTED rather than by a mode -- switching modes to nudge a viewport is the kind of
// friction that means nobody nudges it.
let camDrag=null;

$('#cv').addEventListener('mousedown',e=>{
  const r=e.target.getBoundingClientRect();
  const px=e.clientX-r.left, py=e.clientY-r.top;
  if(camHit(px,py)){ camDrag={dx:px-S.cam.x, dy:py-S.cam.y}; e.preventDefault(); return }
  paint(e,true);
});
addEventListener('mousemove',e=>{
  if(!camDrag) return;
  const cv=$('#cv'), r=cv.getBoundingClientRect();
  S.cam.x=e.clientX-r.left-camDrag.dx;
  S.cam.y=e.clientY-r.top-camDrag.dy;
  draw(); camInfo();
});
addEventListener('mouseup',()=>{ camDrag=null });

$('#cv').addEventListener('mousemove',e=>{if(e.buttons&&!camDrag)paint(e,false)});
function paint(e,click){
  if(!haveMap()||S.ti==null) return;
  const r=e.target.getBoundingClientRect();
  const x=Math.floor((e.clientX-r.left)/S.T), y=Math.floor((e.clientY-r.top)/S.T);
  const m=S.map;
  if(x<0||y<0||y>=m.h||x>=m.w) return;

  if(S.mode==='start'){ if(!click)return; m.start=[x,y]; S.mode='paint'; }
  else if(S.mode==='warp'){
    if(!click)return;
    const i=m.warps.findIndex(w=>w.at[0]===x&&w.at[1]===y);
    if(i>=0){ m.warps.splice(i,1); S.mode='paint'; warpForm(null); }
    else warpForm([x,y]);
  } else {
    // A cell is an index into the map's tile table, which is what both authoring formats
    // reduce to -- so painting is the same operation whether the map is text or a file.
    const at=y*m.w+x;
    if(m.cells[at]===S.ti) return;
    m.cells[at]=S.ti;
  }
  S.dirty=true; mark(); info(); tool(); draw(); budget();
}

addEventListener('keydown',e=>{
  if(e.target.tagName==='SELECT')return;
  // Escape closes whichever overlay is open before it touches the paint mode, so the
  // key that means "get me out of this" does.
  if(e.key==='Escape'){
    for(const id of ['#pickwrap','#setwrap']){
      if($(id).style.display!=='none'){ $(id).style.display='none'; return }
    }
  }
  if(!haveMap()) return;
  if(e.key==='w'||e.key==='W'){S.mode='warp';tool()}
  if(e.key==='s'||e.key==='S'){S.mode='start';tool()}
  if(e.key==='Escape'){S.mode='paint';tool()}
});
$('#mapsel').onchange=e=>{
  if(S.dirty&&!confirm('Discard unsaved changes to this map?')){
    e.target.value=S.data.maps.findIndex(m=>m.name===(S.map&&S.map.name)); return;
  }
  selectMap(+e.target.value);
};
$('#save').onclick=async()=>{
  if(!haveMap()) return;
  const body=JSON.parse(JSON.stringify(S.map));
  if(body.format!=='source'){
    // A text map is stored as characters, so the cell grid is rendered back through each
    // tile's own character -- which is why the table carries it. Reassigning characters
    // here instead would churn the whole map's diff every time it was opened.
    const missing=body.tiles.findIndex(t=>t.ch===undefined);
    if(missing>=0){
      say(`tile #${missing} has no legend character, so this map cannot be saved as text.`);
      return;
    }
    body.rows=[];
    for(let y=0;y<body.h;y++){
      let row='';
      for(let x=0;x<body.w;x++) row+=body.tiles[body.cells[y*body.w+x]].ch;
      body.rows.push(row);
    }
  }
  const r=await (await fetch('/api/map',{method:'POST',
    headers:{'content-type':'application/json'},body:JSON.stringify(body)})).json();
  const log=$('#log');
  log.className=r.ok?'ok':'bad';
  log.textContent=r.ok
    ?`Saved ${S.map.name} to ${S.map.format==='source'?S.map.source:'the manifest'}.`
    :r.error;
  if(r.ok){S.dirty=false;mark()}
};
$('#newmap').onclick=async()=>{
  const name=$('#nmname').value.trim();
  if(!name){alert('Name the map first.');return}
  const r=await (await fetch('/api/newmap',{method:'POST',
    headers:{'content-type':'application/json'},
    body:JSON.stringify({name,w:+$('#nmw').value,h:+$('#nmh').value,
      // The tileset the map being looked at uses, which is nearly always the one a new
      // map next to it wants -- and the only one whose legend characters are certain to
      // resolve, so the blank room it comes with is paintable.
      atlas:(S.map&&S.map.atlases&&S.map.atlases[0])
            ||(S.data.atlases[0]&&S.data.atlases[0].name)})})).json();
  const log=$('#log'); log.className=r.ok?'ok':'bad';
  if(!r.ok){log.textContent=r.error;return}
  log.textContent=`Created map "${name}" and a scene for it. Press Build.`;
  $('#nmname').value='';
  await load();
  const i=S.data.maps.findIndex(m=>m.name===name);
  $('#mapsel').value=i; selectMap(i);
};
// Map properties. Written on change rather than behind a Save, matching the legend and
// the scene panel: every one of these goes straight into the manifest.
function drawMapProps(){
  if(!S.map||!$('#mppal')) return;
  const m=S.map;
  $('#mppal').innerHTML='<option value="">— none —</option>'
    +(m.palettes||[]).map(p=>`<option${p===m.palette?' selected':''}>${p}</option>`)
      .join('');
  $('#mppal').value=m.palette||'';
  $('#mpwt').value=String(m.worldtile==null?'auto':m.worldtile);
  $('#mpslots').value=m.atlas_slots==null?'':m.atlas_slots;
  $('#mpbank').value=m.bank_bytes==null?'':m.bank_bytes;
  $('#mpres').checked=!!m.resident;
  $('#mplog').className='dim';
  $('#mplog').textContent=(m.palettes&&m.palettes.length)?'':
    'no palette variants on this map’s tilesets';
}

async function writeMapProps(changes){
  if(!S.map) return;
  const r=await post('/api/map/props',{name:S.map.name,...changes});
  const log=$('#mplog');
  if(!r.ok){ log.className='bad'; log.textContent=r.error; drawMapProps(); return }
  await load();
  const i=S.data.maps.findIndex(x=>x.name===S.map.name);
  if(i>=0) selectMap(i);
  log.className='ok'; log.textContent='Saved. Press Build.';
  budget(true);
}

// An empty box means "unset", which is how a map goes back to the pipeline's own choice
// -- distinct from a number, and the reason these send "" rather than omitting the key.
$('#mppal').onchange =()=>writeMapProps({palette:$('#mppal').value});
$('#mpwt').onchange  =()=>writeMapProps({worldtile:$('#mpwt').value});
$('#mpslots').onchange=()=>writeMapProps({atlas_slots:$('#mpslots').value});
$('#mpbank').onchange =()=>writeMapProps({bank_bytes:$('#mpbank').value});
$('#mpres').onchange  =()=>writeMapProps({resident:$('#mpres').checked});

// Moving a map out of the manifest and into its own file. Offered rather than done for
// you, and never in reverse automatically: the trade is real in both directions -- a file
// gets you the tile ceiling and the manifest back, and costs you a readable git diff.
$('#cvtmap').onclick=async()=>{
  if(!S.map){ say('No map selected.'); return }
  if(S.map.format==='source'){
    say(`"${S.map.name}" already lives in ${S.map.source}.`); return;
  }
  if(S.dirty){ say('Save this map first — converting reads what is on disk.'); return }
  if(!confirm(`Move "${S.map.name}" into its own .pnxmap file?\n\n`
      +`Its grid, start and warps leave the manifest. Tilesets, palette and streaming `
      +`settings stay.\n\nYou gain the ~90-tile ceiling and a smaller manifest. You `
      +`lose a readable diff on map changes.`)) return;
  const r=await post('/api/map/migrate',{name:S.map.name});
  if(!r.ok){ say(r.error); return }
  await load();
  const i=S.data.maps.findIndex(m=>m.name===S.map.name);
  if(i>=0){ $('#mapsel').value=i; selectMap(i) }
  say(`Moved to ${r.source} — ${r.tiles} tile entries, ${r.bytes} B.`,false);
};

$('#renmap').onclick=async()=>{
  if(!S.map){ say('No map selected.'); return }
  const to=prompt(`Rename map "${S.map.name}" to:\n\n`
    +`Warps aimed at it and scenes that load it are updated too. `
    +`Lowercase letters, digits and underscores.`, S.map.name);
  if(!to||to.trim()===S.map.name) return;
  const r=await post('/api/map/rename',{name:S.map.name, to:to.trim()});
  if(!r.ok){ say(r.error); return }
  await load();
  const i=S.data.maps.findIndex(m=>m.name===to.trim());
  $('#mapsel').value=i; selectMap(i);
  say(`Renamed to "${to.trim()}". Press Build.`,false);
};

// Asks what still points at the map BEFORE confirming, so the dialog says "the cave warps
// to it" rather than offering a delete that is then refused -- the same shape as removing
// an atlas.
$('#delmap').onclick=async()=>{
  if(!S.map){ say('No map selected.'); return }
  const name=S.map.name;
  const u=await post('/api/map/users',{name});
  if(u.users&&u.users.length){
    say(`Cannot remove "${name}" — ${u.users.join('; ')}.`);
    return;
  }
  if(!confirm(`Remove map "${name}"?\n\n`
              +`Nothing points at it. Its rows and its own legend go with it.`)) return;
  const r=await post('/api/map/remove',{name});
  if(!r.ok){ say(r.error); return }
  await load();
  $('#mapsel').value=0; selectMap(0);
  say(`Removed "${name}". Press Build.`,false);
};

// Changing this changes what the pipeline BAKES -- every atlas, sprite, map and glyph
// comes out turned -- so the resources on disk are stale the moment it is picked. Saying
// so beats letting someone wonder why the preview and the watch disagree.
$('#orient').onchange=async()=>{
  const r=await (await fetch('/api/orientation',{method:'POST',
    headers:{'content-type':'application/json'},
    body:JSON.stringify({orientation:$('#orient').value})})).json();
  const log=$('#log'); log.className=r.ok?'ok':'bad';
  if(!r.ok){log.textContent=r.error;return}
  log.textContent='Orientation set. Press Build — every asset is baked turned, so the '
                 +'resources on disk are now stale.';
  const keep=S.map&&S.map.name; await load();
  const i=Math.max(0,S.data.maps.findIndex(m=>m.name===keep));
  $('#mapsel').value=i; selectMap(i);
};

// A VIEW setting, unlike orientation: picking a platform here re-prices what is already
// on disk against a different target, it never touches the manifest or the pipeline.
$('#checkplatform').onchange=()=>{
  S.checkPlatform=$('#checkplatform').value;
  platformNote();
  budget(true);
};
$('#build').onclick=async()=>{
  const log=$('#log'); log.className=''; log.textContent='Building…';
  const r=await (await fetch('/api/build',{method:'POST'})).json();
  log.className=r.ok?'ok':'bad'; log.textContent=r.output.trim()||'(no output)';
  if(r.ok){ const keep=S.map.name; await load();
    const i=S.data.maps.findIndex(m=>m.name===keep);
    $('#mapsel').value=i; selectMap(i); }
  // A build is when the estimate stops being an estimate: every blob it guessed at now
  // exists on disk. Repaint immediately rather than on the next keystroke.
  budget(true);
};

// A real, shippable .pbw -- what Build (above) never produced, since it only ever ran
// the asset pipeline. Reuses the shared Output panel Build already writes into, and
// un-hides it if it was collapsed: this result is worth seeing even if the panel was
// tucked away for something else.
let packagePoll=null;
$('#package').onclick=async()=>{
  const log=$('#log');
  $('#package').disabled=true;
  log.className=''; log.textContent='Packaging…';
  const r=await post('/api/package',{});
  if(!r.ok){
    $('#package').disabled=false;
    log.className='bad'; log.textContent=r.error||'Could not start packaging.';
    return;
  }
  if(!packagePoll) packagePoll=setInterval(packageStatus,700);
};

async function packageStatus(){
  let s;
  try{ s=await (await fetch('/api/package/status')).json() }catch(_){ return }
  const log=$('#log');
  log.textContent=(s.log||'').trim()||'Packaging…';
  if(s.busy) return;
  clearInterval(packagePoll); packagePoll=null;
  $('#package').disabled=false;
  if($('#outpanel').classList.contains('hidden')){
    $('#outpanel').classList.remove('hidden'); $('#outtoggle').textContent='Hide';
  }
  if(s.result&&s.result.ok){
    log.className='ok';
    log.textContent=`Packaged: ${s.result.path}  (${KB(s.result.size)})\n\n`
      +'Install it on a real watch through the Pebble app\'s Developer Connection '
      +'(see Device), or submit it to the Rebble appstore: https://apps.rebble.io/dev\n\n'
      +log.textContent;
  }else{
    log.className='bad';
    log.textContent=((s.result&&s.result.error)||'Packaging failed.')+'\n\n'+log.textContent;
  }
}
// ------------------------------------------------------------------ import view
let sheets=[];
function showTab(which){
  const imp=which==='import', fnt=which==='fonts', maps=which==='maps',
        sdk=which==='sdk', pix=which==='pixel', cod=which==='code',
        scn=which==='scenes', dlg=which==='dialog', mus=which==='music',
        dev=which==='device', proj=which==='project';
  $('#import').style.display=imp?'block':'none';
  $('#fonts').style.display=fnt?'block':'none';
  $('#sdk').style.display=sdk?'block':'none';
  $('#project').style.display=proj?'block':'none';
  $('#pixel').style.display=pix?'block':'none';
  $('#code').style.display=cod?'block':'none';
  $('#scenes').style.display=scn?'block':'none';
  $('#dialog').style.display=dlg?'block':'none';
  $('#music').style.display=mus?'block':'none';
  $('#device').style.display=dev?'block':'none';
  if(mus) drawMusic();
  // Leaving the tab with a pattern still playing would keep the audio going against a
  // grid nobody can see any more -- stopped rather than left to run out on its own.
  else if(typeof stopPatternPlayback==='function') stopPatternPlayback();
  if(scn) drawScenes();
  if(dlg) drawDialog();
  if(fnt) drawFontList();
  // sdkStatus() renders both halves of what used to be one Settings section -- the
  // project summary/recent list now on the Project tab, and the actual SDK/engine
  // status still on Settings -- in one pass, so both tabs call it rather than the
  // function being split along the same line the tabs were.
  if(proj) sdkStatus();
  if(sdk){ sdkStatus(); updCheck(); drawProject() }
  if(pix&&!PX.data){ pxPalette(); pxInit(+$('#pxw').value,+$('#pxh').value,1); pxLoadList() }
  if(cod&&!$('#codelist').children.length) codeTree();
  if(dev) emuEnter(); else emuLeave();
  $('#stage').style.display=maps?'flex':'none';
  // The map sidebar and its toolbar controls belong to Maps. Showing them elsewhere
  // implies they apply there.
  $('#side').style.display=maps?'':'none';
  $('#ctxbar').style.display=maps?'':'none';
  $('#save').style.display=maps?'':'none';
  if(imp){ if(!sheets.length) loadSheets(); atlasMode() }
  if(fnt&&!fontSources.length) loadFonts();
  // The strip spans every tab, so it re-reads on arrival: importing an atlas or adding a
  // font spends the same budget a map edit does, and a number that only refreshed while
  // painting would be a number nobody could trust anywhere else.
  budget(true);

  for(const b of document.querySelectorAll('.act'))
    b.classList.toggle('on', b.dataset.t===which);
  $('#ctxtitle').textContent={maps:'Maps',import:'Atlas',fonts:'Fonts',
    pixel:'Sprites',code:'Code',sdk:'Settings',scenes:'Scenes',
    dialog:'Dialog',music:'Music',device:'Device',project:'Project'}[which]||'';
}

// ------------------------------------------------------- declaring a sprite
//
// The Sprites tab could paint a PNG and not declare it, so the art existed and nothing
// could load it. Frames are derived from a width, a height and a count rather than typed
// as rectangles: a sheet is frames stacked vertically, which is the layout both the
// painter above and `[[sprite]] frames` already assume.

// Frames picked off a sheet, in click order. Null means "none picked", which is different
// from an empty list and is why this is not just an array: with no picks the declare form
// falls back to deriving a vertical stack from the canvas size, which is what it did
// before and is still right for art painted here.
const SH={cells:null, picks:[], sheet:null};

function spFrames(){
  if(SH.picks.length) return SH.picks.map(i=>{
    const c=SH.cells[i]; return [c.x,c.y,c.w,c.h];
  });
  const w=+$('#spfw').value, h=+$('#spfh').value, n=+$('#spn').value;
  return Array.from({length:n},(_,i)=>[0,i*h,w,h]);
}

function shLog(msg,bad){
  const el=$('#shlog');
  el.className=bad===false?'ok':(bad?'bad':'');
  el.textContent=msg;
}

function drawSheetGrid(){
  const grid=$('#shgrid'); grid.innerHTML='';
  if(!SH.cells){ $('#shplate').style.display='none'; return }
  $('#shplate').style.display='';
  $('#shcount').textContent=`${SH.cells.length} cells · ${SH.picks.length} picked`;
  SH.cells.forEach((c,i)=>{
    const at=SH.picks.indexOf(i);
    const b=document.createElement('button');
    b.className='tile'+(at>=0?' sel':'')+(c.blank?' used':'');
    b.title=`${c.x},${c.y} ${c.w}x${c.h}`+(c.blank?' — blank':'')
      +(at>=0?` — frame ${at}`:'');
    b.innerHTML=`<img src="${c.img}" alt="">`
      +`<b>${at>=0?at:(c.blank?'·':'')}</b>`;
    b.onclick=()=>{
      const k=SH.picks.indexOf(i);
      if(k>=0) SH.picks.splice(k,1); else SH.picks.push(i);
      drawSheetGrid();
      // The declare form's size boxes follow the picks, so what is about to be written
      // and what is on screen cannot disagree.
      if(SH.picks.length){
        $('#spfw').value=c.w; $('#spfh').value=c.h; $('#spn').value=SH.picks.length;
      }
      shLog(SH.picks.length?`${SH.picks.length} frame(s) picked — they become anim `
            +`indices 0..${SH.picks.length-1}.`
            :'Click frames in the order the animation plays.');
    };
    // Editing one pose out of a sheet, which the canvas could not do: it opened whole
    // files, so touching one frame of an eight-pose sheet meant loading all eight.
    b.ondblclick=async ev=>{
      ev.preventDefault();
      const r=await post('/api/frame/read',
        {sheet:SH.sheet, x:c.x, y:c.y, w:c.w, h:c.h});
      if(r.error){ shLog(r.error,true); return }
      PX.w=r.w; PX.h=r.h; PX.frames=1; PX.frame=0;
      PX.data=Uint8Array.from(r.pixels); PX.undo=[]; PX.redo=[];
      PX.origin={sheet:SH.sheet, x:c.x, y:c.y};
      $('#pxw').value=r.w; $('#pxh').value=r.h;
      $('#pxtitle').textContent=`Canvas — ${SH.sheet} @ ${c.x},${c.y}`;
      $('#pxnote').textContent=`Editing one frame. Save writes it back into the sheet.`;
      pxDraw();
      shLog(`Editing frame at ${c.x},${c.y}.`,false);
    };
    grid.appendChild(b);
  });
}

$('#shslice').onclick=async()=>{
  const sheet=$('#shsheet').value;
  if(!sheet){ shLog('No PNG in the project to slice.',true); return }
  const r=await post('/api/sheet/frames',{sheet, fw:+$('#shfw').value, fh:+$('#shfh').value,
    ox:+$('#shox').value, oy:+$('#shoy').value,
    gx:+$('#shgx').value, gy:+$('#shgy').value});
  if(r.error){ shLog(r.error,true); return }
  SH.cells=r.cells; SH.picks=[]; SH.sheet=sheet;
  $('#spsheet').value=sheet;
  drawSheetGrid();
  shLog(`${r.cols}x${r.rows} of ${$('#shfw').value}x${$('#shfh').value}`
    +(r.capped?` — showing the first ${r.limit}`:'')
    +'. Click frames in play order.');
};

$('#shclear').onclick=()=>{
  SH.picks=[]; drawSheetGrid();
  shLog('Picks cleared — the declare form is back to a vertical stack.');
};

function spLog(msg,bad){
  const el=$('#splog');
  el.className=bad===false?'ok':(bad?'bad':'');
  el.textContent=msg||'—';
}

function drawSpriteForm(){
  const sel=$('#spsel'), cur=sel.value;
  const list=(S.data&&S.data.sprites)||[];
  sel.innerHTML='<option value="">— new —</option>'
    +list.map(s=>`<option${s.name===cur?' selected':''}>${s.name}</option>`).join('');
  const sheets=$('#spsheet');
  const arts=(S.art||[]).map(a=>a.path);
  sheets.innerHTML=arts.map(p=>`<option>${p}</option>`).join('');
  // The strip follows the selection through a reload, so saving a declaration does not
  // silently leave the frames of the sprite that was selected before it.
  if(typeof spShowFrames==='function') spShowFrames(sel.value);
}

// Loading an existing sprite back into the form. Frame rectangles collapse to w/h/count
// only when they really are a vertical stack; anything else is left to the manifest
// rather than silently rewritten into a shape it never had.
// tools/pnx_assets.py's pack_sprite derives a variant's NAME from its path the same way:
// basename, extension stripped. Kept in step so the dropdown offers exactly the names the
// pipeline would actually accept for bw_variant.
function spVariantName(path){
  return path.split('/').pop().replace(/\.[^.]*$/,'');
}

$('#spsel').onchange=()=>{
  const name=$('#spsel').value;
  if(!name){ spLog(''); return }
  const s=(S.data.sprites||[]).find(x=>x.name===name);
  if(!s) return;
  $('#spname').value=s.name;
  $('#spsheet').value=s.sheet;
  const f=s.frames||[];
  $('#spfw').value=s.w||16; $('#spfh').value=s.h||24; $('#spn').value=f.length||1;
  const stacked=f.every((r,i)=>r[0]===0&&r[1]===i*(s.h||0)&&r[2]===s.w&&r[3]===s.h);
  // The anim map is name -> frame index; the box takes them in frame order.
  const byIndex=[];
  for(const [k,v] of Object.entries(s.anim||{})) byIndex[v]=k;
  $('#spanim').value=byIndex.filter(Boolean).join(',');
  spLog(stacked?'':'frames are not a vertical stack — saving will rewrite them as one',
        stacked?null:true);
  const bw=$('#spbw'), names=(s.variants||[]).map(spVariantName);
  bw.innerHTML='<option value="">— base —</option>'
    +names.map(n=>`<option${n===s.bw_variant?' selected':''}>${n}</option>`).join('');
  bw.disabled=!names.length;
  spShowFrames(name);
};

// The declared sprite's frames, opened straight into the canvas.
//
// Rects come from the manifest rather than from a slicer, which is the whole point: the
// canvas used to guess the frame split from file height against whatever height it
// happened to be showing, so a 24x144 sheet opened as six frames of 24x24 while the
// declaration next to it said four of 24x36.
//
// Clicking one sets PX.origin, so Save composites that pose back into the sheet at its own
// rect and leaves the others alone -- the path the sheet slicer already proved.
async function spShowFrames(name){
  const box=$('#spframes'), log=$('#spframelog');
  box.innerHTML='';
  if(!name){ log.className='dim'; log.textContent='Pick a sprite to open its frames.'; return }

  const r=await post('/api/sprite/frames',{name});
  if(r.error){ log.className='bad'; log.textContent=r.error; return }

  r.cells.forEach(c=>{
    const b=document.createElement('button');
    b.className='cell'+(c.blank?' blank':'');
    b.title=`frame ${c.i} — ${c.w}x${c.h} at ${c.x},${c.y}\nclick to edit it`;
    b.innerHTML=`<img src="${c.img}" alt=""><span>${c.i}</span>`;
    b.onclick=async()=>{
      const f=await post('/api/frame/read',{sheet:r.sheet,x:c.x,y:c.y,w:c.w,h:c.h});
      if(f.error){ log.className='bad'; log.textContent=f.error; return }
      PX.w=f.w; PX.h=f.h; PX.frames=1; PX.frame=0;
      PX.data=Uint8Array.from(f.pixels); PX.undo=[]; PX.redo=[];
      PX.origin={sheet:r.sheet, x:c.x, y:c.y};
      $('#pxw').value=f.w; $('#pxh').value=f.h;
      $('#pxtitle').textContent=`Canvas — ${name} frame ${c.i}`;
      $('#pxnote').textContent='Editing one frame. Save writes it back into the sheet.';
      pxDraw();
      log.className='dim';
      log.textContent=`Editing ${name} frame ${c.i} (${c.w}x${c.h} at ${c.x},${c.y}).`;
      // The canvas is above the fold on a watch-sized window; scrolling to it is the
      // difference between "nothing happened" and "it opened".
      $('#pxcv').scrollIntoView({block:'nearest'});
    };
    box.appendChild(b);
  });

  // A frame hanging off its sheet is reported rather than hidden: it is exactly what a
  // re-imported, smaller sheet looks like, and it will fail the build later with less
  // context than this.
  if(r.out_of_bounds && r.out_of_bounds.length){
    log.className='bad';
    log.textContent=`frame(s) ${r.out_of_bounds.join(', ')} run past `
      +`${r.sheet} (${r.sheet_size[0]}x${r.sheet_size[1]}) and cannot be opened.`;
  }else{
    log.className='dim';
    log.textContent=`${r.cells.length} frame(s) of ${r.sheet}. Click one to edit it.`;
  }
}

$('#spsave').onclick=async()=>{
  const name=$('#spname').value.trim();
  if(!name){ spLog('Name the sprite first.',true); return }
  const sheet=$('#spsheet').value;
  if(!sheet){ spLog('No PNG in the project to point at. Paint and save one first.',true);
              return }
  const names=$('#spanim').value.split(',').map(s=>s.trim()).filter(Boolean);
  const anim={};
  names.forEach((n,i)=>{ anim[n]=i });

  const frames=spFrames();
  // Frames picked off the sheet win over the vertical stack, and they carry their own
  // sheet with them: picking poses out of one file and declaring them against another
  // would validate and then draw the wrong art.
  const useSheet=SH.picks.length?SH.sheet:sheet;
  // variants/colorkey have no edit control of their own in this panel -- carried through
  // from what is already declared so saving a name/frame change does not silently drop
  // them. bw_variant DOES have a control (#spbw), and depends on variants surviving this.
  const existing=(S.data.sprites||[]).find(x=>x.name===$('#spsel').value)||{};
  const variants=existing.variants||[];
  const colorkey=existing.colorkey||null;
  const bwVariant=$('#spbw').value||null;
  // Validated through the real pack_sprite before anything is written, the same way an
  // atlas carve is: a frame running off the sheet used to go into the manifest and only
  // fail when Build was pressed, leaving a broken block to remove by hand.
  const v=await post('/api/sprite/validate',
    {name,sheet:useSheet,frames,anim,variants,colorkey,bw_variant:bwVariant});
  if(!v.ok){ spLog(v.error,true); return }

  const r=await post('/api/sprite/save',
    {name,sheet:useSheet,frames,anim,variants,colorkey,bw_variant:bwVariant});
  if(!r.ok){ spLog(r.error,true); return }
  await load(); drawSpriteForm(); $('#spsel').value=name;
  spLog(`Saved "${name}" — ${v.frames} frame(s) of ${v.w}x${v.h}`
    +`${SH.picks.length?' picked from '+useSheet:''}. Press Build.`,false);
  budget(true);
};

$('#spdel').onclick=async()=>{
  const name=$('#spsel').value||$('#spname').value.trim();
  if(!name){ spLog('Pick a sprite to remove.',true); return }
  const u=await post('/api/sprite/users',{name});
  if(u.users&&u.users.length){
    spLog(`Cannot remove "${name}" — ${u.users.join(', ')} loads it.`,true); return;
  }
  if(!confirm(`Remove sprite "${name}" from the manifest?\n\n`
              +`Nothing loads it. The PNG on disk is left alone.`)) return;
  const r=await post('/api/sprite/remove',{name});
  if(!r.ok){ spLog(r.error,true); return }
  await load(); drawSpriteForm(); $('#spsel').value='';
  spLog(`Removed "${name}". Press Build.`,false);
  budget(true);
};

// ----------------------------------------------------------------- 9-slice panels
//
// A panel is a sprite with one frame plus four border insets (tools/pnx_assets.py's
// pack_nine_slice) -- corners drawn once, edges/centre tiled to fill any box. The form
// mirrors the sprite one throughout; the preview below it is the one thing sprites never
// needed, since "does the tiling look right" only answers itself at a size nobody drew
// art for.

function nsLog(msg,bad){
  const el=$('#nslog');
  el.className=bad===false?'ok':(bad?'bad':'');
  el.textContent=msg||'—';
}

function drawNineSliceForm(){
  const sel=$('#nssel'), cur=sel.value;
  const list=(S.data&&S.data.nine_slices)||[];
  sel.innerHTML='<option value="">— new —</option>'
    +list.map(n=>`<option${n.name===cur?' selected':''}>${n.name}</option>`).join('');
  const sheets=$('#nssheet');
  const arts=(S.art||[]).map(a=>a.path);
  sheets.innerHTML=arts.map(p=>`<option>${p}</option>`).join('');
  nsPreview();
}

$('#nssel').onchange=()=>{
  const name=$('#nssel').value;
  if(!name){ nsLog(''); return }
  const n=(S.data.nine_slices||[]).find(x=>x.name===name);
  if(!n) return;
  $('#nsname').value=n.name;
  $('#nssheet').value=n.sheet;
  const r=n.rect||[0,0,0,0];
  $('#nsrx').value=r[0]; $('#nsry').value=r[1]; $('#nsrw').value=r[2]; $('#nsrh').value=r[3];
  const b=n.border&&n.border.length===4?n.border:[4,4,4,4];
  $('#nsbl').value=b[0]; $('#nsbt').value=b[1]; $('#nsbr').value=b[2]; $('#nsbb').value=b[3];
  nsLog('');
  nsPreview();
};

// The rect fields, read as "0,0,0,0 means the whole sheet" -- entering a real w/h is what
// opts into a sub-rect, so a panel that IS the whole PNG never has to restate its size.
function nsRect(){
  const x=+$('#nsrx').value, y=+$('#nsry').value, w=+$('#nsrw').value, h=+$('#nsrh').value;
  return (w>0&&h>0) ? [x,y,w,h] : null;
}
function nsBorder(){
  return [+$('#nsbl').value, +$('#nsbt').value, +$('#nsbr').value, +$('#nsbb').value];
}

// Debounced: every keystroke in a border field would otherwise fire a request per
// keystroke, and the preview is a nice-to-have that should never make typing feel slow.
let nsPreviewTimer=null;
function nsPreview(){
  clearTimeout(nsPreviewTimer);
  nsPreviewTimer=setTimeout(nsPreviewNow, 150);
}
async function nsPreviewNow(){
  const sheet=$('#nssheet').value;
  const img=$('#nspreviewimg'), log=$('#nsprevlog');
  if(!sheet){ img.removeAttribute('src'); log.textContent='Pick a sheet to preview.'; return }
  const r=await post('/api/nine_slice/preview',
    {sheet, border:nsBorder(), rect:nsRect(),
     test_w:+$('#nstw').value, test_h:+$('#nsth').value});
  if(r.error){ log.className='bad'; log.textContent=r.error; return }
  img.src=r.img;
  log.className='dim';
  log.textContent=`${r.panel_w}x${r.panel_h} panel, tiled to ${r.w}x${r.h}.`;
}
for(const id of ['nssheet','nsrx','nsry','nsrw','nsrh','nsbl','nsbt','nsbr','nsbb',
                 'nstw','nsth']){
  $('#'+id).addEventListener('input', nsPreview);
}

$('#nssave').onclick=async()=>{
  const name=$('#nsname').value.trim();
  if(!name){ nsLog('Name the panel first.',true); return }
  const sheet=$('#nssheet').value;
  if(!sheet){ nsLog('No PNG in the project to point at.',true); return }
  const border=nsBorder(), rect=nsRect();
  const existing=(S.data.nine_slices||[]).find(x=>x.name===$('#nssel').value)||{};
  const colorkey=existing.colorkey||null;
  // Validated through the real pack_nine_slice before anything is written, the same
  // bargain the sprite form makes.
  const v=await post('/api/nine_slice/validate',{name,sheet,border,rect,colorkey});
  if(!v.ok){ nsLog(v.error,true); return }

  const r=await post('/api/nine_slice/save',{name,sheet,border,rect,colorkey});
  if(!r.ok){ nsLog(r.error,true); return }
  await load(); drawNineSliceForm(); $('#nssel').value=name;
  nsLog(`Saved "${name}" — ${v.w}x${v.h} panel, border ${v.border.join('/')}. Press Build.`,
        false);
  budget(true);
};

$('#nsdel').onclick=async()=>{
  const name=$('#nssel').value||$('#nsname').value.trim();
  if(!name){ nsLog('Pick a panel to remove.',true); return }
  const u=await post('/api/nine_slice/users',{name});
  if(u.users&&u.users.length){
    nsLog(`Cannot remove "${name}" — ${u.users.join(', ')} loads it.`,true); return;
  }
  if(!confirm(`Remove panel "${name}" from the manifest?\n\n`
              +`Nothing loads it. The PNG on disk is left alone.`)) return;
  const r=await post('/api/nine_slice/remove',{name});
  if(!r.ok){ nsLog(r.error,true); return }
  await load(); drawNineSliceForm(); $('#nssel').value='';
  nsLog(`Removed "${name}". Press Build.`,false);
  budget(true);
};

// --------------------------------------------------------- removing a font
//
// The list is rebuilt from state rather than held, so a font added in the panel above
// appears here without a reload.

function drawFontList(){
  const sel=$('#fdelsel');
  if(!sel) return;
  const cur=sel.value;
  const list=(S.data&&S.data.fonts)||[];
  sel.innerHTML=list.map(f=>`<option${f.name===cur?' selected':''}>${f.name}</option>`)
    .join('')||'<option value="">— none —</option>';
}

$('#fdelbtn').onclick=async()=>{
  const name=$('#fdelsel').value;
  const log=$('#fdellog');
  if(!name){ log.className='bad'; log.textContent='No font to remove.'; return }
  // Asked before confirming, so the dialog names the scene rather than offering a delete
  // that is then refused.
  const u=await post('/api/font/users',{name});
  if(u.users&&u.users.length){
    log.className='bad';
    log.textContent=`Cannot remove "${name}" — ${u.users.join(', ')} loads it.`;
    return;
  }
  if(!confirm(`Remove font "${name}" from the manifest?\n\n`
              +`Nothing loads it. The TTF in art/fonts/ is left alone.`)) return;
  const r=await post('/api/font/remove',{name});
  if(!r.ok){ log.className='bad'; log.textContent=r.error; return }
  await load(); drawFontList();
  log.className='ok';
  log.textContent=`Removed "${name}". Press Build.`;
  budget(true);
};

// ------------------------------------------------------------------ project keys

function drawProject(){
  const d=S.data||{}, p=d.paths||{};
  if(!$('#prname')||d.no_project) return;
  $('#prname').value=d.name||'';
  $('#prbudget').value=d.budget||262144;
  // Shown relative to the project root, which is how the manifest states them -- an
  // absolute path here would be written back as one and stop building elsewhere.
  $('#prres').value=d.project_resources||'';
  $('#prhdr').value=d.project_header||'';
  $('#prdevaddr').value=d.device_address||'';
}

$('#prsave').onclick=async()=>{
  const log=$('#prlog');
  const want=[['name',$('#prname').value],['budget_bytes',$('#prbudget').value],
              ['resources',$('#prres').value],['header',$('#prhdr').value],
              ['device_address',$('#prdevaddr').value]];
  for(const [key,value] of want){
    const r=await post('/api/project/set',{key,value});
    if(!r.ok){ log.className='bad'; log.textContent=`${key}: ${r.error}`; return }
  }
  await load(); drawProject(); statusbar(); budget(true);
  log.className='ok';
  log.textContent='Saved. The budget strip and status bar now measure against it.';
};

// -------------------------------------------------------------------------- music
//
// A tracker over the sequencer's own model. The cell spelling is the MANIFEST's --
// `NOTE:INSTRUMENT`, '.' to hold, '-' to release -- rather than a prettier one invented
// here, because a song half-edited by hand and half in this tool has to stay one song.

const MU = { song: 0, pattern: 0, inst: 0, rows: null, octave: 4, row: 0 };

function muSong(){ return ((S.data && S.data.songs) || [])[MU.song] || null; }

function muSay(id, msg, bad){
  const el = $(id);
  if(!el) return;
  el.className = (id === '#mslog' ? 'mini ' : '') + (bad === false ? 'ok' : (bad ? 'bad' : 'dim'));
  el.textContent = msg || '';
}

// A row is fixed-width cells separated by spaces. Split on whitespace to read; pad to a
// column on write, so the manifest stays readable as a grid rather than becoming ragged
// the first time it is saved.
function muCells(row, channels){
  const c = row.trim().split(/\s+/).filter(s => s.length);
  while(c.length < channels) c.push('.');
  return c.slice(0, channels);
}
function muRow(cells){
  return cells.map(c => (c || '.').padEnd(5, ' ')).join(' ');
}

function drawMusic(){
  const songs = (S.data && S.data.songs) || [];
  const sel = $('#msong');
  sel.innerHTML = songs.map((s, i) => `<option value="${i}">${s.name}</option>`).join('');
  if(!songs.length){
    $('#mrows').innerHTML = '<small class="dim">No [music.*] in this manifest.</small>';
    $('#minstbody').innerHTML = '';
    drawSamples();
    return;
  }
  if(MU.song >= songs.length) MU.song = 0;
  sel.value = String(MU.song);
  const s = songs[MU.song];

  $('#mtempo').value = s.tempo;
  $('#morder').value = s.order.join(', ');
  $('#mcost').textContent = `${s.patterns.length} patterns x ${s.rows_per} rows x `
    + `${s.channels} ch - ${s.bytes} B` + (s.has_synth ? ' - synth' : ' - envelopes');

  const pat = $('#mpat');
  pat.innerHTML = s.patterns.map((_, i) => `<option value="${i}">${i}</option>`).join('');
  if(MU.pattern >= s.patterns.length) MU.pattern = 0;
  pat.value = String(MU.pattern);

  const inst = $('#minst');
  inst.innerHTML = s.instruments.map((x, i) =>
    `<option value="${i}">${i} - ${x.name || x.wave}</option>`).join('');
  if(MU.inst >= s.instruments.length) MU.inst = 0;
  inst.value = String(MU.inst);

  drawTracker();
  drawInstrument();
  drawSamples();
}

// Note names both ways. A tracker shows `C-4`; the manifest writes `C4`. The dash is the
// tracker's own device for keeping a sharp and a natural the same width so columns line up,
// and it is worth keeping on screen and dropping on save.
const NOTE_NAMES = ['C-','C#','D-','D#','E-','F-','F#','G-','G#','A-','A#','B-'];

// The inverse of parse_note in pnx_assets.py, which maps C4 to MIDI 60 -- so the octave
// is floor(n/12) MINUS ONE. Getting that wrong wrote every keyboard-entered note an octave
// high, which builds cleanly and plays wrong, and is invisible unless you can hear it.
function midiToTracker(n){
  return NOTE_NAMES[n % 12] + (Math.floor(n / 12) - 1);
}

// `C-4` on screen becomes `C4` in the manifest. The dash exists so a natural and a sharp
// occupy the same width and the columns line up; the manifest has no columns and does not
// want it.
function toManifestNote(s){
  return s.trim().replace(/^([A-G])-(-?\d)$/, '$1$2');
}

// A cell is `NOTE:INSTRUMENT`, '.' to hold, '-' to release. Split for display so note and
// instrument are separate fields -- they are separate decisions, and one text box for both
// means retyping the instrument to change a note.
function splitCell(cell){
  const c = (cell || '.').trim();
  if(c === '.') return { note: '', inst: '' };
  if(c === '-') return { note: 'off', inst: '' };
  const i = c.indexOf(':');
  const note = i < 0 ? c : c.slice(0, i);
  // Shown in tracker form so the grid stays aligned; stored without the dash.
  const shown = note.replace(/^([A-G])(-?\d)$/, '$1-$2');
  return { note: shown, inst: i < 0 ? '' : c.slice(i + 1) };
}
function joinCell(note, inst){
  const n = toManifestNote(note || '');
  if(!n) return '.';
  if(n === 'off' || n === '-' || n === '===') return '-';
  const i = (inst || '').trim();
  return i ? `${n}:${i}` : n;
}

// The piano row, as every tracker has mapped it since Fasttracker: the home row is the
// naturals and the row above is the sharps, so a keyboard is a keyboard.
const PIANO = { z:0, s:1, x:2, d:3, c:4, v:5, g:6, b:7, h:8, n:9, j:10, m:11,
                q:12, '2':13, w:14, '3':15, e:16, r:17, '5':18, t:19, '6':20,
                y:21, '7':22, u:23 };

function drawTracker(){
  const s = muSong();
  const box = $('#mrows');
  box.innerHTML = '';
  if(!s) return;
  MU.rows = s.patterns[MU.pattern].map(r => muCells(r, s.channels));

  const head = document.createElement('div');
  head.className = 'thead';
  const hn = document.createElement('b');
  hn.textContent = '';
  head.appendChild(hn);
  for(let c = 0; c < s.channels; c++){
    const sp = document.createElement('span');
    sp.textContent = `ch ${c + 1}`;
    head.appendChild(sp);
  }
  box.appendChild(head);

  MU.rows.forEach((cells, ri) => {
    const row = document.createElement('div');
    row.className = 'trow' + (ri % 4 === 0 ? ' beat' : '');
    const n = document.createElement('b');
    n.textContent = String(ri).padStart(2, '0');
    row.appendChild(n);

    cells.forEach((cell, ci) => {
      const step = document.createElement('div');
      step.className = 'tstep';
      const parts = splitCell(cell);

      const note = document.createElement('input');
      note.className = 'tnote' + (parts.note ? (parts.note === 'off' ? ' off' : ' on') : '');
      note.value = parts.note === 'off' ? '===' : (parts.note || '---');
      note.spellcheck = false;
      note.title = `row ${ri}, channel ${ci + 1} - type a note, or . to clear`;

      const inst = document.createElement('input');
      inst.className = 'tinst' + (parts.inst ? ' on' : '');
      inst.value = parts.inst || '--';
      inst.spellcheck = false;
      inst.title = 'instrument';

      const commit = () => {
        let nv = note.value.trim();
        if(nv === '---' || nv === '.' || nv === '') nv = '';
        if(nv === '===' || nv === '-') nv = 'off';
        let iv = inst.value.trim();
        if(iv === '--' || iv === '.') iv = '';
        MU.rows[ri][ci] = joinCell(nv, iv);
        // Write through to the cached song BEFORE redrawing. drawTracker rebuilds MU.rows
        // from `s.patterns`, and muSavePattern is async, so without this the redraw reads
        // the pre-edit pattern back and the note vanishes from the grid a frame after it
        // was typed -- while the correct value is sitting on disk. That desync is worse
        // than an outright failure, because reloading the page "fixes" it.
        s.patterns[MU.pattern] = MU.rows.map(muRow);
        muSavePattern();
        drawTracker();
        // Keeping the caret where it was: a grid that jumps to the top on every keystroke
        // cannot be played into.
        const sel = box.querySelectorAll('.tnote')[ri * s.channels + ci];
        if(sel) sel.focus();
      };

      // Typing a letter plays the note it sits under, the way a tracker keyboard does.
      // Anything else falls through to ordinary text editing, so `C#4` can still be typed
      // out in full.
      note.onkeydown = e => {
        if(e.ctrlKey || e.metaKey || e.altKey) return;
        const k = e.key.toLowerCase();
        if(k === 'delete' || k === 'backspace'){
          e.preventDefault(); note.value = '---'; commit(); return;
        }
        if(k === '-' || k === '='){
          e.preventDefault(); note.value = '==='; commit(); return;
        }
        if(k in PIANO){
          e.preventDefault();
          const midi = (MU.octave + 1) * 12 + PIANO[k];
          note.value = midiToTracker(Math.max(0, Math.min(119, midi)));
          if(inst.value === '--') inst.value = String(MU.inst);
          commit();
          return;
        }
        if(k === 'arrowdown' || k === 'enter'){
          e.preventDefault();
          const all = box.querySelectorAll('.tnote');
          const nx = all[(ri + 1) * s.channels + ci];
          if(nx){ nx.focus(); nx.select() }
        }
        if(k === 'arrowup'){
          e.preventDefault();
          const all = box.querySelectorAll('.tnote');
          const pv = all[(ri - 1) * s.channels + ci];
          if(pv){ pv.focus(); pv.select() }
        }
      };
      note.onchange = commit;
      inst.onchange = commit;
      note.onfocus = () => { note.select(); MU.row = ri };

      step.append(note, inst);
      row.appendChild(step);
    });
    box.appendChild(row);
  });
}


// ---------------------------------------------------------------- panel controls
//
// Three primitives, because a synth panel is three kinds of control and nothing else: a
// continuous value, a choice between a few options, and a shape you read rather than
// count.

// A knob. Drag vertically, wheel, or focus and arrow; double-click to type an exact value.
// Both halves are needed -- the arc gives the gesture, the readout gives the precision a
// developer tool owes you -- and a knob that could only be dragged would be a worse number
// box wearing a costume.
function knob(label, value, lo, hi, on){
  const wrap = document.createElement('div');
  wrap.className = 'knob';
  const dial = document.createElement('div');
  dial.className = 'dial';
  dial.tabIndex = 0;
  dial.setAttribute('role', 'slider');
  dial.setAttribute('aria-label', label);
  dial.setAttribute('aria-valuemin', lo);
  dial.setAttribute('aria-valuemax', hi);
  const name = document.createElement('b');
  name.textContent = label;
  const read = document.createElement('i');

  let v = value;
  const span = (hi - lo) || 1;
  const paint = () => {
    dial.style.setProperty('--p', String((v - lo) / span));
    dial.setAttribute('aria-valuenow', v);
    read.textContent = v;
  };
  const set = nv => {
    nv = Math.max(lo, Math.min(hi, Math.round(nv)));
    if(nv === v) return;
    v = nv; paint(); on(v);
  };
  paint();

  // Coarse by default, fine with shift -- a 5000 ms envelope and a 0..3 octave want very
  // different sensitivities from the same gesture.
  let dragging = false, lastY = 0;
  dial.addEventListener('pointerdown', e => {
    dragging = true; lastY = e.clientY; dial.setPointerCapture(e.pointerId); dial.focus();
  });
  dial.addEventListener('pointermove', e => {
    if(!dragging) return;
    const step = (e.shiftKey ? 1 : Math.max(1, Math.round(span / 60)));
    set(v + (lastY - e.clientY) * step);
    lastY = e.clientY;
  });
  const stop = () => { dragging = false };
  dial.addEventListener('pointerup', stop);
  dial.addEventListener('pointercancel', stop);
  dial.addEventListener('wheel', e => {
    e.preventDefault();
    set(v + (e.deltaY < 0 ? 1 : -1) * (e.shiftKey ? 1 : Math.max(1, Math.round(span / 60))));
  }, { passive: false });
  dial.addEventListener('keydown', e => {
    const step = e.shiftKey ? 1 : Math.max(1, Math.round(span / 60));
    if(e.key === 'ArrowUp' || e.key === 'ArrowRight'){ set(v + step); e.preventDefault() }
    if(e.key === 'ArrowDown' || e.key === 'ArrowLeft'){ set(v - step); e.preventDefault() }
  });
  // The exact value, for when a knob is the wrong tool -- which it is whenever you already
  // know the number you want.
  read.ondblclick = () => {
    const box = document.createElement('input');
    box.value = v;
    box.onblur = box.onchange = () => {
      set(parseInt(box.value, 10) || lo);
      box.replaceWith(read);
    };
    read.replaceWith(box);
    box.focus(); box.select();
  };

  wrap.append(dial, name, read);
  return wrap;
}

// A switch with every option visible and the chosen one lit. A dropdown would hide the
// alternatives, which is wrong for a choice you make constantly between four things.
function switcher(options, value, on, glyphs){
  const box = document.createElement('div');
  box.className = 'pick' + (glyphs ? '' : ' wide');
  const btns = [];
  options.forEach(o => {
    const b = document.createElement('button');
    b.className = o === value ? 'on' : '';
    b.title = o;
    b.innerHTML = glyphs ? waveGlyph(o) : o;
    // The switch moves its own light. It used to depend on the panel being rebuilt after
    // every save, so once saving stopped redrawing, a click would change the sound without
    // changing anything on screen.
    b.onclick = () => { btns.forEach(x => x.classList.toggle('on', x === b)); on(o) };
    btns.push(b);
    box.appendChild(b);
  });
  return box;
}

// Waveform marks. Iconic, not previews -- the real thing is band-limited per octave and
// drawing an idealised curve as if it were the output would be a preview that lies. The
// harmonic count beside the oscillator is the honest version of that information.
function waveGlyph(w){
  const p = { square:   'M1 9 L1 3 L7 3 L7 9 L13 9 L13 3 L19 3 L19 9',
              saw:      'M1 9 L7 3 L7 9 L13 3 L13 9 L19 3',
              triangle: 'M1 9 L5 3 L9 9 L13 3 L17 9 L19 6',
              noise:    'M1 6 L3 3 L4 8 L6 4 L8 9 L10 3 L12 7 L14 4 L16 8 L18 5 L19 7' }[w]
          || 'M1 6 L19 6';
  return `<svg class="wglyph" viewBox="0 0 20 12" fill="none" stroke="currentColor"
    stroke-width="1.4" stroke-linejoin="round"><path d="${p}"/></svg>`;
}

// The envelope as a shape. Four numbers do not read as one thing; a curve does, and the
// difference between a pluck and a pad is visible in it before you play a note.
function envCurve(e){
  const A = Math.max(0, e.attack || 0), D = Math.max(0, e.decay || 0),
        S = Math.max(0, Math.min(255, e.sustain ?? 0)), R = Math.max(0, e.release || 0);
  const W = 150, H = 44, pad = 3, top = pad, bot = H - pad;
  // Time is scaled to the longest stage so the shape stays legible whether the envelope is
  // 5 ms or 5 s -- an absolute axis would flatten every fast envelope into a vertical line.
  const total = Math.max(1, A + D + R) * 1.25;
  const x = ms => pad + (ms / total) * (W - pad * 2);
  const sy = bot - (S / 255) * (bot - top);
  const hold = total * 0.18;
  const d = `M${x(0)} ${bot} L${x(A)} ${top} L${x(A + D)} ${sy} `
          + `L${x(A + D + hold)} ${sy} L${x(A + D + hold + R)} ${bot}`;
  return `<svg width="${W}" height="${H}" viewBox="0 0 ${W} ${H}" fill="none">
    <path d="${d}" stroke="var(--accent)" stroke-width="1.5" stroke-linejoin="round"/>
    <path d="${d} L${x(0)} ${bot} Z" fill="var(--accent)" opacity=".12"/>
  </svg>`;
}

// How many harmonics survive band-limiting at this pitch. Mirrors MIP_HARMONICS in
// pnx_synth.c -- shown because it is the constraint that decides whether a bright waveform
// stays bright up the keyboard, and it is invisible everywhere else.
function harmonicsAt(midi){
  const H = [31, 31, 31, 31, 31, 16, 8, 4];
  return H[Math.max(0, Math.min(7, Math.floor(midi / 12)))];
}

function drawInstrument(){
  const s = muSong();
  const box = $('#minstbody');
  box.innerHTML = '';
  if(!s) return;
  const ins = s.instruments[MU.inst];
  const waves = (S.data.waveforms) || ['square','saw','triangle','noise'];

  const plain = JSON.parse(JSON.stringify(ins));
  delete plain.synth;
  const sy = ins.synth ? JSON.parse(JSON.stringify(ins.synth)) : null;

  // Readouts derived from the model -- envelope curves, the harmonic count, the header
  // name -- repaint themselves after every change. The panel used to get this for free
  // from a full rebuild on save, which is exactly what made a knob undraggable.
  const live = [];
  const push = () => { for(const f of live) f(); muWrite(plain, sy) };

  const mod = (title, hue) => {
    const m = document.createElement('div');
    m.className = 'mod';
    if(hue) m.style.setProperty('--mod-hue', hue);
    const h = document.createElement('h4');
    h.textContent = title;
    m.appendChild(h);
    return m;
  };
  // The name, first. An instrument is referred to by index everywhere it is USED -- a
  // pattern row, the generated header -- so the one place it can be called something is
  // here, and naming it is what makes the index legible everywhere else.
  const idrow = document.createElement('div');
  idrow.className = 'mini';
  idrow.innerHTML = '<span class="dim" style="font-size:10px;letter-spacing:.09em;'
    + 'text-transform:uppercase">name</span> ';
  const nameBox = document.createElement('input');
  nameBox.value = plain.name || '';
  nameBox.placeholder = `instrument ${MU.inst}`;
  nameBox.size = 14;
  nameBox.title = 'lowercase letters, digits and underscores - becomes '
    + `MUSIC_${(s.name || '').toUpperCase()}_INST_<NAME> in the generated header`;
  nameBox.onchange = () => { plain.name = nameBox.value.trim(); push() };
  idrow.appendChild(nameBox);
  const hint = document.createElement('small');
  hint.className = 'dim';
  hint.style.fontSize = '10px';
  const paintHint = () => {
    hint.textContent = plain.name
      ? `MUSIC_${(s.name || '').toUpperCase()}_INST_${plain.name.toUpperCase()}`
      : 'unnamed - patterns still reach it by index';
  };
  paintHint();
  live.push(paintHint);
  idrow.appendChild(hint);
  // Previews THIS instrument's live, unsaved settings -- `plain`/`sy` are the exact
  // objects every knob below writes into, not a copy fetched back from the manifest --
  // so dragging a knob and pressing this hears the drag, not what was last saved.
  const preview = document.createElement('button');
  preview.textContent = '▶';
  preview.title = 'preview this instrument (approximate -- see the Music tab\'s own note)';
  preview.style.marginLeft = 'auto';
  preview.onclick = () => {
    if(typeof playInstrument==='function')
      playInstrument(plain, sy, noteToMidi('C4'), 0.5);
  };
  idrow.appendChild(preview);
  box.appendChild(idrow);

  const chain = document.createElement('div');
  chain.className = 'synth';
  box.appendChild(chain);

  if(!sy){
    // No synth table. The plain envelope is the whole instrument, so it gets the panel
    // rather than being demoted to a footnote under one that does not exist.
    const m = mod('Envelope', 'var(--accent)');
    const r = document.createElement('div');
    r.className = 'row';
    r.appendChild(switcher(waves, plain.wave, v => { plain.wave = v; push() }, true));
    for(const [k, hi] of [['attack',5000],['decay',5000],['sustain',255],['release',5000]])
      r.appendChild(knob(k, plain[k], 0, hi, v => { plain[k] = v; push() }));
    m.appendChild(r);
    chain.appendChild(m);
    const note = document.createElement('small');
    note.className = 'dim';
    note.textContent = 'This song has no synth table. Adding one means a record for every '
      + 'instrument, because a pattern row names one index and the tables have to line up.';
    box.appendChild(note);
    return;
  }

  // --- oscillators. The signal starts here, so they are leftmost.
  (sy.osc || []).forEach((o, oi) => {
    const m = mod(`Osc ${oi + 1}`, 'var(--accent)');
    const r = document.createElement('div');
    r.className = 'row';
    const col = document.createElement('div');
    col.appendChild(switcher(waves, o.wave || 'square', v => { o.wave = v; push() }, true));
    const harm = document.createElement('small');
    harm.className = 'dim';
    harm.style.cssText = 'display:block;font-size:10px;margin-top:.35rem;letter-spacing:.06em';
    // Real information, not decoration: a saw an octave up carries a quarter the harmonics,
    // and that is why it sounds duller. Nothing else in the tool says so.
    const paintHarm = () => {
      harm.textContent = `${harmonicsAt(60 + (o.octave || 0) * 12)} harm at C4`;
    };
    paintHarm();
    live.push(paintHarm);
    col.appendChild(harm);
    r.appendChild(col);
    r.appendChild(knob('level', o.volume ?? 200, 0, 255, v => { o.volume = v; push() }));
    r.appendChild(knob('detune', o.detune ?? 0, -100, 100, v => { o.detune = v; push() }));
    r.appendChild(knob('oct', o.octave ?? 0, -4, 4, v => { o.octave = v; push() }));
    // Pulse width is square-only, so it is built once and shown conditionally rather than
    // appended conditionally: switching waveform no longer redraws the module, and a knob
    // that can only appear on a redraw would never appear.
    const width = knob('width', o.duty ?? 128, 16, 240, v => { o.duty = v; push() });
    const paintWidth = () => {
      width.style.display = (o.wave || 'square') === 'square' ? '' : 'none';
    };
    paintWidth();
    live.push(paintWidth);
    r.appendChild(width);
    m.appendChild(r);
    chain.appendChild(m);
  });

  // --- filter, with its own envelope drawn beside it.
  const fm = mod('Filter', '#e0a33c');
  const fr = document.createElement('div');
  fr.className = 'row';
  fr.appendChild(switcher(S.data.filter_modes || ['off','lowpass','highpass','bandpass'],
    sy.filter || 'off', v => { sy.filter = v; push() }));
  fr.appendChild(knob('cutoff', sy.cutoff_base ?? 128, 0, 255, v => { sy.cutoff_base = v; push() }));
  fr.appendChild(knob('reson', sy.resonance ?? 0, 0, 255, v => { sy.resonance = v; push() }));
  fr.appendChild(knob('env amt', sy.cutoff_env ?? 0, 0, 255, v => { sy.cutoff_env = v; push() }));
  fm.appendChild(fr);
  const fe = sy.cutoff || {};
  const fenv = document.createElement('div');
  fenv.className = 'row';
  fenv.style.marginTop = '.45rem';
  const fbox = document.createElement('div');
  fbox.className = 'envbox';
  fbox.innerHTML = envCurve(fe);
  live.push(() => { fbox.innerHTML = envCurve(sy.cutoff || {}) });
  fenv.appendChild(fbox);
  for(const [k, hi] of [['a',5000],['d',5000],['s',255],['r',5000]]){
    const key = { a:'attack', d:'decay', s:'sustain', r:'release' }[k];
    fenv.appendChild(knob(k, fe[key] ?? 0, 0, hi,
      v => { sy.cutoff = sy.cutoff || {}; sy.cutoff[key] = v; push() }));
  }
  fm.appendChild(fenv);
  chain.appendChild(fm);

  // --- amplifier.
  const am = mod('Amp', '#5fd28d');
  const ae = sy.amp || {};
  const ar = document.createElement('div');
  ar.className = 'row';
  const abox = document.createElement('div');
  abox.className = 'envbox';
  abox.innerHTML = envCurve(ae);
  live.push(() => { abox.innerHTML = envCurve(sy.amp || {}) });
  ar.appendChild(abox);
  for(const [k, hi] of [['a',5000],['d',5000],['s',255],['r',5000]]){
    const key = { a:'attack', d:'decay', s:'sustain', r:'release' }[k];
    ar.appendChild(knob(k, ae[key] ?? 0, 0, hi,
      v => { sy.amp = sy.amp || {}; sy.amp[key] = v; push() }));
  }
  am.appendChild(ar);
  chain.appendChild(am);

  // --- modulation. The LFO routes to one destination, which is why it is a switch and not
  // four separate depth controls.
  const lm = mod('Mod', '#a98cf0');
  // The destination switch spans the module and the four knobs sit under it in one row.
  // Putting the switch inline with the knobs left a wrapped row and a module two thirds
  // empty -- the LFO destination is a routing choice, not a fifth knob, and reads better
  // as the heading of the controls it governs.
  const lr = document.createElement('div');
  lr.className = 'row';
  lr.style.marginBottom = '.45rem';
  lr.appendChild(switcher(S.data.lfo_targets || ['off','pitch','volume','duty','cutoff'],
    sy.lfo_target || 'off', v => { sy.lfo_target = v; push() }));
  lm.appendChild(lr);
  const pr = document.createElement('div');
  pr.className = 'row';
  pr.appendChild(knob('rate', sy.lfo_rate ?? 0, 0, 255, v => { sy.lfo_rate = v; push() }));
  pr.appendChild(knob('depth', sy.lfo_depth ?? 0, 0, 255, v => { sy.lfo_depth = v; push() }));
  pr.appendChild(knob('pitch env', sy.pitch_env ?? 0, -1200, 1200,
    v => { sy.pitch_env = v; push() }));
  pr.appendChild(knob('fall', sy.pitch_env_decay ?? 0, 0, 255,
    v => { sy.pitch_env_decay = v; push() }));
  lm.appendChild(pr);
  chain.appendChild(lm);

  // --- sends. Global instances, so these are levels into a shared effect rather than
  // effects of their own -- worth saying, because it is why they are cheap.
  const xm = mod('Sends', '#7b8798');
  const xr = document.createElement('div');
  xr.className = 'row';
  xr.appendChild(knob('reverb', sy.reverb ?? 0, 0, 255, v => { sy.reverb = v; push() }));
  xr.appendChild(knob('chorus', sy.chorus ?? 0, 0, 255, v => { sy.chorus = v; push() }));
  xm.appendChild(xr);
  const xn = document.createElement('small');
  xn.className = 'dim';
  xn.style.cssText = 'display:block;font-size:10px;margin-top:.3rem;max-width:9rem';
  xn.textContent = 'one shared reverb and chorus for all four voices';
  xm.appendChild(xn);
  chain.appendChild(xm);

  // --- the fallback envelope, last because it is what plays only when the synth is
  // compiled out. Still shown: a build with PNX_USE_SYNTH=0 makes this the whole sound.
  const pm = mod('If synth is off', '#3a4451');
  pm.style.opacity = '.72';
  const prow = document.createElement('div');
  prow.className = 'row';
  prow.appendChild(switcher(waves, plain.wave, v => { plain.wave = v; push() }, true));
  for(const [k, hi] of [['attack',5000],['decay',5000],['sustain',255],['release',5000]])
    prow.appendChild(knob(k, plain[k], 0, hi, v => { plain[k] = v; push() }));
  pm.appendChild(prow);
  const pn = document.createElement('small');
  pn.className = 'dim';
  pn.style.cssText = 'display:block;font-size:10px;margin-top:.3rem;max-width:11rem';
  pn.textContent = 'the plain envelope, used only in a PNX_USE_SYNTH=0 build';
  pm.appendChild(pn);
  chain.appendChild(pm);
}


// Both halves in one request. The pipeline refuses tables of different lengths precisely
// so a note cannot play a different sound depending on which one it resolved through, and
// saving them separately would be the same mistake one step earlier.
//
// Write-BEHIND, and deliberately so. A knob calls this on every pointermove, so writing
// synchronously would be a request per pixel; and the obvious "reload and redraw" ending
// would replace the very dial the pointer has captured, killing the drag after its first
// step. So: coalesce, and never rebuild the panel from a write. The panel owns the live
// model and repaints its own readouts.
let muWriteTimer = null, muWritePend = null;
function muWrite(plain, synth){
  const s = muSong();
  if(!s) return;
  // The index is captured NOW, not at flush time: switching instruments inside the
  // coalescing window would otherwise land these values on the wrong one.
  muWritePend = { song: s, index: MU.inst, plain, synth };
  muSay('#minstlog', 'editing…');
  if(muWriteTimer) return;
  muWriteTimer = setTimeout(muFlushInstrument, 260);
}

async function muFlushInstrument(){
  muWriteTimer = null;
  const p = muWritePend;
  muWritePend = null;
  if(!p) return;
  const r = await post('/api/song/instrument',
    { name: p.song.name, index: p.index, plain: p.plain, synth: p.synth });
  if(!r.ok){ muSay('#minstlog', r.error, true); return }
  // Write through to the cached song. Without this, switching instruments and back would
  // redraw the panel from the pre-edit model -- the same desync the tracker grid had.
  const merged = JSON.parse(JSON.stringify(p.plain));
  if(p.synth) merged.synth = JSON.parse(JSON.stringify(p.synth));
  p.song.instruments[p.index] = merged;
  muRelabelInstruments();
  muSay('#minstlog', 'saved', false);
  budget(true);
}

// A rename has to reach the picker, which is the only place an instrument is named once
// the panel is drawn. Relabelling in place rather than redrawing keeps the caret in the
// name field the user is still typing in.
function muRelabelInstruments(){
  const s = muSong();
  const sel = $('#minst');
  if(!s || !sel) return;
  [...sel.options].forEach((o, i) => {
    const x = s.instruments[i];
    if(x) o.textContent = `${i} - ${x.name || x.wave}`;
  });
}

function drawSamples(){
  const box = $('#msamples');
  if(!box) return;
  box.innerHTML = '';
  const list = (S.data && S.data.samples) || [];
  if(!list.length) box.innerHTML = '<small class="dim">No samples.</small>';
  for(const sm of list){
    const row = document.createElement('div');
    row.className = 'smprow';
    // Seconds, from the packed size: 16 kHz 8-bit is one byte a sample, so the blob IS the
    // duration. Measured against the 1.5 s the pipeline enforces, because that limit is
    // the reason samples are short and a byte count does not say how close you are.
    const secs = sm.bytes ? (sm.bytes - 8) / 16000 : null;
    const pct = secs === null ? 0 : Math.min(100, (secs / 1.5) * 100);
    row.innerHTML = `<b>${sm.name}</b>`
      + `<span class="dim" style="flex:1">${sm.file}</span>`
      + `<span class="smpbar"><i style="width:${pct}%" class="${pct >= 100 ? 'over' : ''}">`
      + `</i></span>`
      + `<span class="dim" style="width:6.5rem;text-align:right">`
      + (secs === null ? 'not built' : `${secs.toFixed(2)} s of 1.5`) + '</span> ';
    // The actual WAV, not a synthesised approximation -- unlike a note or a pattern,
    // there is nothing here to approximate.
    const play = document.createElement('button');
    play.textContent = '▶';
    play.title = 'play this sample';
    play.onclick = () => {
      new Audio('/api/sample/wav?name=' + encodeURIComponent(sm.name)).play();
    };
    row.appendChild(play);
    const del = document.createElement('button');
    del.textContent = 'Remove';
    del.onclick = async () => {
      if(!confirm(`Remove sample "${sm.name}"? The WAV on disk is left alone.`)) return;
      const r = await post('/api/sample/remove', { name: sm.name });
      if(!r.ok){ muSay('#mslog', r.error, true); return }
      await reload(); drawMusic(); budget(true);
    };
    row.appendChild(del);
    box.appendChild(row);
  }
  const wav = $('#mswav');
  wav.innerHTML = ((S.data && S.data.wavs) || [])
    .map(w => `<option>${w.path}</option>`).join('');
}

// Leaving an instrument settles its pending write first. The write already carries the
// index it was made against, so it would land correctly either way -- but a save that
// completes after you have moved on reports "saved" under a different instrument, and the
// 260 ms is only there to coalesce a drag, not to survive one.
function muSettle(){
  if(!muWriteTimer) return;
  clearTimeout(muWriteTimer);
  muWriteTimer = null;
  muFlushInstrument();
}

$('#msong').onchange = () => { muSettle(); MU.song = +$('#msong').value; MU.pattern = 0; MU.inst = 0; drawMusic() };
$('#mpat').onchange  = () => { MU.pattern = +$('#mpat').value; drawTracker(); muSay('#mpatlog','') };
$('#minst').onchange = () => { muSettle(); MU.inst = +$('#minst').value; drawInstrument() };
$('#moct').onchange  = () => { MU.octave = +$('#moct').value };

// A new pattern is empty; a clone is this one. Both are additive -- nothing here removes a
// pattern, because the order list names patterns by index and deleting one silently
// renumbers every entry after it. That is a change worth making deliberately in the
// manifest rather than accidentally with a button.
async function muAddPattern(copy){
  const s = muSong(); if(!s) return;
  const blank = Array.from({ length: s.rows_per },
    () => Array.from({ length: s.channels }, () => '.').join('     '));
  const rows = copy ? s.patterns[MU.pattern].slice() : blank;
  const r = await post('/api/song/pattern',
    { name: s.name, index: s.patterns.length, rows, append: true });
  if(!r.ok){ muSay('#mpatlog', r.error, true); return }
  await reload();
  MU.pattern = (S.data.songs[MU.song].patterns.length - 1);
  drawMusic(); budget(true);
  muSay('#mpatlog', copy ? 'cloned' : 'added', false);
}
$('#mpatadd').onclick   = () => muAddPattern(false);
$('#mpatclone').onclick = () => muAddPattern(true);

$('#mtempo').onchange = async () => {
  const s = muSong(); if(!s) return;
  const r = await post('/api/song/meta', { name: s.name, tempo: +$('#mtempo').value });
  if(!r.ok){ muSay('#mpatlog', r.error, true); return }
  await reload(); drawMusic();
};

// Written as it changes, like everything else in the editor. The row is redrawn from the
// local copy immediately so typing stays responsive, and the manifest catches up.
async function muSavePattern(){
  const s = muSong(); if(!s || !MU.rows) return;
  const r = await post('/api/song/pattern',
    { name: s.name, index: MU.pattern, rows: MU.rows.map(muRow) });
  muSay('#mpatlog', r.ok ? 'saved' : r.error, r.ok ? false : true);
  if(r.ok) budget(true);
}

$('#morder').onchange = async () => {
  const s = muSong(); if(!s) return;
  const order = $('#morder').value.split(/[,\s]+/).filter(Boolean).map(Number);
  const r = await post('/api/song/meta', { name: s.name, order });
  if(!r.ok){ muSay('#mpatlog', r.error, true); return }
  await reload(); drawMusic(); budget(true);
  muSay('#mpatlog', 'order saved', false);
};

$('#msongnew').onclick = async () => {
  const name = prompt('Name the song.\n\n'
    + 'Game code loads it as PNX_ASSET_MUSIC_<NAME>. Lowercase letters, digits and '
    + 'underscores.');
  if(!name) return;
  const r = await post('/api/song', { name: name.trim() });
  if(!r.ok){ muSay('#mpatlog', r.error, true); return }
  await reload();
  MU.song = (S.data.songs || []).findIndex(x => x.name === name.trim());
  MU.pattern = 0; MU.inst = 0;
  drawMusic(); budget(true);
};

$('#msongdel').onclick = async () => {
  const s = muSong(); if(!s) return;
  if(!confirm(`Remove song "${s.name}"?\n\n`
    + `Game code loading it by name will stop compiling.`)) return;
  const r = await post('/api/song/remove', { name: s.name });
  if(!r.ok){ muSay('#mpatlog', r.error, true); return }
  MU.song = 0; MU.pattern = 0; MU.inst = 0;
  await reload(); drawMusic(); budget(true);
};

$('#minstadd').onclick = async () => {
  const s = muSong(); if(!s) return;
  const r = await post('/api/song/instrument/add', { name: s.name });
  if(!r.ok){ muSay('#mpatlog', r.error, true); return }
  await reload();
  MU.inst = S.data.songs[MU.song].instruments.length - 1;
  drawMusic(); budget(true);
};

$('#minstdel').onclick = async () => {
  const s = muSong(); if(!s) return;
  const r = await post('/api/song/instrument/remove',
    { name: s.name, index: s.instruments.length - 1 });
  if(!r.ok){ muSay('#mpatlog', r.error, true); return }
  MU.inst = 0;
  await reload(); drawMusic(); budget(true);
};

$('#msadd').onclick = async () => {
  const name = $('#msname').value.trim();
  const file = $('#mswav').value;
  if(!name){ muSay('#mslog', 'Name it first.', true); return }
  if(!file){ muSay('#mslog', 'No WAV in the project to add.', true); return }
  const r = await post('/api/sample', { name, file });
  if(!r.ok){ muSay('#mslog', r.error, true); return }
  $('#msname').value = '';
  await reload(); drawMusic(); budget(true);
  muSay('#mslog', `Added "${name}". Press Build.`, false);
};

// ------------------------------------------------------------------------- dialog
//
// A textarea per conversation, one page per line. Saved on blur rather than per
// keystroke: every write goes through the manifest and re-derives the glyph set of every
// `charset = "auto"` font, which is not work to do between two letters of a word.

function dlgSay(msg,bad){
  const el=$('#dialoglog');
  el.className='mini '+(bad===false?'ok':'bad');
  el.textContent=msg||'';
}

function drawDialog(){
  const box=$('#dialoglist'); box.innerHTML='';
  const list=(S.data&&S.data.dialogs)||[];
  if(!list.length){
    box.innerHTML='<small class="dim">No conversations yet.</small>';
    return;
  }
  for(const d of list){
    const card=document.createElement('section');
    card.className='scenecard';
    const head=document.createElement('div');
    head.className='mini';
    head.innerHTML=`<b>${d.name}</b> <span class="dim">${d.pages.length} page`
      +`${d.pages.length===1?'':'s'} · ${d.bytes} B</span>`;
    card.appendChild(head);

    const ta=document.createElement('textarea');
    ta.rows=Math.max(3,d.pages.length+1);
    ta.style.width='100%';
    ta.value=d.pages.join('\n');
    ta.onblur=async()=>{
      const pages=ta.value.split('\n').map(s=>s.trim()).filter(Boolean);
      if(!pages.length){ dlgSay('A conversation needs at least one page.'); return }
      if(pages.join('\n')===d.pages.join('\n')) return;
      const r=await post('/api/dialog',{name:d.name,pages});
      if(!r.ok){ dlgSay(r.error); return }
      await reload(); drawDialog(); dlgSay('');
      budget(true);
    };
    card.appendChild(ta);

    const foot=document.createElement('div');
    foot.className='mini';
    const del=document.createElement('button');
    del.textContent='Remove';
    del.onclick=async()=>{
      if(!confirm(`Remove conversation "${d.name}"?`)) return;
      const r=await post('/api/dialog/remove',{name:d.name});
      if(!r.ok){ dlgSay(r.error); return }
      await reload(); drawDialog(); dlgSay(`Removed "${d.name}".`,false);
      budget(true);
    };
    foot.appendChild(del);
    card.appendChild(foot);
    box.appendChild(card);
  }
}

$('#dlgnew').onclick=async()=>{
  const name=prompt('Name the conversation.\n\n'
    +'Game code reaches it as PNX_DIALOG_<NAME>. Lowercase letters, digits and '
    +'underscores.');
  if(!name) return;
  const r=await post('/api/dialog',{name:name.trim(),pages:['...']});
  if(!r.ok){ dlgSay(r.error); return }
  await reload(); drawDialog(); dlgSay(`Added "${name.trim()}".`,false);
};

// ------------------------------------------------------------------------- scenes
//
// A scene is the framework's only load point, and it was the one part of the manifest
// with no editor at all: a map could be drawn, painted and built and still be unreachable
// from the game. Every control here writes the manifest immediately, the way the legend
// does, so there is no separate save to forget.

function sceneSay(msg, bad){
  const el=$('#scenelog');
  el.className='mini '+(bad===false?'ok':'bad');
  el.textContent=msg||'';
}

function drawScenes(){
  const box=$('#scenelist'); box.innerHTML='';
  const d=S.data||{};
  const scenes=d.scenes||[], maps=(d.maps||[]).map(m=>m.name);
  const sprites=d.sprite_names||[], fonts=(d.fonts||[]).map(f=>f.name);
  const nineSlices=d.nine_slice_names||[];

  // A map nothing loads is the dead end worth naming: it builds, it costs bytes, and the
  // game has no way to reach it.
  const loaded=new Set(scenes.map(s=>s.map).filter(Boolean));
  const orphans=maps.filter(m=>!loaded.has(m));

  if(!scenes.length){
    box.innerHTML='<small class="dim">No scenes yet. Nothing can be loaded until '
      +'there is one.</small>';
  }

  for(const sc of scenes){
    const card=document.createElement('section');
    card.className='scenecard';
    const head=document.createElement('div');
    head.className='mini';
    head.innerHTML=`<b>${sc.name}</b>`;
    card.appendChild(head);

    // Map.
    const mrow=document.createElement('label');
    mrow.className='mini';
    mrow.innerHTML='map ';
    const msel=document.createElement('select');
    msel.innerHTML='<option value="">(none)</option>'
      +maps.map(m=>`<option${m===sc.map?' selected':''}>${m}</option>`).join('');
    msel.onchange=()=>writeScene(sc,{map:msel.value||null});
    mrow.appendChild(msel);
    card.appendChild(mrow);

    // Sprites and fonts, a checkbox each. Listed rather than typed because every name
    // here has to resolve at build time, and a select cannot be misspelled.
    for(const [label,all,cur,key] of [['sprites',sprites,sc.sprites,'sprites'],
                                      ['9-slice',nineSlices,sc.nine_slices,'nine_slices'],
                                      ['fonts',fonts,sc.fonts,'fonts']]){
      const row=document.createElement('div');
      row.className='mini';
      row.innerHTML=`<span class="dim">${label}</span> `;
      if(!all.length) row.innerHTML+='<small class="dim">none defined</small>';
      for(const n of all){
        const on=cur.includes(n);
        const l=document.createElement('label');
        l.className='mini';
        l.innerHTML=`<input type="checkbox" ${on?'checked':''}> ${n}`;
        l.querySelector('input').onchange=ev=>{
          const next=cur.filter(x=>x!==n);
          if(ev.target.checked) next.push(n);
          writeScene(sc,{[key]:next});
        };
        row.appendChild(l);
      }
      card.appendChild(row);
    }

    // Dialog is a flag, not a list: the pipeline packs every [dialog.*] into one blob.
    const drow=document.createElement('label');
    drow.className='mini';
    drow.innerHTML=`<input type="checkbox" ${sc.dialog?'checked':''}> dialog`;
    drow.querySelector('input').onchange=ev=>writeScene(sc,{dialog:ev.target.checked});
    card.appendChild(drow);

    if(sc.atlases.length){
      const a=document.createElement('small');
      a.className='dim';
      a.textContent=`also loads atlases: ${sc.atlases.join(', ')}`;
      card.appendChild(a);
    }

    const foot=document.createElement('div');
    foot.className='mini';
    const del=document.createElement('button');
    del.textContent='Remove';
    del.onclick=async()=>{
      if(!confirm(`Remove scene "${sc.name}"?\n\n`
                  +`Game code loading it by name will stop compiling.`)) return;
      const r=await post('/api/scene/remove',{name:sc.name});
      if(!r.ok){ sceneSay(r.error); return }
      await reload(); drawScenes(); sceneSay(`Removed "${sc.name}".`,false);
    };
    foot.appendChild(del);
    card.appendChild(foot);
    box.appendChild(card);
  }

  if(orphans.length){
    const warn=document.createElement('p');
    warn.className='mini bad';
    warn.textContent=`No scene loads: ${orphans.join(', ')}. `
      +`Those maps cost bytes and cannot be reached.`;
    box.appendChild(warn);
  }
}

// Every field resent, because the endpoint replaces the table rather than patching it --
// the same reason writeLegend resends. A partial write would drop the fonts when you
// ticked a sprite.
async function writeScene(sc,changes){
  const body={name:sc.name, map:sc.map, sprites:sc.sprites, nine_slices:sc.nine_slices,
              fonts:sc.fonts, dialog:sc.dialog, atlases:sc.atlases, ...changes};
  const r=await post('/api/scene',body);
  if(!r.ok){ sceneSay(r.error); drawScenes(); return }
  await reload(); drawScenes(); sceneSay('');
  budget(true);
}

$('#scnew').onclick=async()=>{
  const name=prompt('Name the scene.\n\n'
    +'Game code loads it as PNX_SCENE_<NAME>. Lowercase letters, digits and underscores.');
  if(!name) return;
  const maps=(S.data.maps||[]).map(m=>m.name);
  // Seeded with a map rather than created empty: a scene that loads nothing is refused by
  // the pipeline, so an empty one could not be saved at all.
  const first=maps.find(m=>!(S.data.scenes||[]).some(s=>s.map===m))||maps[0];
  if(!first){ sceneSay('Add a map first — a scene that loads nothing cannot be built.');
              return }
  const r=await post('/api/scene',{name:name.trim(), map:first, sprites:[],
                                   fonts:(S.data.fonts||[]).map(f=>f.name)});
  if(!r.ok){ sceneSay(r.error); return }
  await reload(); drawScenes(); sceneSay(`Added "${name.trim()}".`,false);
};

function statusbar(){
  const d=S.data||{}, e=d.engine||{}, p=d.paths||{};
  if(d.no_project){ $('#stproject').textContent='no project open'; return }
  $('#stproject').textContent=`${d.name} — ${p.root||''}`;
  $('#stengine').textContent=`engine ${e.editor||'?'}`;
  if(estLast) paintBudget(estLast);
  // Fetched once on load rather than polled: it changes when someone installs an SDK,
  // which is not a per-second event.
  fetch('/api/sdk/status').then(r=>r.json()).then(s=>{
    $('#stsdk').textContent=s.can_build?`SDK ${s.active}`:'no SDK — see Settings';
  }).catch(()=>{});
}

$('#outtoggle').onclick=()=>{
  const hid=$('#outpanel').classList.toggle('hidden');
  $('#outtoggle').textContent=hid?'Show':'Hide';
};
$('#tabproject').onclick=()=>showTab('project');
$('#stproject').onclick=()=>showTab('project');
$('#tabmaps').onclick=()=>showTab('maps');
$('#tabscenes').onclick=()=>showTab('scenes');
$('#tabdialog').onclick=()=>showTab('dialog');
$('#tabmusic').onclick=()=>showTab('music');
$('#tabimport').onclick=()=>showTab('import');
$('#tabfonts').onclick=()=>showTab('fonts');
$('#tabsdk').onclick=()=>showTab('sdk');
$('#tabpixel').onclick=()=>showTab('pixel');
$('#tabcode').onclick=()=>showTab('code');
$('#tabdevice').onclick=()=>showTab('device');

// ------------------------------------------------------------------- fonts view
//
// Every pixel shown here is rendered server-side from the packed blob and sent back as
// a PNG. Compositing in the browser would be faster but would mean two rasterisers --
// and the moment they disagree the preview is worse than useless, because it looks
// authoritative while being wrong.
let fontSources=[];

// ARGB2222: alpha in the top two bits, then R, G, B. Only the opaque values are worth
// offering -- the framebuffer is opaque and text is drawn onto it, not composited under.
function argbSwatches(){
  const out=[];
  for(let r=0;r<4;r++)for(let g=0;g<4;g++)for(let b=0;b<4;b++)
    out.push({v:0xC0|(r<<4)|(g<<2)|b,
              css:`rgb(${r*85},${g*85},${b*85})`,
              hex:'0x'+(0xC0|(r<<4)|(g<<2)|b).toString(16).toUpperCase()});
  return out;
}
function fillSwatch(sel,chip,chosen){
  sel.innerHTML=argbSwatches().map(s=>
    `<option value="${s.v}"${s.v===chosen?' selected':''}>${s.hex}</option>`).join('');
  const paint=()=>{
    const s=argbSwatches().find(s=>s.v===+sel.value);
    if(s) $(chip).style.background=s.css;
  };
  sel.addEventListener('input',paint);
  paint();
}

// Controls that only mean something in a particular mode are hidden rather than
// disabled: a scroll offset with no map behind it is noise, not a disabled feature.
function fontModes(){
  const isMap=$('#fbg').value==='map';
  $('#fmapwrap').style.display=isMap?'':'none';
  $('#fscrollwrap').style.display=isMap?'':'none';
  $('#fboxcwrap').style.display=$('#fbox').checked?'':'none';
}

async function loadFonts(){
  fontSources=await (await fetch('/api/fonts')).json();
  // Project fonts first and visually separated: referencing a system font in the
  // manifest would build here and nowhere else, so the distinction matters.
  const mine=fontSources.filter(f=>f.in_project), sys=fontSources.filter(f=>!f.in_project);
  const opt=f=>`<option value="${f.in_project?f.rel:f.path}">${f.name}</option>`;
  $('#fsrc').innerHTML=
    (mine.length?`<optgroup label="in this project">${mine.map(opt).join('')}</optgroup>`:'')+
    (sys.length?`<optgroup label="installed on this machine">${sys.map(opt).join('')}</optgroup>`:'');

  $('#fmap').innerHTML=(S.data.maps||[]).map(m=>`<option>${m.name}</option>`).join('');

  const dlg=S.data.dialog||{};
  const pages=[];
  for(const [k,v] of Object.entries(dlg)) v.forEach((p,i)=>pages.push([`${k} · ${i}`,p]));
  $('#fdlg').innerHTML=`<option value="">— your own text —</option>`+
    pages.map(([l,p])=>`<option value="${encodeURIComponent(p)}">${l}</option>`).join('');

  fillSwatch($('#fbgc'),'#fbgcsw',0xC0);   // black: what a frame is usually cleared to
  fillSwatch($('#fboxc'),'#fboxcsw',0xC0);
  fillSwatch($('#fink'),'#finksw',0xFF);   // white
  declared();
  fontModes();
  refreshFont();
}

function declared(){
  const fonts=S.data.fonts||[];
  $('#fdeclared').innerHTML=fonts.length?fonts.map(f=>
    `<div><b>${f.name}</b><span>${f.size}px · ${f.depth}bpp · ${
      f.bytes!==null?f.bytes.toLocaleString()+' B':'not built'}</span></div>`).join('')
    :'<small>None yet.</small>';
}

function fontSpec(){
  return {source:$('#fsrc').value,size:+$('#fsize').value,depth:+$('#fdepth').value,
          threshold:+$('#fthresh').value,tracking:+$('#ftrack').value,
          charset:$('#fcharset').value,extra:$('#fextra').value};
}

let fpending=null;
function refreshFont(){
  clearTimeout(fpending);
  fpending=setTimeout(async()=>{
    if(!$('#fsrc').value) return;
    const spec=fontSpec();
    const post=(u,b)=>fetch(u,{method:'POST',headers:{'content-type':'application/json'},
                              body:JSON.stringify(b)}).then(r=>r.json());

    const r=await post('/api/font/preview',spec);
    if(r.error){
      $('#fstats').innerHTML=`<div class="warn"><b>!</b><span>${r.error}</span></div>`;
      return;
    }
    const cell=(v,l,warn)=>`<div class="${warn?'warn':''}"><b>${v}</b><span>${l}</span></div>`;
    $('#fstats').innerHTML=
      cell(r.glyph_count,'glyphs')+
      cell(r.line_height+'px','line height')+
      cell(r.baseline+'px','baseline')+
      cell(r.bytes.toLocaleString(),'bytes')+
      cell(r.pct.toFixed(2)+'%','of budget')+
      // A glyph that rasterised to nothing and is not a space means the threshold ate
      // it. That is the commonest way an imported font is quietly broken, so it is
      // called out rather than left to be noticed on the watch.
      cell(r.blank_glyphs,'blank glyphs',r.blank_glyphs>1);
    $('#fsheet').src=r.sheet;
    $('#fchars').textContent=`carries: ${r.chars}`;

    const box=$('#fbox').checked;
    const s=await post('/api/font/scene',{
      spec,background:$('#fbg').value,map:$('#fmap').value,
      scroll_x:+$('#fsx').value,scroll_y:+$('#fsy').value,
      bg_colour:+$('#fbgc').value,ink:+$('#fink').value,
      align:$('#falign').value,scale:+$('#fscale').value,
      text:$('#ftext').value,x:4,y:4,
      box:box?{on:true,x:8,y:140,w:184,h:72,colour:+$('#fboxc').value,
               border:true,border_colour:+$('#fink').value}:{on:false}});
    if(s.error){$('#fscenenote').textContent=s.error;return}
    $('#fscene').src=s.image;
    $('#fscenenote').innerHTML=
      `${s.lines} line${s.lines===1?'':'s'} · ${s.text_height}px of text · shown at ${s.scale}x`+
      (s.overflow?' · <b class="bad">overflows the box</b>':'');
  },140);
}

for(const id of ['fsrc','fsize','fdepth','fthresh','ftrack','fcharset','fextra',
                 'fbg','fmap','fsx','fsy','fbgc','fbox','fboxc','fink','falign',
                 'fscale','ftext'])
  $('#'+id).addEventListener('input',()=>{
    if(id==='fthresh') $('#fthreshv').textContent=$('#fthresh').value;
    if(id==='fdepth'){
      // The threshold means different things at each depth: a hard cutoff at 1bpp, a
      // black point at 2bpp. Carrying 128 across would flatten every antialiased sample
      // and make 2bpp look identical to 1bpp at twice the bytes.
      const two=$('#fdepth').value==='2';
      $('#fthresh').value=two?24:128;
      $('#fthreshv').textContent=$('#fthresh').value;
    }
    if(id==='fbg'||id==='fbox') fontModes();
    refreshFont();
  });

$('#fdlg').addEventListener('change',()=>{
  const v=$('#fdlg').value;
  if(v){$('#ftext').value=decodeURIComponent(v); refreshFont()}
});

$('#faddbtn').onclick=async()=>{
  const name=$('#fname').value.trim(), lic=$('#flic').value.trim();
  if(!name){alert('Name the font first.');return}
  if(!lic){alert('A licence is required — rasterised glyphs are redistribution.');return}
  const r=await (await fetch('/api/font',{method:'POST',
    headers:{'content-type':'application/json'},
    body:JSON.stringify({...fontSpec(),name,license:lic})})).json();
  const log=$('#log'); log.className=r.ok?'ok':'bad';
  log.textContent=r.ok
    ?`Added [[font]] "${name}". Add it to a scene's fonts = [...] and press Build.`
    :r.error;
  if(r.ok){await load(); declared()}
};

// --------------------------------------------------------------- toolchain view
let sdkPoll=null;

// ----------------------------------------------------------------- heartbeat
//
// The server has no other way to know this page is still here. Without it, closing the
// tab left the editor running and holding its port, so the next launch found something
// listening and refused to start -- "closed" and "still running" looked identical.
//
// The goodbye goes through sendBeacon because a normal fetch is cancelled when the page
// unloads; a beacon is queued by the browser and delivered anyway. It only shortens the
// server's grace period, so a second tab's heartbeat can still cancel the shutdown.
function startHeartbeat(){
  setInterval(()=>{ fetch('/api/alive').catch(()=>{}) }, 5000);
  addEventListener('pagehide',()=>{
    try{ navigator.sendBeacon('/api/bye') }catch(_){}
  });
}

// ------------------------------------------------------------------- updates
//
// Check, download, install: three steps, each one the user's. The editor carries the
// engine a project compiles against, so an upgrade that happened by itself would change
// what a build produces without anyone asking for it.

let UPD=null, updPoll=null;

function updRender(){
  const u=UPD||{};
  const info=$('#updinfo');
  if(u.pending){
    // Distinct from "could not reach GitHub": nothing has failed, the answer is simply
    // not back. Saying it failed would be a lie the user acts on by retrying.
    info.innerHTML=`<div><span class="k">running</span> <b>${u.current||'—'}</b></div>`
      +`<small>checking… <span class="dim">${u.why||''}</span></small>`;
  }else if(!u.checked){
    info.innerHTML=`<div><span class="k">running</span> <b>${u.current||'—'}</b></div>`
      +`<small>${u.why||'not checked yet'}</small>`;
  }else if(!u.available){
    info.innerHTML=`<div><span class="k">running</span> <b>${u.current}</b></div>`
      +`<small>up to date</small>`;
  }else{
    info.innerHTML=`<div><span class="k">running</span> <b>${u.current}</b></div>`
      +`<div><span class="k">available</span> <b>${u.version}`
      +`${u.prerelease?' <small>(prerelease)</small>':''}</b></div>`
      +`<small>${u.asset.name} — ${(u.asset.bytes/1048576).toFixed(0)} MB</small>`;
  }
  $('#upddl').style.display=u.available&&!(u.dl&&u.dl.ready)?'':'none';
  $('#updapply').style.display=(u.dl&&u.dl.ready)?'':'none';
  $('#updnotes').style.display=u.available&&u.notes?'':'none';

  // The banner is the only part of this that goes looking for attention, so it appears
  // once per version and stays gone once dismissed.
  const show=u.available&&sessionStorage.getItem('updhide')!==u.version;
  $('#updbanner').style.display=show?'flex':'none';
  if(show){
    $('#updbannertext').innerHTML=
      `<b>${u.version}</b> is available — you are running ${u.current}.`;
  }
}

// The check no longer blocks the editor while GitHub thinks about it, which means it can
// come back "still trying" -- so the page has to come back for the answer rather than
// treating the first reply as final. Bounded, because a resolver that never answers must
// not leave a poll running for the life of the session.
let updRetry=null;
async function updCheck(force){
  clearTimeout(updRetry);
  $('#updcheck').disabled=true;
  try{
    UPD=await (await fetch('/api/update'+(force?'/check':''),
      {method:force?'POST':'GET'})).json();
  }catch(_){ $('#updcheck').disabled=false; return }
  UPD.dl=await (await fetch('/api/update/progress')).json();
  updRender();
  if(UPD.pending && (updCheck.tries=(updCheck.tries||0)+1) <= 10){
    updRetry=setTimeout(()=>updCheck(false), 2000);
  }else{
    updCheck.tries=0;
    $('#updcheck').disabled=false;
  }
}

function updWatch(){
  clearInterval(updPoll);
  updPoll=setInterval(async()=>{
    const d=await (await fetch('/api/update/progress')).json();
    UPD.dl=d;
    $('#updbar').style.display=d.busy?'':'none';
    $('#updfill').style.width=(d.pct||0)+'%';
    if(!d.busy){
      clearInterval(updPoll);
      $('#updbar').style.display='none';
      if(d.error){
        $('#updinfo').innerHTML+=`<small class="bad">${d.error}</small>`;
      }
      updRender();
    }
  },400);
}

$('#updcheck').onclick=()=>updCheck(true);
$('#upddl').onclick=async()=>{
  await fetch('/api/update/download',{method:'POST'});
  $('#updbar').style.display=''; updWatch();
};
// What is on the table if this restarts: a painted map that was never saved, or an edited
// source file. The page is the only thing that knows either, which is why the warning
// lives here rather than in the updater.
function unsavedWork(){
  const lost=[];
  if(typeof S!=='undefined' && S.dirty && S.map) lost.push(`the map "${S.map.name}"`);
  if(typeof CODE!=='undefined' && CODE.path && CODE.editable && $('#codetext')
     && $('#codetext').value!==CODE.clean) lost.push(CODE.path);
  return lost;
}

$('#updapply').onclick=async()=>{
  const v=(UPD&&UPD.version)||'the new version';
  const lost=unsavedWork();
  // Always asked, never assumed: this closes the application someone is working in.
  const warning=lost.length
    ? `Install ${v} and restart?\n\nUNSAVED CHANGES WILL BE LOST:\n`
      + lost.map(w=>'  - '+w).join('\n')
      + `\n\nCancel, save them, then install.`
    : `Install ${v} and restart?\n\nThe editor will close and reopen on the new version.`;
  if(!confirm(warning)) return;

  const log=$('#log'); log.className=''; log.textContent='Installing';
  let r;
  try{
    r=await (await fetch('/api/update/apply',{method:'POST'})).json();
  }catch(_){
    // The process can die before its reply lands. That is a restart, not a failure --
    // the poll below establishes which.
    r={ok:true, restarting:true, message:'Installing'};
  }
  log.className=r.ok?'ok':'bad';
  log.textContent=r.ok?r.message:r.error;
  // Its own block: appended inline it ran straight on from the asset size above it,
  // which read as one sentence about a file rather than a result.
  $('#updinfo').innerHTML+=`<div style="margin-top:.4rem"><small class="${r.ok?'':'bad'}">`
    +`${r.ok?r.message:r.error}</small></div>`;
  if(r.ok && r.restarting) waitForRestart();
};

// The old process exits, the new one binds the same port, and the page reloads onto it --
// so a restart is something the user watches happen rather than something they are told
// to go and do.
async function waitForRestart(){
  const was=(UPD&&UPD.current)||'';
  const deadline=Date.now()+60000;
  const dots=setInterval(()=>{ $('#log').textContent+='.' }, 1000);
  while(Date.now()<deadline){
    await new Promise(r=>setTimeout(r,1000));
    try{
      const p=await (await fetch('/api/ping',{cache:'no-store'})).json();
      // Only a DIFFERENT version means the successor is up; the old process answers
      // right until it exits.
      if(p.app==='pebblnyx-editor' && p.version!==was){
        clearInterval(dots);
        location.reload();
        return;
      }
    }catch(_){ /* down: the middle of a restart, not a failure */ }
  }
  clearInterval(dots);
  $('#log').className='bad';
  $('#log').textContent='The editor did not come back on its own -- start it again.';
}
$('#updnotes').onclick=()=>{
  const b=$('#updbody');
  b.style.display=b.style.display==='none'?'':'none';
  b.textContent=(UPD&&UPD.notes)||'';
};
$('#updbannergo').onclick=()=>{ showTab('sdk'); $('#updbanner').style.display='none' };
$('#updbannerhide').onclick=()=>{
  if(UPD&&UPD.version) sessionStorage.setItem('updhide',UPD.version);
  $('#updbanner').style.display='none';
};

async function sdkStatus(remote){
  const s=await (await fetch('/api/sdk/status'+(remote?'?remote=1':''))).json();
  const row=(k,v,cls)=>`<div><span class="k">${k}</span> <span class="${cls||''}">${v}</span></div>`;

  const d=S.data||{}, p=d.paths||{}, e=d.engine||{};
  const pct=d.budget?(100*d.used/d.budget).toFixed(1):'0';
  $('#projinfo').innerHTML= d.no_project
    ? '<div>No project open. Open a folder, or create one.</div>'
    : row('name', d.name||'—')+
      row('folder', `<span class="p">${p.root||'—'}</span>`)+
      row('manifest', `<span class="p">${p.manifest||'—'}</span>`)+
      row('header', `<span class="p">${p.header||'—'}</span>`)+
      row('resources', `${(d.used||0).toLocaleString()} / ${(d.budget||0).toLocaleString()} B (${pct}%)`)+
      row('app binary', d.app&&d.app.known
        ? `${d.app.used.toLocaleString()} / ${d.app.limit.toLocaleString()} B `
          + `(${d.app.pct.toFixed(1)}%)`
        : `<span class="dim">${(d.app&&d.app.why)||'not measured'}</span>`)+
      // The engine is staged from the editor at each build, so what matters is whether
      // this project last built against a different one.
      row('engine', e.linked
        ? `${e.editor} (live tree, symlinked)`
        : (e.changed
            ? `<span class="no">built against ${e.built_against}, editor has ${e.editor}</span>`
            : `${e.editor}${e.built_against?'':' (not built yet)'}`));

  $('#projadopt').style.display=
    (!d.no_project && d.project_file && !d.project_file.format) ? '' : 'none';

  // Engine ownership. Unlocking is not just write permission: it stops the editor
  // restaging the engine, so the project keeps its changes and stops getting fixes.
  // Both halves of that trade are stated, because only stating the first would be a
  // pleasant surprise followed by an unpleasant one.
  const owned=!!e.owned;
  $('#engstate').innerHTML=
    row('source', e.linked?'symlinked to a live tree'
        :(owned?'owned by this project':'staged from the editor'), owned?'no':'yes')+
    row('version', owned?`forked from ${e.owned_from||'?'}`:(e.editor||'—'))+
    (owned&&e.owned_at?row('since', e.owned_at):'');
  $('#engprose').textContent = e.linked
    ? 'This project points at a live engine tree, so edits are picked up by the next build already.'
    : (owned
      ? 'Restaging is off for this project. Your edits under src/c/pnx are kept and are '
        + 'compiled into every build — and this project no longer receives engine fixes '
        + 'from editor updates. Re-tracking discards them.'
      : 'The engine is restaged from the editor before every build, so edits under '
        + 'src/c/pnx would be silently overwritten. Taking ownership stops the restaging '
        + 'and hands this project its own copy — after which it stops receiving engine '
        + 'fixes when the editor updates.');
  $('#engown').checked=owned;
  $('#engown').disabled=!!e.linked;
  $('#engresync').style.display=owned?'':'none';

  const rec=await (await fetch('/api/project/recent')).json();
  $('#recent').innerHTML=rec.recent.length
    ? rec.recent.map(r=>`<button data-path="${r.path}">${r.name} — ${r.path}</button>`).join('')
    : '<small>—</small>';
  for(const b of $('#recent').querySelectorAll('button'))
    b.onclick=()=>openProject(b.dataset.path);
  $('#sdkstatus').innerHTML=
    row('pebble tool', s.pebble||'not installed', s.pebble?'yes':'no')+
    row('active SDK', s.active||'none', s.active?'yes':'no')+
    row('installed', s.installed.length?s.installed.join(', '):'none')+
    row('can build a .pbw', s.can_build?'yes':'no', s.can_build?'yes':'no')+
    (s.newer?row('newer available', s.newer):'')+
    (!s.pebble&&!s.installer
      ? row('installer', 'none found — install uv, pipx or pip', 'no') : '');

  if(!$('#sdkterms').children.length)
    $('#sdkterms').innerHTML=s.terms.map(([t,u])=>
      `<a href="${u}" target="_blank" rel="noopener">${t} &nearr;</a>`).join('');

  // An acceptance already on record stays ticked and locked: it is a statement the user
  // made, not a form field to toggle.
  if(s.accepted){ $('#sdkaccept').checked=true; $('#sdkaccept').disabled=true; }
  $('#sdkinstall').disabled=s.busy||!$('#sdkaccept').checked;
  $('#sdkinstall').textContent=s.busy?'Installing…'
    :(s.active?'Reinstall / update the SDK':'Install the SDK');
  if(s.log&&s.log.trim()) $('#sdklog').textContent=s.log;

  // Poll only while something is actually running.
  if(s.busy&&!sdkPoll) sdkPoll=setInterval(()=>sdkStatus(),1500);
  if(!s.busy&&sdkPoll){ clearInterval(sdkPoll); sdkPoll=null; }
  return s;
}

$('#sdkaccept').onchange=async()=>{
  if(!$('#sdkaccept').checked){ $('#sdkinstall').disabled=true; return }
  await fetch('/api/sdk/accept',{method:'POST'});
  $('#sdkaccept').disabled=true;
  $('#sdkinstall').disabled=false;
};

$('#sdkinstall').onclick=async()=>{
  $('#sdkinstall').disabled=true;
  $('#sdklog').textContent='Starting…\n';
  const r=await (await fetch('/api/sdk/install',{method:'POST',
    headers:{'content-type':'application/json'},body:'{}'})).json();
  if(r.error){ $('#sdklog').textContent=r.error; $('#sdkinstall').disabled=false; return }
  sdkStatus();
};

$('#sdkrefresh').onclick=()=>sdkStatus(true);

// --------------------------------------------------------------------- emulator
//
// Mirrors the SDK panel's own shape (status object with `busy`/`log`, poll only while
// something is running) rather than inventing a second one -- see sdkStatus above.
// emuPoll asks "is anything running yet" every 1.5s the whole time this tab is open;
// the screen itself is emuFrameLoop below, which is NOT a fixed-interval timer -- see
// its own comment for why that used to be a real bug, not just a smoothness question.
let emuPoll=null;

function emuPlatform(){
  const sel=$('#emuplatform');
  if(sel.options.length===0){
    for(const name of Object.keys(S.data.platforms||{})){
      const o=document.createElement('option'); o.value=name; o.textContent=name;
      sel.appendChild(o);
    }
    sel.value=S.emuPlatform||sel.options[0].value;
  }
  return sel.value;
}

let emuTabActive=false;

function emuEnter(){
  emuTabActive=true;
  emuPlatform();
  $('#emulock').checked=!!(S.data&&S.data.force_screen_lock);
  emuStatus();
  if(!emuPoll) emuPoll=setInterval(emuStatus,1500);
  devAddrNote();
}

$('#emulock').onchange=async()=>{
  const checked=$('#emulock').checked;
  const r=await post('/api/project/set',{key:'force_screen_lock',value:checked});
  if(!r.ok){ $('#emulock').checked=!checked; $('#emunote').textContent=r.error; return }
  $('#emunote').textContent=`Saved. ${checked?'On':'Off'} for the next Build & run.`;
};

// ----------------------------------------------------- real device: install and logs
//
// pebble-tool's own `--phone <address>` target, over the Pebble app's Developer
// Connection -- the address itself lives on the Project Settings tab (a project
// setting: the same phone every session, until its IP changes), read here rather than
// asked for again per action.

function devAddress(){ return (S.data&&S.data.device_address)||'' }

function devAddrNote(){
  const addr=devAddress();
  $('#devaddrnote').textContent=addr
    ? `Installing to ${addr}`
    : 'No device address set -- add one on the Project Settings tab.';
}

let devInstallPoll=null;
$('#devinstall').onclick=async()=>{
  const addr=devAddress();
  if(!addr){
    $('#devnote').textContent='Set a device address on the Project Settings tab first.';
    return;
  }
  $('#devinstall').disabled=true;
  $('#devnote').textContent='Building…';
  $('#devinstalllog').textContent='—';
  const r=await post('/api/device/install',{});
  if(!r.ok){
    $('#devinstall').disabled=false;
    $('#devnote').textContent=r.error||'Could not start.';
    return;
  }
  if(!devInstallPoll) devInstallPoll=setInterval(devInstallStatus,700);
};

async function devInstallStatus(){
  let s;
  try{ s=await (await fetch('/api/device/install/status')).json() }catch(_){ return }
  const el=$('#devinstalllog');
  el.textContent=s.log||'—'; el.scrollTop=el.scrollHeight;
  if(s.busy) return;
  clearInterval(devInstallPoll); devInstallPoll=null;
  $('#devinstall').disabled=false;
  $('#devnote').textContent=(s.result&&s.result.ok) ? 'Installed.'
    : ((s.result&&s.result.error)||'Install failed -- see output below.');
}

let devLogsPoll=null;
$('#devlogstart').onclick=async()=>{
  const addr=devAddress();
  if(!addr){
    $('#devlognote').textContent='Set a device address on the Project Settings tab first.';
    return;
  }
  const r=await post('/api/device/logs/start',{});
  if(!r.ok){ $('#devlognote').textContent=r.error||'Could not attach.'; return }
  $('#devlogstart').style.display='none';
  $('#devlogstop').style.display='';
  $('#devlognote').textContent=`Attached to ${addr}`;
  if(!devLogsPoll) devLogsPoll=setInterval(devLogsStatus,1000);
  devLogsStatus();
};
$('#devlogstop').onclick=async()=>{
  await post('/api/device/logs/stop',{});
  if(devLogsPoll){ clearInterval(devLogsPoll); devLogsPoll=null }
  $('#devlogstart').style.display='';
  $('#devlogstop').style.display='none';
  $('#devlognote').textContent='Detached.';
};
async function devLogsStatus(){
  let s;
  try{ s=await (await fetch('/api/device/logs/status')).json() }catch(_){ return }
  const el=$('#devlogview');
  el.textContent=s.lines||'—'; el.scrollTop=el.scrollHeight;
  // The process can end on its own -- the phone dropped the connection, `pebble` quit
  // -- not just from the Detach button, so this notices that on the next poll rather
  // than showing "Attached" forever against a stream that has actually gone quiet.
  if(!s.attached&&devLogsPoll){
    clearInterval(devLogsPoll); devLogsPoll=null;
    $('#devlogstart').style.display='';
    $('#devlogstop').style.display='none';
    $('#devlognote').textContent='Disconnected.';
  }
}

// Leaving the tab, not stopping the emulator -- pebble-tool keeps it running (that is
// the whole point of the state file), this just stops paying for screenshots and status
// polls of a screen nobody is looking at. Also releases anything still held: a button
// pressed and then abandoned by switching tabs should not stay stuck down on a watch
// nobody is looking at either.
function emuLeave(){
  emuTabActive=false;
  if(emuPoll){ clearInterval(emuPoll); emuPoll=null }
  emuFrameRunning=false;
  if(emuHeld.size) emuRelease();
}

async function emuStatus(){
  const platform=emuPlatform();
  let s;
  try{ s=await (await fetch('/api/emulator/status?platform='+platform)).json() }
  catch(_){ return }
  if(s.error) return;

  $('#emustart').disabled=s.busy;
  $('#emustart').textContent=s.busy?'Building…':(s.running?'Rebuild & reinstall':'Build & run');
  $('#emustop').disabled=!s.running&&!s.busy;
  $('#emuscreenwrap').style.display=s.running?'':'none';
  $('#emunote').textContent=s.busy
    ?`Building and installing for ${platform}… an ARM compile plus a cold boot, so `
     +'this can take a while the first time.'
    :(s.running?`${platform} is running.`
      :`${platform} is not running. Build & run compiles this project for it and `
       +'installs into pebble-tool’s own emulator.');
  if(s.log&&s.log.trim()) $('#emulog').textContent=s.log;

  if(s.running) emuFrameLoop(); else emuFrameRunning=false;
}

// A fetch+blob-URL dance after all, not a cache-busted <img src> on a fixed timer --
// the timer WAS the bug. setInterval fired every 800ms regardless of whether the
// PREVIOUS request had finished, and once a single screendump took longer than that
// (Emulator.frame's own wait can run up to 1.5s), requests piled up: several in
// flight at once, each landing out of order, each one racing the others over what
// used to be a single shared temp file server-side. That is what "2.6fps and massive
// lag" actually was. Fixed on the server by giving every call its own filename and a
// per-platform lock (pnx_editor.py); fixed here by never starting the NEXT fetch until
// the current one has actually resolved -- a plain while loop awaiting each request in
// turn, rather than a timer that cannot know how long the last one took. MIN_FRAME_GAP
// is a yield to the event loop, not a target rate: the real pace is however long a
// screendump genuinely takes, and this loop no longer fights itself over it.
let emuFrameRunning=false;
const EMU_MIN_FRAME_GAP=40;

async function emuFrameLoop(){
  if(emuFrameRunning) return;             // already looping
  emuFrameRunning=true;
  while(emuFrameRunning){
    const platform=emuPlatform();
    try{
      const r=await fetch('/api/emulator/frame?platform='+platform+'&t='+Date.now());
      if(r.status===200){
        const url=URL.createObjectURL(await r.blob());
        const old=$('#emuscreen').src;
        $('#emuscreen').src=url;
        if(old&&old.startsWith('blob:')) URL.revokeObjectURL(old);
      }
      // else 204 -- no frame landed in time (mid-boot, or the lock was held by a call
      // still in flight); leave the last frame showing and try again next turn.
    }catch(_){ /* network hiccup -- try again next turn */ }
    if(!emuFrameRunning) break;
    await new Promise(res=>setTimeout(res,EMU_MIN_FRAME_GAP));
  }
}

$('#emuplatform').onchange=()=>{ S.emuPlatform=$('#emuplatform').value; emuStatus() };

$('#emustart').onclick=async()=>{
  $('#emustart').disabled=true;
  const r=await (await fetch('/api/emulator/start',{method:'POST',
    headers:{'content-type':'application/json'},
    body:JSON.stringify({platform:emuPlatform()})})).json();
  if(!r.ok) $('#emunote').textContent=r.error||'could not start';
  emuStatus();
};

$('#emustop').onclick=async()=>{
  await fetch('/api/emulator/stop',{method:'POST'});
  emuStatus();
};

// Held state, not one-shot clicks: a real QemuButton packet is a BITMASK of everything
// currently down (pebble_tool/sdk/... via libpebble2's protocol), so every change here
// resends the complete set rather than one name at a time -- see Emulator.button's own
// docstring in pnx_editor.py. One source for both the on-screen buttons and the
// keyboard below, so "click and hold" and "press and hold a key" are the same code path
// doing the same thing to the same watch.
let emuHeld=new Set();

function emuPush(){
  document.querySelectorAll('.emubtn').forEach(b=>b.classList.toggle('held',emuHeld.has(b.dataset.btn)));
  return fetch('/api/emulator/button',{method:'POST',
    headers:{'content-type':'application/json'},
    body:JSON.stringify({platform:emuPlatform(),action:'push',buttons:[...emuHeld]})});
}
function emuRelease(){
  emuHeld.clear();
  document.querySelectorAll('.emubtn').forEach(b=>b.classList.remove('held'));
  return fetch('/api/emulator/button',{method:'POST',
    headers:{'content-type':'application/json'},
    body:JSON.stringify({platform:emuPlatform(),action:'release'})});
}
function emuDown(name){
  if(emuHeld.has(name)) return;
  emuHeld.add(name);
  emuPush();
}
function emuUp(name){
  if(!emuHeld.has(name)) return;
  emuHeld.delete(name);
  emuHeld.size?emuPush():emuRelease();
}

// mousedown/mouseup on each button, PLUS a document-level mouseup/mouseleave-of-window
// safety net -- a mouse released after dragging off the button (or off the whole page)
// never fires that button's own mouseup, and without this its button would stay "held"
// on a watch nobody is touching until the next click anywhere lifts it.
for(const b of document.querySelectorAll('.emubtn')){
  b.onmousedown=e=>{ e.preventDefault(); emuDown(b.dataset.btn) };
  b.onmouseup=()=>emuUp(b.dataset.btn);
  b.onmouseleave=()=>emuUp(b.dataset.btn);
}
document.addEventListener('mouseup',()=>{ if(emuHeld.size) emuRelease() });

// Arrow keys / Enter / Backspace, while the BEZEL ITSELF has focus -- not just the
// Device tab, or an arrow key pressed to change the platform dropdown next to it would
// get hijacked into a button press instead of moving the selection. Click the screen or
// tab to it (tabindex on .emubezel) to focus it; the same layout CloudPebble's own
// emulator used keys for. e.repeat is skipped on the way down rather than re-sent: the
// OS's own key-repeat would otherwise resend an identical push every ~30ms, which the
// watch cannot tell apart from a very fast double-press.
const EMU_KEYS={ArrowUp:'up',ArrowDown:'down',ArrowRight:'select',Enter:'select',
                ArrowLeft:'back',Backspace:'back'};
function emuBezelFocused(){
  const a=document.activeElement;
  return !!(emuTabActive&&a&&a.closest&&a.closest('.emubezel'));
}
$('#emuscreen').onclick=()=>document.querySelector('.emubezel').focus();
window.addEventListener('keydown',e=>{
  const btn=EMU_KEYS[e.key]; if(!btn||e.repeat) return;
  if(!emuBezelFocused()) return;
  e.preventDefault();
  emuDown(btn);
});
// NOT gated on focus: a hold started while focused must still release if focus moved
// away before the key came back up, or the button would look stuck down forever on a
// watch nobody is touching any more. Gated on emuHeld instead -- only acts on a key
// this panel itself put a button down for, so an unrelated Enter/arrow elsewhere on the
// page is never swallowed.
window.addEventListener('keyup',e=>{
  const btn=EMU_KEYS[e.key];
  if(!btn||!emuHeld.has(btn)) return;
  e.preventDefault();
  emuUp(btn);
});

// ------------------------------------------------------------------ sprite editor
//
// Pixels are held as ARGB2222 bytes -- the device's own encoding -- not as CSS colours.
// Painting in the target colour space means the canvas cannot show a colour the watch
// cannot, so nothing collapses on import.
// `origin` is set when the canvas holds ONE FRAME cut out of a sheet rather than a whole
// file. Saving then composites it back at that rect instead of replacing the file, because
// the other poses on that sheet are someone else's work and editing one should not be able
// to lose the row it sits in.
// One frame on screen at a time, with a filmstrip below the canvas to switch, add,
// duplicate or delete one -- Aseprite's own model, replacing a single tall canvas that
// showed every frame stacked into it at once with no per-frame navigation at all.
// PX.data stays exactly what it always was: a flat buffer of EVERY frame, w*h each,
// concatenated -- the same layout the sprite sheet is saved as on disk. Only what's
// drawn and addressed changes; PX.frame (0-based) says which w*h slice of it is the
// one currently on screen.
const PX={w:16,h:24,frames:1,frame:0,zoom:12,data:null,colour:0xFF,tool:'pen',
          undo:[],redo:[],origin:null};

function pxPer(){ return PX.w*PX.h }
function pxTotalH(){ return PX.h*PX.frames }

function pxInit(w,h,frames){
  // A fresh canvas is not a frame of anything, so the sheet it came from stops applying.
  // Leaving it set is how Save would composite an unrelated drawing into someone's sheet.
  PX.origin=null;
  if($('#pxtitle')) $('#pxtitle').textContent='Canvas';
  PX.w=w; PX.h=h; PX.frames=frames; PX.frame=0;
  PX.data=new Uint8Array(w*pxTotalH());   // 0 is transparent, as everywhere else
  PX.undo=[]; PX.redo=[];
  pxDraw();
}

// Snapshots frames and frame count alongside the pixels, not just PX.data on its own --
// add/duplicate/delete-frame all change how long PX.data IS, so restoring the bytes
// without restoring PX.frames would leave the two disagreeing about how many frames
// there are.
function pxSnapshotState(){ return {data:PX.data.slice(), frames:PX.frames, frame:PX.frame} }
function pxRestoreState(s){
  PX.data=s.data; PX.frames=s.frames; PX.frame=Math.min(s.frame, s.frames-1);
}
function pxSnapshot(){
  PX.undo.push(pxSnapshotState());
  if(PX.undo.length>40) PX.undo.shift();   // bounded: this is a scratch tool
  PX.redo=[];                              // a new stroke retires whatever could be redone
}

// Inserts one frame's worth of pixels (blank, or a copy -- pxFrameAdd/pxFrameDup below)
// at index `at`, shifting every frame from there on down one slot. Never touches the
// bytes of any OTHER frame, which is the whole point versus the old Frames field's
// full reallocation.
function pxInsertFrame(at, bytes){
  const per=pxPer();
  const nd=new Uint8Array(PX.data.length+per);
  nd.set(PX.data.subarray(0, at*per), 0);
  nd.set(bytes, at*per);
  nd.set(PX.data.subarray(at*per), (at+1)*per);
  PX.data=nd; PX.frames++;
}

function pxDraw(){
  const per=pxPer(), off=PX.frame*per;
  for(const [id,scale] of [['pxcv',PX.zoom],['pxcv1',1]]){
    const cv=$('#'+id); cv.width=PX.w*scale; cv.height=PX.h*scale;
    const g=cv.getContext('2d'); g.imageSmoothingEnabled=false;
    g.clearRect(0,0,cv.width,cv.height);
    for(let y=0;y<PX.h;y++)for(let x=0;x<PX.w;x++){
      const v=PX.data[off+y*PX.w+x];
      if(!v) continue;
      g.fillStyle=argbCss(v);
      g.fillRect(x*scale,y*scale,scale,scale);
    }
    if(scale>3&&$('#pxgrid').checked&&id==='pxcv'){
      g.strokeStyle='rgba(128,128,128,.28)'; g.lineWidth=1;
      for(let x=0;x<=PX.w;x++){g.beginPath();g.moveTo(x*scale+.5,0);g.lineTo(x*scale+.5,PX.h*scale);g.stroke()}
      for(let y=0;y<=PX.h;y++){g.beginPath();g.moveTo(0,y*scale+.5);g.lineTo(PX.w*scale,y*scale+.5);g.stroke()}
    }
  }
  $('#pxundo').disabled=!PX.undo.length;
  $('#pxredo').disabled=!PX.redo.length;
  pxDrawFilmstrip();
}

// The filmstrip. Each thumbnail is its own tiny canvas at the sprite's native
// resolution -- one canvas pixel per sprite pixel -- scaled up or down to a fixed
// DISPLAY height by CSS (.pxthumb canvas), so a 16x24 pose and a 32x32 one both read
// clearly instead of one being squashed to fit a square box.
function pxDrawFilmstrip(){
  const el=$('#pxfilm'); if(!el) return;
  el.innerHTML='';
  for(let f=0; f<PX.frames; f++){
    const b=document.createElement('button');
    b.className='pxthumb'+(f===PX.frame?' on':'');
    b.title=`frame ${f+1} of ${PX.frames}`;
    const cv=document.createElement('canvas');
    cv.width=PX.w; cv.height=PX.h;
    const g=cv.getContext('2d'); g.imageSmoothingEnabled=false;
    const off=f*pxPer();
    for(let y=0;y<PX.h;y++)for(let x=0;x<PX.w;x++){
      const v=PX.data[off+y*PX.w+x];
      if(!v) continue;
      g.fillStyle=argbCss(v);
      g.fillRect(x,y,1,1);
    }
    b.appendChild(cv);
    const num=document.createElement('span'); num.textContent=f+1;
    b.appendChild(num);
    b.onclick=()=>{ PX.frame=f; pxDraw() };
    el.appendChild(b);
  }
  $('#pxframenote').textContent=PX.frames>1 ? `frame ${PX.frame+1} of ${PX.frames}` : '1 frame';
  $('#pxframedel').disabled=PX.frames<=1;
}

$('#pxframeadd').onclick=()=>{
  pxSnapshot();
  pxInsertFrame(PX.frame+1, new Uint8Array(pxPer()));
  PX.frame++;
  pxDraw();
};
$('#pxframedup').onclick=()=>{
  pxSnapshot();
  const per=pxPer();
  pxInsertFrame(PX.frame+1, PX.data.slice(PX.frame*per, (PX.frame+1)*per));
  PX.frame++;
  pxDraw();
};
$('#pxframedel').onclick=()=>{
  if(PX.frames<=1) return;
  pxSnapshot();
  const per=pxPer();
  const nd=new Uint8Array(PX.data.length-per);
  nd.set(PX.data.subarray(0, PX.frame*per), 0);
  nd.set(PX.data.subarray((PX.frame+1)*per), PX.frame*per);
  PX.data=nd; PX.frames--;
  if(PX.frame>=PX.frames) PX.frame=PX.frames-1;
  pxDraw();
};

function argbCss(v){
  const r=((v>>4)&3)*85,g=((v>>2)&3)*85,b=(v&3)*85;
  return `rgb(${r},${g},${b})`;
}

// A multi-frame sprite could only be inspected here before -- the full stack in
// pxcv1, one pose at a time -- never actually seen playing, which is the one thing a
// walk cycle is drawn to look right doing. Reads PX.data live on every tick rather
// than snapshotting it, so painting a frame while the loop runs shows up in the very
// next pass instead of needing Stop/Play to notice the edit.
let pxAnimTimer=null, pxAnimFrame=0;
const PX_ANIM_FPS=12;   // a typical Pebble game-loop cadence, not the device's own limit

function pxDrawAnimFrame(){
  const scale=Math.max(4,PX.zoom), cv=$('#pxanim');
  cv.width=PX.w*scale; cv.height=PX.h*scale;
  const g=cv.getContext('2d'); g.imageSmoothingEnabled=false;
  g.clearRect(0,0,cv.width,cv.height);
  const rowOffset=(pxAnimFrame%Math.max(1,PX.frames))*PX.h;
  for(let y=0;y<PX.h;y++)for(let x=0;x<PX.w;x++){
    const v=PX.data[(rowOffset+y)*PX.w+x];
    if(!v) continue;
    g.fillStyle=argbCss(v);
    g.fillRect(x*scale,y*scale,scale,scale);
  }
}

function pxAnimStop(){
  if(pxAnimTimer){ clearInterval(pxAnimTimer); pxAnimTimer=null }
  $('#pxplay').textContent='▶ Play animation';
  $('#pxanimwrap').style.display='none';
}

$('#pxplay').onclick=()=>{
  if(pxAnimTimer){ pxAnimStop(); return }
  if(PX.frames<=1){
    $('#pxplaynote').textContent='Single frame -- nothing to animate. Add one with + Frame.';
    return;
  }
  $('#pxplaynote').textContent='';
  pxAnimFrame=0;
  $('#pxanimwrap').style.display='';
  $('#pxplay').textContent='■ Stop';
  pxDrawAnimFrame();
  pxAnimTimer=setInterval(()=>{
    pxAnimFrame=(pxAnimFrame+1)%Math.max(1,PX.frames);
    pxDrawAnimFrame();
  },1000/PX_ANIM_FPS);
};

function pxFill(x,y,target){
  // Iterative flood fill: a recursive one blows the stack on a full 128x128 canvas.
  // Bounded to the CURRENT frame's own PX.h rows -- flooding across pxTotalH() (every
  // frame's pixels concatenated) could leak into the next pose whenever its edge
  // happened to share the same colour, which is a correctness bug the old single tall
  // canvas had and this per-frame view closes along with everything else.
  if(target===PX.colour) return;
  const off=PX.frame*pxPer(), stack=[[x,y]];
  while(stack.length){
    const [cx,cy]=stack.pop();
    if(cx<0||cy<0||cx>=PX.w||cy>=PX.h) continue;
    const i=off+cy*PX.w+cx;
    if(PX.data[i]!==target) continue;
    PX.data[i]=PX.colour;
    stack.push([cx+1,cy],[cx-1,cy],[cx,cy+1],[cx,cy-1]);
  }
}

// Canvas pixel coordinates ARE frame-local now that #pxcv only ever shows one frame --
// pxPaint below is what adds PX.frame's own offset when it actually indexes PX.data.
function pxAt(e){
  const r=$('#pxcv').getBoundingClientRect();
  return [Math.floor((e.clientX-r.left)/PX.zoom), Math.floor((e.clientY-r.top)/PX.zoom)];
}

let pxDown=false;
function pxPaint(e,first){
  const [x,y]=pxAt(e);
  if(x<0||y<0||x>=PX.w||y>=PX.h) return;
  const i=PX.frame*pxPer()+y*PX.w+x;
  if(PX.tool==='pick'){ pxSetColour(PX.data[i]); return }
  if(first) pxSnapshot();
  if(PX.tool==='fill') pxFill(x,y,PX.data[i]);
  else PX.data[i]= PX.tool==='erase' ? 0 : PX.colour;
  pxDraw();
}
$('#pxcv').addEventListener('mousedown',e=>{pxDown=true; pxPaint(e,true)});
$('#pxcv').addEventListener('mousemove',e=>{if(pxDown&&PX.tool!=='fill')pxPaint(e,false)});
addEventListener('mouseup',()=>{pxDown=false});

function pxSetColour(v){
  PX.colour=v;
  for(const el of $('#pxpal').querySelectorAll('i'))
    el.className=(+el.dataset.v===v?'on':'')+(+el.dataset.v===0?' tr':'');
  $('#pxcur').textContent = v ? `0x${v.toString(16).toUpperCase()} ${argbCss(v)}`
                              : 'transparent';
  const sw=$('#pxcurswatch');
  sw.classList.toggle('none', !v);
  sw.style.background = v ? argbCss(v) : '';
}

function pxPalette(){
  // Transparent first, then the 64 opaque ARGB2222 values.
  const cells=[{v:0,css:''}].concat(argbSwatches().map(s=>({v:s.v,css:s.css})));
  $('#pxpal').innerHTML=cells.map(c=>
    `<i data-v="${c.v}" class="${c.v?'':'tr'}" style="${c.v?`background:${c.css}`:''}"
       title="${c.v?'0x'+c.v.toString(16).toUpperCase():'transparent'}"></i>`).join('');
  for(const el of $('#pxpal').querySelectorAll('i'))
    el.onclick=()=>pxSetColour(+el.dataset.v);
  pxSetColour(0xFF);
}

// Compact icon toolbar -- .act (the rail's own icon+label convention) reused here
// instead of the four full-width text buttons this used to be.
for(const [id,tool] of [['toolpen','pen'],['toolfill','fill'],['toolpick','pick'],
                        ['toolerase','erase']])
  $('#'+id).onclick=()=>{
    PX.tool=tool;
    for(const t of ['toolpen','toolfill','toolpick','toolerase'])
      $('#'+t).classList.toggle('on', t===id);
  };

$('#pxundo').onclick=()=>{
  if(!PX.undo.length) return;
  PX.redo.push(pxSnapshotState());
  pxRestoreState(PX.undo.pop());
  pxDraw();
};
$('#pxredo').onclick=()=>{
  if(!PX.redo.length) return;
  PX.undo.push(pxSnapshotState());
  pxRestoreState(PX.redo.pop());
  pxDraw();
};
// Clears the frame on screen, not the whole sprite -- matching Aseprite's own "clear
// cel" rather than wiping every pose because one needed a restart.
$('#pxclear').onclick=()=>{
  pxSnapshot();
  const per=pxPer();
  PX.data.fill(0, PX.frame*per, (PX.frame+1)*per);
  pxDraw();
};
for(const id of ['pxw','pxh'])
  $('#'+id).addEventListener('change',()=>
    pxInit(+$('#pxw').value,+$('#pxh').value,PX.frames));
$('#pxzoom').addEventListener('input',()=>{PX.zoom=+$('#pxzoom').value; pxDraw()});
$('#pxgrid').addEventListener('change',pxDraw);

async function pxLoadList(select){
  const files=await (await fetch('/api/art')).json();
  $('#pxopen').innerHTML='<option value="">—</option>'+
    files.map(f=>`<option value="${f.path}">${f.path}</option>`).join('');
  // Kept on S so the Declare panel can offer the same PNGs without a second request:
  // the sheet a sprite points at is nearly always the one just painted.
  S.art=files;
  const sh=$('#shsheet');
  if(sh){
    // `select` wins over the current value: after an import the sheet you just brought in
    // is the one you meant to slice, and leaving the old selection makes the import look
    // like it did nothing.
    const cur=select||sh.value;
    sh.innerHTML=files.map(f=>`<option${f.path===cur?' selected':''}>${f.path}</option>`)
      .join('');
  }
  drawSpriteForm();
  drawNineSliceForm();
}

// Importing art. Files arrive either from the picker or from a drop, and both end here:
// read as base64, posted, then the sheet lists reload with the new file selected.
//
// One at a time rather than in parallel. Two imports landing together can collide on a
// name, and the answer to "that already exists" is a question for the user -- which has
// to be asked about one file, in order, not about whichever of four requests replied
// first.
//
// `logId` is which tab is watching. The endpoint and the destination folder are the same
// wherever the file was dropped -- only the line that reports it differs.
async function importArt(files,logId){
  const log=$(logId||'#shimplog');
  const say=(msg,bad)=>{ log.className=bad?'bad':'dim'; log.textContent=msg };
  let last=null, done=0;
  for(const file of files){
    say(`importing ${file.name}…`);
    let data;
    try{
      data=await new Promise((ok,fail)=>{
        const fr=new FileReader();
        fr.onerror=()=>fail(new Error('could not be read'));
        // The result is a data: URL; everything after the comma is the base64 payload.
        fr.onload=()=>ok(String(fr.result).split(',')[1]||'');
        fr.readAsDataURL(file);
      });
    }catch(e){ say(`${file.name}: ${e.message}`,true); continue }

    let r=await post('/api/art/import',{name:file.name,data});
    if(!r.ok && /already exists/.test(r.error||'')){
      if(!confirm(`art/${file.name} already exists.\n\nReplace it?`)){
        say(`${file.name} skipped`); continue;
      }
      r=await post('/api/art/import',{name:file.name,data,replace:true});
    }
    if(!r.ok){ say(`${file.name}: ${r.error}`,true); continue }
    last=r.path; done++;
  }
  if(!last) return;
  // Both lists, whichever tab the file came in through: the two tabs read the same folder,
  // and a sheet imported on one being absent from the other is the same dead end again.
  await pxLoadList(last);
  if(typeof loadSheets==='function'){
    await loadSheets();
    const sel=$('#sheet');
    if(sel && [...sel.options].some(o=>o.value===last)){
      sel.value=last;
      // 'input', which is what the atlas fields are actually bound to. Assigning .value
      // fires nothing, so the region analysis would still be describing the old sheet.
      sel.dispatchEvent(new Event('input',{bubbles:true}));
    }
  }
  const f=S.art.find(a=>a.path===last)||{};
  // Bytes under a kilobyte. A 168-byte sheet rounding to "0 KB" reads as an import that
  // brought in nothing, which is the one thing the message exists to disprove.
  const size=!f.bytes ? ''
    : f.bytes<1024 ? ` — ${f.bytes} B` : ` — ${Math.round(f.bytes/1024)} KB`;
  say(done===1 ? `${last}${size}` : `${done} files imported, ${last} selected`);
}

// Wires one import well: a button that opens the picker, and the same box as a drop
// target. Called for both tabs, because "bring a file in" should not be two behaviours.
function wireImport(zoneId,fileId,pickId,logId){
  const zone=$(zoneId), input=$(fileId), pick=$(pickId);
  if(!zone||!input||!pick) return;
  pick.onclick=()=>input.click();
  input.onchange=async()=>{
    await importArt([...input.files],logId);
    // Cleared so picking the same file twice fires change again, which is what someone
    // does after replacing the file on disk.
    input.value='';
  };
  // dragover must be cancelled or the browser navigates to the dropped file instead,
  // which throws away the whole editor and any unsaved canvas with it.
  for(const ev of ['dragenter','dragover'])
    zone.addEventListener(ev,e=>{ e.preventDefault(); zone.classList.add('over') });
  for(const ev of ['dragleave','drop'])
    zone.addEventListener(ev,e=>{ e.preventDefault(); zone.classList.remove('over') });
  zone.addEventListener('drop',e=>importArt([...(e.dataTransfer.files||[])],logId));
}
wireImport('#shdrop','#shfile','#shpick','#shimplog');
wireImport('#atdrop','#atfile','#atpick','#atimplog');

$('#pxopen').addEventListener('change',async()=>{
  const path=$('#pxopen').value; if(!path) return;
  const r=await (await fetch('/api/sprite/read',{method:'POST',
    headers:{'content-type':'application/json'},body:JSON.stringify({path})})).json();
  if(r.error){ $('#pxnote').textContent=r.error; return }
  // Height is assumed to be whole frames of the current frame height where it divides
  // cleanly -- the importer's own convention -- and one frame otherwise.
  const frames=(PX.h && r.h % PX.h===0) ? r.h/PX.h : 1;
  PX.origin=null;
  if($('#pxtitle')) $('#pxtitle').textContent='Canvas';
  PX.w=r.w; PX.h=r.h/frames; PX.frames=frames; PX.frame=0;
  $('#pxw').value=PX.w; $('#pxh').value=PX.h;
  PX.data=Uint8Array.from(r.pixels); PX.undo=[]; PX.redo=[];
  $('#pxname').value=path;
  pxDraw();
  $('#pxnote').textContent=`Loaded ${r.w}x${r.h}${frames>1?` — ${frames} frame(s)`:''}.`;
});

$('#pxsave').onclick=async()=>{
  // A frame cut out of a sheet goes back where it came from. Falling through to the
  // whole-file write would replace an eight-pose sheet with one 16x24 pose, which is a
  // loss no undo in this editor reaches.
  if(PX.origin){
    const o=PX.origin;
    const r=await post('/api/frame/write',{sheet:o.sheet, x:o.x, y:o.y,
      w:PX.w, h:PX.h, pixels:Array.from(PX.data)});
    if(r.error){ $('#pxnote').textContent=r.error; return }
    $('#pxnote').textContent=`Wrote the frame back into ${o.sheet} at ${o.x},${o.y}.`;
    // Re-slice so the grid shows what was just painted rather than the stale thumbnail.
    if(SH.sheet===o.sheet){ const keep=SH.picks.slice(); await $('#shslice').onclick();
                            SH.picks=keep; drawSheetGrid() }
    // And the declared-frame strip, for the same reason: it is the other view of the same
    // pixels, and a thumbnail that still shows the pose you just repainted reads as a save
    // that did not happen.
    if($('#spsel') && $('#spsel').value) await spShowFrames($('#spsel').value);
    return;
  }

  let path=$('#pxname').value.trim();
  if(!path){ $('#pxnote').textContent='Give it a filename first.'; return }
  if(!path.includes('/')) path='art/'+path;
  const r=await (await fetch('/api/sprite/write',{method:'POST',
    headers:{'content-type':'application/json'},
    body:JSON.stringify({path,w:PX.w,h:pxTotalH(),pixels:Array.from(PX.data)})})).json();
  $('#pxnote').textContent=r.error?r.error
    :`Saved ${r.path} (${r.bytes} B). Declare it below, or import it as a tileset.`;
  if(r.ok){
    await pxLoadList();
    // The sheet just saved, at the size just painted: the Declare panel underneath is
    // almost always about this PNG, and retyping what the canvas already knows is the
    // step that used to send people to the manifest.
    $('#spsheet').value=r.path;
    $('#spfw').value=PX.w; $('#spfh').value=PX.h; $('#spn').value=PX.frames;
  }
};

// -------------------------------------------------------------------- code editor
//
// Highlighting and analysis are deliberately bare: one tokenising pass and three checks.
// Not a parser, and it does not try to be -- the value is in catching the cheap mistakes
// before an ARM compile does, and an ARM compile is the authority on everything else.
const CODE={path:null,clean:'',editable:false,symbols:null};

const C_KEYWORDS=new Set(('if else for while do switch case default break continue return '
 +'goto sizeof typedef struct union enum static const volatile extern inline register '
 +'restrict auto _Static_assert').split(' '));
const C_TYPES=new Set(('void char short int long float double signed unsigned bool '
 +'size_t int8_t int16_t int32_t int64_t uint8_t uint16_t uint32_t uint64_t '
 +'ptrdiff_t intptr_t uintptr_t').split(' '));

const esc=s=>s.replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');

// One left-to-right pass. Comments and strings are consumed whole so that a keyword or
// a brace inside them is never seen by anything downstream -- which is also what makes
// the brace check below trustworthy.
function cTokens(src){
  const out=[]; let i=0, n=src.length;
  const push=(cls,text)=>out.push({cls,text});
  while(i<n){
    const c=src[i], two=src.substr(i,2);
    if(two==='/*'){ const e=src.indexOf('*/',i+2); const j=e<0?n:e+2;
      push('tk-c',src.slice(i,j)); i=j; continue }
    if(two==='//'){ let j=src.indexOf('\n',i); if(j<0)j=n;
      push('tk-c',src.slice(i,j)); i=j; continue }
    if(c==='"'||c==="'"){
      let j=i+1;
      while(j<n){ if(src[j]==='\\'){j+=2;continue} if(src[j]===c){j++;break}
                  if(src[j]==='\n')break; j++ }
      push('tk-s',src.slice(i,j)); i=j; continue;
    }
    if(c==='#'&&(i===0||src[i-1]==='\n')){
      let j=src.indexOf('\n',i); if(j<0)j=n;
      push('tk-p',src.slice(i,j)); i=j; continue;
    }
    if(/[A-Za-z_]/.test(c)){
      let j=i; while(j<n&&/[A-Za-z0-9_]/.test(src[j]))j++;
      const w=src.slice(i,j);
      push(C_KEYWORDS.has(w)?'tk-k':C_TYPES.has(w)?'tk-t'
           :/^(pnx|Pnx|PNX)/.test(w)?'tk-x':'', w);
      i=j; continue;
    }
    if(/[0-9]/.test(c)){
      let j=i; while(j<n&&/[0-9a-fA-FxXuUlL.]/.test(src[j]))j++;
      push('tk-n',src.slice(i,j)); i=j; continue;
    }
    let j=i; while(j<n&&!/[A-Za-z0-9_#"'\\/]/.test(src[j]))j++;
    if(j===i)j=i+1;
    push('',src.slice(i,j)); i=j;
  }
  return out;
}

function highlight(){
  const src=$('#codetext').value;
  const bad=new Set(CODE.diagBad||[]);
  const html=cTokens(src).map(t=>{
    const cls=(t.cls==='tk-x'&&bad.has(t.text))?'tk-x tk-bad':t.cls;
    return cls?`<span class="${cls}">${esc(t.text)}</span>`:esc(t.text);
  }).join('');
  // A trailing newline keeps the last line scrollable to, matching the textarea.
  $('#codehl').querySelector('code').innerHTML=html+'\n';
  $('#codehl').dataset.ro=CODE.editable?'0':'1';
  // The textarea grows to its content so the wrapper scrolls both layers as one.
  const ta=$('#codetext');
  ta.style.height='auto';
  ta.style.height=Math.max(ta.scrollHeight,$('#codescroll').clientHeight)+'px';
}

// Three checks. Balance, unterminated strings, and unknown engine symbols -- the last
// being the one that earns its place: it catches `pnx_platform_exit` for
// `pnx_platform_quit` in the editor rather than after a full ARM compile.
function codeAnalyse(){
  const src=$('#codetext').value;
  const toks=cTokens(src);
  const diags=[];
  const lineOf=off=>src.slice(0,off).split('\n').length;

  let off=0, depth={'(':0,'[':0,'{':0}, opens=[];
  const close={')':'(',']':'[','}':'{'};
  for(const t of toks){
    if(t.cls==='tk-c'||t.cls==='tk-s'||t.cls==='tk-p'){
      if(t.cls==='tk-s'&&t.text.length<2)
        diags.push({line:lineOf(off),msg:'unterminated string or character literal'});
      off+=t.text.length; continue;
    }
    for(let k=0;k<t.text.length;k++){
      const ch=t.text[k];
      if(ch in depth){ depth[ch]++; opens.push({ch,off:off+k}) }
      else if(ch in close){
        const want=close[ch];
        if(depth[want]===0)
          diags.push({line:lineOf(off+k),msg:`unmatched '${ch}'`});
        else { depth[want]--;
          for(let z=opens.length-1;z>=0;z--) if(opens[z].ch===want){opens.splice(z,1);break} }
      }
    }
    off+=t.text.length;
  }
  for(const o of opens)
    diags.push({line:lineOf(o.off),msg:`'${o.ch}' is never closed`});

  const bad=[];
  if(CODE.symbols){
    const seen=new Set();
    let p=0;
    for(const t of toks){
      if(t.cls==='tk-x'&&!CODE.symbols.has(t.text)&&!seen.has(t.text)){
        seen.add(t.text); bad.push(t.text);
        diags.push({line:lineOf(p),
                    msg:`unknown engine symbol '${t.text}'`+nearest(t.text)});
      }
      p+=t.text.length;
    }
  }
  CODE.diagBad=bad;

  diags.sort((a,b)=>a.line-b.line);
  CODE.quick=diags;
  paintDiags();
}

// The compiler's diagnostics and the in-page ones share a panel, tagged by where they
// came from. They answer different questions -- the page checks what you are typing right
// now, the compiler checks what the file MEANS -- and merging them without saying which
// is which would make a stale compiler result look live.
function paintDiags(){
  const quick=(CODE.quick||[]).map(d=>({...d, src:'edit'}));
  const cc=(CODE.cc||[]).map(d=>({...d, src:d.level==='warning'?'warn':'cc'}));
  const all=[...cc, ...quick].sort((a,b)=>(a.line||0)-(b.line||0));

  $('#codediag').innerHTML=(CODE.ccnote?`<div class="note"><i></i><span>`
      +`${esc(CODE.ccnote)}</span></div>`:'')
    + all.slice(0,60).map(d=>
      `<div data-line="${d.line||0}"><i>${d.line?'line '+d.line:'—'}</i>`
      +`<b class="${d.src}">${d.src==='cc'?'error':(d.src==='warn'?'warn':'·')}</b>`
      +`<span>${esc(d.msg)}${d.note?' — '+esc(d.note):''}</span></div>`).join('');

  for(const el of $('#codediag').querySelectorAll('div[data-line]'))
    el.onclick=()=>{ const n=+el.dataset.line; if(n) gotoLine(n) };
}

// The compiler runs on the SAVED file, so it runs after a save and on open -- not on
// every keystroke, which would compile a file mid-word and report nonsense.
async function codeLint(){
  if(!CODE.path||!/\.(c|h)$/.test(CODE.path)){ CODE.cc=[]; CODE.ccnote=''; return }
  let r;
  try{
    r=await (await fetch('/api/code/lint',{method:'POST',
      headers:{'content-type':'application/json'},
      body:JSON.stringify({path:CODE.path})})).json();
  }catch(_){ return }
  CODE.cc=r.ok?r.diags:[];
  CODE.ccnote=r.ok
    ? (r.clean?`${r.compiler}: no complaints`:'')
    : r.why;
  paintDiags();
}

// Edit distance, not shared prefix. Prefix length looks reasonable and is not: every
// `pnx_platform_*` shares thirteen characters, so `pnx_platform_exit` matched
// `pnx_platform_audio_close` as readily as `pnx_platform_quit`. Distance ranks by how
// wrong the name actually is, which is the question being asked.
function editDistance(a,b,cap){
  if(Math.abs(a.length-b.length)>cap) return cap+1;
  let prev=Array.from({length:b.length+1},(_,i)=>i);
  for(let i=1;i<=a.length;i++){
    const cur=[i]; let best=i;
    for(let j=1;j<=b.length;j++){
      cur[j]=Math.min(prev[j]+1, cur[j-1]+1, prev[j-1]+(a[i-1]===b[j-1]?0:1));
      if(cur[j]<best) best=cur[j];
    }
    if(best>cap) return cap+1;      // whole row already too far; stop early
    prev=cur;
  }
  return prev[b.length];
}

function nearest(name){
  if(!CODE.symbols) return '';
  // A quarter of the name, capped: enough for a wrong suffix or a transposition,
  // not enough to suggest something unrelated with confidence.
  const cap=Math.min(5,Math.max(2,Math.floor(name.length/4)));
  let best=null, bestD=cap+1;
  for(const s of CODE.symbols){
    const d=editDistance(name,s,cap);
    if(d<bestD){ bestD=d; best=s; if(d===1) break }
  }
  return best?` — did you mean '${best}'?`:'';
}

function gotoLine(n){
  const ta=$('#codetext'), lines=ta.value.split('\n');
  let off=0; for(let i=0;i<n-1&&i<lines.length;i++) off+=lines[i].length+1;
  ta.focus(); ta.setSelectionRange(off,off+ (lines[n-1]||'').length);
}

// A real tree, not a flat list under directory headings. `src/c/pnx` alone is seven
// directories deep in places, and a flat list makes a project's own two files look like
// part of the engine. Collapsed state is kept per folder, and the engine subtree starts
// closed: it is read-only, it is the biggest thing here, and it is not what someone
// opened the tab to edit.
const CODEOPEN=new Set(['src','src/c']);

function codeNest(files){
  const root={dirs:new Map(), files:[]};
  for(const f of files){
    let node=root;
    const parts=f.path.replace(/\\/g,'/').split('/');
    const name=parts.pop();
    let sofar='';
    for(const p of parts){
      sofar=sofar?sofar+'/'+p:p;
      if(!node.dirs.has(p)) node.dirs.set(p,{dirs:new Map(),files:[],path:sofar});
      node=node.dirs.get(p);
    }
    node.files.push({...f,name});
  }
  return root;
}

function codeRender(node, depth){
  let html='';
  for(const [name,dir] of [...node.dirs.entries()].sort((a,b)=>a[0].localeCompare(b[0]))){
    const open=CODEOPEN.has(dir.path);
    const engine=dir.path.startsWith('src/c/pnx');
    html+=`<div class="cdir${engine?' ro':''}" data-dir="${dir.path}"
      style="padding-left:${depth*.7}rem">${open?'▾':'▸'} ${name}`
      +`${engine&&depth<3?' <small>engine</small>':''}</div>`;
    if(open) html+=codeRender(dir, depth+1);
  }
  for(const f of node.files.sort((a,b)=>a.name.localeCompare(b.name))){
    html+=`<button data-path="${f.path}" class="${f.editable?'':'ro'}"
      style="padding-left:${depth*.7+.85}rem">${f.name}`
      +`${f.generated?' <small>gen</small>':''}</button>`;
  }
  return html;
}

async function codeTree(){
  if(!CODE.symbols){
    try{ CODE.symbols=new Set(await (await fetch('/api/code/symbols')).json()) }
    catch(_){ CODE.symbols=null }
  }
  if(!CODE.files) CODE.files=await (await fetch('/api/code/tree')).json();

  $('#codelist').innerHTML=codeRender(codeNest(CODE.files), 0);
  for(const b of $('#codelist').querySelectorAll('button'))
    b.onclick=()=>codeOpen(b.dataset.path);
  for(const d of $('#codelist').querySelectorAll('.cdir'))
    d.onclick=()=>{
      const p=d.dataset.dir;
      CODEOPEN.has(p)?CODEOPEN.delete(p):CODEOPEN.add(p);
      codeTree();
      if(CODE.path)
        for(const b of $('#codelist').querySelectorAll('button'))
          b.classList.toggle('on', b.dataset.path===CODE.path);
    };
}

async function codeOpen(path){
  if(CODE.path && $('#codetext').value!==CODE.clean &&
     !confirm('Discard unsaved changes?')) return;
  const r=await (await fetch('/api/code/read',{method:'POST',
    headers:{'content-type':'application/json'},body:JSON.stringify({path})})).json();
  if(r.error){ $('#codenote').textContent=r.error; return }
  CODE.path=path; CODE.clean=r.text; CODE.editable=r.editable;
  $('#codetext').value=r.text;
  $('#codetext').readOnly=!r.editable;
  $('#codepath').textContent=path;
  $('#codenote').textContent=r.note||'';
  $('#codesave').disabled=true;
  for(const b of $('#codelist').querySelectorAll('button'))
    b.classList.toggle('on', b.dataset.path===path);
  codeDirty();
  // Analyse first: highlighting reads its list of unknown symbols.
  if(/\.(c|h)$/.test(path)){ codeAnalyse(); codeLint(); }
  else { CODE.diagBad=[]; CODE.cc=[]; CODE.quick=[]; CODE.ccnote=''; paintDiags() }
  highlight();
  $('#codescroll').scrollTop=0;
}

function codeDirty(){
  const dirty=CODE.editable && $('#codetext').value!==CODE.clean;
  $('#codedirty').textContent=dirty?'● unsaved':'';
  $('#codesave').disabled=!dirty;
}
let codeTimer=null;
$('#codetext').addEventListener('input',()=>{
  codeDirty();
  highlight();                       // immediate: the overlay must not lag the caret
  clearTimeout(codeTimer);           // analysis can wait for a pause in typing
  codeTimer=setTimeout(()=>{
    if(/\.(c|h)$/.test(CODE.path||'')){ codeAnalyse(); highlight() }
  },300);
});
$('#codetext').addEventListener('scroll',()=>{
  // Only the wrapper scrolls, but a long line can still shift the textarea itself.
  $('#codehl').style.transform=`translateX(${-$('#codetext').scrollLeft}px)`;
});

// Tab inserts a tab rather than leaving the field, which is the single thing that makes
// a textarea usable for code at all.
$('#codetext').addEventListener('keydown',e=>{
  if(e.key==='Tab'){
    e.preventDefault();
    const t=e.target, s=t.selectionStart, en=t.selectionEnd;
    t.value=t.value.slice(0,s)+'  '+t.value.slice(en);
    t.selectionStart=t.selectionEnd=s+2;
    codeDirty();
  }
  if((e.ctrlKey||e.metaKey)&&e.key==='s'){ e.preventDefault(); codeSave() }
});

async function codeSave(){
  if(!CODE.path||!CODE.editable) return;
  const text=$('#codetext').value;
  const r=await (await fetch('/api/code/write',{method:'POST',
    headers:{'content-type':'application/json'},
    body:JSON.stringify({path:CODE.path,text})})).json();
  if(r.error){ $('#codenote').textContent=r.error; return }
  CODE.clean=text; codeDirty();
  $('#codenote').textContent=`saved ${r.bytes} B`;
  codeLint();          // the compiler reads the file, so this is when it can say anything
}
$('#codesave').onclick=codeSave;

// ----------------------------------------------------------------- project picker
let pickerMode='open';

async function openProject(path){
  const r=await (await fetch('/api/project/open',{method:'POST',
    headers:{'content-type':'application/json'},body:JSON.stringify({path})})).json();
  if(!r.ok){ $('#pickernote').textContent=r.error; return }
  // A different project means every cached atlas, map and palette is wrong, so the
  // simplest correct thing is to start over.
  location.reload();
}

async function drawPicker(path){
  const b=await (await fetch('/api/project/browse'+(path?'?path='+encodeURIComponent(path):''))).json();
  $('#pickerpath').value=b.path;
  $('#pickerlist').innerHTML=b.entries.length
    ? b.entries.map(e=>`<button data-path="${e.path}" class="${e.project?'isproj':''}"
        >${e.project?'◆':'▸'} ${e.name}</button>`).join('')
    : '<small>(no subfolders)</small>';
  for(const el of $('#pickerlist').querySelectorAll('button'))
    el.onclick=()=>drawPicker(el.dataset.path);

  if(pickerMode==='open'){
    $('#pickerok').disabled=!b.is_project;
    $('#pickernote').textContent=b.is_project
      ? 'This folder is a project.'
      : 'Not a project — pick a folder containing a .pknproj or an assets.toml. ◆ marks one.';
  }else{
    $('#pickerok').disabled=false;
    $('#pickernote').textContent='A new folder is created inside this one.';
  }
  return b;
}

function showPicker(mode){
  pickerMode=mode;
  $('#picker').style.display='';
  $('#pickertitle').textContent=mode==='open'?'Open a project':'New project';
  $('#newfields').style.display=mode==='new'?'':'none';
  $('#pickerok').textContent=mode==='open'?'Open this folder':'Create it here';
  drawPicker((S.data&&S.data.paths&&S.data.paths.root)||null);
}

$('#projopen').onclick=()=>showPicker('open');
$('#projnew').onclick=()=>showPicker('new');
$('#pickercancel').onclick=()=>{$('#picker').style.display='none'};
$('#pickergo').onclick=()=>drawPicker($('#pickerpath').value);
$('#pickerup').onclick=async()=>{
  const b=await (await fetch('/api/project/browse?path='+
    encodeURIComponent($('#pickerpath').value))).json();
  if(b.parent) drawPicker(b.parent);
};

$('#pickerok').onclick=async()=>{
  if(pickerMode==='open'){ openProject($('#pickerpath').value); return }
  const name=$('#newname').value.trim();
  const folder=$('#newfolder').value.trim()||name.toLowerCase().replace(/[^a-z0-9]+/g,'-');
  if(!name){ $('#pickernote').textContent='Name the project first.'; return }
  const r=await (await fetch('/api/project/create',{method:'POST',
    headers:{'content-type':'application/json'},
    body:JSON.stringify({parent:$('#pickerpath').value,folder,name,
                         author:$('#newauthor').value})})).json();
  if(!r.ok){ $('#pickernote').textContent=r.error; return }
  location.reload();
};

$('#projadopt').onclick=async()=>{
  await fetch('/api/project/adopt',{method:'POST'});
  location.reload();
};

async function engineOwn(on){
  await fetch('/api/engine/own',{method:'POST',
    headers:{'content-type':'application/json'},body:JSON.stringify({on})});
  location.reload();
}
$('#engown').onchange=()=>{
  if($('#engown').checked) return engineOwn(true);
  $('#engown').checked=true;      // re-tracking is destructive; route it through the button
  alert('Use "Discard changes and re-track" — going back replaces your engine copy.');
};
$('#engresync').onclick=()=>{
  if(confirm('Replace src/c/pnx with the editor engine copy? Your modifications there '
             +'are discarded.')) engineOwn(false);
};

load();
