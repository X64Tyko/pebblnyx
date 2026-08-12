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
- She is wrong about something at least once. Currently she never is, which makes her an
  oracle rather than a person. **Open:** pick the thing she gets wrong. Slice-level, not deferred.

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

## The Client

Unseen, unnamed, referenced three times. Should stay that way for the whole slice. The final
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
