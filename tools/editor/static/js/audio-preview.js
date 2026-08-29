// A Web Audio APPROXIMATION of the on-device synth (src/pnx/audio/pnx_synth.c, 1099
// lines) -- not a port of it. Porting the device synth exactly is its own project; this
// exists to fix a narrower, sharper gap: turning an oscillator/envelope/filter knob in
// the Instrument panel changed a number with no audible feedback at all, so there was no
// way to hear a note, an instrument or a pattern without a full build and a
// device/emulator round trip. Pitch, rough timbre (waveform, octave, detune) and
// envelope shape (attack/decay/sustain/release) all come through; pulse width, the
// filter's own envelope and LFO modulation do not -- flagged in the UI, not silently
// approximated as if they were exact.
//
// Loaded after app.js: reuses its NOTE_NAMES/toManifestNote/splitCell/muCells/muSong
// exactly as they already parse a pattern cell, rather than a second copy of that
// parsing here.

let actx = null;
function audioCtx(){
  if(!actx) actx = new (window.AudioContext || window.webkitAudioContext)();
  // Browsers suspend a context created before any user gesture; every entry point here
  // is itself a click, so resuming on each call is cheap and always the right thing.
  if(actx.state === 'suspended') actx.resume();
  return actx;
}

let noiseBuffer = null;
function noiseSource(ctx){
  if(!noiseBuffer){
    noiseBuffer = ctx.createBuffer(1, ctx.sampleRate * 2, ctx.sampleRate);
    const data = noiseBuffer.getChannelData(0);
    for(let i = 0; i < data.length; i++) data[i] = Math.random() * 2 - 1;
  }
  const src = ctx.createBufferSource();
  src.buffer = noiseBuffer;
  src.loop = true;
  return src;
}

// `C4` (parse_note in pnx_assets.py's own convention, MIDI 60) -> a MIDI number.
// toManifestNote() (app.js) already turns the tracker's `C-4` into this shape.
function noteToMidi(note){
  const m = /^([A-G])(#?)(-?\d+)$/.exec(note);
  if(!m) return null;
  const base = {C:0, D:2, E:4, F:5, G:7, A:9, B:11}[m[1]] + (m[2] ? 1 : 0);
  return (+m[3] + 1) * 12 + base;
}
function midiToFreq(midi){ return 440 * Math.pow(2, (midi - 69) / 12); }

// ms (the manifest's own unit for attack/decay/release) -> seconds, floored above zero:
// a zero-length AudioParam ramp is a discontinuity some browsers render as a click
// rather than an instant change.
function sec(ms){ return Math.max(0.001, (ms || 0) / 1000); }

const WAVE_TYPE = {square: 'square', saw: 'sawtooth', triangle: 'triangle'};

/** Plays one note through one instrument's CURRENT settings -- the live in-memory
 * object drawInstrument() edits (`plain`/`sy`), not a re-fetched copy, so previewing
 * while dragging a knob hears the knob, not what was last saved. `durationSec` is how
 * long the note is held before release; the release tail plays out after that. */
function playInstrument(instrument, synth, midi, durationSec){
  const ctx = audioCtx();
  const t0 = ctx.currentTime;
  const releaseAt = t0 + durationSec;

  const master = ctx.createGain();
  master.gain.value = 0;
  master.connect(ctx.destination);

  // Amp envelope -- the shape of the sound, and the one piece every instrument has
  // whether or not it has a synth table (drawInstrument()'s own "no synth table" branch
  // puts attack/decay/sustain/release directly on the instrument in that case).
  const amp = (synth && synth.amp) || instrument;
  const a = sec(amp.attack), d = sec(amp.decay), rel = sec(amp.release);
  const susLevel = Math.max(0, Math.min(1, (amp.sustain ?? 255) / 255));
  const g = master.gain;
  g.setValueAtTime(0, t0);
  g.linearRampToValueAtTime(1, t0 + a);
  g.linearRampToValueAtTime(susLevel, t0 + a + d);
  const releaseStart = Math.max(t0 + a + d, releaseAt);
  g.setValueAtTime(susLevel, releaseStart);
  g.linearRampToValueAtTime(0, releaseStart + rel);

  // Filter -- static cutoff/resonance, not animated by its own envelope (sy.cutoff):
  // that's the approximation this preview draws the line at, per the module comment.
  let sink = master;
  if(synth && synth.filter && synth.filter !== 'off'){
    const f = ctx.createBiquadFilter();
    f.type = {lowpass: 'lowpass', highpass: 'highpass', bandpass: 'bandpass'}[synth.filter]
      || 'lowpass';
    const cutoff = synth.cutoff_base ?? 128;
    f.frequency.value = 80 * Math.pow(2, (cutoff / 255) * 7);   // ~80 Hz .. 10 kHz
    f.Q.value = ((synth.resonance ?? 0) / 255) * 20;
    f.connect(master);
    sink = f;
  }

  const oscs = (synth && synth.osc && synth.osc.length)
    ? synth.osc
    : [{wave: instrument.wave || 'square', volume: 255, detune: 0, octave: 0}];
  const stopAt = releaseStart + rel + 0.05;
  const nodes = [];
  for(const o of oscs){
    const og = ctx.createGain();
    og.gain.value = ((o.volume ?? 200) / 255) / oscs.length;
    og.connect(sink);
    let src;
    if((o.wave || 'square') === 'noise'){
      src = noiseSource(ctx);
    }else{
      src = ctx.createOscillator();
      src.type = WAVE_TYPE[o.wave] || 'square';
      src.frequency.value = midiToFreq(midi) * Math.pow(2, o.octave || 0);
      src.detune.value = o.detune || 0;
    }
    src.connect(og);
    src.start(t0);
    src.stop(stopAt);
    nodes.push(src);
  }
  setTimeout(() => { master.disconnect(); nodes.forEach(n => n.disconnect()); },
             (stopAt - t0 + 0.1) * 1000);
}

// --------------------------------------------------------------- pattern playback

let patternTimer = null;

function stopPatternPlayback(){
  if(patternTimer){ clearTimeout(patternTimer); patternTimer = null }
  for(const el of document.querySelectorAll('.trow.here')) el.classList.remove('here');
  const btn = $('#mplay');
  if(btn) btn.textContent = '▶ Play pattern';
}

function playPattern(){
  const s = muSong();
  const rows = s && muTargetSourceRows();
  if(!s || !rows || !rows.length) return;
  stopPatternPlayback();
  $('#mplay').textContent = '■ Stop';
  const channels = muTargetChannels();
  // 4 rows/beat is the tracker's own convention -- .trow.beat (style.css) already marks
  // every 4th row for the same reason.
  const rowSec = 60 / (s.tempo || 120) / 4;
  let ri = 0;

  const step = () => {
    for(const el of document.querySelectorAll('.trow.here')) el.classList.remove('here');
    const rowEls = document.querySelectorAll('#mrows .trow');
    if(rowEls[ri]) rowEls[ri].classList.add('here');

    for(const cell of muCells(rows[ri], channels)){
      const parts = splitCell(cell);
      if(!parts.note || parts.note === 'off') continue;
      const midi = noteToMidi(toManifestNote(parts.note));
      if(midi == null) continue;
      // A clip's cells carry no instrument of their own (see muTargetChannels) -- which
      // one is actually active comes from the track it's placed on, which this preview
      // has no context for, so it previews through whatever's selected in the Instrument
      // panel instead.
      const idx = parts.inst ? parseInt(parts.inst, 10) : (s.arrangement ? MU.inst : 0);
      const inst = s.instruments[idx];
      if(inst) playInstrument(inst, inst.synth, midi, rowSec * 1.8);
    }
    ri = (ri + 1) % rows.length;
    patternTimer = setTimeout(step, rowSec * 1000);
  };
  step();
}

$('#mplay').onclick = () => { patternTimer ? stopPatternPlayback() : playPattern(); };
// Leaving the Music tab, changing pattern/song, or a pattern edit landing mid-playback
// are all reasons the loop's own row index would stop meaning anything -- simplest to
// just stop rather than try to keep it consistent through every one of those.
$('#mpat').addEventListener('change', stopPatternPlayback);
$('#msong').addEventListener('change', stopPatternPlayback);

// ----------------------------------------------------------------- arrangement playback
//
// playPattern above previews exactly one clip, looped, through whatever instrument
// happens to be selected in the Instrument panel -- fine for editing one clip, wrong for
// hearing the SONG: an arrangement's channels only get their real instrument from the
// instrument-change placements on their own track. This walks every channel's placements
// in row order, tracking the active instrument exactly like compile_arrangement
// (tools/pnx_assets.py) does at build time, so what plays here is what the real build
// will play -- not a different, cheaper approximation of it.

let arrangeTimer = null;

function stopArrangementPlayback(){
  if(arrangeTimer){ clearTimeout(arrangeTimer); arrangeTimer = null }
  const btn = $('#marrangeplay');
  if(btn) btn.textContent = '▶ Play arrangement';
  const ph = document.getElementById('marrangeplayhead');
  if(ph) ph.style.display = 'none';
}

// {row, midi, inst}[] for one channel across the WHOLE song -- every clip's notes,
// stamped with whichever instrument was active at that point in the track, the same
// current_inst bookkeeping compile_arrangement itself does.
function arrangeChannelEvents(s, channel){
  const track = s.tracks.find(t => t.channel === channel);
  if(!track) return [];
  const events = [];
  let currentInst = 0;
  const placements = track.placement.slice().sort((a, b) => a.start - b.start);
  for(const p of placements){
    if('instrument' in p){ currentInst = p.instrument; continue }
    const clip = s.clips.find(c => c.name === p.clip);
    if(!clip) continue;
    clip.rows.forEach((cell, i) => {
      const parts = splitCell(cell);
      if(!parts.note || parts.note === 'off') return;
      const midi = noteToMidi(toManifestNote(parts.note));
      if(midi != null) events.push({row: p.start + i, midi, inst: currentInst});
    });
  }
  return events;
}

function playArrangement(){
  const s = muSong();
  if(!s || !s.arrangement) return;
  stopArrangementPlayback();
  $('#marrangeplay').textContent = '■ Stop';

  const span = arrangeSpan(s);
  const perChannel = [0, 1, 2, 3].map(ch => arrangeChannelEvents(s, ch));
  const rowSec = 60 / (s.tempo || 120) / 4;
  const ph = document.getElementById('marrangeplayhead');
  let row = 0;

  const step = () => {
    if(ph){
      ph.style.display = '';
      ph.style.left = `calc(3.4rem + ${row * arrangePxPerRow()}px)`;
    }
    const anySolo = MU.channelSolo.some(x => x);
    for(let ch = 0; ch < 4; ch++){
      if(MU.channelMute[ch]) continue;
      if(anySolo && !MU.channelSolo[ch]) continue;
      for(const ev of perChannel[ch]){
        if(ev.row !== row) continue;
        const inst = s.instruments[ev.inst];
        if(inst) playInstrument(inst, inst.synth, ev.midi, rowSec * 1.8);
      }
    }
    row++;
    // Once this preview's own loop-start UI (§ the arrangement's loop point) lands, the
    // wrap-around below already honors it -- restart from s.loop_start rather than 0,
    // matching pnx_music_update's own loop-wrap exactly.
    if(row >= span) row = s.loop_start != null ? s.loop_start : 0;
    arrangeTimer = setTimeout(step, rowSec * 1000);
  };
  step();
}

$('#marrangeplay').onclick = () => { arrangeTimer ? stopArrangementPlayback() : playArrangement(); };
$('#msong').addEventListener('change', stopArrangementPlayback);

// ------------------------------------------------------------------------- mini piano
//
// Every other preview in this tab plays a fixed C4 -- fine for a quick check, useless for
// hearing an instrument across its actual range (harmonicsAt's own band-limiting means a
// bright waveform can sound quite different an octave up). One row of clickable keys,
// always previewing the CURRENTLY EDITED instrument's live, unsaved settings, same as the
// ▶ button beside it.
const PIANO_KEYS = [
  {midi: 60, black: false}, {midi: 61, black: true}, {midi: 62, black: false},
  {midi: 63, black: true}, {midi: 64, black: false}, {midi: 65, black: false},
  {midi: 66, black: true}, {midi: 67, black: false}, {midi: 68, black: true},
  {midi: 69, black: false}, {midi: 70, black: true}, {midi: 71, black: false},
  {midi: 72, black: false},
];

function drawMiniPiano(){
  const box = $('#mpiano');
  if(!box || box.childElementCount) return; // built once; clicks always read the LIVE instrument
  for(const k of PIANO_KEYS){
    const key = document.createElement('button');
    key.className = 'pianokey' + (k.black ? ' black' : '');
    key.dataset.midi = String(k.midi);
    key.title = midiToTracker(k.midi);
    box.appendChild(key);
  }
  box.addEventListener('pointerdown', ev => {
    const key = ev.target.closest('.pianokey');
    if(!key || typeof MU_LIVE_INSTRUMENT === 'undefined' || !MU_LIVE_INSTRUMENT) return;
    playInstrument(MU_LIVE_INSTRUMENT.plain, MU_LIVE_INSTRUMENT.synth,
      +key.dataset.midi, 0.5);
  });
}
