"""Generates the round-1 OpenPiso WiFi concept SVGs (symbols + exploratory lockups)."""
import math
import sys
from pathlib import Path

OUT = Path(__file__).parent / "concepts"
OUT.mkdir(exist_ok=True)
V = sys.argv[1] if len(sys.argv) > 1 else "v1"


def pt(cx, cy, r, a):
    # a: degrees clockwise from 12 o'clock
    t = math.radians(a)
    return f"{cx + r * math.sin(t):.2f} {cy - r * math.cos(t):.2f}"


def ring(cx, cy, r1, r2, a0, a1, round_caps=False):
    """Annular sector from a0 to a1 (clockwise), inner radius r1, outer r2."""
    large = 1 if (a1 - a0) > 180 else 0
    c = (r2 - r1) / 2
    if round_caps:
        return (f"M{pt(cx, cy, r2, a0)}A{r2} {r2} 0 {large} 1 {pt(cx, cy, r2, a1)}"
                f"A{c} {c} 0 0 1 {pt(cx, cy, r1, a1)}"
                f"A{r1} {r1} 0 {large} 0 {pt(cx, cy, r1, a0)}"
                f"A{c} {c} 0 0 1 {pt(cx, cy, r2, a0)}Z")
    return (f"M{pt(cx, cy, r2, a0)}A{r2} {r2} 0 {large} 1 {pt(cx, cy, r2, a1)}"
            f"L{pt(cx, cy, r1, a1)}A{r1} {r1} 0 {large} 0 {pt(cx, cy, r1, a0)}Z")


def circle(cx, cy, r):
    return f"M{cx - r} {cy}a{r} {r} 0 1 0 {2 * r} 0a{r} {r} 0 1 0 {-2 * r} 0Z"


def svg(paths, w=256, h=256):
    d = "".join(paths)
    return (f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {w} {h}">'
            f'<path fill="#000" fill-rule="evenodd" d="{d}"/></svg>\n')


# A — Signal P: a mast (P stem) radiating signal; dot + one wave form the bowl.
cx, cy = 98, 108
a = [f"M70 20H98V236H70Z", ring(cx, cy, 60, 88, 0, 180),
     f"M{cx} {cy - 36}A36 36 0 0 1 {cx} {cy + 36}Z"]

# B — Coin Drop: the wifi dot is a coin over its slot; signal radiates from the coin.
sx, sy = 128, 160
b = [circle(sx, sy, 24),
     f"M{sx - 48} {sy + 38}H{sx + 48}A12 12 0 0 1 {sx + 48} {sy + 62}H{sx - 48}A12 12 0 0 1 {sx - 48} {sy + 38}Z",
     ring(sx, sy, 48, 74, -48, 48, True), ring(sx, sy, 98, 124, -48, 48, True)]

# C — Slot Coin: a coin whose centre is a coin slot broadcasting both ways, "( | )".
c = [circle(128, 128, 112), "M116 96A12 12 0 0 1 140 96V160A12 12 0 0 1 116 160Z",
     ring(128, 128, 48, 72, 48, 132, True), ring(128, 128, 48, 72, 228, 312, True)]

for key, paths in {"a": a, "b": b, "c": c}.items():
    (OUT / f"{key}-symbol-{V}.svg").write_text(svg(paths))
    # Exploratory lockup: live <text> (not final; letterforms get drawn as paths in the kit).
    sym = "".join(paths)
    (OUT / f"{key}-lockup-{V}.svg").write_text(
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 1120 256">'
        f'<path fill="#000" fill-rule="evenodd" d="{sym}"/>'
        f'<text x="290" y="168" font-family="Inter, DejaVu Sans, sans-serif" font-size="112" '
        f'font-weight="700" letter-spacing="-3" fill="#000">openpiso'
        f'<tspan font-weight="400" fill="#555"> wifi</tspan></text></svg>\n')
print("ok", V)
