# How Pebble games are actually played

Off the wrist, in two hands, like a very small handheld.

That sounds trivial and it is not, because the entire published discussion of smartwatch gaming
treats wrist-mounted play as the only mode and concludes the category is a dead end.

## What the criticism says

On Pebble gaming specifically, [Tom's Guide](https://www.tomsguide.com/uk/us/Pebble-Wristwatch-gaming-iphone-android,news-14954.html):

> Pebble smartwatches don't have a touchscreen, so you're relegated to playing games with the
> physical controls, and once you try it while the watch is mounted to your wrist, you'll see just
> how awkward it is.

And the broader strategic objection, from [Hacker News](https://news.ycombinator.com/item?id=8294890):
games work on phones because the phone is what you stare at when you are bored, where a smartwatch is
for when you are busy -- so there is no point gaming on it.

Both are stated as conclusions. **Neither considers removing the watch**, and as far as searching
finds, nobody else does either.

## Why that answers both

| Objection | Off the wrist |
|---|---|
| One-handed button pressing is awkward | Two hands, thumbs on the buttons |
| Screen is too far and at a bad angle | Held at reading distance, square on |
| Touch means a thumb across the display | A finger, from the front |
| "It is for when you are busy" | It is now the thing you are looking at |

The measured input figures support it rather than merely tolerating it: anticipation timing hits 100%
on touch and 97% on buttons at a +-74 ms window, and touch has half the lag of buttons. Both are
comfortable in two hands and neither is comfortable one-handed on a wrist.

It also explains a thing worth designing for: the device gets **handed around**. Verified the plain
way -- taking the watch off and playing Pebblemon with kids, which is where this note came from.

## What it changes

- **Sessions are not inherently ninety seconds.** Design for *interruptibility* -- a notification can
  arrive and throttles rendering to ~0.4 fps -- but not for brevity. Do not pad content into chunks
  for an attention span the hardware does not actually impose.
- **Multi-button and timed input are fair game.** A four-symbol timed sequence is unreasonable on a
  wrist and unremarkable in two hands.
- **Say it out loud.** If nobody has proposed this, proposing it is a contribution: the answer to
  "smartwatch games are awkward" is "then take the watch off", and it costs nothing to state.

## What it makes possible

Two-handed play means the game gets to choose what the buttons are *for*, by choosing which way it
draws. Rotate so the three-button cluster sits along the top edge and it is under both index fingers:
shoulder triggers, for a shooter. Rotate so it sits along the bottom and it is under both thumbs:
pinball flippers. Neither reading exists on a wrist, where there is one thumb and an awkward angle.

That is M4c in [`ROADMAP.md`](ROADMAP.md), and it is the clearest argument that off-wrist play is a
platform fact worth designing around rather than a personal preference.

## Still unverified

Whether anyone in the Pebble or Rebble communities already plays this way and simply has not written
it down. Two searches found the criticism and no answer to it, which is suggestive rather than
conclusive -- forums and Discord are not well indexed.
