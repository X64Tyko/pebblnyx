# Stats

Sized for the machine first. There is no FPU, one 64-bit division costs 754 bytes of
`__udivmoddi4`, and every byte of a struct is multiplied by however many entities exist.
`pnx_fx` (16.16 fixed point) is available where fractions are genuinely needed; stats do not
need them.

## Character record

```c
typedef struct {
  uint8_t  level;          // 1..50
  uint16_t hp, hp_max;     // up to 65535; a u8 cap of 255 is too tight by mid-game
  uint8_t  focus, focus_max;  // tech resource, 0..100
  uint8_t  power;          // physical damage
  uint8_t  ward;           // physical defence
  uint8_t  charge;         // Resonance/tech damage
  uint8_t  insulation;     // Resonance/tech defence
  uint8_t  speed;          // initiative order
  uint8_t  precision;      // widens the Addition window
  uint8_t  stun;           // turns of frozen ATB remaining
  uint8_t  destab;         // Trance-style gauge; auto-triggers at 100
  uint8_t  balance;        // Node balance: governs fill rate, duration and power
  uint32_t xp;
} Actor;                   // 20 bytes
```

Twenty bytes each, three party members, plus enemies on screen. At eight actors that is 160
bytes -- irrelevant against the budget, which is the point: **stats are not where the memory
goes, so choose them for clarity.** Art is where it goes.

## What each stat does

| Stat | Effect |
|---|---|
| **Power** | physical damage scalar |
| **Ward** | physical damage reduction |
| **Charge** | tech and Resonance damage |
| **Insulation** | tech and Resonance reduction |
| **Speed** | turn order; ties break toward the player |
| **Precision** | +1 ms of Addition window per 4 points, capped at +37 ms (one frame) |
| **Focus** | spent on techs; refunded by Guard, gained by landing Additions |
| **Destabilisation** | fills from damage dealt and taken; **transforms automatically at 100** |
| **Balance** | earned from Nodes and the balancing quest; slower fill, longer duration, more power |
| **Stun** | turns of frozen ATB remaining. Free damage early; the OPRA branch depends on it |

**Precision deserves scrutiny.** It makes a stat out of the timing window, which risks
making the skill expression buyable. The cap of one frame is deliberate -- it should feel
like forgiveness, never like automation. **Deferred (post-slice):** whether it should exist at all. Simplest is to leave Precision
out of the slice entirely and decide once Additions have been played.

## Formulas

All integer, all 32-bit. No division by a runtime value where a shift will do.

```
physical damage  = max(1, (power * mult / 16) - ward / 2)
tech damage      = max(1, (charge * mult / 16) - insulation / 2)
addition step    = base damage * step_index / 2, +25% on a perfect
destab gain      = (damage_taken / 4 + damage_dealt / 8) * (16 - balance / 16) / 8
destab drain     = 4 per turn at balance 0, falling to 1 at balance 255
transform power  = mult 16 + balance / 8
```

`mult` is the weapon or tech multiplier in sixteenths, so a 1.5x weapon is `mult = 24`. Powers
of two keep this to shifts wherever it matters.

**The subtraction floor matters.** `max(1, ...)` guarantees every attack does something, which
avoids the classic failure where an over-levelled defender makes an encounter unwinnable
rather than merely slow.

## Growth

Levels 1-50, but the vertical slice covers 1-8. Growth is per-character and table-driven
rather than computed -- a 50-entry `uint8_t` table per stat per character is 50 bytes and
removes an entire class of tuning problem. Curves belong in the manifest, not in C.

## XP

```
xp_to_next = 12 * level * level
```
Level 2 at 48, level 8 at 768. Quadratic and cheap. The slice should land the player at
level 7-8 by the boss if they fight most encounters, level 5-6 if they avoid what they can.
Both must be winnable; see `Slice.md`.

## Open questions

- Precision is deferred; see `README.md`. Destabilisation is settled: a Trance-style gauge that
  auto-triggers at 100, with Node balance moving fill rate down and duration and power up. There is
  no fail state and no penalty meter -- Van's objection to it is characterisation, not a stat. See
  `World.md`.
- Enemy stats are not tabulated yet and should live in the manifest, so the pipeline can
  validate them and the editor can show them.
