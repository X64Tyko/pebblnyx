// M4 device test: sequenced music with effects firing over it.
//
// Host tests settle the lead arithmetic and the envelope stages. What they cannot settle
// is whether four sequenced channels plus effects actually survive a real frame cadence
// without the stream running dry -- so this reports underrun on screen and in the log,
// and fires effects hard enough to force voice stealing.
//
// Also, since M4's own "Done when" is specifically about a covered app (a notification
// arriving mid-play): FOCUS_LOST snapshots PnxAudioStats, FOCUS_GAINED snapshots again and
// logs the delta -- left_playing (audible restarts), worst_gap_ms and short_writes -- so
// covering the watch with a real notification answers "did anything glitch" directly,
// rather than needing to infer it from a HUD nobody was watching while it was covered.
//
// SELECT fires a laser. DOWN fires an explosion. UP toggles the music.

#include "pnx/pnx.h"
#include "assets_gen.h"

#include <string.h>

#define PERSIST_BYTES 1024
#define SCENE_BYTES	  (16 * 1024)

typedef struct
{
	PnxArena persistent, scene;
	PnxFont hud_font;
	bool has_font;
	PnxSong song;
	PnxEnvelope tone_env;
	uint8_t held;
	const int8_t* laser;
	uint32_t laser_len, laser_hz;
	const int8_t* boom;
	uint32_t boom_len, boom_hz;

	bool ready;
	bool music_on;
	bool sfx_on;
	uint8_t lead_index;
	uint8_t fmt_index;
	uint8_t cut_index;
	bool
		seq_on; // sequencer; off at startup so a bare tone can be judged alone      // auto-firing effects; off by default so the tone is clean
	uint32_t ticks, accumulator_ms;
	uint32_t next_auto_ms;
	char hud[48];
	char hud2[48];
	char hud3[48];

	// The covered-period check -- see the header comment.
	bool covered;
	uint32_t t_lost;
	PnxAudioStats stats_at_lost;
} App;

static const uint32_t RESOURCES[] = PNX_ASSET_RESOURCE_TABLE;

// A sample blob is rate, loop point, then PCM -- read directly rather than through a
// helper, because this is the only place that needs it and the shape is three fields.
static const int8_t* load_sample(uint16_t asset, uint32_t* len, uint32_t* hz)
{
	size_t payload	 = 0;
	const uint8_t* d = pnx_blob_load(asset, "PW", NULL, NULL, NULL, NULL, &payload);
	if (!d || payload < 8)
		return NULL;
	*hz	 = (uint32_t)(d[0] | (d[1] << 8) | (d[2] << 16) | ((uint32_t)d[3] << 24));
	*len = (uint32_t)(payload - 8);
	return (const int8_t*)(d + 8);
}

static void draw_hud(App* a, PnxTarget* target);

static void frame(void* ctx, uint32_t elapsed_ms, PnxTarget* target)
{
	App* a					  = (App*)ctx;
	const uint32_t now		  = pnx_platform_now_ms();
	const uint32_t work_start = now;

	PnxEvent ev;
	while (pnx_platform_poll_event(&ev))
	{
		// ev.time_ms, not `now` -- stamped at delivery. While covered the app is throttled
		// and may not get a frame callback until both FOCUS_LOST and FOCUS_GAINED are already
		// queued, so draining them in the same instant and timing against `now` would report
		// a near-zero gap that never happened. This bit a real run once already.
		if (ev.type == PNX_EVENT_FOCUS_LOST)
		{
			a->covered		 = true;
			a->t_lost		 = ev.time_ms;
			a->stats_at_lost = *pnx_audio_stats();
			pnx_log("audio: FOCUS_LOST -- snapshot g%u wg%u short%u",
					(unsigned)a->stats_at_lost.left_playing,
					(unsigned)a->stats_at_lost.worst_gap_ms,
					(unsigned)a->stats_at_lost.short_writes);
			continue;
		}
		if (ev.type == PNX_EVENT_FOCUS_GAINED)
		{
			if (a->covered)
			{
				const PnxAudioStats* now_stats = pnx_audio_stats();
				pnx_log("audio: FOCUS_GAINED after %ums -- g +%u wg %u->%u short +%u",
						(unsigned)(ev.time_ms - a->t_lost),
						(unsigned)(now_stats->left_playing - a->stats_at_lost.left_playing),
						(unsigned)a->stats_at_lost.worst_gap_ms,
						(unsigned)now_stats->worst_gap_ms,
						(unsigned)(now_stats->short_writes - a->stats_at_lost.short_writes));
			}
			a->covered = false;
			continue;
		}
		if (ev.type != PNX_EVENT_BUTTON_DOWN)
			continue;
		if (ev.button == PNX_BUTTON_UP)
		{
			// Sweeps the low-pass cutoff. 0 is off, which is what every earlier build did.
			static const uint16_t cuts[] = { 3200, 2400, 1800, 5000, 0 };
			a->cut_index				 = (uint8_t)((a->cut_index + 1) % 5);
			pnx_audio_set_lowpass(cuts[a->cut_index]);
		}
		else if (ev.button == PNX_BUTTON_SELECT)
		{
			// Four states on one button, covering the two questions that separate causes of
			// harshness the counters cannot tell apart:
			//
			//   is it QUANTISATION?  8-bit against 16-bit, same everything else.
			//   is it the MIX?       music alone against music plus effects.
			//
			// `fmt_index` and `lead_index` sat here unused for a long time, which is how the
			// format A/B ended up being something claimed rather than something available.
			a->fmt_index	  = (uint8_t)((a->fmt_index + 1) % 4);
			const bool want16 = (a->fmt_index >= 2);
			a->sfx_on		  = (a->fmt_index & 1) == 0;
			a->next_auto_ms	  = now + 400;

			const PnxAudioFormat want = want16 ? PNX_AUDIO_16KHZ_16BIT : PNX_AUDIO_16KHZ_8BIT;
			if (want != pnx_audio_format())
			{
				// Reopening drops whatever was queued, so the stream restarts -- audible once, and
				// not a fault.
				if (!pnx_audio_reopen(want, 85))
					pnx_log("reopen to %s failed", want16 ? "16-bit" : "8-bit");
			}
		}
		else if (ev.button == PNX_BUTTON_DOWN && a->boom)
		{
			pnx_audio_play_pri(a->boom, a->boom_len, PNX_AUDIO_NO_LOOP, a->boom_hz, 255, 5,
							   NULL);
		}
	}

	a->accumulator_ms += elapsed_ms;
	const uint32_t max_ms = PNX_TICK_MS * PNX_MAX_CATCHUP_TICKS;
	if (a->accumulator_ms > max_ms)
		a->accumulator_ms = max_ms;
	while (a->accumulator_ms >= PNX_TICK_MS)
	{
		a->accumulator_ms -= PNX_TICK_MS;
		a->ticks++;
	}

	// Unattended effects, off unless asked for. Pattern 0 is a control -- one voice, one
	// note, no row events -- and effects sprayed over it make any artefact heard there
	// unattributable, which is the one thing a control has to rule out.
	if (a->ready && a->sfx_on && now >= a->next_auto_ms)
	{
		static bool alternate;
		alternate = !alternate;
		if (alternate && a->laser)
		{
			pnx_audio_play_pri(a->laser, a->laser_len, PNX_AUDIO_NO_LOOP, a->laser_hz, 220, 4,
							   NULL);
		}
		else if (a->boom)
		{
			pnx_audio_play_pri(a->boom, a->boom_len, PNX_AUDIO_NO_LOOP, a->boom_hz, 200, 5,
							   NULL);
		}
		a->next_auto_ms = now + 1400;
	}
	else if (!a->sfx_on)
	{
		// Do not let the timer accumulate while off, or switching on delivers a burst of
		// everything that was owed.
		a->next_auto_ms = now + 1400;
	}

	pnx_gfx_clear(target, 0xC0);

	const PnxAudioStats* au	   = pnx_audio_stats();
	const PnxFrameStats* fs	   = pnx_diag_stats();
	static const char* STATE[] = { "idle", "play", "drain", "?" };
	// Keyed by the enum, not by position, for the same reason the platform map is.
	static const char* FMT[] = {
		[PNX_AUDIO_16KHZ_16BIT] = "16k/16",
		[PNX_AUDIO_16KHZ_8BIT]	= "16k/8",
		[PNX_AUDIO_8KHZ_16BIT]	= "8k/16",
		[PNX_AUDIO_8KHZ_8BIT]	= "8k/8",
	};
	// peak/clip/dry together, because "it sounds bad" has three causes that are identical
	// by ear: too hot (peak > 127, clip rising), the stream running dry (dry rising), and
	// aliasing (both zero and it still sounds harsh). One line separates them.
	pnx_format(a->hud, sizeof(a->hud), "pk%u clip%u dry%u  g%u v%u", (unsigned)au->peak,
			   (unsigned)au->clipped, (unsigned)au->left_playing, au->gap_ms,
			   au->active_voices);
	pnx_format(a->hud3, sizeof(a->hud3), "%s%u r%2u %s  %s %s", a->seq_on ? "pat " : "off ",
			   pnx_music_pattern(), pnx_music_row(), a->song.synth_count ? "SYN" : "env",
			   FMT[pnx_audio_format() & 3], a->sfx_on ? "+sfx" : "solo");
	pnx_format(
		a->hud2, sizeof(a->hud2), "%u.%ufps  work %uus  %s",
		fs ? (unsigned)(fs->fps_x10 / 10) : 0, fs ? (unsigned)(fs->fps_x10 % 10) : 0,
		fs ? (unsigned)fs->work_us : 0,
		a->seq_on ? (a->sfx_on ? "seq+sfx" : "seq") : (a->sfx_on ? "tone+sfx" : "TONE ONLY"));

	// Was `if (a->ticks % 250 == 0) { if (a->ticks == 50) pnx_diag_flush(); ... }` -- the
	// inner check was unreachable, since 50 is never a multiple of 250 for any tick value
	// that also satisfies the outer gate. pnx_diag_flush() was never called via this path
	// at all, so nothing logged before the ring's 24-line capacity filled ever left the
	// device via `pebble install --logs`. Two separate checks now, on purpose.
	if (a->ticks == 50)
		pnx_diag_flush();
	if (a->ticks % 250 == 0)
	{
		pnx_log("audio: %s | %s | %s | short %u/%u carry %u dry %u", a->hud, a->hud3, a->hud2,
				(unsigned)au->short_writes, (unsigned)au->feeds, (unsigned)au->carried,
				(unsigned)au->left_playing);
	}

	draw_hud(a, target);

	pnx_diag_frame(elapsed_ms, pnx_platform_now_ms() - work_start);
}

// The audio feed, on its own timer rather than the frame loop: every few milliseconds
// instead of every 37 ms, so the buffer stays full on a small lead. The text draw used to
// share this hook because the SDK path could only run after the framebuffer was released;
// the glyph blitter has no such constraint and draws in the frame.
static void audio_tick(void* ctx)
{
	App* a			   = (App*)ctx;
	const uint32_t now = pnx_platform_now_ms();
	if (a->seq_on)
		pnx_music_update(now);
	pnx_audio_update(now);
}

// Drawn with the framework's own glyph blitter, into the frame target.
//
// This used to be five `pnx_platform_text_draw` calls, which go through the SDK at ~4.3 ms
// each -- 21.5 ms, 58% of a 37.3 ms frame. The diagnostics were the most expensive thing
// in the app, and enabling the synth was merely what pushed the total past the deadline.
//
// Two things change by moving to the framework font. It is a blit rather than a system
// call, so it costs a fraction as much; and it draws DURING the frame instead of after the
// framebuffer is released, which is why this is no longer a post-frame hook at all. That
// second property is what E7 was for: anything can now be drawn over the text.
static void draw_hud(App* a, PnxTarget* target)
{
	if (!a->has_font)
		return;

	char screen[288];
	pnx_format(screen, sizeof(screen),
			   "pnx audio test\n%s\n%s\n%s\n\n"
			   "UP     low-pass cutoff\n"
			   "SELECT 8bit/16bit x music/+sfx\n"
			   "DOWN   one explosion",
			   a->hud, a->hud2, a->hud3);
	pnx_text_draw_wrapped(target, &a->hud_font, screen, 6, FONT_HUD_BASELINE + 6, 188, 0, 0xFF,
						  PNX_ALIGN_LEFT);
}

int main(void)
{
	static App a;
	memset(&a, 0, sizeof(a));

	if (!pnx_arena_init(&a.persistent, "persistent", PERSIST_BYTES, 4) ||
		!pnx_arena_init(&a.scene, "scene", SCENE_BYTES, 4))
	{
		pnx_platform_log("arena init failed");
		return 1;
	}
	pnx_assets_init(&a.persistent, &a.scene, RESOURCES, PNX_ASSET_COUNT);

	if (!pnx_audio_init(PNX_AUDIO_16KHZ_8BIT, 85))
	{
		pnx_log("audio would not open");
	}

#if PNX_USE_SYNTH
	// A longer lead, because the synth changed what a late feed COSTS.
	//
	// The lead absorbs a late timer tick, and the default 60 ms was sized when a catch-up
	// mixed 768 samples of plain wavetable voices -- effectively free. A synth voice is
	// ~10,900 ns a sample, so the same catch-up is now 8.4 ms of solid compute inside a
	// timer callback, and one late tick can cost enough to make the next one late as well.
	// That feedback is heard as crackle with occasional dropouts.
	//
	// Lead is SFX latency, which is a game-design knob rather than an implementation
	// detail -- 180 ms is a lot for a twitch game and nothing for music.
	pnx_audio_set_lead(180);
#endif

	a.laser	   = load_sample(PNX_ASSET_SAMPLE_LASER, &a.laser_len, &a.laser_hz);
	a.boom	   = load_sample(PNX_ASSET_SAMPLE_EXPLOSION, &a.boom_len, &a.boom_hz);
	a.ready	   = pnx_music_load(&a.song, PNX_ASSET_MUSIC_THEME);
	a.has_font = pnx_font_load(&a.hud_font, PNX_ASSET_FONT_HUD);
	if (!a.has_font)
		pnx_log("hud font would not load");

	// Start with a bare sustained note and NO sequencer. If this is clean and enabling the
	// sequencer introduces blips, the sequencer is the cause; if it blips on its own, the
	// fault is below everything we have written.
	a.tone_env =
		(PnxEnvelope){ .attack_ms = 6, .decay_ms = 70, .sustain = 165, .release_ms = 60 };
	if (a.ready)
	{
		pnx_music_play(&a.song, true);
		a.seq_on = true;
	}
	else
	{
		// No song loaded, so at least give the control tone something to sound.
		a.held = pnx_audio_note(PNX_WAVE_TRIANGLE, 69, 255, &a.tone_env, 1);
	}
	a.sfx_on = true;
	// Which audio path this build actually took. A song carrying synth instruments plays
	// through the plain envelopes when PNX_USE_SYNTH is 0 -- it sounds fine, it is just not
	// the thing you were listening for, and nothing else would say so.
#if PNX_USE_SYNTH
	pnx_log("synth: ON, song carries %u synth instrument(s)%s", a.song.synth_count,
			a.song.synth_count ? "" : " -- none, so the plain envelopes play");
#else
	pnx_log(
		"synth: OFF (PNX_USE_SYNTH=0) -- plain envelopes, even if the song has a "
		"synth table");
#endif
	pnx_log("start: song=%d (%u patterns, %ubpm) laser=%u boom=%u arena %u/%u", (int)a.ready,
			a.song.pattern_count, a.song.tempo_bpm, (unsigned)a.laser_len, (unsigned)a.boom_len,
			(unsigned)a.scene.used, (unsigned)a.scene.capacity);

	pnx_platform_set_audio_timer(audio_tick, &a, 10);
	pnx_platform_run(frame, &a);

	pnx_audio_shutdown();
	pnx_arena_destroy(&a.scene);
	pnx_arena_destroy(&a.persistent);
	return 0;
}
