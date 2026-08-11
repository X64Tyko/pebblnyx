# Items

## The economy

The Sanctum vending machine is what makes scrap mean anything inside the slice. Before it,
the script awarded 18 scrap against nothing to spend it on -- currency the player cannot
spend is currency they will not value.

Prices below are proposed so the numbers mean something. **If they are wrong, the awards
should move, not the prices** -- an economy is easier to read from what things cost.

| Item | Cost | Effect |
|---|---|---|
| Patch kit | 8 | restore 40 HP |
| Field patch | 20 | restore 120 HP |
| Focus tab | 12 | restore 30 Focus |
| Damper | 15 | -10 Instability |
| Charge cell | 25 | fill Resonance meter |
| Breach charge | 30 | heavy damage, consumed |

At those prices 18 scrap buys two patch kits, which is the right feel for a slice: enough to
matter once, not enough to trivialise the boss. **Recommend raising slice awards to ~45
total** so the player can afford a Damper and learn what Instability is by spending against
it. Teaching a mechanic through its counter-item is cheaper than a tutorial.

## Inventory

Flat list, stack limit 9, no categories. A category system needs tabs and tabs need screen
width. Nine of a thing is more than a session's worth.

```c
typedef struct { uint8_t id; uint8_t count; } Stack;   // 2 bytes
Stack inventory[16];                                   // 32 bytes
```

Sixteen distinct kinds is generous for the full game and trivial for the save format.

## Key items

Not in the inventory. Key items are flags (see `Flags.md`) -- an item that exists only to
open one door is a flag with art, and the art is the expensive part. The Relic is the
exception: it is embedded in Van and is not an item at all.

## Consumables in combat

Using an item takes a turn and always resolves -- no timing, no failure. That is deliberate:
items are the guaranteed option when a player's timing is failing, whether because the fight
is hard or because they are on a train. **On a wrist device, the reliable option must always
exist**, because the player's attention is not guaranteed and a game that punishes
interruption gets uninstalled.

## Scrap as currency and material

Scrap is both money and forge material, which means every purchase competes with every
upgrade. That is a good tension and it is free -- one counter, one meaning. Do not add a
second currency.

## The Sanctum vending machine

Ancient pre-collapse tech, still powered, still cheerfully selling to a facility that has
been a tomb for decades. It is the slice's only merchant and its purpose is Chrono Trigger's
fair: an optional grind for a reward clearly above the curve, available to a player who wants
it and invisible to one who does not.

**Location.** Inner Sanctum, off the main corridor. Reachable before the boss, after `05C`,
so the third party member exists and grinding is efficient enough to be tempting.

**Stock.** Consumables at the prices above, plus a complete alternate gear set:

| Item | Cost | Effect |
|---|---|---|
| Sanctum blade | 120 | `mult 24`, 4-step Addition |
| Sanctum plating | 100 | `ward 10` |
| Regulator core | 140 | Instability accrues at half rate |
| **Full set** | **360** | |

**The grind maths.** Normal play through the slice yields ~45 scrap. The full set is 360 --
eight times that, which is a deliberate detour of maybe fifteen minutes of farming drones, not
an accident. Each piece is individually affordable at ~2-3x normal income, so a player can
take one and leave. That gradient is the whole design: the set is a goal, one piece is a
reward.

The Regulator core is the interesting purchase. It is the only thing in the slice that
directly counters Instability, so buying it teaches the mechanic by mattering rather than by
explanation -- and it is the most expensive item, so learning that way is a choice.

**Voice.** The machine is cheerful. It is still running marketing copy for a company that no
longer exists, in a room where everyone died. Tag `VEND`, and the tonal contrast costs nothing
because dialogue is free.

> **VEND:** "WELCOME, VALUED PERSONNEL! SECTOR 4 COMMISSARY IS PLEASED TO SERVE."
> **VAN:** "It's still running."
> **KELL:** "Of course it is. The grid never went down -- only the people did."

**Balance risk, stated plainly.** A player who grinds the full set arrives at the boss
substantially over-tuned, and the boss must not become trivial or the grind is punished with
boredom. Tune the boss to level 6 and let the gear make it *fast* rather than free -- the
reward for grinding should be a shorter fight, not a skipped one.
