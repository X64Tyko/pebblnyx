# Items

## The scrap problem

The script awards **18 scrap** across the whole slice: 5 at Event 01, 10 at Event 04, 3 from
the optional terminal. Nothing has a price anywhere, so 18 is currently unjudgeable -- it is
either generous or insulting and the document cannot say which.

Prices below are proposed so the number means something. **If they are wrong, the awards
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

## Open

- Slice awards: 18 as written, ~45 recommended. Needs a decision before encounters are tuned.
- Whether patch kits are buyable during the slice at all, given there is no merchant in it.
  If not, the prices above are theoretical and the slice is a pure drop economy -- which is
  fine, but then scrap has no use inside the slice and should probably be introduced later.
  **This is the sharpest open question in this file:** currency the player cannot spend is
  currency they will not value.
