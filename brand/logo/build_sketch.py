"""Round 3: Rafael's sketch. O + a P whose bowl is the outer wifi wave, open at the bottom,
with the dot and inner wave floating in the counter."""
import sys
from pathlib import Path

from build_op import annulus, circle, rect, ring

OUT = Path(__file__).parent / "concepts"
V = sys.argv[1] if len(sys.argv) > 1 else "v1"


def open_p(x, top, cap, w, gap, caps=False, open_top=False):
    """Stem + half-ring bowl that stops short of the stem at the bottom (and the top, with open_top,
    otherwise a top bar joins them); dot and wave inside."""
    r_dot = w * 0.6
    r1 = r_dot + gap
    r2 = r1 + w
    r3 = r2 + gap
    r4 = r3 + w
    cx, cy = x + w + gap + w * 0.5, top + r4
    bar = [] if open_top else [rect(x, top, cx, top + w)]
    return [rect(x, top, x + w, top + cap), *bar,
            ring(cx, cy, r3, r4, 0, 180, caps), ring(cx, cy, r1, r2, 40, 140, caps), circle(cx, cy, r_dot)], cx + r4


def mark(w, gap, caps=False, open_top=False):
    cap = 228
    p, right = open_p(0, 0, cap, w, gap, caps, open_top)
    r = cap / 2
    o = annulus(-gap * 1.3 - r, r, r - w * 1.1, r)  # O slightly heavier: rounds read lighter than stems
    left = -gap * 1.3 - cap
    return [o, *p], (left, right)


def write(name, paths, box, cap=228, pad=24, ybox=None):
    """box: horizontal extent; ybox: vertical extent, defaults to (0, cap)."""
    left, right = box
    top, bottom = ybox or (0, cap)
    side = max(right - left, bottom - top) + 2 * pad
    x0, y0 = (left + right - side) / 2, (top + bottom - side) / 2
    vb = f"{x0:.1f} {y0:.1f} {side:.1f} {side:.1f}"
    (OUT / f"{name}-symbol-{V}.svg").write_text(
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="{vb}"><path fill="#000" d="{"".join(paths)}"/></svg>\n')
    # Exploratory lockup: mark at 176 units tall, live <text> name (drawn as paths in the kit).
    k = 176 / (bottom - top)
    (OUT / f"{name}-lockup-{V}.svg").write_text(
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 1160 256">'
        f'<path fill="#000" transform="translate({40 - left * k:.1f} {40 - top * k:.1f}) scale({k:.4f})" d="{"".join(paths)}"/>'
        f'<text x="{40 + (right - left) * k + 44:.0f}" y="168" font-family="Inter, DejaVu Sans, sans-serif" '
        f'font-size="112" font-weight="700" letter-spacing="-3" fill="#000">openpiso'
        f'<tspan font-weight="400" fill="#555"> wifi</tspan></text></svg>\n')


if __name__ == "__main__":
    write("sk-round", *mark(24, 14, caps=True))
    write("sk-open", *mark(24, 14, caps=True, open_top=True))
    p, right = open_p(0, 0, 228, 26, 16, caps=True, open_top=True)
    write("sk-p-open", p, (0, right))
    print("ok", V)
