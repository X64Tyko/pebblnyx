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

const ARRANGE_PX_PER_ROW = 14;
const ARRANGE_MIN_ROWS = 32;

function arrangeLog(msg, bad){
  const el = $('#marrangelog');
  el.className = bad === false ? 'ok' : (bad ? 'bad' : 'dim');
  el.textContent = msg || '';
}

// The widest a placement (or a marker) reaches, so the lanes/ruler are wide enough to show
// everything without the last block or marker getting clipped.
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
  return end;
}

function drawArrangement(s){
  drawClipList(s);
  drawClipPalette(s);
  drawLanes(s);
  drawMarkerList(s);
  $('#mresolution').value = s.resolution || 16;
}

// ---------------------------------------------------------------------------------- clips

function drawClipList(s){
  const box = $('#mcliplist');
  box.innerHTML = '';
  for(const clip of s.clips){
    const row = document.createElement('div');
    row.className = 'mini';
    row.style.marginBottom = '.3rem';
    const label = document.createElement('b');
    label.textContent = clip.name;
    label.style.minWidth = '6rem';
    label.style.display = 'inline-block';
    row.appendChild(label);

    const inputs = clip.rows.map((cell, ri) => {
      const inp = document.createElement('input');
      inp.value = cell;
      inp.size = 4;
      inp.style.width = '3.2rem';
      inp.title = `row ${ri}`;
      inp.onchange = () => saveClipRows(s.name, clip.name, inputs.map(i => i.value));
      return inp;
    });
    inputs.forEach(i => row.appendChild(i));

    const add = document.createElement('button');
    add.textContent = '+row';
    add.title = 'append a row';
    add.onclick = () => saveClipRows(s.name, clip.name, [...inputs.map(i => i.value), '.']);
    row.appendChild(add);

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

async function saveClipRows(song, clip, rows){
  const r = await post('/api/song/clip', {name: song, clip, rows});
  if(!r.ok){ arrangeLog(r.error, true); return }
  await reload(); drawMusic();
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
  await reload(); drawMusic();
};

// -------------------------------------------------------------------------------- palette

function drawClipPalette(s){
  const box = $('#marrangeclips');
  box.innerHTML = '';
  for(const clip of s.clips){
    const chip = document.createElement('div');
    chip.className = 'clipchip';
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
  const span = arrangeSpan(s);
  const px = span * ARRANGE_PX_PER_ROW;

  const ruler = document.createElement('div');
  ruler.className = 'arrangeruler';
  ruler.style.marginLeft = '3.4rem';
  ruler.style.width = px + 'px';
  for(let r = 0; r < span; r += 8){
    const tick = document.createElement('i');
    tick.style.left = (r * ARRANGE_PX_PER_ROW) + 'px';
    tick.textContent = r;
    ruler.appendChild(tick);
  }
  wrap.appendChild(ruler);

  for(let ch = 0; ch < 4; ch++){
    const track = s.tracks.find(t => t.channel === ch) || {channel: ch, placement: []};
    const lane = document.createElement('div');
    lane.className = 'arrangelane';

    const label = document.createElement('b');
    label.textContent = 'ch ' + ch;
    lane.appendChild(label);

    const area = document.createElement('div');
    area.className = 'arrangetrack';
    area.style.width = px + 'px';
    area.dataset.channel = String(ch);

    for(const p of track.placement){
      if('clip' in p){
        const len = clipLenOf(s, p.clip);
        const block = document.createElement('div');
        block.className = 'arrangeblock';
        block.style.left = (p.start * ARRANGE_PX_PER_ROW) + 'px';
        block.style.width = Math.max(1, len * ARRANGE_PX_PER_ROW - 2) + 'px';
        block.draggable = true;
        block.title = `${p.clip} @ row ${p.start}`;
        block.textContent = p.clip;
        const x = document.createElement('span');
        x.className = 'arrangex';
        x.textContent = '×';
        x.onclick = ev => { ev.stopPropagation(); removePlacement(s, ch, p); };
        block.appendChild(x);
        block.addEventListener('dragstart', ev => {
          ev.dataTransfer.effectAllowed = 'move';
          ev.dataTransfer.setData('text/plain',
            JSON.stringify({clip: p.clip, moveFrom: ch, moveStart: p.start}));
        });
        area.appendChild(block);
      }else{
        const pin = document.createElement('div');
        pin.className = 'arrangepin';
        pin.style.left = (p.start * ARRANGE_PX_PER_ROW) + 'px';
        pin.title = `instrument -> ${p.instrument} @ row ${p.start} (click to remove)`;
        const b2 = document.createElement('b');
        b2.textContent = 'i' + p.instrument;
        pin.appendChild(b2);
        pin.onclick = () => removePlacement(s, ch, p);
        area.appendChild(pin);
      }
    }

    for(const m of s.markers){
      const mk = document.createElement('div');
      mk.className = 'markerpin';
      mk.style.left = (m.at * ARRANGE_PX_PER_ROW) + 'px';
      mk.title = `marker ${m.name} @ row ${m.at}`;
      area.appendChild(mk);
    }

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
      const start = Math.max(0, Math.round((ev.clientX - rect.left) / ARRANGE_PX_PER_ROW));
      dropClip(s, ch, payload, start);
    });
    area.addEventListener('contextmenu', ev => {
      ev.preventDefault();
      const rect = area.getBoundingClientRect();
      const start = Math.max(0, Math.round((ev.clientX - rect.left) / ARRANGE_PX_PER_ROW));
      addInstrumentChange(s, ch, start);
    });

    lane.appendChild(area);
    wrap.appendChild(lane);
  }
}

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
  await reload(); drawMusic();
}

async function addInstrumentChange(s, channel, start){
  if(!s.instruments.length) return;
  const raw = prompt(`Instrument index 0-${s.instruments.length - 1} at row ${start}?`, '0');
  if(raw === null) return;
  const inst = parseInt(raw, 10);
  if(!Number.isFinite(inst)) return;
  const track = s.tracks.find(t => t.channel === channel);
  const placements = (track ? track.placement.slice() : [])
    .concat([{instrument: inst, start}]);
  const r = await post('/api/song/track', {name: s.name, channel, placements});
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
    row.innerHTML = `<b>${m.name}</b> <span class="dim">row ${m.at}</span>`;
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
