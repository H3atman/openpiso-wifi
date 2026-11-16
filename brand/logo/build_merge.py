"""Round 5: the O merged into the P (no separate O)."""
import math
import sys

from build_op import annulus, circle, rect, ring
from build_sketch import write

V = sys.argv[1] if len(sys.argv) > 1 else "v1"
CAP, W, GAP = 228, 24, 14


def o_source_p():
    """Open P (top and bottom) whose wifi source is a small O ring instead of a dot."""
    o_in, o_out = 10, 28
    r1, r2 = o_out + GAP, o_out + GAP + W
    r3, r4 = r2 + GAP, r2 + GAP + W
    cx, cy = W + GAP + W * 0.5, r4
    return [rect(0, 0, W, CAP), annulus(cx, cy, o_in, o_out),
            ring(cx, cy, r1, r2, 40, 140, True), ring(cx, cy, r3, r4, 0, 180, True)], (0, cx + r4)


def o_bowl_p(waves, dot=0):
    """P whose bowl is a full O sharing its left side with the stem; open waves radiate from the O.
    dot > 0 adds a wifi-source dot of that radius at the O's centre."""
    R = 64
    cx, cy = R, R
    paths = [rect(0, 0, W, CAP), annulus(cx, cy, R - W, R)] + ([circle(cx, cy, dot)] if dot else [])
    r = R
    for _ in range(waves):
        paths.append(ring(cx, cy, r + GAP + 2, r + GAP + 2 + W, 35, 145, True))
        r += GAP + 2 + W
    top = min(0, cy - r * math.cos(math.radians(35)))
    return paths, (0, cx + r), (top, CAP)


if __name__ == "__main__":
    for name, args in {"mg-bowl2": (2,), "mg-bowl2-dot": (2, 14), "mg-bowl2-dotlg": (2, 20)}.items():
        paths, box, ybox = o_bowl_p(*args)
        write(name, paths, box, ybox=ybox)
    print("ok", V)
