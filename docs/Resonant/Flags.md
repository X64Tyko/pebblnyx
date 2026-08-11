# Flags

The canonical list. `Script.md` currently uses two names for one flag, which is why this
file exists rather than the declaration block being the source of truth.

## The bug

`Script.md` section I declares **`flag_boss_defeated`**. Events 04C, 05, 06 and 06B test
**`flag_boss_dead`**. One of those is undeclared, and on a watch the symptom is not a crash:
the event simply never fires, or always fires, and the binary looks fine. This is exactly the
class of fault `tools/pnx_assets.py` already fails the build over for warps and doors, and
the same treatment should extend to dialogue conditions.

**Resolution:** `flag_boss_defeated` is canonical. Fix the four call sites.

## Canonical set

Story flags are a bitfield, not a struct of bools -- 16 flags is 2 bytes and the save format
packs to 256-byte chunks, so this is about legibility rather than space.

| Bit | Flag | Set by | Read by |
|---|---|---|---|
| 0 | `mage_joined` | 02 | 02, 03, 04, 05B, opt A, opt B |
| 1 | `ward_open` | 02 | door tile swap |
| 2 | `statue_examined` | 03 | 03 |
| 3 | `midboss_defeated` | 04B | 04C, 05C, 06B |
| 4 | `guard_awakened` | 05C | 05C, party roster |
| 5 | `terminal_1` | 06 | 06 |
| 6 | `terminal_2` | 06B | 06B |
| 7 | `boss_defeated` | 07 | 04C, 05, 06, 06B, 07B, 08 |
| 8 | `opr_escaped` | 07 | **nothing** |
| 9 | `gear_check` | 05B | 05B |
| 10 | `mural_seen` | opt B | **nothing** |

## Write-only flags

`opr_escaped` and `mural_seen` are set and never read. Either they gate content not yet
written, or they are dead weight. Both are defensible -- `opr_escaped` obviously wants to
matter in Act III of the full game -- but a flag with no reader should be marked as a
forward declaration, not left ambiguous.

`terminal_1` and `terminal_2` are each read only by the event that sets them, meaning they
are re-entry guards and nothing more. That is fine and worth stating, because it means the
logs have no downstream consequence yet. If reading both should change a later line, now is
the cheap time to decide.

## Convention

- A flag is set in exactly one place.
- An event that can fire twice must test its own flag. Every event above does.
- Flags gate *dialogue variants* freely, but should gate *map geometry* rarely: a door that
  changes state is one tile swap, while a room that changes shape is a second map.
