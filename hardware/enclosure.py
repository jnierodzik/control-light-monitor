"""Printable enclosure for the machine stack light tap: Raspberry Pi 5 + optocoupler board.

Generates two STLs. Both print flat on the bed with no supports.

    python hardware/enclosure.py --out hardware/stl

PETG or ASA rather than PLA: a closed box near machinery gets warm and PLA softens
around 60C.

The board posts are printed plastic on purpose. They are the standoffs *and* the
isolation -- the optocouplers exist so the machine's 0V and the Pi's 0V never meet, and
metal standoffs screwed into a common plate would quietly bypass them. Screws self-tap
straight into the posts, so no metal spacers are involved.

Everything is parametric; edit the constants below and re-run.
"""

from __future__ import annotations

import argparse
from pathlib import Path

import numpy as np
import trimesh

# -- dimensions, all mm ----------------------------------------------------------

WALL = 2.5
FLOOR = 2.5
INNER_X = 126.0
INNER_Y = 126.0
INNER_Z = 40.0  # Pi with Active Cooler is ~20mm; the rest is room for wiring

POST_H = 6.0
POST_OD = 7.0

# Raspberry Pi 5: the standard HAT footprint
PI_W, PI_D = 85.0, 56.0
PI_HOLE_DX, PI_HOLE_DY = 58.0, 49.0
PI_INSET = 3.5
PI_PILOT = 2.1  # M2.5 self-tapping into plastic

# MT-301R4P-P optocoupler board, measured
OPTO_W, OPTO_D = 72.15, 45.45
OPTO_HOLE_DX, OPTO_HOLE_DY = 40.0, 37.5
OPTO_PILOT = 2.5  # M3
# Assumes the hole pattern is centred on the outline. If the posts do not line up on a
# test fit, measure one hole from a board corner and set these two directly.
OPTO_INSET_X = (OPTO_W - OPTO_HOLE_DX) / 2
OPTO_INSET_Y = (OPTO_D - OPTO_HOLE_DY) / 2

MARGIN = 8.0
BOARD_GAP = 8.0
PI_AT = (MARGIN, MARGIN)
OPTO_AT = (MARGIN, MARGIN + PI_D + BOARD_GAP)

CABLE_D = 14.0  # passes a USB-C plug or a JST-XH housing
CABLE_Z = FLOOR + 14.0
CABLE_YS = (30.0, 90.0)

CORNER_OD = 9.0
CORNER_PILOT = 2.5
LID_T = 2.5
LID_LIP = 3.0
LID_CLEAR = 0.3

EAR_LEN = 14.0
EAR_W = 14.0
EAR_T = 4.0
EAR_HOLE = 4.5  # clearance for M4

SECTIONS = 64


# -- primitives ------------------------------------------------------------------


def box(x: float, y: float, z: float, at=(0.0, 0.0, 0.0)) -> trimesh.Trimesh:
    """Axis-aligned box with its minimum corner at ``at``."""
    mesh = trimesh.creation.box(extents=(x, y, z))
    mesh.apply_translation((at[0] + x / 2, at[1] + y / 2, at[2] + z / 2))
    return mesh


def cyl(d: float, h: float, at=(0.0, 0.0, 0.0)) -> trimesh.Trimesh:
    """Z-axis cylinder standing on ``at``."""
    mesh = trimesh.creation.cylinder(radius=d / 2, height=h, sections=SECTIONS)
    mesh.apply_translation((at[0], at[1], at[2] + h / 2))
    return mesh


def cyl_x(d: float, length: float, at=(0.0, 0.0, 0.0)) -> trimesh.Trimesh:
    """Cylinder lying along X, starting at ``at``."""
    mesh = trimesh.creation.cylinder(radius=d / 2, height=length, sections=SECTIONS)
    mesh.apply_transform(trimesh.transformations.rotation_matrix(np.pi / 2, (0, 1, 0)))
    mesh.apply_translation((at[0] + length / 2, at[1], at[2]))
    return mesh


def union(parts):
    return trimesh.boolean.union(parts, engine="manifold")


def difference(parts):
    return trimesh.boolean.difference(parts, engine="manifold")


# -- features --------------------------------------------------------------------


def hole_positions(origin, dx, dy, inset_x, inset_y):
    ox, oy = origin
    return [
        (WALL + ox + inset_x + i * dx, WALL + oy + inset_y + j * dy)
        for i in (0, 1)
        for j in (0, 1)
    ]


def board_posts(origin, dx, dy, inset_x, inset_y, pilot):
    solids, cuts = [], []
    for x, y in hole_positions(origin, dx, dy, inset_x, inset_y):
        solids.append(cyl(POST_OD, POST_H, (x, y, FLOOR)))
        cuts.append(cyl(pilot, POST_H, (x, y, FLOOR + 1.0)))
    return solids, cuts


def corner_xy():
    return [
        (x, y)
        for x in (WALL + CORNER_OD / 2, WALL + INNER_X - CORNER_OD / 2)
        for y in (WALL + CORNER_OD / 2, WALL + INNER_Y - CORNER_OD / 2)
    ]


def ears():
    out = []
    for y in (WALL + 18, WALL + INNER_Y - 18):
        plate = box(EAR_LEN + 2, EAR_W, EAR_T, (-EAR_LEN, y - EAR_W / 2, 0))
        hole = cyl(EAR_HOLE, EAR_T + 2, (-EAR_LEN / 2, y, -1))
        out.append(difference([plate, hole]))
    return out


def tie_slots():
    """Zip-tie slots. Strain relief is what stops vibration working a connector loose."""
    slots = []
    for sx, sy in ((104, 24), (104, 54), (104, 84), (30, 118)):
        slots.append(box(12, 3.5, FLOOR + 2, (WALL + sx - 6, WALL + sy - 1.75, -1)))
    return slots


# -- parts -----------------------------------------------------------------------


def base() -> trimesh.Trimesh:
    shell = difference(
        [
            box(INNER_X + 2 * WALL, INNER_Y + 2 * WALL, INNER_Z + FLOOR),
            box(INNER_X, INNER_Y, INNER_Z + 1, (WALL, WALL, FLOOR)),
        ]
    )

    solids = [shell, *ears()]
    cuts = list(tie_slots())

    for args in (
        (PI_AT, PI_HOLE_DX, PI_HOLE_DY, PI_INSET, PI_INSET, PI_PILOT),
        (OPTO_AT, OPTO_HOLE_DX, OPTO_HOLE_DY, OPTO_INSET_X, OPTO_INSET_Y, OPTO_PILOT),
    ):
        post_solids, post_cuts = board_posts(*args)
        solids += post_solids
        cuts += post_cuts

    for x, y in corner_xy():
        solids.append(cyl(CORNER_OD, INNER_Z, (x, y, FLOOR)))
        cuts.append(cyl(CORNER_PILOT, INNER_Z, (x, y, FLOOR + 2.0)))

    for y in CABLE_YS:
        cuts.append(cyl_x(CABLE_D, WALL + 2, (-1, WALL + y, CABLE_Z)))

    return difference([union(solids), *cuts])


def lid() -> trimesh.Trimesh:
    plate = box(INNER_X + 2 * WALL, INNER_Y + 2 * WALL, LID_T)
    lip = box(
        INNER_X - 2 * LID_CLEAR,
        INNER_Y - 2 * LID_CLEAR,
        LID_LIP,
        (WALL + LID_CLEAR, WALL + LID_CLEAR, -LID_LIP),
    )
    solid = union([plate, lip])

    cuts = []
    for x, y in corner_xy():
        cuts.append(cyl(3.4, LID_T + LID_LIP + 2, (x, y, -LID_LIP - 1)))
        # countersink so the screw head sits flush with the lid
        cone = trimesh.creation.cone(radius=3.2, height=3.2, sections=SECTIONS)
        cone.apply_translation((x, y, LID_T - 3.2 + 1.6))
        cuts.append(cone)
        # clearance so the lip does not foul the corner posts
        cuts.append(cyl(CORNER_OD + 0.6, LID_LIP + 0.1, (x, y, -LID_LIP - 0.05)))

    return difference([solid, *cuts])


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", default="hardware/stl")
    args = parser.parse_args()

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)

    for name, part in (("enclosure-base", base()), ("enclosure-lid", lid())):
        path = out / f"{name}.stl"
        part.export(path)
        size = part.extents
        print(
            f"{path}  {size[0]:.1f} x {size[1]:.1f} x {size[2]:.1f} mm  "
            f"watertight={part.is_watertight}  {path.stat().st_size // 1024} KB"
        )


if __name__ == "__main__":
    main()
