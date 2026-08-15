"""Songs, patterns, instruments and samples."""

import contextlib
import io
import json
import os
import re

import pnx_assets as pa                                     # noqa: E402


class MusicMixin:
    def songs(self):
        """Every [music.*], in the shape a tracker draws."""
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
            out.append({
                "name": name,
                "tempo": spec.get("tempo", 120),
                "channels": spec.get("channels", 4),
                "rows_per": rows_per,
                "patterns": patterns,
                "order": list(spec.get("order", list(range(len(patterns))))),
                "instruments": instruments,
                "has_synth": bool(synth),
                # What the blob will cost: two bytes a cell, plus the tables.
                "bytes": (len(patterns) * rows_per * spec.get("channels", 4) * 2
                          + len(instruments) * 8
                          + (2 + len(synth) * 48 if synth else 0)),
            })
        return out

    def add_song(self, name, tempo=120, rows=16, synth=True):
        """Create a [music.*] with one instrument and one empty pattern.

        Seeded rather than left blank: the pipeline refuses a song with no instruments and
        no patterns, so an empty one could not be saved at all -- the same reason a new map
        arrives with a room already in it.
        """
        if not re.fullmatch(r"[a-z][a-z0-9_]*", name):
            raise ValueError("a song name must be lowercase letters, digits and "
                             "underscores -- it becomes a C identifier")
        if name in self.man.get("music", {}):
            raise ValueError(f"a song named {name!r} already exists")
        rows = int(rows)
        if not 1 <= rows <= 64:
            raise ValueError("a pattern holds between 1 and 64 rows")

        blank = "     ".join(["."] * 4)
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
        body += [f"[[music.{name}.pattern]]", "rows = ["]
        body += [f'  "{blank}",' for _ in range(rows)]
        body += ["]"]

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

    def save_song_meta(self, name, tempo=None, order=None):
        """Tempo and the order list -- the two song-level things a tracker changes often.

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

