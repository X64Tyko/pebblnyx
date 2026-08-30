// The Arrangement sub-view of the Music tab: clips (pure note sequences, no instrument of
// their own -- see compile_arrangement in tools/pnx_assets.py) placed on 4 fixed lanes (one
// per sequencer channel), plus the song-level marker list. This is the ONLY authoring surface
// a new song gets (drawMusic in app.js only shows the legacy tracker/piano-roll for a song
// that predates arrangement -- see songs()'s `arrangement` flag).
//
// Placement dragging reuses the exact mechanism the Sprites tab's clip builder already has
// (app.js:1702, sprites.js's frame chips): native HTML5 drag-and-drop, dataTransfer carrying a
// small JSON payload, a drop target that turns it into a placement. Existing blocks are
// themselves draggable the same way, carrying enough of their own identity in the payload to
// be removed from their old spot rather than duplicated.

const ARRANGE_MIN_ROWS = 32;
const ARRANGE_ZOOM_MIN = 4, ARRANGE_ZOOM_MAX = 32, ARRANGE_ZOOM_STEP = 2;

// Pixels per row, live -- a session-only zoom level (MU.zoom), not saved to the
// manifest. A short clip at the old fixed 14px/row truncated its own name; a long song
// was unreadably wide. Read through one function so every drawer agrees on the current
// scale without each reaching into MU directly.
function arrangePxPerRow(){
  return MU.zoom || 14;
}

function arrangeLog(msg, bad){
  const el = $('#marrangelog');
  el.className = bad === false ? 'ok' : (bad ? 'bad' : 'dim');
  el.textContent = msg || '';
}

// The widest a placement (or a marker, or the loop point) reaches, so the lanes/ruler
// are wide enough to show everything without the last one getting clipped.
function arrangeSpan(s){
  let end = ARRANGE_MIN_ROWS;
  const clipLen = name => {
    const c = s.clips.find(c => c.name === name);
    return c ? c.rows.length : 0;
  };
  for(const t of s.tracks)
    for(const p of t.placement)
      if('clip' in p) end = Math.max(end, p.start + clipLen(p.clip));
  for(const m of s.markers) end = Math.max(end, m.at + 1);
  if(s.loop_start != null) end = Math.max(end, s.loop_start + 1);
  return end;
}

// A deterministic hue per clip name, so the same clip reads as the same color in the
// clip list, the drag palette and every block placed on the timeline -- the one thing
// that told two different clips apart before this was their (often truncated) name.
function clipHue(name){
  let h = 0;
  for(let i = 0; i < name.length; i++) h = (h * 31 + name.charCodeAt(i)) >>> 0;
  return h % 360;
}

// `m:ss`, from the row span and the song's own tempo -- a tracker row is a sixteenth
// note (see audio-preview.js's own row_ms), so this matches what "Play arrangement"
// actually takes to loop around once.
function formatDuration(rows, tempo){
  const totalSec = rows * (60 / (tempo || 120) / 4);
  const m = Math.floor(totalSec / 60), sec = Math.round(totalSec % 60);
  return `${m}:${String(sec).padStart(2, '0')}`;
}

function drawArrangement(s){
  drawClipList(s);
  drawClipPalette(s);
  drawLanes(s);
  drawMarkerList(s);
  $('#mresolution').value = s.resolution || 16;
  $('#mduration').textContent = `length ${formatDuration(arrangeSpan(s), s.tempo)}`;
}

// ---------------------------------------------------------------------------------- clips

// Editing a clip's actual notes happens in the shared tracker/piano-roll below (#mrows/
// #mroll -- see muTargetChannels/muTargetSourceRows/muCommitRows in app.js), not here.
// This list is just clip management: which clips exist, how long each is, pick one to
// edit, or delete it.
function drawClipList(s){
  const box = $('#mcliplist');
  box.innerHTML = '';
  for(const clip of s.clips){
    const row = document.createElement('div');
    row.className = 'mini';
    row.style.marginBottom = '.2rem';

    const swatch = document.createElement('i');
    swatch.className = 'clipswatch';
    swatch.style.setProperty('--hue', clipHue(clip.name));
    row.appendChild(swatch);

    const label = document.createElement('b');
    label.textContent = clip.name;
    label.style.minWidth = '6rem';
    label.style.display = 'inline-block';
    row.appendChild(label);

    const rowsLen = document.createElement('span');
    rowsLen.className = 'dim';
    rowsLen.textContent = `${clip.rows.length} row${clip.rows.length === 1 ? '' : 's'}`;
    row.appendChild(rowsLen);

    // +/- change the clip's own length in place -- padding with holds or truncating --
    // through the same /api/song/clip save_clip already uses for every other edit, so
    // no new endpoint is needed for this.
    const shrink = document.createElement('button');
    shrink.textContent = '−';
    shrink.title = 'remove the last row';
    shrink.disabled = clip.rows.length <= 1;
    shrink.onclick = async () => {
      const rows = clip.rows.slice(0, -1);
      const r = await post('/api/song/clip', {name: s.name, clip: clip.name, rows});
      if(!r.ok){ arrangeLog(r.error, true); return }
      await reload(); drawMusic();
    };
    row.appendChild(shrink);
    const grow = document.createElement('button');
    grow.textContent = '+';
    grow.title = 'append a held row';
    grow.disabled = clip.rows.length >= 255;
    grow.onclick = async () => {
      const rows = clip.rows.concat(['.']);
      const r = await post('/api/song/clip', {name: s.name, clip: clip.name, rows});
      if(!r.ok){ arrangeLog(r.error, true); return }
      await reload(); drawMusic();
    };
    row.appendChild(grow);

    const edit = document.createElement('button');
    edit.textContent = clip.name === MU.clip ? 'editing' : 'Edit';
    edit.disabled = clip.name === MU.clip;
    edit.onclick = () => {
      MU.clip = clip.name;
      $('#mpat').value = clip.name;
      if(MU.view === 'piano') drawPianoRoll(); else drawTracker();
      drawClipList(s); // refresh which row shows "editing"
    };
    row.appendChild(edit);

    const ren = document.createElement('button');
    ren.textContent = 'Rename';
    ren.onclick = () => {
      const box2 = document.createElement('input');
      box2.value = clip.name;
      box2.size = 10;
      const commit = async () => {
        const to = box2.value.trim();
        if(!to || to === clip.name){ drawClipList(s); return }
        const r = await post('/api/song/clip/rename', {name: s.name, clip: clip.name, to});
        if(!r.ok){ arrangeLog(r.error, true); drawClipList(s); return }
        await reload(); drawMusic();
      };
      box2.onblur = commit;
      box2.onkeydown = e => { if(e.key === 'Enter') box2.blur(); if(e.key === 'Escape') drawClipList(s) };
      label.replaceWith(box2);
      box2.focus(); box2.select();
    };
    row.appendChild(ren);

    const dup = document.createElement('button');
    dup.textContent = 'Duplicate';
    dup.onclick = async () => {
      let to = `${clip.name}_copy`, n = 2;
      while(s.clips.some(c => c.name === to)) to = `${clip.name}_copy${n++}`;
      const r = await post('/api/song/clip/add', {name: s.name, clip: to, rows: clip.rows.slice()});
      if(!r.ok){ arrangeLog(r.error, true); return }
      await reload(); drawMusic();
    };
    row.appendChild(dup);

    const del = document.createElement('button');
    del.textContent = '×';
    del.title = `remove clip ${clip.name}`;
    del.onclick = async () => {
      const r = await post('/api/song/clip/remove', {name: s.name, clip: clip.name});
      if(!r.ok){ arrangeLog(r.error, true); return }
      await reload(); drawMusic();
    };
    row.appendChild(del);

    box.appendChild(row);
  }
}

$('#mclipadd').onclick = async () => {
  const s = muSong();
  if(!s) return;
  const name = $('#mclipname').value.trim();
  const n = Math.max(1, +$('#mcliprows').value || 8);
  if(!name){ $('#mcliplog').textContent = 'Name the clip first.'; return }
  const r = await post('/api/song/clip/add', {name: s.name, clip: name,
    rows: Array(n).fill('.')});
  const log = $('#mcliplog');
  if(!r.ok){ log.className = 'bad'; log.textContent = r.error; return }
  log.className = 'ok'; log.textContent = `${name} added.`;
  $('#mclipname').value = '';
  MU.clip = name; // straight into the shared tracker/piano-roll to fill it in
  await reload(); drawMusic();
};

// -------------------------------------------------------------------------------- palette

function drawClipPalette(s){
  const box = $('#marrangeclips');
  box.innerHTML = '';
  for(const clip of s.clips){
    const chip = document.createElement('div');
    chip.className = 'clipchip';
    chip.style.setProperty('--hue', clipHue(clip.name));
    chip.draggable = true;
    chip.title = `${clip.rows.length} rows -- drag onto a channel below`;
    const b = document.createElement('b');
    b.textContent = clip.name;
    chip.appendChild(b);
    chip.addEventListener('dragstart', ev => {
      ev.dataTransfer.effectAllowed = 'copy';
      ev.dataTransfer.setData('text/plain', JSON.stringify({clip: clip.name}));
    });
    box.appendChild(chip);
  }
}

// ---------------------------------------------------------------------------------- lanes

function clipLenOf(s, name){
  const c = s.clips.find(c => c.name === name);
  return c ? c.rows.length : 0;
}

function drawLanes(s){
  const wrap = $('#marrangelanes');
  wrap.innerHTML = '';
  const px = arrangePxPerRow();
  // .arrangelane is a flex row with no explicit width, so as a block-level box it always
  // stretches to fill this container -- regardless of how far its own children (the
  // ruler, the actual .arrangetrack drop target) reach. Left alone, that means the lane
  // visibly extends far past the real, droppable content whenever there's little placed
  // yet, and a drop attempted in that gap silently does nothing: it's lane background,
  // not .arrangetrack. Padding the span to the container's own visible width keeps what
  // you SEE as draggable-onto matching what actually is, rather than a track that looks
  // like it spans the screen but functionally clamps at whatever arrangeSpan() alone
  // would have returned.
  const rootPx = parseFloat(getComputedStyle(document.documentElement).fontSize) || 16;
  const labelPx = 3.4 * rootPx;
  const fillSpan = Math.max(0, Math.ceil((wrap.clientWidth - labelPx) / px));
  const span = Math.max(arrangeSpan(s), fillSpan);
  const widthPx = span * px;

  const ruler = document.createElement('div');
  ruler.className = 'arrangeruler';
  ruler.style.marginLeft = '3.4rem';
  ruler.style.width = widthPx + 'px';
  ruler.title = 'click to set the loop point here';
  for(let r = 0; r < span; r += 8){
    const tick = document.createElement('i');
    tick.style.left = (r * px) + 'px';
    tick.textContent = r;
    ruler.appendChild(tick);
  }
  ruler.addEventListener('click', ev => {
    const rect = ruler.getBoundingClientRect();
    const row = Math.max(0, Math.round((ev.clientX - rect.left) / px));
    setLoopStart(s, row);
  });
  wrap.appendChild(ruler);

  // One timeline-wide strip for named markers AND the loop point, above the lanes --
  // replacing the old per-lane bare line with an actual visible, draggable, LABELED
  // flag (a marker's name used to only exist in a hover tooltip).
  const strip = document.createElement('div');
  strip.className = 'arrangemarkerstrip';
  strip.style.marginLeft = '3.4rem';
  strip.style.width = widthPx + 'px';
  for(const m of s.markers){
    const mk = document.createElement('div');
    mk.className = 'markerflag';
    mk.style.left = (m.at * px) + 'px';
    mk.title = `marker ${m.name} @ row ${m.at} -- drag to move`;
    mk.textContent = m.name;
    mk.draggable = true;
    mk.addEventListener('dragstart', ev => {
      ev.dataTransfer.effectAllowed = 'move';
      ev.dataTransfer.setData('text/plain', JSON.stringify({marker: m.name}));
    });
    strip.appendChild(mk);
  }
  if(s.loop_start != null){
    const lp = document.createElement('div');
    lp.className = 'loopflag';
    lp.style.left = (s.loop_start * px) + 'px';
    lp.title = `loop point @ row ${s.loop_start} -- drag to move`;
    lp.draggable = true;
    lp.textContent = '↻'; // ↻
    const x = document.createElement('span');
    x.className = 'arrangex';
    x.title = 'clear the loop point';
    x.textContent = '×';
    x.onclick = ev => { ev.stopPropagation(); setLoopStart(s, null); };
    lp.appendChild(x);
    lp.addEventListener('dragstart', ev => {
      ev.dataTransfer.effectAllowed = 'move';
      ev.dataTransfer.setData('text/plain', JSON.stringify({loop: true}));
    });
    strip.appendChild(lp);
  }
  strip.addEventListener('dragover', ev => { ev.preventDefault(); ev.dataTransfer.dropEffect = 'move' });
  strip.addEventListener('drop', ev => {
    ev.preventDefault();
    let payload;
    try{ payload = JSON.parse(ev.dataTransfer.getData('text/plain')) }catch(_){ return }
    const rect = strip.getBoundingClientRect();
    const row = Math.max(0, Math.round((ev.clientX - rect.left) / px));
    if(payload && payload.loop) setLoopStart(s, row);
    else if(payload && payload.marker) moveMarker(s, payload.marker, row);
  });
  wrap.appendChild(strip);

  // Hidden until "Play arrangement" starts it moving -- see playArrangement in
  // audio-preview.js, which owns this element by id from here on.
  const playhead = document.createElement('div');
  playhead.id = 'marrangeplayhead';
  playhead.className = 'arrangeplayhead';
  playhead.style.left = '3.4rem';
  playhead.style.display = 'none';
  wrap.appendChild(playhead);

  for(let ch = 0; ch < 4; ch++){
    const track = s.tracks.find(t => t.channel === ch) || {channel: ch, placement: []};
    const lane = document.createElement('div');
    lane.className = 'arrangelane';

    const label = document.createElement('b');
    const name = document.createElement('span');
    name.textContent = 'ch ' + ch;
    label.appendChild(name);
    // Editor-only auditioning aids -- the engine has exactly one global volume, no
    // per-channel mute/pan (see pnx_music_set_volume), so this is a preview convenience,
    // never written to the manifest.
    const mute = document.createElement('button');
    mute.className = 'lanebtn' + (MU.channelMute[ch] ? ' on' : '');
    mute.textContent = 'M';
    mute.title = 'mute this channel during preview (not saved)';
    mute.onclick = () => { MU.channelMute[ch] = !MU.channelMute[ch]; drawLanes(s) };
    const solo = document.createElement('button');
    solo.className = 'lanebtn' + (MU.channelSolo[ch] ? ' on' : '');
    solo.textContent = 'S';
    solo.title = 'solo this channel during preview (not saved)';
    solo.onclick = () => { MU.channelSolo[ch] = !MU.channelSolo[ch]; drawLanes(s) };
    label.appendChild(mute);
    label.appendChild(solo);
    lane.appendChild(label);

    const area = document.createElement('div');
    area.className = 'arrangetrack';
    area.style.width = widthPx + 'px';
    area.dataset.channel = String(ch);

    for(const p of track.placement){
      if('clip' in p){
        const len = clipLenOf(s, p.clip);
        const block = document.createElement('div');
        block.className = 'arrangeblock';
        block.style.setProperty('--hue', clipHue(p.clip));
        block.style.left = (p.start * px) + 'px';
        block.style.width = Math.max(1, len * px - 2) + 'px';
        block.draggable = true;
        block.tabIndex = 0;
        block.title = `${p.clip} @ row ${p.start} -- click to select, Delete to remove`;
        block.textContent = p.clip;
        if(MU.selectedPlacement && MU.selectedPlacement.channel === ch
           && MU.selectedPlacement.start === p.start && MU.selectedPlacement.clip === p.clip)
          block.classList.add('selected');
        const x = document.createElement('span');
        x.className = 'arrangex';
        x.textContent = '×';
        x.onclick = ev => { ev.stopPropagation(); removePlacement(s, ch, p) };
        block.appendChild(x);
        block.addEventListener('click', () => {
          MU.selectedPlacement = {channel: ch, start: p.start, clip: p.clip};
          drawLanes(s);
          const again = area.querySelector('.arrangeblock.selected');
          if(again) again.focus();
        });
        block.addEventListener('keydown', ev => {
          if(ev.key === 'Delete' || ev.key === 'Backspace'){
            ev.preventDefault();
            removePlacement(s, ch, p);
          }
        });
        block.addEventListener('dragstart', ev => {
          ev.dataTransfer.effectAllowed = 'move';
          ev.dataTransfer.setData('text/plain',
            JSON.stringify({clip: p.clip, moveFrom: ch, moveStart: p.start}));
        });
        area.appendChild(block);
      }else{
        const pin = document.createElement('div');
        pin.className = 'arrangepin';
        pin.style.left = (p.start * px) + 'px';
        pin.title = `instrument -> ${p.instrument} @ row ${p.start} (click to remove)`;
        const b2 = document.createElement('b');
        b2.textContent = 'i' + p.instrument;
        pin.appendChild(b2);
        pin.onclick = () => removePlacement(s, ch, p);
        area.appendChild(pin);
      }
    }

    // dragenter needs its own preventDefault() too, not just dragover's -- some browsers
    // never fire drop at all if the FIRST event over a target went unhandled, even when
    // every dragover after it calls preventDefault correctly.
    area.addEventListener('dragenter', ev => ev.preventDefault());
    area.addEventListener('dragover', ev => {
      ev.preventDefault();
      ev.dataTransfer.dropEffect = 'move';
      area.classList.add('over');
    });
    area.addEventListener('dragleave', () => area.classList.remove('over'));
    area.addEventListener('drop', ev => {
      ev.preventDefault();
      area.classList.remove('over');
      let payload;
      try{ payload = JSON.parse(ev.dataTransfer.getData('text/plain')) }catch(_){ return }
      if(!payload || !payload.clip) return;
      const rect = area.getBoundingClientRect();
      const start = Math.max(0, Math.round((ev.clientX - rect.left) / px));
      dropClip(s, ch, payload, start);
    });
    area.addEventListener('contextmenu', ev => {
      ev.preventDefault();
      const rect = area.getBoundingClientRect();
      const start = Math.max(0, Math.round((ev.clientX - rect.left) / px));
      showInstrumentPicker(s, ch, start, ev.clientX, ev.clientY);
    });

    lane.appendChild(area);
    wrap.appendChild(lane);
  }
}

$('#mzoomin').onclick = () => {
  const s = muSong(); if(!s) return;
  MU.zoom = Math.min(ARRANGE_ZOOM_MAX, arrangePxPerRow() + ARRANGE_ZOOM_STEP);
  drawLanes(s);
};
$('#mzoomout').onclick = () => {
  const s = muSong(); if(!s) return;
  MU.zoom = Math.max(ARRANGE_ZOOM_MIN, arrangePxPerRow() - ARRANGE_ZOOM_STEP);
  drawLanes(s);
};

async function dropClip(s, toChannel, payload, start){
  const track = s.tracks.find(t => t.channel === toChannel);
  const placements = track ? track.placement.slice() : [];
  placements.push({clip: payload.clip, start});

  // A block dragged from another channel (or another spot on this one) moves rather than
  // duplicates -- remove it from wherever it came from first.
  if(payload.moveFrom !== undefined){
    if(payload.moveFrom === toChannel){
      const i = placements.findIndex((p, idx) =>
        idx < placements.length - 1 && p.clip === payload.clip && p.start === payload.moveStart);
      if(i >= 0) placements.splice(i, 1);
    }else{
      const fromTrack = s.tracks.find(t => t.channel === payload.moveFrom);
      if(fromTrack){
        const rest = fromTrack.placement.filter(p =>
          !('clip' in p && p.clip === payload.clip && p.start === payload.moveStart));
        const r = await post('/api/song/track',
          {name: s.name, channel: payload.moveFrom, placements: rest});
        if(!r.ok){ arrangeLog(r.error, true); return }
      }
    }
  }

  const r = await post('/api/song/track', {name: s.name, channel: toChannel, placements});
  if(!r.ok){ arrangeLog(r.error, true); return }
  arrangeLog('');
  await reload(); drawMusic();
}

async function removePlacement(s, channel, placement){
  const track = s.tracks.find(t => t.channel === channel);
  if(!track) return;
  const placements = track.placement.filter(p => p !== placement);
  const r = await post('/api/song/track', {name: s.name, channel, placements});
  if(!r.ok){ arrangeLog(r.error, true); return }
  const sel = MU.selectedPlacement;
  if(sel && sel.channel === channel && 'clip' in placement
     && sel.clip === placement.clip && sel.start === placement.start)
    MU.selectedPlacement = null;
  await reload(); drawMusic();
}

// Replaces a right-click -> browser prompt() (blocking, inconsistent with every other
// control in this tab) with a small floating <select> at the click point -- same
// right-click gesture, populated from the song's own instruments so there's nothing to
// mistype.
function showInstrumentPicker(s, channel, start, clientX, clientY){
  if(!s.instruments.length) return;
  const existing = document.getElementById('minstpicker');
  if(existing) existing.remove();

  const sel = document.createElement('select');
  sel.id = 'minstpicker';
  sel.className = 'floatpicker';
  sel.style.left = clientX + 'px';
  sel.style.top = clientY + 'px';
  const placeholder = document.createElement('option');
  placeholder.textContent = `instrument @ row ${start}…`;
  placeholder.disabled = true; placeholder.selected = true; placeholder.value = '';
  sel.appendChild(placeholder);
  s.instruments.forEach((ins, i) => {
    const o = document.createElement('option');
    o.value = String(i);
    o.textContent = `${i} - ${ins.name || ins.wave}`;
    sel.appendChild(o);
  });
  document.body.appendChild(sel);
  sel.focus();

  const close = () => sel.remove();
  sel.addEventListener('change', async () => {
    const inst = parseInt(sel.value, 10);
    close();
    if(!Number.isFinite(inst)) return;
    const track = s.tracks.find(t => t.channel === channel);
    const placements = (track ? track.placement.slice() : [])
      .concat([{instrument: inst, start}]);
    const r = await post('/api/song/track', {name: s.name, channel, placements});
    if(!r.ok){ arrangeLog(r.error, true); return }
    await reload(); drawMusic();
  });
  // A click outside blurs the <select> without a change -- close it rather than leave a
  // stray floating control behind. Delayed so a genuine `change` fires first.
  sel.addEventListener('blur', () => setTimeout(close, 150));
}

async function setLoopStart(s, row){
  const r = await post('/api/song/loopstart', {name: s.name, loop_start: row});
  if(!r.ok){ arrangeLog(r.error, true); return }
  await reload(); drawMusic();
}

async function moveMarker(s, name, at){
  const markers = s.markers.map(m => m.name === name ? {name: m.name, at} : m);
  const r = await post('/api/song/markers', {name: s.name, markers});
  if(!r.ok){ arrangeLog(r.error, true); return }
  await reload(); drawMusic();
}

$('#mresolution').onchange = async () => {
  const s = muSong();
  if(!s) return;
  const r = await post('/api/song/meta', {name: s.name, resolution: +$('#mresolution').value});
  if(!r.ok){ arrangeLog(r.error, true); return }
  await reload(); drawMusic();
};

// --------------------------------------------------------------------------------- markers

function drawMarkerList(s){
  const box = $('#mmarkerlist');
  box.innerHTML = '';
  for(const m of s.markers){
    const row = document.createElement('div');
    row.className = 'mini';

    const nameBox = document.createElement('input');
    nameBox.value = m.name;
    nameBox.size = 8;
    nameBox.title = 'marker name';
    const atBox = document.createElement('input');
    atBox.type = 'number';
    atBox.min = 0;
    atBox.style.width = '4rem';
    atBox.value = m.at;
    atBox.title = 'row';

    const commit = async () => {
      const name = nameBox.value.trim();
      const at = +atBox.value;
      if(!name || (name === m.name && at === m.at)){ nameBox.value = m.name; atBox.value = m.at; return }
      const markers = s.markers.map(x => x === m ? {name, at} : x);
      const r = await post('/api/song/markers', {name: s.name, markers});
      if(!r.ok){ $('#mmarkerlog').textContent = r.error; drawMarkerList(s); return }
      await reload(); drawMusic();
    };
    nameBox.onchange = commit;
    atBox.onchange = commit;
    row.appendChild(nameBox);
    row.appendChild(document.createTextNode(' @ row '));
    row.appendChild(atBox);

    const del = document.createElement('button');
    del.textContent = '×';
    del.onclick = async () => {
      const r = await post('/api/song/markers',
        {name: s.name, markers: s.markers.filter(x => x !== m)});
      if(!r.ok){ $('#mmarkerlog').textContent = r.error; return }
      await reload(); drawMusic();
    };
    row.appendChild(del);
    box.appendChild(row);
  }
}

$('#mmarkeradd').onclick = async () => {
  const s = muSong();
  if(!s) return;
  const name = $('#mmarkername').value.trim();
  const at = +$('#mmarkerat').value;
  if(!name){ $('#mmarkerlog').textContent = 'Name the marker first.'; return }
  const markers = s.markers.concat([{name, at}]);
  const r = await post('/api/song/markers', {name: s.name, markers});
  const log = $('#mmarkerlog');
  if(!r.ok){ log.className = 'bad'; log.textContent = r.error; return }
  log.className = 'ok'; log.textContent = `${name} added.`;
  $('#mmarkername').value = '';
  await reload(); drawMusic();
};

// -------------------------------------------------------------------------------- convert

$('#mconvertbtn').onclick = async () => {
  const s = muSong();
  if(!s) return;
  const r = await post('/api/song/convert', {name: s.name});
  const log = $('#mconvertlog');
  if(!r.ok){ log.className = 'bad'; log.textContent = r.error; return }
  log.className = 'ok'; log.textContent = 'Converted.';
  await reload(); drawMusic();
};
