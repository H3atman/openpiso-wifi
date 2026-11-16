"""Final OpenPiso WiFi logo kit masters: symbol (full + small cut), constructed wordmark, lockups.
Writes into kit/masters/. Geometry: the locked round-6 mark (p + O bowl + coin dot + two waves)."""
import math
from pathlib import Path

from build_op import annulus, circle, pt, rect, ring

OUT = Path(__file__).parent / "kit" / "masters"
OUT.mkdir(parents=True, exist_ok=True)

# Greens shared with the OpenPiso client (apps/client/public): Forest tile + Mint mark.
FOREST = "#123C1C"  # the letter on light backgrounds; the background for reversed use
MINT = "#71DA88"    # the coin and its signal on Forest / dark backgrounds
LEAF = "#2E9E4F"    # the coin and its signal on white (Mint is only 1.7:1 there)

# (letter, signal, "wifi" word) colours per variant.
VARIANTS = {
    "color": (FOREST, LEAF, LEAF),
    "reversed": ("#fff", MINT, MINT),
    "black": ("#000",) * 3,
    "white": ("#fff",) * 3,
}


# ---------- symbol ----------

def symbol(w=24, gap=16, R=64, dot=20, waves=2, overshoot=1.5):
    """Returns (ink paths, signal paths, bbox). Stem top sits `overshoot` below the O's top so the
    round bowl optically matches the flat stem."""
    cx, cy = R, R
    cap = 228
    ink = [rect(0, overshoot, w, cap), annulus(cx, cy, R - w, R)]
    signal = [circle(cx, cy, dot)]
    r = R
    for _ in range(waves):
        signal.append(ring(cx, cy, r + gap, r + gap + w, 35, 145, True))
        r += gap + w
    top = min(0, cy - (r - w / 2) * math.cos(math.radians(35)) - w / 2)
    return ink, signal, (0, top, cx + r, cap)


# ---------- constructed wordmark (x-height 128, baseline y=128, stroke W) ----------

W = 22
H = 128
GLYPHS = {}


def glyph(fn):
    GLYPHS[fn.__name__.strip("_")] = fn
    return fn


@glyph
def o(x):
    return [annulus(x + 64, 64, 64 - W, 64)], 128


@glyph
def p(x):
    return [rect(x, 0, x + W, 176), annulus(x + 64, 64, 64 - W, 64)], 128


@glyph
def e(x):
    # Ring from the lower-right terminal clockwise round to 3 o'clock; the crossbar's underside is flush
    # with that horizontal cut (crossbar sits just above centre, as in most geometric e's).
    return [ring(x + 64, 64, 64 - W, 64, 122, 450), rect(x + 10, 64 - W, x + 64 + 64 - W + 4, 64)], 128


@glyph
def n(x):
    R = 54
    return [rect(x, 0, x + W, H), ring(x + R, R, R - W, R, 270, 450), rect(x + 2 * R - W, R, x + 2 * R, H)], 2 * R


@glyph
def i(x):
    return [rect(x, 0, x + W, H), circle(x + W / 2, -32, 13)], W


@glyph
def s(x):
    # Two stacked rings sharing the middle stroke; terminals cut at 50° off vertical.
    rc = (H - W) / 4
    cx = x + rc + W / 2
    up, lo = W / 2 + rc, W / 2 + 3 * rc
    return [ring(cx, up, rc - W / 2, rc + W / 2, 180, 410), ring(cx, lo, rc - W / 2, rc + W / 2, 0, 230)], 2 * rc + W


@glyph
def w(x):
    t, a = 25, H * math.tan(math.radians(15))  # strokes at exactly 15° off vertical
    quads = []
    for k, (top, bot) in enumerate([(0, a), (2 * a, a), (2 * a, 3 * a), (4 * a, 3 * a)]):
        quads.append(f"M{x + top} 0H{x + top + t}L{x + bot + t} {H}H{x + bot}Z")
    return quads, 4 * a + t


@glyph
def f(x):
    R = 38
    return [rect(x, -10, x + W, H), ring(x + R, -10, R - W, R, 270, 360),
            rect(x - 18, 0, x + 52, W)], 52


# Sidebearings by neighbour shape: round-round tight, straight-straight loose.
SHAPE = {"o": "round", "p": "straight-left", "e": "round", "n": "straight", "i": "straight",
         "s": "round", "w": "diag", "f": "straight"}


def set_word(word, x=0, track=None):
    track = track or {}
    paths, prev = [], None
    for ch in word:
        if prev:
            x += track.get(prev + ch, 20)
        ps, adv = GLYPHS[ch](x)
        paths += ps
        x += adv
        prev = ch
    return paths, x


# Optical spacing pairs (units between glyph boxes); default 20.
TRACK = {"op": 16, "pe": 18, "en": 18, "np": 26, "pi": 28, "is": 22, "so": 14,
         "wi": 14, "if": 24, "fi": 10}


def wordmark():
    a, x1 = set_word("openpiso", 0, TRACK)
    b, x2 = set_word("wifi", x1 + 50, TRACK)
    return a, b, (-18, -48, x2, 176)


# ---------- SVG writing ----------

def svg(layers, box, pad=0, title="OpenPiso WiFi"):
    x0, y0, x1, y1 = box
    vb = f"{x0 - pad:.1f} {y0 - pad:.1f} {x1 - x0 + 2 * pad:.1f} {y1 - y0 + 2 * pad:.1f}"
    body = "".join(f'<path fill="{fill}"{f" transform={chr(34)}{tf}{chr(34)}" if tf else ""} d="{"".join(ps)}"/>'
                   for fill, ps, tf in layers if ps)
    return f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="{vb}"><title>{title}</title>{body}</svg>\n'


def square(box, pad):
    x0, y0, x1, y1 = box
    side = max(x1 - x0, y1 - y0) + 2 * pad
    cx, cy = (x0 + x1) / 2, (y0 + y1) / 2
    return (cx - side / 2, cy - side / 2, cx + side / 2, cy + side / 2)


def write(name, layers, box, pad=0):
    (OUT / name).write_text(svg(layers, box, pad))


def colourways(name, ink_paths, sig_paths, box, pad=0, tf=None, extra=None):
    """extra: (paths, transform) drawn in the 'secondary' colour (wordmark 'wifi')."""
    ex_p, ex_tf = extra or ([], None)
    for v, (c1, c2, c3) in VARIANTS.items():
        write(f"{name}-{v}.svg", [(c1, ink_paths, tf), (c2, sig_paths, tf), (c3, ex_p, ex_tf)], box, pad)


if __name__ == "__main__":
    ink, sig, sbox = symbol()
    colourways("symbol", ink, sig, square(sbox, 20))
    # Small cut (<= 32 px): heavier strokes, one wave, bigger gaps.
    ink_s, sig_s, sbox_s = symbol(w=32, gap=22, R=68, dot=16, waves=1)
    colourways("symbol-small", ink_s, sig_s, square(sbox_s, 16))

    wa, wb, wbox = wordmark()
    colourways("wordmark", wa, [], wbox, 24, extra=(wb, None))

    # Horizontal lockup: symbol cap (228) scaled so the O bowl (128) matches the x-height (128) -> scale 1.
    sx0, sy0, sx1, sy1 = sbox
    k = 1.0
    gap = 84
    tf = f"translate({sx1 * k + gap:.1f} 0)"
    hbox = (0, min(sy0, -48), sx1 * k + gap + wbox[2], max(sy1, 176))
    for v, (c1, c2, c3) in VARIANTS.items():
        write(f"lockup-horizontal-{v}.svg", [(c1, ink, None), (c2, sig, None), (c1, wa, tf), (c3, wb, tf)],
              hbox, 32)

    # Stacked lockup: symbol centred above the wordmark.
    ww = wbox[2] - wbox[0]
    stf = f"translate({(ww - (sx1 - sx0) * 1.4) / 2:.1f} {-(sy1 - sy0) * 1.4 - 40:.1f}) scale(1.4)"
    sb = (wbox[0], -(sy1 - sy0) * 1.4 - 40 + sy0 * 1.4, wbox[2], 176)
    for v, (c1, c2, c3) in VARIANTS.items():
        write(f"lockup-stacked-{v}.svg", [(c1, ink, stf), (c2, sig, stf), (c1, wa, None), (c3, wb, None)], sb, 32)
    print("ok")
