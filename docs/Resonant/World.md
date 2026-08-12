# World

## Resonance

An energy that bonds to people rather than being wielded by them. The Mage's line is the
thesis: *"Resonance isn't a tool. It's a pact. Break it, and you become that"* -- said over a
statue scorched from the inside.

Rules, as the slice establishes them:

- Bonding is not voluntary in all cases. The mural says some were chosen; it does not say how.
- An uncontrolled Vessel detonates. Subject 7 was scheduled for termination on that basis.
- Van is the first living subject the Mage knows of that did not burn out.
- Constructs can detect Resonance and classify by it -- GRD reads Van positive, Mage negative.
- Resonance is *stored* in Nodes and Vessels are the mobile form.

**Now urgent rather than deferred:** what the Nodes were *for*, and what the Client is. The 60%
escalation from "stop a mob boss" to "fight a god" only lands if the reveal has been prepared, and
preparation has to start in Act I. What the Nodes were *for*. The facility
contained them; the client wants them. Neither reason is stated, and the slice is stronger
for withholding it. But the answer should exist now, because it determines whether the client
is a villain or a rival.

## The Nodes

**Three**, not six. Three nodes with a minor quest between each, then a final line once all three
cores are held -- which lands at roughly the **60% mark**, where the story stops being about a
contract.

| | Node | Between it and the next |
|---|---|---|
| 1 | **Crystal Peaks** | the forge, and learning gear matters |
| 2 | **Iron Keep** -- fortified, someone holds it | **balancing the Resonance** |
| 3 | **The Pit** -- "people don't come back" | the final act's antagonist takes shape |

The three tangent destinations are the other places GORD names: **Sunken City**, **Ashen Forest**,
**Forgotten Coast**. Six locations either way, but only three of them are nodes -- which keeps every
name in the outro doing work and costs nothing.

Three fits the budget where six did not. The slice measured ~77,000 bytes for two zones, ~39,000 of
which is shared across the whole game (fonts, music, samples, UI, dialogue). Marginal cost is about
one atlas per zone, so seven zones lands near **150-170KB of 262KB**. Six nodes plus their tangents
would have been eleven zones and overrun.

## The arc, and what drives it

**Destabilisation is the early-game engine.** Van is not on a quest; he is trying to get the thing
out of him before it kills him. Everything in Act I follows from that, and it means the plot needs
no external call to adventure at all -- the inciting incident is already inside him.

That gives the power curve a shape most JRPGs have to fake:

| | Resonance is | Instability |
|---|---|---|
| **Act I** | a dangerous emergency button, Van only | drops only at Nodes -- scarce and tense |
| **After node 2** | balanceable, and transferable to any party member | manageable, once the tangent teaches how |
| **After node 3 (~60%)** | all three transform, freely | mastered; the cost is no longer the story |

**The tension to watch:** in Act I the player's most exciting mechanic makes their stated goal
worse. That is good drama and a real risk -- players may simply never transform, and then the
mechanic that carries the endgame goes unlearned. The Damper item exists as the early stopgap for
exactly this, and the forced transformation at Event 07 exists so the player has felt it before
having to choose it. Worth watching in playtest rather than designing around now.

**Free transfer at node 2** is the strongest idea here. Once the collected shards temporarily
stabilise the node, it can pass between party members -- so the player chooses who becomes powerful,
which is a real tactical decision, and it costs one variable. It also quietly undercuts the
chosen-one framing: Van was never special, he was holding it. Consistent with "I didn't know I was
a battery."

**The escalation risk.** A massive boost at 60% is the standard way to trivialise the last 40%. The
final act's encounters have to be a step change, which means new silhouettes rather than palette
swaps -- a content cost arriving exactly where the budget is tightest. Budget four to six genuinely
new enemy designs for the last act and take them out of the earlier zones' allowance, not on top
of it.

## The facility

Sector 4 of a research installation. Two zones in the slice:

- **Outer Ruins** -- rusted, breached, drones scavenging. Atlas `ATLAS_OUTER_RUINS`.
- **Inner Sanctum** -- intact, powered, defence grid online. Atlas `ATLAS_INNER_SANCTUM`.

The Sanctum still has power, and the commissary vending machine still has stock and a sales
pitch. That is the zone's thesis in one object: the facility never failed, only its people
did. Cheap to deliver -- it is a tile, a shop, and some cheerful marketing copy.

The environmental shift is doing narrative work ("No more rusted scrap") and should be visible
at a glance: different palette, different tile density, different music. It is the cheapest
possible act break -- a palette swap and a track change read as a whole new place.

## Factions

| | What they want | Present in slice |
|---|---|---|
| **Van** | paid, then alive | yes |
| **The Mage** | to understand Resonance | yes |
| **The facility** (automated) | containment, still executing | yes, as GRD and the grid |
| **OPR** | to complete a contract | yes |
| **The client** | the Nodes, reason unstated | referenced only |

Four of five have a presence. That is a dense enough world for a slice, and the fifth being
absent is the hook.

## Timeline

- **The First Resonance.** Humans bond with Nodes. Depicted on the mural; no date.
- **The facility era.** Nodes contained and studied. LOG-07 records the cascade; LOG-14
  records Subject 7's scheduled termination.
- **The collapse.** Sector 4 becomes a tomb. Duration unstated.
- **Now.** Van takes the contract.

**Deferred (post-slice):** is Van Subject 7? The script strongly implies it and never confirms it, and Van's
"Subject 7." followed by the Mage's "you're not a subject" plays either way. Deciding is not
urgent -- but knowing which one you are writing toward is, because it changes whether Van is
returning to this place or arriving at it.
