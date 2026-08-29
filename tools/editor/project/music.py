"""Songs, patterns, instruments and samples."""

import contextlib
import io
import json
import os
import re

import pnx_assets as pa                                     # noqa: E402


class MusicMixin:
    def songs(self):
        """Every [music.*], in the shape the editor draws.

        Arrangement is the only authoring surface now -- a song with [[track]] entries hands
        the UI its clips/tracks/markers and a LIVE preview of what they compile to (by calling
        the exact same compile_arrangement the real build uses), not the raw patterns/order
        that preview compiles into. A song that predates arrangement (no tracks yet) still
        reports its raw patterns/order too, so the editor can offer "convert to arrangement"
        instead of a dead end.
        """
        out = []
        for name, spec in sorted(self.man.get("music", {}).items()):
            patterns = [list(p.get("rows", [])) for p in spec.get("pattern", [])]
            rows_per = len(patterns[0]) if patterns else 0
            instruments = []
            synth = spec.get("synth", [])
            for i, ins in enumerate(spec.get("instrument", [])):
                entry = {"name": ins.get("name", ""),
                         "wave": ins.get("wave", "square"),
                         "attack": ins.get("attack", 5),
                         "decay": ins.get("decay", 50),
                         "sustain": ins.get("sustain", 180),
                         "release": ins.get("release", 100),
                         # The synth record for this index, if the song carries one. The
                         # pipeline requires the two tables be the same length -- a row
                         # names ONE instrument index -- so they are shown as one thing.
                         "synth": dict(synth[i]) if i < len(synth) else None}
                instruments.append(entry)

            tracks = spec.get("track", [])
            clips = [{"name": c.get("name", ""), "rows": list(c.get("rows", []))}
                     for c in spec.get("clip", [])]
            markers = [{"name": m.get("name", ""), "at": int(m.get("at", 0))}
                       for m in spec.get("markers", [])]

            preview = None
            if tracks:
                try:
                    derived_patterns, derived_order, _ = pa.compile_arrangement(spec)
                    preview = {
                        "patterns": len(derived_patterns),
                        "order": len(derived_order),
                        "rows_per": len(derived_patterns[0]["rows"]) if derived_patterns else 0,
                        "bytes": (len(derived_patterns)
                                  * (len(derived_patterns[0]["rows"]) if derived_patterns else 0)
                                  * int(spec.get("channels", 4)) * 2
                                  + len(instruments) * 8
                                  + (2 + len(synth) * 48 if synth else 0)),
                    }
                    preview_error = None
                except pa.BuildError as e:
                    preview_error = str(e)
            else:
                preview_error = None

            out.append({
                "name": name,
                "tempo": spec.get("tempo", 120),
                "channels": spec.get("channels", 4),
                "clips": clips,
                "tracks": [{"channel": int(t.get("channel", 0)),
                            "placement": list(t.get("placement", []))} for t in tracks],
                "markers": markers,
                "arrangement": bool(tracks),
                "resolution": spec.get("resolution", 16),
                "preview": preview,
                "preview_error": preview_error,
                # Legacy-only: present so a song that predates arrangement can still be
                # inspected/converted. A song with tracks ignores these at build time (see
                # compile_arrangement's hook in pack_music) -- kept here only until
                # convert_to_arrangement runs, which deletes them.
                "rows_per": rows_per,
                "patterns": patterns,
                "order": list(spec.get("order", list(range(len(patterns))))),
                "instruments": instruments,
                "has_synth": bool(synth),
                "bytes": (len(patterns) * rows_per * spec.get("channels", 4) * 2
                          + len(instruments) * 8
                          + (2 + len(synth) * 48 if synth else 0)),
            })
        return out

    def add_song(self, name, tempo=120, rows=16, synth=True):
        """Create a [music.*] with one instrument and one silent clip on one track.

        Seeded rather than left blank: the pipeline refuses a song with no instruments and no
        placements, so an empty one could not be saved at all -- the same reason a new map
        arrives with a room already in it. Arrangement is the only authoring surface a new
        song gets now -- see songs()/compile_arrangement -- so the seed is a clip+track, not a
        raw pattern.
        """
        if not re.fullmatch(r"[a-z][a-z0-9_]*", name):
            raise ValueError("a song name must be lowercase letters, digits and "
                             "underscores -- it becomes a C identifier")
        if name in self.man.get("music", {}):
            raise ValueError(f"a song named {name!r} already exists")
        rows = int(rows)
        if not 1 <= rows <= 64:
            raise ValueError("a clip holds between 1 and 64 rows")

        body = [f"[music.{name}]", f"tempo = {int(tempo)}", "channels = 4", "",
                f"[[music.{name}.instrument]]",
                'wave = "square"', "attack = 5", "decay = 80", "sustain = 180",
                "release = 120", ""]
        if synth:
            body += [f"[[music.{name}.synth]]",
                     'filter = "lowpass"', "cutoff_base = 128", "resonance = 0",
                     "cutoff_env = 0", 'lfo_target = "off"', "lfo_rate = 0",
                     "lfo_depth = 0", "pitch_env = 0", "pitch_env_decay = 0",
                     "reverb = 0", "chorus = 0",
                     "amp = { attack = 5, decay = 80, sustain = 180, release = 120 }",
                     "cutoff = { attack = 5, decay = 80, sustain = 128, release = 120 }",
                     "osc = [",
                     '  { wave = "square", volume = 200, detune = 0, octave = 0, '
                     'duty = 128 },',
                     "]", ""]
        body += [f"[[music.{name}.clip]]", 'name = "intro"', "rows = ["]
        body += ['  ".",' for _ in range(rows)]
        body += ["]", "",
                 f"[[music.{name}.track]]", "channel = 0", "placement = [",
                 '  { clip = "intro", start = 0 },', "]"]

        with open(self.path, "a") as f:
            f.write("\n\n" + "\n".join(body) + "\n")
        self.reload()

    def remove_song(self, name):
        """Delete a song and every table under it."""
        if name not in self.man.get("music", {}):
            raise ValueError(f"no song named {name!r}")
        lines = open(self.path).read().split("\n")
        keep, drop = [], False
        for line in lines:
            s = line.lstrip()
            if s.startswith("["):
                inner = s.lstrip("[")
                drop = (inner.startswith(f"music.{name}]")
                        or inner.startswith(f"music.{name}."))
            if not drop:
                keep.append(line)
        with open(self.path, "w") as f:
            f.write("\n".join(keep))
        self.reload()

    def add_clip(self, name, clip_name, rows):
        """Append a [[music.x.clip]] -- a pure note sequence, no instrument of its own (see
        compile_arrangement for why). Name-keyed rather than index-keyed like a pattern, so
        removing one doesn't renumber every placement that names a LATER clip."""
        spec = self.man.get("music", {}).get(name)
        if spec is None:
            raise ValueError(f"no song named {name!r}")
        if not re.fullmatch(r"[a-z][a-z0-9_]*", clip_name):
            raise ValueError("a clip name must be lowercase letters, digits and "
                             "underscores -- it becomes a C identifier")
        if any(c.get("name") == clip_name for c in spec.get("clip", [])):
            raise ValueError(f"song {name!r} already has a clip named {clip_name!r}")
        self._validate_clip_rows(clip_name, rows)

        body = [f"[[music.{name}.clip]]", f'name = "{clip_name}"', "rows = ["]
        body += [f'  {json.dumps(r)},' for r in rows]
        body += ["]"]
        with open(self.path, "a") as f:
            f.write("\n\n" + "\n".join(body) + "\n")
        self.reload()

    def save_clip(self, name, clip_name, rows):
        """Rewrite one clip's rows in place."""
        spec = self.man.get("music", {}).get(name)
        if spec is None:
            raise ValueError(f"no song named {name!r}")
        idx = self._clip_index(spec, clip_name)
        self._validate_clip_rows(clip_name, rows)
        body = [f"[[music.{name}.clip]]", f'name = "{clip_name}"', "rows = ["]
        body += [f'  {json.dumps(r)},' for r in rows]
        body += ["]"]
        self._replace_table(f"[[music.{name}.clip]]", idx, body)

    def clip_users(self, name, clip_name):
        """Which track/channel placements play this clip, so removing it can refuse and say
        where -- same shape as instrument_users."""
        spec = self.man.get("music", {}).get(name, {})
        hits = []
        for t in spec.get("track", []):
            for p in t.get("placement", []):
                if p.get("clip") == clip_name:
                    hits.append(f"channel {t.get('channel')} row {p.get('start', 0)}")
        return hits

    def remove_clip(self, name, clip_name):
        spec = self.man.get("music", {}).get(name)
        if spec is None:
            raise ValueError(f"no song named {name!r}")
        idx = self._clip_index(spec, clip_name)
        users = self.clip_users(name, clip_name)
        if users:
            raise ValueError(
                f"clip {clip_name!r} is placed on {', '.join(users[:3])}"
                + (f" and {len(users) - 3} more" if len(users) > 3 else "")
                + ". Remove those placements first.")
        found = self._nth_table(f"[[music.{name}.clip]]", idx)
        if not found:
            raise ValueError(f"no [[music.{name}.clip]] #{idx} in the manifest")
        lines, head, end = found
        while end < len(lines) and lines[end].strip() == "":
            end += 1
        while head > 0 and lines[head - 1].strip() == "":
            head -= 1
        lines[head:end] = [""]
        with open(self.path, "w") as f:
            f.write("\n".join(lines))
        self.reload()

    def _validate_clip_rows(self, clip_name, rows):
        if not rows:
            raise ValueError("a clip needs at least one row")
        if len(rows) > 255:
            raise ValueError("a clip holds at most 255 rows")
        for ri, tok in enumerate(rows):
            try:
                pa.parse_note(tok, f"clip {clip_name!r} row {ri}")
            except pa.BuildError as e:
                raise ValueError(str(e)) from None

    def _clip_index(self, spec, clip_name):
        clips = spec.get("clip", [])
        idx = next((i for i, c in enumerate(clips) if c.get("name") == clip_name), None)
        if idx is None:
            raise ValueError(f"no clip named {clip_name!r}")
        return idx

    def save_track(self, name, channel, placements):
        """Rewrite (or create) the track for one channel -- the sequencer has exactly 4, so
        a track IS a channel rather than its own identity. Placements are either a clip
        (positioned by `start`) or an instrument-change (a program-change event, also
        positioned by `start`) -- validated the same way compile_arrangement itself would
        reject them, so a bad save fails here with a clear message instead of at Build.
        """
        spec = self.man.get("music", {}).get(name)
        if spec is None:
            raise ValueError(f"no song named {name!r}")
        channel = int(channel)
        if not 0 <= channel < 4:
            raise ValueError("channel must be 0-3 -- the sequencer has exactly 4")

        clip_names = {c.get("name") for c in spec.get("clip", [])}
        instrument_count = len(spec.get("instrument", []))
        placements = sorted(placements, key=lambda p: int(p.get("start", 0)))
        next_free = 0
        for p in placements:
            start = int(p.get("start", 0))
            if start < 0:
                raise ValueError("a placement can't start before row 0")
            if "instrument" in p:
                inst = int(p["instrument"])
                if not 0 <= inst < instrument_count:
                    raise ValueError(f"instrument {inst} does not exist")
                continue
            clip_name = p.get("clip")
            if clip_name not in clip_names:
                raise ValueError(f"no such clip {clip_name!r}")
            if start < next_free:
                raise ValueError(f"clip {clip_name!r} at row {start} overlaps the placement "
                                 f"before it, which runs through row {next_free - 1}")
            clip = next(c for c in spec["clip"] if c.get("name") == clip_name)
            next_free = start + len(clip.get("rows", []))

        body = [f"[[music.{name}.track]]", f"channel = {channel}", "placement = ["]
        for p in placements:
            if "instrument" in p:
                body.append(f'  {{ instrument = {int(p["instrument"])}, '
                            f'start = {int(p.get("start", 0))} }},')
            else:
                body.append(f'  {{ clip = "{p["clip"]}", start = {int(p.get("start", 0))} }},')
        body += ["]"]

        tracks = spec.get("track", [])
        idx = next((i for i, t in enumerate(tracks) if int(t.get("channel", -1)) == channel),
                   None)
        if idx is not None:
            self._replace_table(f"[[music.{name}.track]]", idx, body)
            return
        found = self._nth_table(f"[[music.{name}.track]]", len(tracks) - 1) if tracks else None
        if found:
            lines, head, end = found
            while end > head and lines[end - 1].strip() == "":
                end -= 1
            lines[end:end] = [""] + body
            with open(self.path, "w") as f:
                f.write("\n".join(lines))
            self.reload()
            return
        with open(self.path, "a") as f:
            f.write("\n\n" + "\n".join(body) + "\n")
        self.reload()

    def save_markers(self, name, markers):
        """Rewrite the song-level `markers` list -- transition-safe row positions, named so
        they reach the generated header (MUSIC_<SONG>_MARKER_<NAME>) and so game code and the
        editor's timeline can both refer to one by a name instead of a bare row number."""
        spec = self.man.get("music", {}).get(name)
        if spec is None:
            raise ValueError(f"no song named {name!r}")
        seen = set()
        for m in markers:
            label = str(m.get("name", "")).strip()
            if not re.fullmatch(r"[a-z][a-z0-9_]*", label):
                raise ValueError("a marker name must be lowercase letters, digits and "
                                 "underscores -- it becomes a C identifier")
            if label in seen:
                raise ValueError(f"two markers are both named {label!r}")
            seen.add(label)
            if int(m.get("at", -1)) < 0:
                raise ValueError("a marker's row can't be negative")

        value = ("markers = [\n" + "".join(
            f'  {{ name = "{m["name"]}", at = {int(m["at"])} }},\n' for m in markers) + "]"
            ) if markers else "markers = []"

        lines, start, end = self._music_block(name)
        at = next((j for j in range(start + 1, end) if re.match(r"\s*markers\s*=", lines[j])),
                  None)
        if at is not None:
            stop = at + 1
            depth = lines[at].count("[") - lines[at].count("]")
            while depth > 0 and stop < end:
                depth += lines[stop].count("[") - lines[stop].count("]")
                stop += 1
            lines[at:stop] = [value]
        else:
            limit = next((j for j in range(start + 1, end)
                         if lines[j].lstrip().startswith("[")), end)
            at = start + 1
            for j in range(start + 1, limit):
                if re.match(r"\s*[A-Za-z_][A-Za-z0-9_]*\s*=", lines[j]):
                    at = j + 1
            lines[at:at] = [value]
        with open(self.path, "w") as f:
            f.write("\n".join(lines))
        self.reload()

    def convert_to_arrangement(self, name):
        """One-time migration for a song still authored as raw [[pattern]]/order: splits each
        pattern's per-channel columns into name-keyed clips and builds one track per channel
        replaying the old order sequence, then deletes the now-superseded patterns/order.

        A clip carries no instrument, so a channel whose notes actually change instrument
        partway through a pattern splits into more than one clip there, joined by an
        instrument-change placement -- see compile_arrangement's own docstring for why a clip
        can't just carry one. Lossless: the SAME order position reusing the SAME pattern
        produces the SAME clip name (dict-keyed, so redefining it twice is a no-op) and a
        second placement rather than a second clip, the same reuse a hand-written `order`
        list already relied on.
        """
        spec = self.man.get("music", {}).get(name)
        if spec is None:
            raise ValueError(f"no song named {name!r}")
        if spec.get("track"):
            raise ValueError(f"song {name!r} already uses an arrangement")
        patterns = spec.get("pattern", [])
        if not patterns:
            raise ValueError(f"song {name!r} has no patterns to convert")

        order = spec.get("order")
        if order is None:
            # A stray `order = [...]` written after the LAST [[pattern]] block parses, by
            # TOML's own table-scoping rules, as a key on THAT pattern rather than on the
            # song -- an easy mistake to make (examples/audiotest and examples/overworld are
            # both written exactly this way) that otherwise silently falls back to "play
            # every pattern once, in file order" with nothing to say why. Recovered here
            # rather than left to produce a lossy conversion of a song that never actually
            # played what its own manifest looked like it said.
            order = patterns[-1].get("order")
        order = list(order) if order is not None else list(range(len(patterns)))

        channels = int(spec.get("channels", 4))
        rows_per = len(patterns[0].get("rows", []))

        # Chunked at exactly the original pattern size, so a repeated section dedupes back
        # down to one clip+placement pair per occurrence the same way it deduped to one
        # reused pattern before conversion, rather than however compile_arrangement's own
        # default (16) happens to slice this particular song.
        self._set_song_key(name, "resolution", str(rows_per))

        clip_defs = {}
        placements = {c: [] for c in range(channels)}

        for order_pos, pat_idx in enumerate(order):
            base_row = order_pos * rows_per
            rows = patterns[pat_idx].get("rows", [])
            for c in range(channels):
                cells = []
                for row in rows:
                    cell = row.split()[c]
                    if ":" in cell:
                        note_tok, inst = cell.split(":", 1)
                        cells.append((note_tok, int(inst)))
                    else:
                        cells.append((cell, None))

                # Split into runs: a new run starts only where a REAL note names an
                # instrument different from the run's so far. Holds/offs (inst is None)
                # never start a run -- they ride along in whichever run they sustain under.
                runs, run_start, run_inst = [], 0, None
                for i, (tok, inst) in enumerate(cells):
                    if inst is not None and run_inst is not None and inst != run_inst:
                        runs.append((run_start, i, run_inst))
                        run_start = i
                        run_inst = inst
                    elif inst is not None and run_inst is None:
                        run_inst = inst
                runs.append((run_start, len(cells), run_inst))

                for run_start, run_end, run_inst in runs:
                    run_cells = cells[run_start:run_end]
                    if run_inst is None or not any(t != "." for t, _ in run_cells):
                        continue  # nothing real in this run -- no placement needed
                    clip_name = f"{name}_p{pat_idx}_c{c}_r{run_start}"
                    clip_defs[clip_name] = [t for t, _ in run_cells]
                    start = base_row + run_start
                    placements[c].append({"instrument": run_inst, "start": start})
                    placements[c].append({"clip": clip_name, "start": start})

        clip_lines = []
        for cname, rows in clip_defs.items():
            clip_lines += [f"[[music.{name}.clip]]", f'name = "{cname}"', "rows = ["]
            clip_lines += [f'  {json.dumps(r)},' for r in rows]
            clip_lines += ["]", ""]

        track_lines = []
        for c in range(channels):
            plist = sorted(placements[c], key=lambda p: p["start"])
            if not plist:
                continue
            track_lines += [f"[[music.{name}.track]]", f"channel = {c}", "placement = ["]
            for p in plist:
                if "instrument" in p:
                    track_lines.append(f'  {{ instrument = {p["instrument"]}, '
                                       f'start = {p["start"]} }},')
                else:
                    track_lines.append(f'  {{ clip = "{p["clip"]}", start = {p["start"]} }},')
            track_lines += ["]", ""]

        with open(self.path, "a") as f:
            f.write("\n\n" + "\n".join(clip_lines + track_lines).rstrip() + "\n")
        self.reload()

        # Now superseded -- delete the raw patterns and the order key so the manifest
        # doesn't carry two conflicting descriptions of the same song.
        for idx in range(len(patterns) - 1, -1, -1):
            found = self._nth_table(f"[[music.{name}.pattern]]", idx)
            if not found:
                continue
            lines, head, end = found
            while end < len(lines) and lines[end].strip() == "":
                end += 1
            while head > 0 and lines[head - 1].strip() == "":
                head -= 1
            lines[head:end] = [""]
            with open(self.path, "w") as f:
                f.write("\n".join(lines))
            self.reload()

        lines, start, end = self._music_block(name)
        at = next((j for j in range(start + 1, end) if re.match(r"\s*order\s*=", lines[j])),
                  None)
        if at is not None:
            stop = at + 1
            depth = lines[at].count("[") - lines[at].count("]")
            while depth > 0 and stop < end:
                depth += lines[stop].count("[") - lines[stop].count("]")
                stop += 1
            lines[at:stop] = [""]
            with open(self.path, "w") as f:
                f.write("\n".join(lines))
            self.reload()

    def add_instrument(self, name):
        """Append an instrument, and its synth record when the song has a synth table.

        Both or neither. The pipeline refuses tables of different lengths because a pattern
        row names one index, so adding to one alone would break the build in a way that
        points at the song rather than at this button.
        """
        spec = self.man.get("music", {}).get(name)
        if spec is None:
            raise ValueError(f"no song named {name!r}")
        count = len(spec.get("instrument", []))
        if count >= 255:
            raise ValueError("a song holds at most 255 instruments")

        body = [f"[[music.{name}.instrument]]", 'wave = "square"', "attack = 5",
                "decay = 80", "sustain = 180", "release = 120"]
        found = self._nth_table(f"[[music.{name}.instrument]]", count - 1)
        if not found:
            raise ValueError(f"song {name!r} has no instruments to append after")
        lines, head, end = found
        while end > head and lines[end - 1].strip() == "":
            end -= 1
        lines[end:end] = [""] + body
        with open(self.path, "w") as f:
            f.write("\n".join(lines))
        self.reload()

        if spec.get("synth"):
            self.add_synth_record(name)

    def add_synth_record(self, name):
        body = [f"[[music.{name}.synth]]", 'filter = "off"', "cutoff_base = 128",
                "resonance = 0", "cutoff_env = 0", 'lfo_target = "off"', "lfo_rate = 0",
                "lfo_depth = 0", "pitch_env = 0", "pitch_env_decay = 0", "reverb = 0",
                "chorus = 0",
                "amp = { attack = 5, decay = 80, sustain = 180, release = 120 }",
                "cutoff = { attack = 5, decay = 80, sustain = 128, release = 120 }",
                "osc = [",
                '  { wave = "square", volume = 200, detune = 0, octave = 0, duty = 128 },',
                "]"]
        count = len(self.man.get("music", {}).get(name, {}).get("synth", []))
        found = self._nth_table(f"[[music.{name}.synth]]", count - 1)
        if not found:
            with open(self.path, "a") as f:
                f.write("\n\n" + "\n".join(body) + "\n")
            self.reload()
            return
        lines, head, end = found
        while end > head and lines[end - 1].strip() == "":
            end -= 1
        lines[end:end] = [""] + body
        with open(self.path, "w") as f:
            f.write("\n".join(lines))
        self.reload()

    def instrument_users(self, name, index):
        """Which patterns play this instrument, so removing it can refuse and say where."""
        spec = self.man.get("music", {}).get(name, {})
        hits = []
        for pi, pat in enumerate(spec.get("pattern", [])):
            for ri, row in enumerate(pat.get("rows", [])):
                for cell in row.split():
                    if ":" in cell and cell.split(":", 1)[1] == str(index):
                        hits.append(f"pattern {pi} row {ri}")
                        break
        return hits

    def remove_instrument(self, name, index):
        """Delete an instrument, once no row plays it.

        Refused while in use rather than renumbering, because a row names an instrument by
        INDEX -- removing one silently repoints every note above it at a different sound.
        """
        spec = self.man.get("music", {}).get(name)
        if spec is None:
            raise ValueError(f"no song named {name!r}")
        count = len(spec.get("instrument", []))
        if count <= 1:
            raise ValueError("a song needs at least one instrument")
        if not 0 <= index < count:
            raise ValueError(f"song {name!r} has {count} instruments, not {index + 1}")
        users = self.instrument_users(name, index)
        if users:
            raise ValueError(
                f"instrument {index} is played in {', '.join(users[:3])}"
                + (f" and {len(users) - 3} more" if len(users) > 3 else "")
                + ". Repoint those notes first -- removing it would renumber every "
                  "instrument above it and silently change what they play.")
        if index != count - 1:
            raise ValueError(
                f"only the last instrument can be removed ({count - 1}), because a row "
                f"names an instrument by index and removing one from the middle repoints "
                f"every note above it.")

        for header in (f"[[music.{name}.instrument]]", f"[[music.{name}.synth]]"):
            found = self._nth_table(header, index)
            if not found:
                continue
            lines, head, end = found
            while end < len(lines) and lines[end].strip() == "":
                end += 1
            while head > 0 and lines[head - 1].strip() == "":
                head -= 1
            lines[head:end] = [""]
            with open(self.path, "w") as f:
                f.write("\n".join(lines))
            self.reload()

    def _music_block(self, name):
        """(lines, start, end) for a [music.x] table and every subtable under it.

        `[[music.x.pattern]]` and `[[music.x.instrument]]` bind to the song, so the block
        runs past them -- stopping at the first bracket would cut a song in half and leave
        its patterns orphaned under whatever came next.
        """
        lines = open(self.path).read().split("\n")
        want = f"[music.{name}]"
        start = next((i for i, l in enumerate(lines) if l.strip() == want), None)
        if start is None:
            return None
        end = len(lines)
        for j in range(start + 1, len(lines)):
            s = lines[j].lstrip()
            if not s.startswith("["):
                continue
            # Both spellings belong to the song: `[music.x.foo]` for a subtable and
            # `[[music.x.foo]]` for an array of them. Matching only the first stopped the
            # block at the song's own first instrument, which made every pattern in it
            # invisible to the editor.
            inner = s.lstrip("[")
            if not inner.startswith(f"music.{name}."):
                end = j
                break
        return lines, start, end

    def _set_song_key(self, name, key, value_text):
        """Insert or replace one bare `key = value_text` line directly under [music.x],
        BEFORE its first subtable -- the one placement TOML parses as belonging to the song
        itself rather than to whichever subtable happens to come first. Writing it anywhere
        else is the exact mistake convert_to_arrangement has to recover from on read (a key
        placed after the LAST subtable binds to THAT subtable, not the song)."""
        lines, start, end = self._music_block(name)
        line = f"{key} = {value_text}"
        at = next((j for j in range(start + 1, end) if re.match(rf"\s*{key}\s*=", lines[j])),
                  None)
        if at is not None:
            lines[at] = line
        else:
            limit = next((j for j in range(start + 1, end)
                         if lines[j].lstrip().startswith("[")), end)
            ins = start + 1
            for j in range(start + 1, limit):
                if re.match(r"\s*[A-Za-z_][A-Za-z0-9_]*\s*=", lines[j]):
                    ins = j + 1
            lines[ins:ins] = [line]
        with open(self.path, "w") as f:
            f.write("\n".join(lines))
        self.reload()

    def samples(self):
        out = []
        for name, spec in sorted(self.man.get("sample", {}).items()):
            rel = spec.get("file", "")
            full = os.path.join(self.root, rel)
            entry = {"name": name, "file": rel, "source_bytes": None, "bytes": None}
            if os.path.exists(full):
                entry["source_bytes"] = os.path.getsize(full)
            blob = os.path.join(self.res, f"sfx_{name}.bin")
            if os.path.exists(blob):
                entry["bytes"] = os.path.getsize(blob)
            out.append(entry)
        return out

    def sample_wav_bytes(self, name):
        """The raw WAV bytes behind a declared [sample.*] -- for previewing it in the
        browser (an <audio> element), not for the pipeline, which reads it itself.
        `_safe` (CodeMixin) is what keeps this from serving anything outside the
        project even given a manifest with a stray `../` in it."""
        spec = self.man.get("sample", {}).get(name)
        if not spec:
            raise ValueError(f"no such sample: {name!r}")
        full = self._safe(spec.get("file", ""))
        with open(full, "rb") as f:
            return f.read()

    def wav_files(self):
        """WAVs inside the project, so adding one needs no file dialog."""
        out = []
        for dirpath, dirnames, files in os.walk(self.root):
            dirnames[:] = [d for d in dirnames
                           if d not in ("build", "resources", "__pycache__", ".git")]
            for fn in sorted(files):
                if fn.lower().endswith(".wav"):
                    full = os.path.join(dirpath, fn)
                    out.append({"path": os.path.relpath(full, self.root),
                                "bytes": os.path.getsize(full)})
        return out

    def save_sample(self, name, rel):
        """Declare a [sample.*], validated by packing it the way the build will."""
        if not re.fullmatch(r"[a-z][a-z0-9_]*", name):
            raise ValueError("a sample name must be lowercase letters, digits and "
                             "underscores -- it becomes a C identifier")
        if not os.path.exists(self._safe(rel)):
            raise ValueError(f"no such file: {rel}")
        try:
            with contextlib.redirect_stdout(io.StringIO()):
                pa.pack_samples(self.root, {name: {"file": rel}}, self.orientation)
        except pa.BuildError as e:
            raise ValueError(str(e)) from None

        body = [f"[sample.{name}]", f'file = "{rel}"']
        found = self._nth_table(f"[sample.{name}]", 0)
        if found:
            self._replace_table(f"[sample.{name}]", 0, body)
            return
        with open(self.path, "a") as f:
            f.write("\n\n" + "\n".join(body) + "\n")
        self.reload()

    def remove_sample(self, name):
        if name not in self.man.get("sample", {}):
            raise ValueError(f"no sample named {name!r}")
        found = self._nth_table(f"[sample.{name}]", 0)
        if not found:
            raise ValueError(f"no [sample.{name}] block in the manifest")
        lines, head, end = found
        while end < len(lines) and lines[end].strip() == "":
            end += 1
        while head > 0 and lines[head - 1].strip() == "":
            head -= 1
        lines[head:end] = [""]
        with open(self.path, "w") as f:
            f.write("\n".join(lines))
        self.reload()

    def _nth_table(self, header, index):
        """(lines, start, end) for the index-th occurrence of a table header, file-wide.

        Searched across the WHOLE file rather than inside the song's leading block,
        because TOML lets a table's subtables appear anywhere -- audiotest's synth records
        sit after its samples, which are not part of the song at all. Assuming contiguity
        found the patterns and silently missed the synth table.
        """
        lines = open(self.path).read().split("\n")
        heads = [j for j, l in enumerate(lines) if l.strip() == header]
        if index >= len(heads):
            return None
        head = heads[index]
        end = next((j for j in range(head + 1, len(lines))
                    if lines[j].lstrip().startswith("[")), len(lines))
        return lines, head, end

    def _replace_table(self, header, index, body):
        """Rewrite one table in place, keeping the blank lines that separate it."""
        found = self._nth_table(header, index)
        if not found:
            raise ValueError(f"{header} #{index} is not in the manifest")
        lines, head, end = found
        gap = 0
        while end - gap > head and lines[end - gap - 1].strip() == "":
            gap += 1
        lines[head:end] = body + [""] * gap
        with open(self.path, "w") as f:
            f.write("\n".join(lines))
        self.reload()

    def save_song_meta(self, name, tempo=None, order=None, resolution=None):
        """Tempo, the (legacy) order list, and an arrangement's resolution -- the song-level
        things changed often enough not to deserve their own writer each.

        Patterns and instruments are edited through their own writers, because rewriting a
        whole song to change one cell would discard every comment in it.
        """
        spec = self.man.get("music", {}).get(name)
        if spec is None:
            raise ValueError(f"no song named {name!r}")

        want = []
        if tempo is not None:
            t = int(tempo)
            if not 20 <= t <= 400:
                raise ValueError("tempo must be between 20 and 400 bpm")
            want.append(("tempo", f"tempo = {t}"))
        if order is not None:
            count = len(spec.get("pattern", []))
            for p in order:
                if not 0 <= int(p) < count:
                    raise ValueError(f"order names pattern {p}, but the song has {count}")
            if not order:
                raise ValueError("an order list with no entries plays nothing")
            want.append(("order", "order = " + json.dumps([int(p) for p in order])))

        lines, start, end = self._music_block(name)
        for key, value in want:
            at = next((j for j in range(start + 1, end)
                       if re.match(rf"\s*{key}\s*=", lines[j])), None)
            if at is not None:
                # The key may span several lines -- `order` is routinely written one
                # pattern per line -- so the whole array is consumed. Replacing only the
                # line the key sits on leaves the continuation lines behind as bare TOML,
                # which is the same way migrating a map broke on a multi-line `warps`.
                stop = at + 1
                depth = lines[at].count("[") - lines[at].count("]")
                while depth > 0 and stop < end:
                    depth += lines[stop].count("[") - lines[stop].count("]")
                    stop += 1
                lines[at:stop] = [value]
                end -= (stop - at) - 1
            else:
                # Before the first subtable, or the key would bind to a pattern.
                limit = next((j for j in range(start + 1, end)
                             if lines[j].lstrip().startswith("[")), end)
                # One past the last actual key line before that subtable -- not "walk
                # back over blanks from the subtable", which can land a new key between
                # a subtable's own explanatory comment and the subtable it describes.
                at = start + 1
                for j in range(start + 1, limit):
                    if re.match(r"\s*[A-Za-z_][A-Za-z0-9_]*\s*=", lines[j]):
                        at = j + 1
                lines[at:at] = [value]
                end += 1
        with open(self.path, "w") as f:
            f.write("\n".join(lines))
        self.reload()

        if resolution is not None:
            r = int(resolution)
            if not 1 <= r <= 255:
                raise ValueError("resolution must be between 1 and 255 rows")
            self._set_song_key(name, "resolution", str(r))

    def save_pattern(self, name, index, rows, append=False):
        """Rewrite one pattern's rows, or append a new one. The unit a tracker edits.

        Appending only. Removing a pattern is deliberately not offered: the order list
        names patterns by INDEX, so deleting one silently renumbers every entry after it
        and the song plays something different with nothing to show why.
        """
        spec = self.man.get("music", {}).get(name)
        if spec is None:
            raise ValueError(f"no song named {name!r}")
        patterns = spec.get("pattern", [])
        if append:
            if len(patterns) >= 255:
                raise ValueError("a song holds at most 255 patterns")
            index = len(patterns)
        elif not 0 <= index < len(patterns):
            raise ValueError(f"song {name!r} has {len(patterns)} patterns, not {index + 1}")

        channels = int(spec.get("channels", 4))
        expect = len(patterns[0].get("rows", [])) if patterns else len(rows)
        if len(rows) != expect:
            raise ValueError(f"pattern 0 has {expect} rows, so this one must too -- the "
                             f"pipeline requires every pattern in a song to match")
        for ri, row in enumerate(rows):
            cells = row.split()
            if len(cells) != channels:
                raise ValueError(f"row {ri} has {len(cells)} cells for {channels} channels")

        body = [f"[[music.{name}.pattern]]", "rows = ["]
        body += [f'  {json.dumps(r)},' for r in rows]
        body += ["]"]

        if append:
            # After the LAST existing pattern, so the file order matches the index order --
            # a pattern appended at the end of the file but numbered from the middle would
            # make the manifest unreadable next to the tracker.
            found = self._nth_table(f"[[music.{name}.pattern]]", len(patterns) - 1)
            if not found:
                raise ValueError(f"song {name!r} has no patterns to append after")
            lines, head, end = found
            while end > head and lines[end - 1].strip() == "":
                end -= 1
            lines[end:end] = [""] + body
            with open(self.path, "w") as f:
                f.write("\n".join(lines))
            self.reload()
            return

        self._replace_table(f"[[music.{name}.pattern]]", index, body)

    def save_instrument(self, name, index, plain, synth=None):
        """Rewrite one instrument of a song, both halves.

        The plain envelope and the synth record are edited as ONE thing because a pattern
        row names one instrument index: the pipeline refuses tables of different lengths
        precisely so a note cannot play a different sound depending on which table it
        resolved through. Splitting them in the UI would invite exactly that.

        Validated by packing the candidate through the real pipeline, the same way an
        atlas carve and a sprite are, so anything the build would reject is rejected here.
        """
        spec = self.man.get("music", {}).get(name)
        if spec is None:
            raise ValueError(f"no song named {name!r}")
        count = len(spec.get("instrument", []))
        if not 0 <= index < count:
            raise ValueError(f"song {name!r} has {count} instruments, not {index + 1}")

        if plain.get("wave") not in pa.WAVEFORMS:
            raise ValueError(f"unknown waveform {plain.get('wave')!r} "
                             f"(known: {', '.join(pa.WAVEFORMS)})")
        if synth is not None:
            try:
                pa.pack_synth_instrument(synth, f"instrument {index}")
            except pa.BuildError as e:
                raise ValueError(str(e)) from None
            if not spec.get("synth"):
                raise ValueError(
                    f"song {name!r} has no synth table. Adding one means adding a record "
                    f"for every instrument, because a row names one index and the two "
                    f"tables have to line up.")

        label = str(plain.get("name", "")).strip()
        if label and not re.fullmatch(r"[a-z][a-z0-9_]*", label):
            raise ValueError("an instrument name must be lowercase letters, digits and "
                             "underscores -- it becomes a C identifier")

        body = [f"[[music.{name}.instrument]]"]
        # Optional, and written first when present so the block reads as a named thing.
        # A song's instruments are referred to by INDEX in a pattern row, which is fine for
        # the four bytes it costs and useless for reading -- `3` says nothing, `bass` does.
        if label:
            body.append(f'name = "{label}"')
        body += [f'wave = "{plain["wave"]}"',
                f'attack = {int(plain.get("attack", 5))}',
                f'decay = {int(plain.get("decay", 50))}',
                f'sustain = {int(plain.get("sustain", 180))}',
                f'release = {int(plain.get("release", 100))}']
        self._replace_table(f"[[music.{name}.instrument]]", index, body)

        if synth is not None:
            self._save_synth_record(name, index, synth)

    def _save_synth_record(self, name, index, synth):
        """Rewrite one [[music.x.synth]] entry, keeping the rest of the table."""
        def env(e, d_attack, d_decay, d_sustain, d_release):
            return ("{ attack = %d, decay = %d, sustain = %d, release = %d }"
                    % (int(e.get("attack", d_attack)), int(e.get("decay", d_decay)),
                       int(e.get("sustain", d_sustain)), int(e.get("release", d_release))))

        body = [f"[[music.{name}.synth]]",
                f'filter = "{synth.get("filter", "off")}"',
                f'cutoff_base = {int(synth.get("cutoff_base", 128))}',
                f'resonance = {int(synth.get("resonance", 0))}',
                f'cutoff_env = {int(synth.get("cutoff_env", 0))}',
                f'lfo_target = "{synth.get("lfo_target", "off")}"',
                f'lfo_rate = {int(synth.get("lfo_rate", 0))}',
                f'lfo_depth = {int(synth.get("lfo_depth", 0))}',
                f'pitch_env = {int(synth.get("pitch_env", 0))}',
                f'pitch_env_decay = {int(synth.get("pitch_env_decay", 0))}',
                f'reverb = {int(synth.get("reverb", 0))}',
                f'chorus = {int(synth.get("chorus", 0))}',
                "amp = " + env(synth.get("amp", {}), 5, 80, 180, 120),
                "cutoff = " + env(synth.get("cutoff", {}), 5, 80, 128, 120),
                "osc = ["]
        for o in synth.get("osc", []):
            body.append('  { wave = "%s", volume = %d, detune = %d, octave = %d, '
                        'duty = %d },'
                        % (o.get("wave", "square"), int(o.get("volume", 200)),
                           int(o.get("detune", 0)), int(o.get("octave", 0)),
                           int(o.get("duty", 128))))
        body.append("]")
        self._replace_table(f"[[music.{name}.synth]]", index, body)

