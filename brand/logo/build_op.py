"""Round 2: "OP" monogram iterations, each carrying the wifi signal. Writes concepts/op*-v*.svg."""
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
    """Annular sector from a0 to a1 (clockwise), inner radius r1, outer r2; butt or round ends."""
    large = 1 if (a1 - a0) > 180 else 0
    if round_caps:
        c = (r2 - r1) / 2
        return (f"M{pt(cx, cy, r2, a0)}A{r2} {r2} 0 {large} 1 {pt(cx, cy, r2, a1)}"
                f"A{c} {c} 0 0 1 {pt(cx, cy, r1, a1)}"
                f"A{r1} {r1} 0 {large} 0 {pt(cx, cy, r1, a0)}"
                f"A{c} {c} 0 0 1 {pt(cx, cy, r2, a0)}Z")
    return (f"M{pt(cx, cy, r2, a0)}A{r2} {r2} 0 {large} 1 {pt(cx, cy, r2, a1)}"
            f"L{pt(cx, cy, r1, a1)}A{r1} {r1} 0 {large} 0 {pt(cx, cy, r1, a0)}Z")


def circle(cx, cy, r, sweep=1):
    # sweep=1 is clockwise like every other shape here; sweep=0 cuts a hole under nonzero.
    return f"M{cx - r} {cy}a{r} {r} 0 1 {sweep} {2 * r} 0a{r} {r} 0 1 {sweep} {-2 * r} 0Z"


def annulus(cx, cy, r1, r2):
    return circle(cx, cy, r2) + circle(cx, cy, r1, sweep=0)


def rect(x0, y0, x1, y1):
    return f"M{x0} {y0}H{x1}V{y1}H{x0}Z"


def half_disc(cx, cy, r):
    return f"M{cx} {cy - r}A{r} {r} 0 0 1 {cx} {cy + r}Z"


def signal_p(x, top, height, stem, r_out, r_in, r_dot):
    """P whose bowl is wifi: a half-ring wave and a half-disc dot radiating from the stem."""
    cx, cy = x + stem, top + r_out
    return [rect(x, top, x + stem, top + height), ring(cx, cy, r_in, r_out, 0, 180), half_disc(cx, cy, r_dot)]


def paths_svg(paths, fill="#000"):
    # All shapes wind clockwise (holes counter-clockwise), so one nonzero path unions them seamlessly.
    return f'<path fill="{fill}" d="{"".join(paths)}"/>'


MARKS = {
    # O + Signal P side by side: the O is the coin, the P is the signal.
    "op1": [annulus(82, 128, 46, 70), *signal_p(158, 58, 140, 24, 50, 28, 14)],
    # Lowercase "op", matching the lowercase wordmark; the p's bowl is the signal, its stem drops like a coin.
    "op2": [annulus(79, 103, 38, 60), *signal_p(155, 43, 170, 22, 60, 38, 22)],
    # Signal P inside an O opened at the top right, where the signal escapes the coin.
    "op3": [ring(128, 128, 88, 112, 72, 378), *signal_p(94, 68, 120, 22, 46, 26, 13)],
    # The O is the wifi: coin dot, O ring as first wave, one wave above; plain P beside it.
    "op4": [circle(94, 140, 16), annulus(94, 140, 38, 60), ring(94, 140, 82, 104, -40, 40),
            rect(170, 80, 192, 200), ring(192, 118, 16, 38, 0, 180)],
}

if __name__ == "__main__":
    for key, paths in MARKS.items():
        (OUT / f"{key}-symbol-{V}.svg").write_text(
            f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 256 256">{paths_svg(paths)}</svg>\n')
        # Exploratory lockup: live <text> (letterforms get drawn as paths in the kit).
        (OUT / f"{key}-lockup-{V}.svg").write_text(
            f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 1120 256">{paths_svg(paths)}'
            f'<text x="290" y="168" font-family="Inter, DejaVu Sans, sans-serif" font-size="112" '
            f'font-weight="700" letter-spacing="-3" fill="#000">openpiso'
            f'<tspan font-weight="400" fill="#555"> wifi</tspan></text></svg>\n')
    print("ok", V)
