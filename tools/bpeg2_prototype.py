#!/usr/bin/env python3
"""Prototype/validation for swapping bitplane's Elias-gamma run-length coding for a
per-unit Huffman code over run lengths (see the design chat this came out of -- the core
idea: concatenate a unit's bitplanes into ONE bit sequence with ONE start bit, RLE that,
then Huffman-code the run-length ALPHABET explicitly per unit instead of Elias-gamma
coding each run generically).

Not part of the pipeline. Round-trip + size validation only, against the SAME real
fixture pixel data bpeg_fixtures.h already carries (tests/fixtures/bitplane/), so results
compare directly against the shipped Elias-gamma format's own measured sizes for the same
content -- no re-extraction from PNGs, no synthetic-only corpus.

Format (see Specs.md for the full breakdown):

  Header (8 bits): 2b (bpp-1), 4b (color_count-1), 2b encoding
    00 = fixed-16x16 huffman   -- run_length field width fixed to cover a 16x16 tile's
                                  own worst case (this prototype: 10 bits, see WHY below)
    01 = variable-width huffman -- run_length field width computed from this unit's own
                                  n * bpp (stored explicitly, see below)
    10 = ESC_RAW                -- packed source pixels verbatim, no table
    11 = ESC_FILL                -- single colour, no table, no stream

  ESC_FILL body: 4 bits (the one colour's real palette index).
  ESC_RAW body: pack_unit_4bpp_raw_indices(pixels) verbatim.

  Huffman body (00/01):
    Colour LUT: color_count * 4 bits (real palette index per local index, ascending
    local-index order -- local index IS frequency rank, so this doubles as the
    frequency-sorted remap table).
    [01 only] 1 byte: n (pixel count) -- needed since ONLY this mode's field widths
    depend on it; 00 already knows n is <=256 from its own tile_px.
    Run dictionary meta: 1b start_bit + 6b (cols-1) (max 64 unique run lengths).
    Per unique run length (ascending by first appearance): run_length field (10 bits
    fixed-16x16, or ceil(log2(bpp*n+1)) bits variable) + 3b (code_length-1) + code_length
    bits of code.
    Encoded stream: one Huffman code per run, in emission order.

  WHY 10 bits, not 9, for the fixed-16x16 mode's run_length field: a run can span the
  WHOLE concatenated bpp*n-bit sequence in the worst case reachable under the pre-crush
  invariant (frequency-sorted CONSECUTIVE local indices), not just one bitplane's own n
  bits -- confirmed by hand for the 2-plane case (a run of exactly n zeros crossing the
  plane 0 -> plane 1 boundary is reachable, see the design chat), and this prototype
  additionally never trusts that derivation alone: encode_unit's own overflow check
  falls back to ESC_RAW for any unit where a real run doesn't fit the field, whatever the
  true worst case turns out to be. 10 bits covers up to 1023, comfortably past a 16x16
  tile's own 4*256=1024 concatenated-bit ceiling minus one.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import pnx_assets as pipeline  # today's shipped _write_elias_gamma, reused rather than
                               # reimplemented, so "today's format" here can never drift
                               # from what actually ships.


# ---------------------------------------------------------------------- bit I/O

class BitWriter:
    def __init__(self):
        self.bits = []

    def write_bit(self, b):
        self.bits.append(b & 1)

    def write_bits_msb(self, v, n):
        for i in range(n - 1, -1, -1):
            self.write_bit((v >> i) & 1)

    def to_bytes(self):
        pad = (-len(self.bits)) % 8
        bits = self.bits + [0] * pad
        out = bytearray()
        for i in range(0, len(bits), 8):
            byte = 0
            for b in bits[i:i + 8]:
                byte = (byte << 1) | b
            out.append(byte)
        return bytes(out)

    def __len__(self):
        return len(self.bits)


class BitReader:
    def __init__(self, data):
        self.data = data
        self.pos = 0

    def read_bit(self):
        byte = self.data[self.pos >> 3]
        shift = 7 - (self.pos & 7)
        self.pos += 1
        return (byte >> shift) & 1

    def read_bits_msb(self, n):
        v = 0
        for _ in range(n):
            v = (v << 1) | self.read_bit()
        return v


# ---------------------------------------------------------------------- pre-crush

def crush(pixels):
    """Frequency-sorted local index remap -- identical rule to tools/pnx_assets.py's
    encode_bitplane_unit: index 0 is the MOST frequent real colour. Returns
    (local_indices, order) where order[local_i] is the real palette value."""
    freq = {}
    for p in pixels:
        freq[p] = freq.get(p, 0) + 1
    order = [c for c, _ in sorted(freq.items(), key=lambda kv: (-kv[1], kv[0]))]
    remap = {c: i for i, c in enumerate(order)}
    local = [remap[p] for p in pixels]
    return local, order


def bits_for_k(k):
    """Bitplanes needed for k distinct local indices -- ceil(log2(k)), k>=2 (k==1 is
    ESC_FILL's own case, never reaches here)."""
    if k <= 2:
        return 1
    if k <= 4:
        return 2
    if k <= 8:
        return 3
    return 4


def concat_bitplanes(local, n, bpp):
    """The bpp*n-bit concatenated sequence: bitplane 0 (LSB of every pixel's local
    index) across all n pixels, then bitplane 1, etc. -- the core structural change from
    today's shipped format, which RLEs each bitplane independently."""
    seq = []
    for p in range(bpp):
        for v in local:
            seq.append((v >> p) & 1)
    return seq


def rle(seq):
    """(start_bit, [run_lengths...]) for a bit sequence, alternating value per run --
    identical alternation rule to today's decoder (flip after each run)."""
    if not seq:
        return 0, []
    start = seq[0]
    runs = []
    cur = seq[0]
    n = 0
    for b in seq:
        if b == cur:
            n += 1
        else:
            runs.append(n)
            cur = b
            n = 1
    runs.append(n)
    return start, runs


# ---------------------------------------------------------------------- length-limited Huffman

def huffman_lengths(freqs, max_len):
    """Length-limited Huffman code lengths (Larmore-Hirschberg package-merge) for a
    {symbol: freq} table -- guaranteed prefix-free, guaranteed no length over max_len,
    optimal for that constraint. Plain (unbounded) Huffman was tried first here and
    dropped: real run-length distributions routinely produce a long tail of
    count-1/count-2 run lengths, which unbounded Huffman happily codes at 7-9+ bits --
    not a property of THIS content being unusual, just of Huffman without a length cap.
    Falling back to ESC_RAW whenever that happened (an earlier cut of this prototype did
    exactly that) made nearly every real fixture look like a regression; it was measuring
    a bail-out, not the format.

    Returns None only when max_len itself cannot address every symbol at all (more than
    2**max_len distinct symbols) -- true infeasibility, not a tuning knob.
    """
    symbols = sorted(freqs.items(), key=lambda kv: kv[1])
    n = len(symbols)
    if n == 1:
        return {symbols[0][0]: 1}
    if 2 ** max_len < n:
        return None

    base = [(f, (s,)) for s, f in symbols]  # reintroduced fresh at every level
    current = list(base)
    for _ in range(max_len - 1):
        packaged = [(current[i][0] + current[i + 1][0], current[i][1] + current[i + 1][1])
                   for i in range(0, len(current) - 1, 2)]
        current = sorted(packaged + base, key=lambda kv: kv[0])

    chosen = sorted(current, key=lambda kv: kv[0])[:2 * (n - 1)]
    counts = {s: 0 for s, _ in symbols}
    for _, syms in chosen:
        for s in syms:
            counts[s] += 1
    assert all(c > 0 for c in counts.values()), "package-merge left a symbol uncounted"
    return counts


def canonical_codes(lengths):
    """Canonical Huffman codes from a {symbol: length} table -- deterministic, so the
    encoder and this prototype's decoder (and, later, the C decoder) agree without the
    table needing to transmit anything about tree SHAPE, only each symbol's length. The
    format still transmits length+code explicitly per entry (Specs.md) rather than
    relying on canonical reconstruction at decode time -- simpler decoder, one less thing
    it has to get right -- but generating codes canonically here keeps the encoder itself
    simple and standard."""
    by_len = sorted(lengths.items(), key=lambda kv: (kv[1], kv[0]))
    codes = {}
    code = 0
    prev_len = 0
    for sym, length in by_len:
        code <<= (length - prev_len)
        codes[sym] = (code, length)
        code += 1
        prev_len = length
    return codes


# ---------------------------------------------------------------------- unit encode/decode

HDR_FIXED16, HDR_VARIABLE, HDR_RAW, HDR_FILL = 0, 1, 2, 3
FIXED16_RUN_BITS = 10  # see module docstring
COLS_BITS = 6  # `6b (cols-1)` per Specs.md -- max 64 unique run lengths per unit

# Every field this format bounds, and what was actually seen against that bound across
# the whole sweep -- printed by main() at the end. The point isn't "did anything
# overflow" (encode_unit already falls back to ESC_RAW when something does, so
# correctness never depends on this), it's "how close does REAL content run to each
# limit", so a field width is a measured decision, not a guess carried over from the
# 16x16/1024-bit worked example.
FIELD_STATS = {
    "run_length": [],        # (unit_name, value, bits_available)
    "cols": [],              # (unit_name, value, bits_available)
    "shared_table_cols": [], # (unit_name, value, bits_available) -- one entry per built
                             # shared/global table, not per unit
}


def _log_field(field, unit_name, value, bits_available):
    FIELD_STATS[field].append((unit_name, value, bits_available))
    limit = (1 << bits_available) - 1
    if value > limit:
        print(f"  OVERFLOW  {unit_name}: {field}={value} exceeds the {bits_available}-bit "
              f"field's {limit} max -- falling back to ESC_RAW for this unit")


def pack_raw_indices(local, bpp_unused=4):
    """Nibble-packed local/real indices, 2/byte, high nibble first -- ESC_RAW's body,
    matching pack_unit_4bpp_raw_indices's own layout."""
    px = list(local)
    if len(px) % 2:
        px.append(0)
    return bytes((px[i] << 4) | px[i + 1] for i in range(0, len(px), 2))


def encode_unit(pixels, tile_px_is_16=True, max_code_len=6, name="?"):
    """Returns (mode_name, packed_bytes). `pixels` are real (pre-crush) palette indices,
    flat row-major, length n."""
    n = len(pixels)
    local, order = crush(pixels)
    k = len(order)
    raw_body = pack_raw_indices(local)
    raw_total_bits = 8 + 4 * k + len(raw_body) * 8  # header + LUT + raw body, for comparison

    if k == 1:
        bw = BitWriter()
        bw.write_bits_msb((HDR_FILL << 6) | 0, 8)  # bpp/colour_count fields unused, zeroed
        bw.write_bits_msb(order[0], 4)
        return "fill", bw.to_bytes()

    bpp = bits_for_k(k)
    seq = concat_bitplanes(local, n, bpp)
    start_bit, runs = rle(seq)

    freqs = {}
    for r in runs:
        freqs[r] = freqs.get(r, 0) + 1
    lengths = huffman_lengths(freqs, max_code_len)

    run_bits = FIXED16_RUN_BITS if tile_px_is_16 else max(1, (bpp * n).bit_length())
    cols = len(freqs)
    _log_field("cols", name, cols, COLS_BITS)
    if cols > (1 << COLS_BITS):
        return None  # logged above; caller falls back to ESC_RAW

    def build(mode):
        bw = BitWriter()
        bw.write_bits_msb((mode << 6) | ((bpp - 1) << 4) | (k - 1), 8)
        for c in order:
            bw.write_bits_msb(c, 4)
        if mode == HDR_VARIABLE:
            bw.write_bits_msb(n, 16)
        bw.write_bit(start_bit)
        bw.write_bits_msb(cols - 1, 6)
        codes = canonical_codes(lengths)
        # Table order fixed (ascending run-length value) so encoder and decoder agree
        # without transmitting an explicit index -- decoder reads `cols` entries, done.
        for run_len in sorted(freqs.keys()):
            code, clen = codes[run_len]
            _log_field("run_length", name, run_len, run_bits)
            if run_len >= (1 << run_bits):
                return None  # logged above; caller falls back to ESC_RAW
            bw.write_bits_msb(run_len, run_bits)
            bw.write_bits_msb(clen - 1, 3)
            bw.write_bits_msb(code, clen)
        for r in runs:
            code, clen = codes[r]
            bw.write_bits_msb(code, clen)
        return bw.to_bytes()

    if lengths is not None:
        mode = HDR_FIXED16 if tile_px_is_16 else HDR_VARIABLE
        packed = build(mode)
        if packed is not None and len(packed) * 8 <= raw_total_bits:
            return ("fixed16" if mode == HDR_FIXED16 else "variable"), packed

    bw = BitWriter()
    bw.write_bits_msb((HDR_RAW << 6) | ((bpp - 1) << 4) | (k - 1), 8)
    for c in order:
        bw.write_bits_msb(c, 4)
    return "raw", bw.to_bytes() + raw_body


def decode_unit(data, n):
    br = BitReader(data)
    hdr = br.read_bits_msb(8)
    mode = hdr >> 6
    bpp = ((hdr >> 4) & 0x3) + 1
    k = (hdr & 0xF) + 1

    if mode == HDR_FILL:
        c = br.read_bits_msb(4)
        return [c] * n

    order = [br.read_bits_msb(4) for _ in range(k)]

    if mode == HDR_RAW:
        out = []
        byte_pos = (br.pos + 7) // 8
        for i in range(n):
            byte = data[byte_pos + i // 2]
            local = (byte >> 4) if i % 2 == 0 else (byte & 0xF)
            out.append(order[local])
        return out

    if mode == HDR_VARIABLE:
        n_stored = br.read_bits_msb(16)
        assert n_stored == n, (n_stored, n)
        run_bits = max(1, (bpp * n).bit_length())
    else:
        run_bits = FIXED16_RUN_BITS

    start_bit = br.read_bit()
    cols = br.read_bits_msb(6) + 1

    entries = []  # (run_length, code, code_len)
    for _ in range(cols):
        run_len = br.read_bits_msb(run_bits)
        clen = br.read_bits_msb(3) + 1
        code = br.read_bits_msb(clen)
        entries.append((run_len, code, clen))

    total = bpp * n
    seq = []
    cur = start_bit
    while len(seq) < total:
        code = 0
        clen = 0
        run_len = None
        while run_len is None:
            code = (code << 1) | br.read_bit()
            clen += 1
            for rl, c, l in entries:
                if l == clen and c == code:
                    run_len = rl
                    break
        seq.extend([cur] * run_len)
        cur ^= 1

    return _seq_to_pixels(seq, n, bpp, order)


def _seq_to_pixels(seq, n, bpp, order):
    """Un-concatenates a decoded bpp*n-bit sequence back into n local indices (one bit
    per pixel per plane, bitplane p occupying seq[p*n:(p+1)*n]) and remaps through
    `order` to real values -- shared tail for every concat-bitplane decoder here."""
    local = [0] * n
    for p in range(bpp):
        plane = seq[p * n:(p + 1) * n]
        for i, b in enumerate(plane):
            local[i] |= b << p
    return [order[v] for v in local]


# ---------------------------------------------------------------------- concat + Elias-gamma
# (today's shipped format, unchanged, but bitplanes concatenated into one run sequence
# instead of RLE'd independently per plane -- isolates the concatenation change from the
# Huffman change above, so the two can be measured separately.)

def _read_elias_gamma(br):
    """Python mirror of pnx_bitplane.c's bp_read_elias_gamma, for round-trip verification
    of the encoder below -- there is no Python decoder for today's shipped format
    anywhere in the pipeline (it only ever encodes), so this is written fresh from the C
    source rather than reused."""
    n_bits = 1
    while br.read_bit():
        n_bits += 1
    mantissa = br.read_bits_msb(n_bits)
    return (1 << n_bits) + mantissa - 1


def encode_unit_concat_eg(pixels):
    """Today's per-unit Elias-gamma run coding, unchanged, applied to ONE concatenated
    bpp*n-bit sequence (one start bit, runs may cross plane boundaries) instead of `bpp`
    independent per-plane sequences. Mirrors encode_bitplane_unit's own header/LUT/raw-
    fallback conventions exactly so the ONLY variable that changed is concatenation."""
    n = len(pixels)
    local, order = crush(pixels)
    k = len(order)
    raw_body = pack_raw_indices(local)

    if k == 1:
        return bytes([0, order[0] << 4])

    bpp = bits_for_k(k)
    seq = concat_bitplanes(local, n, bpp)
    start_bit, runs = rle(seq)

    off_table = bytearray()
    for i in range(0, k, 2):
        hi = order[i]
        lo = order[i + 1] if i + 1 < k else 0
        off_table.append((hi << 4) | lo)

    bw = BitWriter()
    bw.write_bit(start_bit)
    for r in runs:
        pipeline._write_elias_gamma(bw, r)
    coded_body = bytes(off_table) + bw.to_bytes()
    coded_total = 1 + len(coded_body)
    raw_total = 1 + len(off_table) + len(raw_body)

    if coded_total <= raw_total:
        return bytes([(k - 1) & 0xF]) + coded_body
    return bytes([0x80 | ((k - 1) & 0xF)]) + bytes(off_table) + raw_body


def decode_unit_concat_eg(data, n):
    header = data[0]
    if header & 0x80:
        k = (header & 0xF) + 1
        off_bytes = (k + 1) // 2
        order = []
        for i in range(off_bytes):
            order.append(data[1 + i] >> 4)
            order.append(data[1 + i] & 0xF)
        order = order[:k]
        body = data[1 + off_bytes:]
        local = _unpack_4bpp(body, n)
        return [order[v] for v in local]

    k = (header & 0xF) + 1
    if k == 1:
        return [data[1] >> 4] * n

    off_bytes = (k + 1) // 2
    order = []
    for i in range(off_bytes):
        order.append(data[1 + i] >> 4)
        order.append(data[1 + i] & 0xF)
    order = order[:k]

    bpp = bits_for_k(k)
    br = BitReader(data[1 + off_bytes:])
    start_bit = br.read_bit()
    total = bpp * n
    seq = []
    cur = start_bit
    while len(seq) < total:
        run = _read_elias_gamma(br)
        seq.extend([cur] * run)
        cur ^= 1
    seq = seq[:total]
    return _seq_to_pixels(seq, n, bpp, order)


def _unpack_4bpp(data, n):
    out = []
    for i in range(n // 2 + (n % 2)):
        b = data[i]
        out.append(b >> 4)
        if len(out) < n:
            out.append(b & 0x0F)
    return out[:n]


# ---------------------------------------------------------------------- shared-table Huffman
# (one run-length table amortized across every frame of a sprite, or every tile of an
# atlas -- or, for build_shared_table's caller, across an entire project's worth of
# units -- instead of one table per unit. Each unit still gets its own colour LUT (that
# never changes: it maps THIS unit's own local index to a real palette slot, unrelated to
# run-length statistics) and its own bpp/k/start_bit; only the run-length table itself and
# the field width it implies are shared.)

def _unit_runs(pixels):
    """crush + concat + RLE for one unit -- the shared first half of both
    encode_unit_concat_eg and every shared-table path below, factored out once they both
    needed it rather than copied."""
    n = len(pixels)
    local, order = crush(pixels)
    k = len(order)
    if k == 1:
        return n, local, order, k, None, None, None
    bpp = bits_for_k(k)
    seq = concat_bitplanes(local, n, bpp)
    start_bit, runs = rle(seq)
    return n, local, order, k, bpp, start_bit, runs


def build_shared_table(unit_pixel_lists, max_code_len=16):
    """Pools run-length frequencies across every unit in `unit_pixel_lists` and builds
    ONE length-limited Huffman table from the combined distribution. Returns
    (codes, run_bits, per_unit) -- `codes` a {run_length: (code, code_len)} dict shared by
    every caller, `run_bits` the field width needed for the WORST unit's own longest run
    (the table is one shared shape, so its field width has to cover everyone, not just
    the average case), `per_unit` the (n, local, order, k, bpp, start_bit, runs) tuples
    already computed so callers encoding right after building don't redo crush/RLE."""
    per_unit = [_unit_runs(px) for px in unit_pixel_lists]
    freqs = {}
    max_run = 0
    for n, local, order, k, bpp, start_bit, runs in per_unit:
        if runs is None:
            continue
        for r in runs:
            freqs[r] = freqs.get(r, 0) + 1
            max_run = max(max_run, r)
    if not freqs:
        return {}, 1, per_unit
    lengths = huffman_lengths(freqs, max_code_len)
    codes = canonical_codes(lengths)
    run_bits = max(1, max_run.bit_length())
    return codes, run_bits, per_unit


# A shared table (per-asset or global) pools run lengths across many units, so its own
# alphabet -- and the code lengths a length-limited Huffman build needs to cover it --
# both run well past what the per-unit format's 6b cols / 3b code-length fields (sized
# for ONE unit's own small alphabet) can hold. Separate, wider fields for the table
# itself; SHARED_COLS_BITS=12 covers up to 4096 distinct run lengths, SHARED_CLEN_BITS=5
# covers code lengths up to 32 -- both comfortably past anything a real project's pooled
# run-length alphabet reaches, verified empirically per-run via the same FIELD_STATS
# logging encode_unit already uses (see build_shared_table's own overflow check).
SHARED_COLS_BITS = 12
SHARED_CLEN_BITS = 5


def encode_table(codes, run_bits):
    """Serializes a shared run-length table: SHARED_COLS_BITS (cols, NOT cols-1 -- unlike
    the per-unit table's own field, a shared table's `cols` can legitimately be zero, an
    asset whose every unit is a k==1 flat fill with no runs at all to pool, and cols-1
    would underflow to -1 for that case) then, per unique run length (ascending),
    run_bits + SHARED_CLEN_BITS (code_length-1) + code_length bits of code -- same entry
    shape the per-unit table already writes, just with wider cols/code-length fields
    (see SHARED_COLS_BITS's own comment) and written once here instead of once per unit."""
    bw = BitWriter()
    cols = len(codes)
    _log_field("shared_table_cols", "(table)", cols, SHARED_COLS_BITS)
    bw.write_bits_msb(cols, SHARED_COLS_BITS)
    bw.write_bits_msb(run_bits - 1, 5)  # 5b: run_bits ranges 1-32, shared tables can need
                                        # a wider field than any one unit's own 10-bit cap
    for run_len in sorted(codes.keys()):
        code, clen = codes[run_len]
        bw.write_bits_msb(run_len, run_bits)
        bw.write_bits_msb(clen - 1, SHARED_CLEN_BITS)
        bw.write_bits_msb(code, clen)
    return bw.to_bytes()


def decode_table(data):
    """Returns (entries, run_bits, bytes_consumed) -- entries a list of
    (run_length, code, code_len), bytes_consumed rounded up to the byte the unit stream
    that follows actually starts on (tables are byte-aligned so a unit's own bitstream
    starts at a known offset, not a bit offset threaded through every caller)."""
    br = BitReader(data)
    cols = br.read_bits_msb(SHARED_COLS_BITS)
    run_bits = br.read_bits_msb(5) + 1
    entries = []
    for _ in range(cols):
        run_len = br.read_bits_msb(run_bits)
        clen = br.read_bits_msb(SHARED_CLEN_BITS) + 1
        code = br.read_bits_msb(clen)
        entries.append((run_len, code, clen))
    return entries, run_bits, (br.pos + 7) // 8


def encode_unit_with_table(n, local, order, k, bpp, start_bit, runs, codes):
    """One unit's own header + colour LUT + start_bit + coded stream, using an
    ALREADY-BUILT shared `codes` table -- no table of its own. Still falls back to
    ESC_RAW if, for this one unit, the shared table's codes happen to cost more than raw
    would (e.g. a unit whose own runs are rare values the shared table gives long codes
    to) -- the shared table is a bet on the AGGREGATE, not a guarantee for every member."""
    off_table = bytearray()
    for i in range(0, k, 2):
        hi = order[i]
        lo = order[i + 1] if i + 1 < k else 0
        off_table.append((hi << 4) | lo)

    if k == 1:
        return bytes([0]) + bytes([order[0] << 4])

    bw = BitWriter()
    bw.write_bit(start_bit)
    for r in runs:
        code, clen = codes[r]
        bw.write_bits_msb(code, clen)
    coded_body = bytes(off_table) + bw.to_bytes()

    raw_body = pack_raw_indices(local)
    raw_total = 1 + len(off_table) + len(raw_body)
    if 1 + len(coded_body) <= raw_total:
        return bytes([(k - 1) & 0xF]) + coded_body
    return bytes([0x80 | ((k - 1) & 0xF)]) + bytes(off_table) + raw_body


def decode_unit_with_table(data, n, entries, run_bits):
    header = data[0]
    k = (header & 0xF) + 1
    if header & 0x80:
        off_bytes = (k + 1) // 2
        order = []
        for i in range(off_bytes):
            order.append(data[1 + i] >> 4)
            order.append(data[1 + i] & 0xF)
        local = _unpack_4bpp(data[1 + off_bytes:], n)
        return [order[v] for v in local[:n]]
    if k == 1:
        return [data[1] >> 4] * n

    off_bytes = (k + 1) // 2
    order = []
    for i in range(off_bytes):
        order.append(data[1 + i] >> 4)
        order.append(data[1 + i] & 0xF)
    order = order[:k]

    bpp = bits_for_k(k)
    br = BitReader(data[1 + off_bytes:])
    start_bit = br.read_bit()
    total = bpp * n
    seq = []
    cur = start_bit
    while len(seq) < total:
        code = 0
        clen = 0
        run_len = None
        while run_len is None:
            code = (code << 1) | br.read_bit()
            clen += 1
            for rl, c, l in entries:
                if l == clen and c == code:
                    run_len = rl
                    break
        seq.extend([cur] * run_len)
        cur ^= 1
    return _seq_to_pixels(seq[:total], n, bpp, order)


def encode_asset_shared_table(unit_pixel_lists, max_code_len=16):
    """One asset (a sprite's frames, or an atlas's tiles): builds ONE run-length table
    from every unit's own runs, writes it once, then encodes each unit against it.
    Returns (table_bytes, [unit_bytes...])."""
    codes, run_bits, per_unit = build_shared_table(unit_pixel_lists, max_code_len)
    table_bytes = encode_table(codes, run_bits)
    unit_bytes = []
    for n, local, order, k, bpp, start_bit, runs in per_unit:
        if k == 1:
            unit_bytes.append(bytes([0, order[0] << 4]))
        else:
            unit_bytes.append(
                encode_unit_with_table(n, local, order, k, bpp, start_bit, runs, codes))
    return table_bytes, unit_bytes


def decode_asset_shared_table(table_bytes, unit_bytes_list, ns):
    entries, run_bits, _ = decode_table(table_bytes)
    return [decode_unit_with_table(data, n, entries, run_bits)
           for data, n in zip(unit_bytes_list, ns)]


# ---------------------------------------------------------------------- test harness

def main():
    fixtures_path = "tests/fixtures/bitplane/bpeg_fixtures.h"
    try:
        text = open(fixtures_path).read()
    except FileNotFoundError:
        print(f"run from the pebblnyx repo root ({fixtures_path} not found)")
        sys.exit(1)

    import re
    units = []
    for m in re.finditer(r'static const uint8_t (\w+)_px\[\]\s*=\s*\{([^}]*)\};', text):
        name = m.group(1)
        px = [int(x) for x in m.group(2).split(',') if x.strip()]
        units.append((name, px))
    enc_sizes = {}
    for m in re.finditer(r'static const uint8_t (\w+)_enc\[\]\s*=\s*\{([^}]*)\};', text):
        enc_sizes[m.group(1)] = len([x for x in m.group(2).split(',') if x.strip()])

    print(f"{'unit':<20} {'n':>5} {'raw4bpp':>8} {'elias-g':>8} {'concat-eg':>9} {'huffman':>8} {'mode':>10} {'vs elias-g':>10}")
    total_raw = total_eg = total_ceg = total_hf = 0
    fallbacks = []
    for name, px in units:
        n = len(px)
        mode, packed = encode_unit(px, tile_px_is_16=(n <= 256), name=name)
        decoded = decode_unit(packed, n)
        ok = decoded == px
        if not ok:
            print(f"  ROUND-TRIP FAILED (huffman): {name}")
            continue

        ceg_packed = encode_unit_concat_eg(px)
        ceg_decoded = decode_unit_concat_eg(ceg_packed, n)
        if ceg_decoded != px:
            print(f"  ROUND-TRIP FAILED (concat-eg): {name}")
            continue

        raw4bpp = (n + 1) // 2
        eg = enc_sizes.get(name, 0)
        total_raw += raw4bpp
        total_eg += eg
        total_ceg += len(ceg_packed)
        total_hf += len(packed)
        if mode == "raw":
            fallbacks.append(name)
        print(f"{name:<20} {n:>5} {raw4bpp:>8} {eg:>8} {len(ceg_packed):>9} {len(packed):>8} {mode:>10} "
              f"{(1 - len(packed) / eg) * 100 if eg else 0:>9.1f}%")

    print(f"\n{'TOTAL':<20} {'':<5} {total_raw:>8} {total_eg:>8} {total_ceg:>9} {total_hf:>8}"
          f" {'':<10} {(1 - total_hf / total_eg) * 100:>9.1f}%")
    print(f"concat-eg vs today's per-plane elias-gamma: {(1 - total_ceg / total_eg) * 100:+.1f}%")
    if fallbacks:
        print(f"\nESC_RAW fallbacks (a field overflowed, or huffman lost to raw on size): {fallbacks}")

    print("\nfield usage vs the bit budget actually given to it, per unit (real data, "
          "not the worked example) -- run_length's own width varies by mode (fixed 10b "
          "for fixed16, ceil(log2(bpp*n)) for variable), so headroom is reported against "
          "each unit's OWN width, not one field-wide number:")
    for field in ("run_length", "cols"):
        entries = FIELD_STATS[field]
        if not entries:
            continue
        headroom = [(1 << bits) - 1 - v for _, v, bits in entries]
        over = [(nm, v, bits) for nm, v, bits in entries if v > (1 << bits) - 1]
        worst_name, worst_val, worst_bits = min(entries, key=lambda e: (1 << e[2]) - 1 - e[1])
        print(f"  {field:<12} n={len(entries)}  tightest margin: {min(headroom)} "
              f"(unit={worst_name}, value={worst_val} against a {worst_bits}b/{(1 << worst_bits) - 1}-max field)  "
              f"overflows={len(over)}")


if __name__ == "__main__":
    main()
