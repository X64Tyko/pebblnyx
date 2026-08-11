# Gear

The script commits to three things: Van wears "Type-2 plating, ceremonial-grade", a forge is
needed before Crystal Peaks, and scrap is the currency. This builds out from those.

## Slots

Three per character. Fewer than a console RPG on purpose -- an equip screen on a 200x228
display shows three rows comfortably and five badly.

| Slot | Van | Mage | GRD |
|---|---|---|---|
| **Weapon** | blades | focus implements | integrated -- fixed |
| **Armour** | plating | vestments | chassis plating |
| **Core** | Resonance shard | reagent | directive module |

The **Core** slot is the interesting one: it modifies behaviour rather than adding numbers.
A core might add a step to an Addition chain, change a tech's element, or convert Guard into
a counter. Numbers come from the other two slots.

GRD's fixed weapon is characterisation as much as economy -- it is a machine that came with
what it has, and it means the third party member does not double the gear list.

## Grades

Plating grades are named in the script, so the ladder is already implied:

| Grade | Meaning | Where |
|---|---|---|
| Ceremonial | looks the part, stops blades, nothing else | starting kit |
| Type-1 | field standard | Act I drops |
| Type-2 | Van's starting plate -- ceremonial-grade *make* | start |
| Type-3 | proper bolt resistance | forge, post-slice |
| Resonant | scales with Instability | full game only |

**Note the joke the script is making:** Type-2 sounds like an upgrade over Type-1 and is not
-- it is ceremonial workmanship at a Type-2 designation. The Mage's line lands better if the
player has already found a plain Type-1 and assumed the higher number was better.

## The forge

Teased at Event 05B, delivered after the slice. Upgrading consumes scrap and a component;
upgrade paths are linear per item rather than a tree, because a tree needs a screen to
display it and this device does not have one.

```
Type-1 plating + 40 scrap + intact servo -> Type-3 plating
```

**Open:** whether the forge is a place or a portable tool. A place gives Crystal Peaks a
reason to be a hub; a tool suits a game played in ninety-second sessions on a wrist. Leaning
portable, and giving up the hub.

## Drops

Enemies drop scrap, not equipment, with two exceptions in the slice: the mid-boss drops the
first Core, and the boss drops a Resonance shard. Equipment as a rare event keeps each piece
memorable and keeps the inventory screen from needing to scroll.

## Vertical slice gear list

| Item | Slot | Source | Effect |
|---|---|---|---|
| Chipped blade | Weapon | start | `mult 16`, 2-step Addition |
| Type-2 plating | Armour | start | `ward 4` |
| Field blade | Weapon | 04 drop | `mult 20`, 3-step Addition |
| Type-1 plating | Armour | 04B drop | `ward 6` |
| Anchor core | Core | 04B boss | Guard becomes a counter |
| Reagent, basic | Core | Mage start | `charge +3` |
| Directive module | Core | 05C | GRD taunts on entry |
| Resonance shard | Core | 07 boss | Resonance fills 25% faster |

Eight pieces. That is a complete-feeling set for a slice and costs almost nothing to draw:
equipment needs an icon, not a sprite, and icons can share one 16x16 atlas.
