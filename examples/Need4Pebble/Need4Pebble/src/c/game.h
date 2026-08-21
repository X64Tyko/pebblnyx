// Simulation state and the fixed-tick step. No rendering, no input polling beyond
// reading button/touch state -- main.c owns the frame loop and event pump.

#pragma once

#include "pnx/pnx.h"

// touring_normal (player, tier 0) + touring_crash (player, only while crashing) +
// palettes. Traffic reuses the SAME touring_normal handle at other tiers -- the sprite
// is already "the traffic/opponent car at every depth" per assets.toml's own comment,
// so it costs nothing extra to load.
//
// Was 20KB, silently too small once police_normal/police_crash joined the load list:
// touring_normal(12434) + touring_crash(~1900) + menu_font(646) leaves ~5.5KB, and
// police_normal alone needs 12434 -- pnx_sprite_load for it failed every single boot
// (logged: "asset 6: arena full, needed 12434, 5450 free"), silently, the same way
// docs/PLATFORM.md's own init-logging caveat warns about -- `has_police` was false the
// whole time, so draw_police's own guard was skipping the cop's actual chase sprite on
// every draw call. This is almost certainly the real root cause of "I don't see the
// cop car while driving" -- the lateral-drift/near-row-range fixes earlier in this
// work were real improvements but couldn't have mattered if the sprite never loaded to
// begin with. 32KB covers the full actual load list (~29.3KB) with headroom; heap was
// never the constraint (~115KB free at 20KB, per `pebble build`'s own memory report).
#define PERSIST_BYTES (2 * 1024)
#define SCENE_BYTES	  (32 * 1024)

#define MAX_TRAFFIC 5

typedef struct
{
	bool active;
	int32_t z;		// world-Z position, same units as Game.distance
	int32_t lane_x; // lane_center() of whichever lane it spawned in
	int32_t speed;	// world units per tick, its own -- independent of the player's
} Traffic;

// A chase, not reskinned traffic (DESIGN.md's "Police: chase, not traffic") -- tracks
// the player's own lane with a lag, offset to one side rather than dead centre (see
// `offset_x`), and can crash on its own (off-road or into a traffic car) same as the
// player can. `cooldown_ticks` counts down while `!active`, toward the NEXT occasional
// pursuit -- see game.c's police_tick for both cooldown ranges (first chase vs.
// between chases).
typedef struct
{
	bool active;
	int32_t z;
	int32_t lane_x;
	int32_t speed;
	int32_t offset_x;				// current lateral bias off the player's own lane_x
	int32_t offset_target;			// where offset_x is easing toward -- see POLICE_OFFSET_*
	uint32_t offset_retarget_ticks; // ticks left before picking a new offset_target
	uint32_t crash_ticks_left;		// >0: cop is wrecked/animating, chase logic is paused
	uint32_t cooldown_ticks;		// >0 while inactive: ticks until eligible to spawn again
} Police;

typedef struct
{
	PnxArena persistent, scene;
	PnxSprite car;			// touring_normal
	PnxSprite crash;		// touring_crash
	PnxSprite police_car;	// police_normal
	PnxSprite police_crash; // police_crash
	PnxFont menu_font;
	bool has_car, has_crash, has_menu_font;
	bool has_police, has_police_crash;

	uint32_t accumulator_ms;
	uint32_t tick_count; // simulated ticks since boot/restart -- render.c's police light
						 // flash times off this rather than `distance`, so the flash keeps
						 // a steady rate independent of speed (and freezes with everything
						 // else while paused/game_over, since this only advances past both
						 // early-returns in game_tick, same as `distance` already does)
	uint32_t distance;	 // world units travelled; also the player's position on TRACK
	int32_t speed;		 // world units per tick
	int32_t accel_accum; // fractional-tick carry for ACCEL_NUM/ACCEL_DEN, see game_tick
	int32_t lane_x;		 // car offset from road centre, same scale as ROAD_HALF_MAX
	int8_t steer_visual; // -4..4, eased toward the current steer input for the lean pose

	int32_t corner_penalty_accum; // fractional-tick carry for the outside-of-a-curve
								  // penalty, same mechanism as accel_accum

	uint32_t crash_ticks_left; // >0 while crashing/stunned: no input, speed forced to 0
	uint32_t rng;			   // xorshift32 state, seeded fixed -- traffic is deterministic
							   // on purpose, not device-random (see game.c's rng_next)

	bool paused;		 // frozen: game_tick returns immediately, see its own top
	bool use_tilt_steer; // false = touch drag (default), true = accelerometer tilt
	bool game_over;		 // "BUSTED" -- frozen like `paused`, but BACK restarts, not resumes

	Traffic traffic[MAX_TRAFFIC];
	Police police;
} Game;

#define MAX_SPEED 26
// Gas adds 1 speed every ACCEL_DEN/ACCEL_NUM ticks rather than every tick -- plain
// integers can't add 1/3 speed a tick, so game_tick banks the remainder in
// Game.accel_accum instead. At 25 ticks/sec this is ~3s from a stop to MAX_SPEED, not
// the ~1s a flat +1/tick gave -- see DESIGN.md's open questions for where that number
// came from (eyeballed, not felt in-hand).
#define ACCEL_NUM	1
#define ACCEL_DEN	3
#define BRAKE_DECEL 3
#define FRICTION	1
#define STEER_RATE	2 // world units of lane_x per tick per unit of steer_visual, AT FULL turn authority

// Turn authority scales with speed rather than being flat: none at a standstill (can't
// steer a car that isn't rolling), ramping linearly up to full STEER_RATE by this speed,
// then clamped -- not reduced -- above it, so a car already going fast doesn't keep
// gaining turn rate forever. "medium" per the ask, not MAX_SPEED itself.
#define TURN_RATE_SPEED_CAP (MAX_SPEED / 2)

// Hard clamp on Game.lane_x -- keeps the player's own car sprite fully on screen no
// matter how far it drifts. LOGICAL_W/2 (114) +/- this, +/- the widest player frame's
// own half-width (~23px, touring_normal tier 0), has to stay inside [0, LOGICAL_W]; 85
// leaves a few px of margin. Strictly greater than ROAD_HALF_MAX (track.h, 75) on
// purpose -- that gap is real off-road territory, not just a renamed road edge.
#define PLAYER_LANE_MAX 85

// While off the pavement (lane_x past ROAD_HALF_MAX): speed can never exceed this --
// a ceiling, not a brake. Off-road used to also apply a continuous FRICTION*2 drain,
// which dragged the car all the way to a dead stop rather than just capping it; that's
// gone (user: "driving off road shouldn't stop the car, just limit top speed"). Roughly
// TRAFFIC_MIN_SPEED's own magnitude, so grass reads as about as slow as the slowest
// traffic.
#define OFFROAD_MAX_SPEED 8

// The illusion of speed: the player's own car pulls back a little and swaps to a
// smaller pre-rendered tier as speed rises. See render.c's draw_car for why this is a
// tier swap rather than a scale -- pebblnyx sprites have no runtime scale at all.
#define PLAYER_NEAR_ROW_MIN 6  // rows back from the near edge at a stop (the old fixed value)
#define PLAYER_NEAR_ROW_MAX 16 // rows back at MAX_SPEED -- still "just a bit"
#define PLAYER_TIER_MAX		1  // caps how far the tier swap goes; traffic uses the full 0-5

// Accelerometer steering: an alternative to the touch drag, toggled from the pause
// menu (see game_toggle_steer_mode). `PnxAccel.y` is portrait's vertical axis, which is
// the one that becomes left-right once the watch is turned into PNX_ORIENT_BUTTONS_TOP
// landscape -- confirmed on real hardware, axis was right but the sign was backwards
// (tilt-left steered right); game.c's steer_input negates raw `.y` to correct it.
// DEADZONE is milli-Gs of tilt ignored as noise/near-flat before a direction registers.
#define ACCEL_STEER_DEADZONE 150

#define CENTRIFUGAL_DIVISOR 30 // higher = gentler pull off the racing line; tuned by eye
#define CORNER_INSIDE_BONUS 1
// A flat -1/tick here (or worse, the old -2/tick) beats ACCEL's ~+0.33/tick outright --
// holding gas on the wrong side of ANY curve, even barely, would only ever lose speed,
// down to a dead stop that gas alone could never climb back out of. That's not "the
// outside is slower," that's "the game stops once you reach the first curve" (verbatim
// bug report) for anyone who doesn't already know to counter-steer, which is everyone
// the first time through. Fractional via the same accum trick as ACCEL, and tuned
// strictly under ACCEL_NUM/ACCEL_DEN, so full gas always nets positive even on the
// outside of every curve -- slower to build speed than the inside line, never stalled.
#define CORNER_OUTSIDE_NUM 1
#define CORNER_OUTSIDE_DEN 6

// ------------------------------------------------------------------------- traffic
//
// Traffic crawls forward on its own (slower than the player can cruise, so catching
// up to it is the normal case, not an edge case), snapped to a lane centre rather than
// steering freely -- the player dodges, traffic doesn't swerve. A car that falls far
// enough behind the player is recycled ahead rather than despawned/respawned as a
// separate step, so MAX_TRAFFIC stays constant and there's always something to dodge.
// Close to MAX_SPEED (26), not far below it: the gap that matters is CLOSING speed
// (player's speed minus traffic's), and at the old 8-14 that was up to 18/tick --
// enough to cross the whole visible depth window in well under a second, reported as
// traffic "zooming by" / "showing up out of nowhere". 17-22 keeps traffic on-screen
// noticeably longer before it's passed, while still leaving a real gap to close.
#define TRAFFIC_MIN_SPEED 17
#define TRAFFIC_MAX_SPEED 22
// 600-1400 (the original spawn range) plus the closing-speed gap actually GROWING
// during the ~3s accel ramp (player's average speed is below traffic's until near
// MAX_SPEED) meant 10-15+ seconds of uninterrupted driving before the first car was
// ever reached -- easy to read as "traffic is disabled" in an ordinary play session
// that doesn't hold gas that long without a pause/pursuit/lane change. 250-550 gets
// the first car on screen within a few seconds of reaching cruising speed.
#define TRAFFIC_SPAWN_AHEAD_MIN	 250
#define TRAFFIC_SPAWN_AHEAD_SPAN 300 // spawn Z is SPAWN_AHEAD_MIN + rng % this
// Recycle once this far behind the player. Was 60 -- wide enough that a passed car
// was gone almost immediately, no matter what the player did afterward. Raised so a
// player who slows back down can actually catch up to and re-pass a car they already
// went by, and so a passed car sticks around long enough to be a real target for the
// player to steer a chasing police car into (POLICE_DESIRED_GAP's own scale, game.h,
// is 60 -- the old margin gave a passed car and a trailing cop almost no shared window
// to ever be near the same Z at once).
#define TRAFFIC_BEHIND_MARGIN 220

// A hit is close in BOTH world-Z and lane at once -- half a car length and roughly a
// lane's width of slop, not a pixel-exact hitbox (DESIGN.md doesn't ask for one yet).
#define TRAFFIC_COLLIDE_Z		  36
#define TRAFFIC_COLLIDE_LANE_HALF ((ROAD_HALF_MAX * 2 / LANES) / 2 + 8)

// -------------------------------------------------------------------------- police
//
// See DESIGN.md's "Police: chase, not traffic". One pursuer at a time; "occasional"
// is a cooldown timer, not a per-tick chance -- a fixed range has a predictable worst
// case (never more than SPAWN_SPAN past MIN before a chase is possible), where a
// per-tick roll doesn't. Ticks are PNX_TICK_MS (40ms) apart, ~25/sec.
#define POLICE_FIRST_SPAWN_MIN_TICKS  (10 * 25) // >=10s of driving before the first chase
#define POLICE_FIRST_SPAWN_SPAN_TICKS (15 * 25) // ...within the next 15s after that (10-25s)
#define POLICE_RESPAWN_MIN_TICKS	  (15 * 25) // same idea, between one chase ending and the next
#define POLICE_RESPAWN_SPAN_TICKS	  (20 * 25) // (15-35s)
#define POLICE_MIN_SPEED_TO_SPAWN	  12		// won't start a chase while crawling/off-road
#define POLICE_SPAWN_BEHIND			  220		// world units behind the player at spawn

// Chase speed: corrects toward POLICE_DESIRED_GAP rather than simply matching the
// player -- matching alone would let the cop settle at whatever gap it happened to
// spawn at and never actually close in. Overshoot (gap shrinks below half the desired
// gap) backs off by the same amount, so it doesn't glue itself motionlessly to the
// player's bumper either. This proportional wobble is also what makes it "try to
// force a collision" (DESIGN.md) without a separate ramming state: a player who
// brakes or slows into a turn can end up inside TRAFFIC_COLLIDE_Z before the cop's
// next correction, same hitbox check_collision already uses for traffic.
#define POLICE_DESIRED_GAP	 60
#define POLICE_CATCHUP_BONUS 3
// Lane tracking lags the player's own lane_x (moves this fraction of the gap per
// tick) rather than snapping to it -- gives the player a real window to juke the cop
// into traffic or off-road instead of it being glued to their line.
#define POLICE_LANE_LAG_DIVISOR 4

// Lateral drift: the cop never sits dead centre behind the player, favouring one side
// or the other and wandering between them over time instead (reported directly: it
// should "never be directly behind the player, rather favoring centering on the
// player's left or right side and drifting back and forth"). `offset_target` is
// re-rolled every POLICE_OFFSET_RETARGET_MIN/SPAN ticks to a magnitude in
// [POLICE_OFFSET_MIN, POLICE_OFFSET_MAX] on a random side (never near 0 -- the whole
// point is staying off the player's own line); `offset_x` eases toward it
// (POLICE_OFFSET_EASE_DIVISOR) rather than snapping, for a smooth side-to-side sway.
// This is also what actually fixes the cop's own visibility (a second, separate
// report: "I don't see the cop car while driving") -- centred, it mostly rendered
// underneath the player's own car sprite; offset, it reads as a distinct car beside
// the player rather than hidden behind it. Kept comfortably under ROAD_HALF_MAX (75)
// so the added offset alone, on top of a centred player, doesn't constantly wreck the
// cop off-road -- only a player already hugging one edge pushes it past the line.
#define POLICE_OFFSET_MIN				  15
#define POLICE_OFFSET_MAX				  45
#define POLICE_OFFSET_RETARGET_MIN_TICKS  (2 * 25) // new target every 2-5s
#define POLICE_OFFSET_RETARGET_SPAN_TICKS (3 * 25)
#define POLICE_OFFSET_EASE_DIVISOR		  10 // slower than POLICE_LANE_LAG_DIVISOR -- a sway, not a snap

// Rendering only -- no real rear horizon to project a "behind" car against, so this is
// a stylised near-edge approach instead of true depth: invisible past this many world
// units behind the player, closing from POLICE_BEHIND_ROW_MAX rows back at that range
// to POLICE_BEHIND_ROW_MIN right on the player's bumper (gap 0). Both bounds have to
// clear draw_car's own PLAYER_NEAR_ROW_MIN (6) -- the wx coordinate this maps to
// (render.c's draw_police) is the same "distance from the true near edge" draw_car
// uses (near_row-1), and anything much under that live-tested floor clips off the near
// edge of the framebuffer instead of drawing a visible near-field car. Measured
// directly: ROW_MIN=5 (this range's whole span squeezed into wx 0-5) rendered
// nothing, in-emulator, across several separate mid-chase checks -- ROW_MIN=10/
// ROW_MAX=40 puts the cop visibly further back than the player's own car (whose wx
// never exceeds PLAYER_NEAR_ROW_MAX-1 = 15) rather than crowding the same few pixels.
#define POLICE_BEHIND_RANGE	  150
#define POLICE_BEHIND_ROW_MIN 10
#define POLICE_BEHIND_ROW_MAX 40

// "Busted": speed 0 with an active (not currently crashed) pursuer within this many
// world units counts as caught, regardless of why speed hit 0 -- the player's own
// crash, or simply braking to a stop mid-chase. Wider than POLICE_DESIRED_GAP on
// purpose: a cop that's a little behind because of a missed correction tick is still
// very much "right there," not out of the chase.
#define POLICE_BUST_RANGE 350

// Forced steering on a ram: the hit shoves the player's lane_x to the side OPPOSITE
// wherever the cop currently is, not just a stop-in-place crash -- "if they do this it
// should show the player to the opposite side the cop is currently on." Roughly one
// lane width (`ROAD_HALF_MAX*2/LANES` = 37) so it reads as a real knock, not a nudge;
// clamped to PLAYER_LANE_MAX same as any other lane_x write, so it can push the player
// off the pavement (or, at the extreme, right up against the hard wall) but never off
// the visible screen.
#define POLICE_RAM_SHOVE 40

// Flashing red/blue light-bar wash over the near field while an active pursuer is on
// your tail -- the sprite alone (POLICE_BEHIND_ROW_MIN/MAX, above) reads as too subtle
// to notice while actually driving (reported directly: "I don't see the cop car while
// driving"), so this is a much louder, unmissable signal that a chase is live. Ticks,
// not wall-clock, same reasoning as CRASH_FRAME_COUNT's own timing -- see
// Game.tick_count's comment (game.h) for why.
#define POLICE_LIGHT_FLASH_TICKS 6	// ticks per colour phase (~240ms at ~25 ticks/sec)
#define POLICE_LIGHT_BAND_ROWS	 50 // rows of near field the wash covers, from the near edge up

// ------------------------------------------------------------------------- crash
//
// Tick-based, not `pnx_anim_play`/`pnx_anim_frame` (pnx_sprite.h): those are wall-clock,
// meant for animations that keep playing through a paused/covered app, and a crash
// reaction is exactly the opposite -- it should freeze with the sim, not run on while
// ticks aren't. `TOURING_CRASH_CRASH_COUNT` (assets_gen.h) is 4; CRASH_FRAME_COUNT
// repeats that number rather than including assets_gen.h from a header everything
// includes.
#define CRASH_FRAME_COUNT	  4
#define CRASH_TICKS_PER_FRAME 3	 // ~8fps at 40ms/tick, matching the clip's authored fps
#define CRASH_HOLD_TICKS	  15 // extra ticks holding the wreck frame before control returns
#define CRASH_TOTAL_TICKS	  (CRASH_TICKS_PER_FRAME * CRASH_FRAME_COUNT + CRASH_HOLD_TICKS)

// Arenas, asset registry, the car sprite, input -- everything that has to happen once,
// in order, before the first tick. False only on the fatal case (an arena wouldn't
// init); a missing sprite is logged and leaves `has_car`/`has_crash` false rather than
// failing the whole boot, same as the palette load already did in the single-file
// version.
bool game_boot(Game* g);
void game_shutdown(Game* g);

void game_tick(Game* g);

// BACK: pause/resume, always -- long-press force-quits at the OS level regardless of
// what the app does (only a single click is ever claimable, per docs/PLATFORM.md), so
// there is no in-app quit path to build. SELECT while paused swaps the steering mode;
// it's a no-op while driving (game_toggle_steer_mode checks `paused` itself), so main.c
// doesn't need to gate the call.
void game_toggle_pause(Game* g);
void game_toggle_steer_mode(Game* g);

// BACK while `game_over`, instead of the usual pause toggle (main.c gates the call).
// Resets gameplay state (distance/speed/lane/traffic/police, the fixed rng seed) the
// same way game_boot does, without re-touching arenas or reloading assets -- those
// only need to happen once, ever.
void game_restart(Game* g);
