"""Global-table Huffman run-length codec -- the validated core, extracted from
tools/bpeg2_prototype.py so both the benchmark prototype and the real pipeline
(tools/pnx_assets.py) import the SAME code instead of two copies drifting apart.
Extracted rather than left in the prototype because pnx_assets.py importing
bpeg2_prototype directly would be circular (bpeg2_prototype imports pnx_assets, to reuse
its real crush/palette logic for apples-to-apples benchmark comparisons).

Format, validation history, and the design rationale for every choice here (concatenated
bitplanes instead of per-plane independent RLE, length-limited package-merge Huffman
instead of plain Huffman, wide SHARED_COLS_BITS/SHARED_CLEN_BITS fields) all live in
tools/bpeg2_prototype.py's own module docstring and comments -- this file is the logic,
that file (and docs/GAME-COMPARISON.md) is the "why", kept in one place rather than
copied here too.
"""


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


def crush(pixels):
    """Frequency-sorted local index remap -- index 0 is the MOST frequent value. Returns
    (local_indices, order) where order[local_i] is the original value."""
    freq = {}
    for p in pixels:
        freq[p] = freq.get(p, 0) + 1
    order = [c for c, _ in sorted(freq.items(), key=lambda kv: (-kv[1], kv[0]))]
    remap = {c: i for i, c in enumerate(order)}
    local = [remap[p] for p in pixels]
    return local, order


def bits_for_k(k):
    """Bitplanes needed for k distinct local indices -- ceil(log2(k)), k>=2 (k==1 is the
    fill/ESC_FILL case, never reaches here)."""
    if k <= 2:
        return 1
    if k <= 4:
        return 2
    if k <= 8:
        return 3
    return 4


def concat_bitplanes(local, n, bpp):
    """The bpp*n-bit concatenated sequence: bitplane 0 (LSB of every pixel's local index)
    across all n pixels, then bitplane 1, etc."""
    seq = []
    for p in range(bpp):
        for v in local:
            seq.append((v >> p) & 1)
    return seq


def rle(seq):
    """(start_bit, [run_lengths...]) for a bit sequence, alternating value per run."""
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


def huffman_lengths(freqs, max_len):
    """Length-limited Huffman code lengths (Larmore-Hirschberg package-merge) for a
    {symbol: freq} table. Returns None only when max_len itself cannot address every
    symbol at all (more than 2**max_len distinct symbols)."""
    symbols = sorted(freqs.items(), key=lambda kv: kv[1])
    n = len(symbols)
    if n == 1:
        return {symbols[0][0]: 1}
    if 2 ** max_len < n:
        return None

    base = [(f, (s,)) for s, f in symbols]
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
    """Canonical Huffman codes from a {symbol: length} table."""
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


def pack_raw_indices(local):
    """Nibble-packed local indices, 2/byte, high nibble first -- ESC_RAW's body."""
    px = list(local)
    if len(px) % 2:
        px.append(0)
    return bytes((px[i] << 4) | px[i + 1] for i in range(0, len(px), 2))


def unit_runs(pixels):
    """crush + concat + RLE for one unit. Returns (n, local, order, k, bpp, start_bit,
    runs) -- bpp/start_bit/runs are None for k==1 (a flat fill, nothing to RLE)."""
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
    (codes, run_bits, per_unit)."""
    per_unit = [unit_runs(px) for px in unit_pixel_lists]
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
# both run well past what a per-unit format's small 6b cols / 3b code-length fields would
# hold. SHARED_COLS_BITS=12 covers up to 4096 distinct run lengths, SHARED_CLEN_BITS=5
# covers code lengths up to 32.
SHARED_COLS_BITS = 12
SHARED_CLEN_BITS = 5


def encode_table(codes, run_bits):
    """Serializes a shared run-length table: SHARED_COLS_BITS (cols, not cols-1 -- a
    shared table can legitimately have zero entries, an asset whose every unit is a
    k==1 flat fill) then, per unique run length (ascending), run_bits + SHARED_CLEN_BITS
    (code_length-1) + code_length bits of code."""
    bw = BitWriter()
    cols = len(codes)
    if cols >= (1 << SHARED_COLS_BITS):
        raise ValueError(f"shared Huffman table: {cols} distinct run lengths exceeds the "
                         f"{(1 << SHARED_COLS_BITS) - 1} the table's own field can hold")
    bw.write_bits_msb(cols, SHARED_COLS_BITS)
    bw.write_bits_msb(run_bits - 1, 5)
    for run_len in sorted(codes.keys()):
        code, clen = codes[run_len]
        if run_len >= (1 << run_bits):
            raise ValueError(f"shared Huffman table: run length {run_len} exceeds its own "
                             f"{run_bits}-bit field")
        if clen > 16 or clen == 0:
            raise ValueError(f"shared Huffman table: code length {clen} out of range (1-16)")
        bw.write_bits_msb(run_len, run_bits)
        bw.write_bits_msb(clen - 1, SHARED_CLEN_BITS)
        bw.write_bits_msb(code, clen)
    return bw.to_bytes()


def encode_unit_with_table(n, local, order, k, bpp, start_bit, runs, codes):
    """One unit's own header + colour LUT + start_bit + coded stream, using an
    ALREADY-BUILT shared `codes` table -- no table of its own. Falls back to a raw
    (uncompressed) escape if, for this one unit, the shared table's codes happen to cost
    more than raw would."""
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
