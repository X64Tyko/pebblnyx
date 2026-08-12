# Characters

Voice is the cheapest characterisation available here. A 200x228 screen shows a few lines at
a time and there is no room for portraits at full size, so a reader must know who is speaking
from the words alone. That constraint is doing real work in the V1 script and these rules
exist to keep it.

## Van

**Tag:** `VAN` | **Role:** front line, Vessel | **Age:** early 30s

A mercenary who takes contracts he does not ask questions about, which is how he ends up
carrying something that wants out of him. Competent, unsentimental, and not stupid -- his
flippancy is a working style, not a lack of understanding.

**Voice rules**
- Short sentences. Rarely over 12 words.
- Answers a question with a decision, not an opinion.
- Deflects fear with logistics: "Looks heavy. Stand back."
- Never explains the world. He does not know it, and pretending otherwise costs the Mage her
  function.
- One vulnerability tell across the whole slice: "It wants out. I can feel it." Do not add
  a second. It lands because it is the only one.

**Arc across the slice.** Contractor -> unwilling cargo -> someone who chooses the next job.
The last beat only works if he is never shown *wanting* purpose beforehand.

**Arc across the game.** He experiences Destabilisation as becoming a mindless violent thing, and the
auto-trigger means he cannot decline it. That resolves when OPRA transforms in front of him and
becomes precisely what he fears -- then Kell takes the burden and transforms *lucid*, present and
herself. The fear is shown, then disproved. What sickened him was not the power.

## Kell

**Tag:** `KELL` | **Role:** support, AoE, doors

Four characters, which matters: the speaker tag is drawn on every dialogue page and eats width
the line needs. `Script.md` still uses the `MAGE` tag throughout and wants a pass to replace
it -- same length, so nothing reflows.

An independent researcher, ten years into studying Resonance, who has never seen a living
subject survive it. Her interest in Van is genuine and also self-serving, and the script is
good about not resolving which dominates.

**Voice rules**
- Longer sentences, subordinate clauses, precise nouns: "Class-4 Arcanist", "kinetic-
  dampening ward", "Class-7 containment unit".
- Corrects people. It is her primary verbal habit.
- Her warmth arrives as concession, never as declaration: "...Fair point." "Fine. A partner.
  For now."
- **The thing she is wrong about is settled, and the plot supplies it.** In Event 06B she tells Van he is
  "the first living sample that didn't burn out." Moireage has been bearing a stone since before the
  record was falsified. Her ten years of research rest on a history he forged, so her most confident
  claim -- the one that makes Van feel singular -- is false, and she has no way to know it. That is the
  correction the character needed and it costs nothing to plant, because the line is already written.

## GORD - from a mispronunciation of guard

**Tag:** `GRD` | **Role:** tank, joins 05C

A security construct that reclassifies Van as its principal mid-encounter. Never gains
personality; the comedy and the pathos both come from the gap between its register and the
situation.

**Voice rules**
- Uppercase, declarative, no articles where they can be dropped.
- Reports state, never opinion. `VESSEL STABILITY: 34%.`
- Never addressed as a person by Van without Van immediately undercutting it.
- **Never becomes funny on purpose.** "ACKNOWLEDGED." after "Don't shoot the wizard" works
  because GRD is not making a joke.

## OPRA - female handler, OPRA is her call sign because her voice has a musical quality.

**Tag:** `OPR` | **Role:** antagonist, escapes the slice

Van's handler. **OPR is a callsign, not a name** -- proposed: *Operator*, the role rather
than the person, which is why Van says "a handler, not a babysitter". Keeping the person
behind it unnamed through the slice is correct and should be deliberate rather than
accidental.

**Voice rules**
- Familiar with Van in a way nobody else is. Uses his name.
- Speaks in contract terms: closed, client, loose ends, delivery method.
- Never raises her voice until the last line, where she does: "Scrap it all. Kill them!"

**At Node 2 she carries a synthetic Node**, and she is *a little odd* before the fight -- the warning
the player gets. Then she transforms into something dark and feral: Van's fear made literal, wearing
the face of someone he knew. She is the demonstration Kell's lucid transformation refutes.

## Moireage -- the Client

Unnamed and unseen through the whole slice; referenced three times and no more.

**He is a Vessel, and he is very old.** One of the original four bearers, a protector whose methods ran
harder than the rest and who believed -- rightly or not -- that his people saw an animal rather than a
guardian. He lost the will to keep suffering for them, went after the other three stones, found he could
never approach a sealed one, and spent however long it took **erasing the history of the stones instead**.
Everything the world believes about Resonance is his forgery. Then he withdrew to desolation with a few
servants and began manufacturing synthetic Nodes.

**He is wrong about what he is building.** He knows four combined make a god and intends to be it; what
actually happens is that his body is the door and Runigran is what comes through. A means who believes he
is an end -- which is a better antagonist than a competent one, and means he never needs a scene
explaining himself, because he does not know the part worth explaining.

**He is Van's mirror, and the third answer to the question Van is asking.** A bearer who accepted the
story that he was a monster. OPRA shows what a stone does to you acutely and against your will; Moireage
shows what it does chronically and by consent; Kell shows it does nothing of the kind. Van picks.

**Voice, when he finally speaks:** not grandiose. Someone who stopped explaining himself to people a very
long time ago. The final
exchange -- "You think it's just one client?" -- is the hook, and it only holds if the player
has been given nothing to guess with.

## Dialogue budget note

**Measured from the battle mockups: a page holds about 87 characters** -- ~29 per line at 6.2px
average, three lines in a 64px box. The earlier 110 estimate was 26% too generous.

Against 87, **8 of 107 dialogue lines need two pages** and none need three; the median line is 50
characters. So the script is in good shape, but those eight should be split *deliberately, at the
beat*, rather than wherever the renderer wraps. The longest is Kell's ward explanation at 145.

Proportional spacing would buy roughly 15% more per page for a 74-byte width table, which would
clear several of the eight.
