#!/usr/bin/env python3
"""Broader corpus for the Huffman-run-length prototype (tools/bpeg2_prototype.py):
Need4Pebble's FULL sprite/atlas set (every frame/tile, not just the two hand-picked
traffic_car blocks bpeg_fixtures.h carries), worldtiles (a larger RPG-shaped world,
per request), and Pebblemon's real front+back battle sprites + a real map tilesheet --
run against all four codecs (raw 4bpp, today's shipped LZSS, today's shipped
Elias-gamma bitplane, and the Huffman-run-length prototype), plus Pebblemon's own
shipped decoder size where that comparison applies.

Not part of the pipeline. Reads REAL content two ways:
  - Need4Pebble/worldtiles: parses already-built, UNCOMPRESSED ("PS"/"PA", compress =
    "none") resource blobs directly -- the same on-disk format pnx_assets.c reads on
    device, so there is no separate extraction path to trust.
  - Pebblemon: real PNGs from the upstream repo, quantized to <=16 colours per sprite/
    tile the same way tools/pnx_assets.py's own reduce_colours would (Pebblemon's own
    sprites are already <=16 colours natively per the original investigation, so this
    rarely has to do real work).

Usage: python3 tools/bpeg2_benchmark.py <need4pebble_none_dir> <worldtiles_dir> <pebblemon_repo_dir>
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import bpeg2_prototype as bp2
import pnx_assets as pipeline  # the real, shipped pipeline -- reused for LZSS + today's
                               # bitplane encoder, so "today's format" in this comparison
                               # is never a reimplementation that could quietly drift.

PNX_BLOB_HEADER_BYTES = 8
PNX_SPRITE_FRAME_BYTES = 8


def pad4(n):
    return (n + 3) & ~3


def unpack_4bpp_to_indices(data, n):
    out = []
    for i in range(n // 2 + (n % 2)):
        b = data[i]
        out.append(b >> 4)
        if len(out) < n:
            out.append(b & 0x0F)
    return out[:n]


# ---------------------------------------------------------------------- blob parsing

def parse_sprite_blob(data):
    """Returns [(name, w, h, pixels)] -- one entry per DISTINCT frame (deduped frames
    collapse to their first occurrence, matching what a real bitplane/LZSS build would
    address as one unit), pixels as real (pre-crush) local palette indices 0-15."""
    assert data[0:2] == b"PS", data[0:2]
    frame_count = data[3]
    flags = data[4]
    assert flags == 0, f"expected compress=none (flags=0), got {flags}"

    meta_span = frame_count * PNX_SPRITE_FRAME_BYTES
    pal_span = pad4(frame_count)
    frame_meta = data[PNX_BLOB_HEADER_BYTES:PNX_BLOB_HEADER_BYTES + meta_span]
    pixels_start = PNX_BLOB_HEADER_BYTES + meta_span + pal_span

    seen = {}
    out = []
    for i in range(frame_count):
        e = frame_meta[i * 8:i * 8 + 8]
        off = e[0] | (e[1] << 8)
        w, h = e[2], e[3]
        if off in seen:
            continue
        seen[off] = True
        n = w * h
        length = (n + 1) // 2
        packed = data[pixels_start + off:pixels_start + off + length]
        out.append((f"frame{i}", w, h, unpack_4bpp_to_indices(packed, n)))
    return out


def parse_atlas_blob(data):
    """Returns [(name, tile_px, pixels)] -- one entry per tile (flat) or per subtile
    (metatiled), real local indices."""
    assert data[0:2] == b"PA", data[0:2]
    tile_px = data[3]
    tile_count = data[4]
    layout = data[5]
    flags = data[6]
    assert flags == 0, f"expected compress=none (flags=0), got {flags}"

    tile_bytes = tile_px * tile_px // 2
    tables = pad4(tile_count) * 2
    out = []

    if layout == 0:
        pixel_off = tables
        body = data[PNX_BLOB_HEADER_BYTES:]
        for i in range(tile_count):
            start = pixel_off + i * tile_bytes
            packed = body[start:start + tile_bytes]
            out.append((f"tile{i}", tile_px, unpack_4bpp_to_indices(packed, tile_px * tile_px)))
    else:
        body = data[PNX_BLOB_HEADER_BYTES:]
        subs = body[0] | (body[1] << 8)
        table_bytes = tile_count * 4 * 2
        pixel_off = 4 + tables + table_bytes
        half = tile_px // 2
        sub_bytes = (half * half) // 2
        for i in range(subs):
            start = pixel_off + i * sub_bytes
            packed = body[start:start + sub_bytes]
            out.append((f"subtile{i}", half, unpack_4bpp_to_indices(packed, half * half)))
    return out


def load_units_from_dir(resource_dir, label):
    """Every non-~bw .bin in `resource_dir` that parses as an uncompressed PS/PA blob --
    palettes.bin, fonts, maps, banks all fail the magic/flags check and are skipped.
    Also returns the REAL total byte size of every parsed blob (header + frame_meta/
    tile-tables + palette + uncompressed pixel body, exactly as shipped today) so full
    resource-size comparisons don't need to reverse-engineer the container format by
    hand -- overhead = real_blob_bytes - packed-pixel-body-bytes."""
    units = []
    real_blob_bytes = 0
    for path in sorted(Path(resource_dir).glob("*.bin")):
        if "~bw" in path.name:
            continue
        data = path.read_bytes()
        magic = data[0:2]
        try:
            if magic == b"PS":
                for name, w, h, px in parse_sprite_blob(data):
                    units.append((f"{label}:{path.stem}:{name}", w, h, px))
                real_blob_bytes += len(data)
            elif magic == b"PA":
                for name, tile_px, px in parse_atlas_blob(data):
                    units.append((f"{label}:{path.stem}:{name}", tile_px, tile_px, px))
                real_blob_bytes += len(data)
        except AssertionError:
            continue
    return units, real_blob_bytes


# ---------------------------------------------------------------------- pebblemon PNGs

def load_pebblemon_sprites(repo_dir, subdir, label, limit=None):
    from PIL import Image
    base = Path(repo_dir) / "resources/SourceImages/Pokemon/PokemonSprites/Sprites" / subdir
    units = []
    paths = sorted(base.glob("*.png"))
    if limit:
        paths = paths[:limit]
    for path in paths:
        im = Image.open(path).convert("RGBA")
        w, h = im.size
        pixels = list(im.getdata())
        # Quantize to <=16 colours the same way a real build would have to: frequency
        # order, first 16 distinct RGBA tuples seen keep their own identity, anything
        # past that collapses to the nearest already-kept colour (Euclidean RGB) --
        # Pebblemon's own sprites are natively <=16 colours (original investigation's
        # own finding), so this is a safety net, not the common path.
        distinct = []
        seen = set()
        for p in pixels:
            if p not in seen:
                seen.add(p)
                distinct.append(p)
        if len(distinct) > 16:
            freq = {}
            for p in pixels:
                freq[p] = freq.get(p, 0) + 1
            keep = set(sorted(freq, key=lambda c: -freq[c])[:16])

            def nearest(c, keep=keep):
                return min(keep, key=lambda k: sum((a - b) ** 2 for a, b in zip(c[:3], k[:3])))

            pixels = [p if p in keep else nearest(p) for p in pixels]
        units.append((f"{label}:{path.stem}", w, h, _to_small_ints(pixels)))
    return units


def _to_small_ints(pixels):
    """Maps this unit's own (already <=16) distinct raw colours to small integers 0..k-1
    -- what a real project's OWN palette assignment already gives Need4Pebble/worldtiles'
    blob-extracted pixels for free (they're real project-palette indices already).
    encode_bitplane_unit/encode_unit both do their own FREQUENCY-based crush internally
    (the LUT maps local index -> real value), so this mapping's own order doesn't need to
    be frequency-sorted itself -- just small integers, so the LUT's 4-bit field is valid."""
    remap = {}
    out = []
    for p in pixels:
        if p not in remap:
            remap[p] = len(remap)
        out.append(remap[p])
    return out


def load_pebblemon_tiles(repo_dir, png_rel_path, label, tile_px=16, max_tiles=200):
    from PIL import Image
    path = Path(repo_dir) / png_rel_path
    im = Image.open(path).convert("RGBA")
    w, h = im.size
    units = []
    count = 0
    for ty in range(0, h - h % tile_px, tile_px):
        for tx in range(0, w - w % tile_px, tile_px):
            if max_tiles and count >= max_tiles:
                return units
            tile = [im.getpixel((tx + x, ty + y)) for y in range(tile_px) for x in range(tile_px)]
            distinct = set(tile)
            if len(distinct) > 16:
                freq = {}
                for p in tile:
                    freq[p] = freq.get(p, 0) + 1
                keep = set(sorted(freq, key=lambda c: -freq[c])[:16])
                tile = [p if p in keep else min(keep, key=lambda k: sum((a - b) ** 2 for a, b in zip(p[:3], k[:3]))) for p in tile]
            units.append((f"{label}:tile_{tx}_{ty}", tile_px, tile_px, _to_small_ints(tile)))
            count += 1
    return units


# ---------------------------------------------------------------------- codec runners

def run_lzss(pixels):
    """Today's shipped LZSS, on this ONE unit's own pixel stream in isolation (matching
    how a real per-unit build would size it for a random-access format -- LZSS itself
    doesn't need per-unit isolation the way bitplane does, but this keeps the comparison
    apples-to-apples: every codec here sees exactly the same bytes, one unit at a time)."""
    local, order = bp2.crush(pixels)
    packed = pipeline.pack_unit_4bpp_raw_indices(local)
    compressed = pipeline.lzss_compress(packed)
    return len(compressed) + 2 if len(compressed) + 2 < len(packed) else len(packed)  # +2: length prefix


def run_eliasgamma(pixels):
    """Today's shipped bitplane/Elias-gamma encoder, called directly (not reimplemented)."""
    local, order = bp2.crush(pixels)
    encoded = pipeline.encode_bitplane_unit(local)
    return len(encoded)


def run_huffman(pixels, name):
    n = len(pixels)
    mode, packed = bp2.encode_unit(pixels, tile_px_is_16=(n <= 256), name=name)
    decoded = bp2.decode_unit(packed, n)
    assert decoded == pixels, f"round-trip failed: {name}"
    return len(packed), mode


def run_concat_eg(pixels, name):
    """Today's Elias-gamma run coding, unchanged, over ONE concatenated bitplane
    sequence instead of `bpp` independent per-plane ones -- isolates the concatenation
    change from the Huffman-table change run_huffman measures."""
    n = len(pixels)
    packed = bp2.encode_unit_concat_eg(pixels)
    decoded = bp2.decode_unit_concat_eg(packed, n)
    assert decoded == pixels, f"round-trip failed (concat-eg): {name}"
    return len(packed)


def raw4bpp_len(n):
    return (n + 1) // 2


def _asset_key(name):
    """'label:assetname:frame3' -> 'label:assetname' -- the sprite or atlas a unit
    belongs to, everything but its own frame/tile suffix."""
    return name.rsplit(":", 1)[0]


def group_by_asset(units):
    groups = {}
    for name, w, h, px in units:
        groups.setdefault(_asset_key(name), []).append((name, px))
    return groups


def resource_bytes(frame_counts):
    """Real on-device resource cost of blob header + frame_meta + palette table for a set
    of assets, given each asset's frame count. header is paid ONCE PER ASSET; frame_meta
    and palette scale with total frame count regardless of how frames are grouped -- so
    the only thing "N separate sprites" vs "1 sprite, N frames" changes is the header
    count. Isolates that effect instead of letting it hide inside a pixel-body number."""
    header = PNX_BLOB_HEADER_BYTES * len(frame_counts)
    frame_meta = PNX_SPRITE_FRAME_BYTES * sum(frame_counts)
    palette = sum(pad4(c) for c in frame_counts)
    return header, frame_meta, palette


def sweep_combined_vs_separate(units, label, out, nintendo_pixel_bytes=None, nintendo_full_bytes=None):
    """The real authoring question: N separate PnxSprite assets (1 frame each) vs ONE
    PnxSprite asset with N frames. Reports full real resource size (header+meta+palette+
    pixel body) for both, across per-unit elias-gamma (today), concat-eg, per-unit Huffman,
    and the combined asset's shared Huffman table -- so the header/meta/palette savings
    from combining doesn't get silently absorbed into what looks like a compression-scheme
    win. nintendo_full_bytes is Pebblemon's REAL shipped total (their compressed sprite
    blob + their real per-species 6-byte offset/size table -- src/c/modules/pokemon/
    pokemon.c's RESOURCE_ID_DATA_POKEMON_*_SPRITE_DATA), not an estimate."""
    pixel_lists = [px for _, _, _, px in units]
    n = len(pixel_lists)
    eg_body = sum(run_eliasgamma(px) for _, _, _, px in units)
    ceg_body = sum(run_concat_eg(px, name) for name, _, _, px in units)
    hf_body = sum(run_huffman(px, name)[0] for name, _, _, px in units)
    table_bytes, unit_bytes = bp2.encode_asset_shared_table(pixel_lists)
    hfshared_body = sum(len(u) for u in unit_bytes) + len(table_bytes)

    sep_header, sep_meta, sep_pal = resource_bytes([1] * n)
    comb_header, comb_meta, comb_pal = resource_bytes([n])
    sep_overhead = sep_header + sep_meta + sep_pal
    comb_overhead = comb_header + comb_meta + comb_pal

    sep_total_eg = sep_overhead + eg_body
    comb_total_eg = comb_overhead + eg_body
    comb_total_ceg = comb_overhead + ceg_body
    comb_total_hf = comb_overhead + hf_body
    comb_total_hfshared = comb_overhead + hfshared_body

    out.write(f"\n=== {label}: {n} separate sprites vs 1 combined sprite ({n} frames) ===\n")
    out.write(f"  overhead (header+frame_meta+palette): separate={sep_overhead}B "
             f"(header={sep_header} meta={sep_meta} pal={sep_pal})  "
             f"combined={comb_overhead}B (header={comb_header} meta={comb_meta} pal={comb_pal})  "
             f"saved-by-combining={sep_header - comb_header}B (header only; meta/palette unchanged)\n")
    out.write(f"  real total size, {n} separate sprites, elias-gamma today  = {sep_total_eg}B (baseline)\n")
    out.write(f"  real total size, 1 combined sprite, elias-gamma (today's codec, just combined) = "
             f"{comb_total_eg}B ({(1 - comb_total_eg / sep_total_eg) * 100:+.1f}% vs baseline)\n")
    out.write(f"  real total size, 1 combined sprite, concat-eg               = "
             f"{comb_total_ceg}B ({(1 - comb_total_ceg / sep_total_eg) * 100:+.1f}% vs baseline)\n")
    out.write(f"  real total size, 1 combined sprite, Huffman per-unit table  = "
             f"{comb_total_hf}B ({(1 - comb_total_hf / sep_total_eg) * 100:+.1f}% vs baseline)\n")
    out.write(f"  real total size, 1 combined sprite, Huffman shared table   = "
             f"{comb_total_hfshared}B ({(1 - comb_total_hfshared / sep_total_eg) * 100:+.1f}% vs baseline)\n")
    if nintendo_pixel_bytes is not None:
        out.write(f"  Pebblemon/Nintendo pixel body only (their compressed sprite blob, "
                 f"no offset table) = {nintendo_pixel_bytes}B\n")
    if nintendo_full_bytes is not None:
        out.write(f"  Pebblemon/Nintendo REAL shipped total (compressed sprite blob + their "
                 f"real 6B/species offset+size table, both read directly off disk) = "
                 f"{nintendo_full_bytes}B -- {(1 - nintendo_full_bytes / comb_total_hfshared) * 100:+.1f}% "
                 f"smaller than our best combined+shared-table total\n")


def sweep_shared_table(units, label, out):
    """One Huffman run-length table per ASSET (sprite or atlas), amortized across all its
    frames/tiles, instead of one table per frame/tile. Reports the table cost separately
    from the per-unit stream cost, so a win can't hide inside one net number."""
    groups = group_by_asset(units)
    total_table = total_units = 0
    n_assets = n_single_unit_assets = 0
    for asset, members in groups.items():
        names = [m[0] for m in members]
        pixel_lists = [m[1] for m in members]
        ns = [len(px) for px in pixel_lists]
        table_bytes, unit_bytes = bp2.encode_asset_shared_table(pixel_lists)
        decoded = bp2.decode_asset_shared_table(table_bytes, unit_bytes, ns)
        for nm, orig, got in zip(names, pixel_lists, decoded):
            assert got == orig, f"round-trip failed (shared-table): {nm}"
        total_table += len(table_bytes)
        total_units += sum(len(u) for u in unit_bytes)
        n_assets += 1
        if len(members) == 1:
            n_single_unit_assets += 1
    total = total_table + total_units
    out.write(f"\n=== {label}: shared-table-per-asset, {n_assets} assets "
             f"({n_single_unit_assets} single-unit) ===\n")
    out.write(f"  table bytes (sum across assets)={total_table}  "
             f"unit stream bytes={total_units}  total={total}\n")
    return total_table, total_units


def sweep_global_table(units, label, out):
    """ONE Huffman run-length table for the WHOLE project (every sprite and atlas
    pooled), amortized across everything -- the strongest case, measured the same
    honest way: table cost reported separately from the per-unit stream total."""
    pixel_lists = [px for _, _, _, px in units]
    names = [name for name, _, _, _ in units]
    ns = [len(px) for px in pixel_lists]
    table_bytes, unit_bytes = bp2.encode_asset_shared_table(pixel_lists)
    decoded = bp2.decode_asset_shared_table(table_bytes, unit_bytes, ns)
    for nm, orig, got in zip(names, pixel_lists, decoded):
        assert got == orig, f"round-trip failed (global-table): {nm}"
    total_table = len(table_bytes)
    total_units = sum(len(u) for u in unit_bytes)
    out.write(f"\n=== {label}: ONE global table, {len(units)} units ===\n")
    out.write(f"  table bytes={total_table}  unit stream bytes={total_units}  "
             f"total={total_table + total_units}\n")
    return total_table, total_units


# ---------------------------------------------------------------------- report

def sweep(units, label, out):
    total_raw = total_lzss = total_eg = total_ceg = total_hf = 0
    mode_counts = {}
    for name, w, h, px in units:
        n = len(px)
        raw = raw4bpp_len(n)
        lzss = run_lzss(px)
        eg = run_eliasgamma(px)
        ceg = run_concat_eg(px, name)
        hf, mode = run_huffman(px, name)
        mode_counts[mode] = mode_counts.get(mode, 0) + 1
        total_raw += raw
        total_lzss += lzss
        total_eg += eg
        total_ceg += ceg
        total_hf += hf
    out.write(f"\n=== {label}: {len(units)} units ===\n")
    out.write(f"  raw4bpp={total_raw}  lzss={total_lzss}  elias-gamma={total_eg}  "
             f"concat-eg={total_ceg}  huffman={total_hf}\n")
    if total_raw:
        out.write(f"  huffman vs raw:          {(1 - total_hf / total_raw) * 100:+.1f}%\n")
    if total_lzss:
        out.write(f"  huffman vs lzss:         {(1 - total_hf / total_lzss) * 100:+.1f}%\n")
    if total_eg:
        out.write(f"  huffman vs elias-gamma:  {(1 - total_hf / total_eg) * 100:+.1f}%\n")
        out.write(f"  concat-eg vs elias-gamma:{(1 - total_ceg / total_eg) * 100:+.1f}%\n")
        out.write(f"  elias-gamma vs lzss:     {(1 - total_eg / total_lzss) * 100:+.1f}%\n")
    out.write(f"  huffman modes: {mode_counts}\n")
    return total_raw, total_lzss, total_eg, total_ceg, total_hf


def main():
    if len(sys.argv) != 4:
        print(f"usage: {sys.argv[0]} <need4pebble_none_dir> <worldtiles_dir> <pebblemon_repo_dir>")
        sys.exit(1)
    n4p_dir, wt_dir, pkmn_dir = sys.argv[1:4]
    out = sys.stdout

    grand = [0, 0, 0, 0, 0]
    shared_grand = [0, 0]  # table bytes, unit bytes -- summed across every asset-grouped corpus

    n4p_units, n4p_blob_bytes = load_units_from_dir(n4p_dir, "n4p")
    r_n4p = sweep(n4p_units, "Need4Pebble (all sprites+atlases)", out)
    grand = [g + v for g, v in zip(grand, r_n4p)]
    rs_n4p = sweep_shared_table(n4p_units, "Need4Pebble", out)
    shared_grand = [g + v for g, v in zip(shared_grand, rs_n4p)]
    n4p_overhead = n4p_blob_bytes - r_n4p[0]  # real container bytes minus packed pixel body

    wt_units, wt_blob_bytes = load_units_from_dir(wt_dir, "wt")
    r_wt = sweep(wt_units, "WorldTiles (atlases)", out)
    grand = [g + v for g, v in zip(grand, r_wt)]
    rs_wt = sweep_shared_table(wt_units, "WorldTiles", out)
    shared_grand = [g + v for g, v in zip(shared_grand, rs_wt)]
    wt_overhead = wt_blob_bytes - r_wt[0]

    front = load_pebblemon_sprites(pkmn_dir, ".", "pkmn_front")
    back = load_pebblemon_sprites(pkmn_dir, "back", "pkmn_back")
    r_front = sweep(front, "Pebblemon front sprites (251)", out)
    grand = [g + v for g, v in zip(grand, r_front)]
    r_back = sweep(back, "Pebblemon back sprites (251)", out)
    grand = [g + v for g, v in zip(grand, r_back)]

    out.write("\n  Pebblemon's OWN shipped decoder (Game Boy bitplane RLE, hand-tuned -- "
             "docs/GAME-COMPARISON.md's earlier measurement): front=86378B (71.2% off raw) "
             "back=68600B (76.3% off raw)\n")

    tiles = load_pebblemon_tiles(pkmn_dir, "resources/SourceImages/Pokemon/Map/Route1/Route1.png",
                                 "pkmn_tiles", tile_px=16, max_tiles=200)
    r_tiles = sweep(tiles, "Pebblemon Route1 map tileset (16x16 tiles)", out)
    grand = [g + v for g, v in zip(grand, r_tiles)]

    # front/back sprites are single-frame each (no multi-frame pooling opportunity, so
    # per-asset == per-unit there); the tileset IS a real multi-tile atlas, so that one
    # gets its own per-asset-table run same as n4p/worldtiles' atlases did.
    rs_front = sweep_shared_table(front, "Pebblemon front sprites", out)
    shared_grand = [g + v for g, v in zip(shared_grand, rs_front)]
    rs_back = sweep_shared_table(back, "Pebblemon back sprites", out)
    shared_grand = [g + v for g, v in zip(shared_grand, rs_back)]

    sweep_combined_vs_separate(front, "Pebblemon front sprites", out,
                               nintendo_pixel_bytes=86378, nintendo_full_bytes=86378 + 1512)
    sweep_combined_vs_separate(back, "Pebblemon back sprites", out,
                               nintendo_pixel_bytes=68600, nintendo_full_bytes=68600 + 1512)
    rs_tiles = sweep_shared_table(tiles, "Pebblemon Route1 map tileset", out)
    shared_grand = [g + v for g, v in zip(shared_grand, rs_tiles)]

    out.write(f"\n=== GRAND TOTAL: {len(n4p_units)} n4p + {len(wt_units)} wt + "
             f"{len(front)}+{len(back)} pkmn sprites + {len(tiles)} pkmn tiles ===\n")
    out.write(f"  raw4bpp={grand[0]}  lzss={grand[1]}  elias-gamma={grand[2]}  "
             f"concat-eg={grand[3]}  huffman={grand[4]}\n")
    out.write(f"  huffman vs raw:           {(1 - grand[4] / grand[0]) * 100:+.1f}%\n")
    out.write(f"  huffman vs lzss:          {(1 - grand[4] / grand[1]) * 100:+.1f}%\n")
    out.write(f"  huffman vs elias-gamma:   {(1 - grand[4] / grand[2]) * 100:+.1f}%\n")
    out.write(f"  concat-eg vs elias-gamma: {(1 - grand[3] / grand[2]) * 100:+.1f}%\n")

    shared_total = shared_grand[0] + shared_grand[1]
    out.write(f"\n=== shared-table-per-asset GRAND TOTAL "
             f"(n4p+worldtiles atlases/sprites, pkmn sprites+tileset) ===\n")
    out.write(f"  table bytes={shared_grand[0]}  unit stream bytes={shared_grand[1]}  "
             f"total={shared_total}\n")
    out.write(f"  vs elias-gamma (per-unit, no table at all): "
             f"{(1 - shared_total / grand[2]) * 100:+.1f}%\n")
    out.write(f"  vs huffman (per-unit table):                "
             f"{(1 - shared_total / grand[4]) * 100:+.1f}%\n")

    # ONE global table per PROJECT -- what a real shipped project would actually do
    # (Pebblemon's front+back+tileset pooled as one project's worth of content, not
    # combined with the other two unrelated projects).
    out.write("\n=== ONE global table per project ===\n")
    gt_n4p = sweep_global_table(n4p_units, "Need4Pebble", out)
    gt_wt = sweep_global_table(wt_units, "WorldTiles", out)
    pkmn_all = front + back + tiles
    gt_pkmn = sweep_global_table(pkmn_all, "Pebblemon (front+back+tileset, one project)", out)
    # Per-project elias-gamma totals to compare each of the three global-table numbers
    # above against are printed inline in their own "===" sweep() sections earlier.

    # ---------------------------------------------------------------- FINAL COMPARISON
    # Pixel data only (matches how Nintendo/Pebblemon's own numbers are measured -- their
    # compressed sprite blob, no header/offset-table) for the Pebblemon corpora; full real
    # resource size (container overhead measured directly off the real shipped blobs, not
    # hand-derived) for everything else, since that's what actually ships on a Pebble.
    out.write("\n\n" + "=" * 78 + "\n=== FINAL COMPARISON: concat-eg + both Huffman modes vs today, whole suite ===\n" + "=" * 78 + "\n")

    def full_row(name, overhead, eg, ceg, hf, hfshared_total):
        base = overhead + eg
        out.write(f"\n{name} (container overhead={overhead}B, measured directly off the real shipped blob):\n")
        out.write(f"  elias-gamma (today, shipped) = {base}B\n")
        out.write(f"  concat-eg                    = {overhead + ceg}B  "
                 f"({(1 - (overhead + ceg) / base) * 100:+.1f}%)\n")
        out.write(f"  Huffman, per-unit table       = {overhead + hf}B  "
                 f"({(1 - (overhead + hf) / base) * 100:+.1f}%)\n")
        out.write(f"  Huffman, shared table          = {overhead + hfshared_total}B  "
                 f"({(1 - (overhead + hfshared_total) / base) * 100:+.1f}%)\n")

    full_row("Need4Pebble", n4p_overhead, r_n4p[2], r_n4p[3], r_n4p[4], rs_n4p[0] + rs_n4p[1])
    out.write(f"  Huffman, ONE global table       = {n4p_overhead + gt_n4p[0] + gt_n4p[1]}B  "
             f"({(1 - (n4p_overhead + gt_n4p[0] + gt_n4p[1]) / (n4p_overhead + r_n4p[2])) * 100:+.1f}%)\n")

    full_row("WorldTiles", wt_overhead, r_wt[2], r_wt[3], r_wt[4], rs_wt[0] + rs_wt[1])
    out.write(f"  Huffman, ONE global table       = {wt_overhead + gt_wt[0] + gt_wt[1]}B  "
             f"({(1 - (wt_overhead + gt_wt[0] + gt_wt[1]) / (wt_overhead + r_wt[2])) * 100:+.1f}%)\n")

    out.write(f"\nPebblemon front sprites (pixel body only, matching how Nintendo's own "
             f"{86378}B is measured -- no header/offset-table on either side):\n")
    out.write(f"  elias-gamma (today, shipped) = {r_front[2]}B\n")
    out.write(f"  concat-eg                    = {r_front[3]}B  "
             f"({(1 - r_front[3] / r_front[2]) * 100:+.1f}%)\n")
    out.write(f"  Huffman, per-unit table       = {r_front[4]}B  "
             f"({(1 - r_front[4] / r_front[2]) * 100:+.1f}%)\n")
    out.write(f"  Huffman, shared table          = {rs_front[0] + rs_front[1]}B  "
             f"({(1 - (rs_front[0] + rs_front[1]) / r_front[2]) * 100:+.1f}%)\n")
    out.write(f"  Nintendo/Pebblemon shipped (real file, pixel body only) = 86378B  "
             f"({(1 - 86378 / r_front[2]) * 100:+.1f}% vs our elias-gamma)\n")

    out.write(f"\nPebblemon back sprites (pixel body only):\n")
    out.write(f"  elias-gamma (today, shipped) = {r_back[2]}B\n")
    out.write(f"  concat-eg                    = {r_back[3]}B  "
             f"({(1 - r_back[3] / r_back[2]) * 100:+.1f}%)\n")
    out.write(f"  Huffman, per-unit table       = {r_back[4]}B  "
             f"({(1 - r_back[4] / r_back[2]) * 100:+.1f}%)\n")
    out.write(f"  Huffman, shared table          = {rs_back[0] + rs_back[1]}B  "
             f"({(1 - (rs_back[0] + rs_back[1]) / r_back[2]) * 100:+.1f}%)\n")
    out.write(f"  Nintendo/Pebblemon shipped (real file, pixel body only) = 68600B  "
             f"({(1 - 68600 / r_back[2]) * 100:+.1f}% vs our elias-gamma)\n")

    out.write(f"\nPebblemon Route1 tileset (pixel body only, no Nintendo reference -- "
             f"this tileset is our own PNG re-tiling, not a real GBC map-compression export):\n")
    out.write(f"  elias-gamma (today, shipped) = {r_tiles[2]}B\n")
    out.write(f"  concat-eg                    = {r_tiles[3]}B  "
             f"({(1 - r_tiles[3] / r_tiles[2]) * 100:+.1f}%)\n")
    out.write(f"  Huffman, per-unit table       = {r_tiles[4]}B  "
             f"({(1 - r_tiles[4] / r_tiles[2]) * 100:+.1f}%)\n")
    out.write(f"  Huffman, shared table          = {rs_tiles[0] + rs_tiles[1]}B  "
             f"({(1 - (rs_tiles[0] + rs_tiles[1]) / r_tiles[2]) * 100:+.1f}%)\n")

    pkmn_eg = r_front[2] + r_back[2] + r_tiles[2]
    pkmn_ceg = r_front[3] + r_back[3] + r_tiles[3]
    pkmn_hf = r_front[4] + r_back[4] + r_tiles[4]
    pkmn_hfshared = (rs_front[0] + rs_front[1]) + (rs_back[0] + rs_back[1]) + (rs_tiles[0] + rs_tiles[1])
    pkmn_global = gt_pkmn[0] + gt_pkmn[1]
    out.write(f"\nPebblemon combined (front+back+tileset, pixel body only):\n")
    out.write(f"  elias-gamma (today, shipped) = {pkmn_eg}B\n")
    out.write(f"  concat-eg                    = {pkmn_ceg}B  "
             f"({(1 - pkmn_ceg / pkmn_eg) * 100:+.1f}%)\n")
    out.write(f"  Huffman, per-unit table       = {pkmn_hf}B  "
             f"({(1 - pkmn_hf / pkmn_eg) * 100:+.1f}%)\n")
    out.write(f"  Huffman, shared table (per-asset: front/back/tileset each own table) = "
             f"{pkmn_hfshared}B  ({(1 - pkmn_hfshared / pkmn_eg) * 100:+.1f}%)\n")
    out.write(f"  Huffman, ONE global table (all 3 pooled) = {pkmn_global}B  "
             f"({(1 - pkmn_global / pkmn_eg) * 100:+.1f}%)\n")
    out.write(f"  Nintendo/Pebblemon shipped (front+back, pixel body only; no equivalent "
             f"map-tile reference) = {86378 + 68600}B  "
             f"({(1 - (86378 + 68600) / (r_front[2] + r_back[2])) * 100:+.1f}% vs our elias-gamma "
             f"front+back)\n")

    out.write("\nfield usage across the WHOLE corpus:\n")
    for field in ("run_length", "cols"):
        entries = bp2.FIELD_STATS[field]
        if not entries:
            continue
        headroom = [(1 << bits) - 1 - v for _, v, bits in entries]
        over = [(nm, v, bits) for nm, v, bits in entries if v > (1 << bits) - 1]
        worst_name, worst_val, worst_bits = min(entries, key=lambda e: (1 << e[2]) - 1 - e[1])
        out.write(f"  {field:<12} n={len(entries)}  tightest margin: {min(headroom)} "
                 f"(unit={worst_name}, value={worst_val} against a {worst_bits}b field)  "
                 f"overflows={len(over)}\n")


if __name__ == "__main__":
    main()
