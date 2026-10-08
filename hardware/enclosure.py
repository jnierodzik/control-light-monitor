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
# Verified against the real board with the fit-test coupon on 2026-10-06: the pattern
# is centred on the outline and the posts line up.
OPTO_INSET_X = (OPTO_W - OPTO_HOLE_DX) / 2
OPTO_INSET_Y = (OPTO_D - OPTO_HOLE_DY) / 2

# The Pi sits close to the two walls its connectors face, so a plug reaches the socket
# instead of being fished 10mm down a slot. Its corner posts merge into the walls, which
# is fine -- they are stronger for it.
PORT_MARGIN = 4.0
MARGIN = 8.0
BOARD_GAP = 8.0
PI_AT = (PORT_MARGIN, PORT_MARGIN)
OPTO_AT = (PORT_MARGIN, PORT_MARGIN + PI_D + BOARD_GAP)

# Every union overlaps and every cut overshoots by this much. Solids that merely touch
# produce edges shared by four faces, which is exactly what a slicer calls non-manifold.
EPS = 1.0

# Pi port access. Two generous slots rather than individual cutouts: I do not have
# verified millimetre positions for the Pi 5's connectors, and a slot that clears
# everything cannot be wrong. The Pi goes in with USB-C and HDMI toward the front wall
# (y = 0) and Ethernet and USB toward the left wall (x = 0).
PCB_TOP = FLOOR + POST_H + 1.6
SLOT_Z0 = PCB_TOP - 1.5
SLOT_Z1 = PCB_TOP + 18.0  # Ethernet is the tallest connector
FRONT_SLOT_X = (8.0, 85.0)  # along the Pi's 85mm edge
LEFT_SLOT_Y = (8.0, 56.0)  # along the Pi's 56mm edge

# Tap harness entry, in the right-hand wall beside the Wago strip
CABLE_D = 14.0  # passes a JST-XH housing
CABLE_Z = FLOOR + 14.0
CABLE_YS = (30.0, 90.0)

CORNER_OD = 9.0
CORNER_OVERLAP = 1.0  # how far each lid boss bites into the walls
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
        (WALL + ox + inset_x + i * dx, WALL + oy + inset_y + j * dy) for i in (0, 1) for j in (0, 1)
    ]


def board_posts(origin, dx, dy, inset_x, inset_y, pilot):
    solids, cuts = [], []
    for x, y in hole_positions(origin, dx, dy, inset_x, inset_y):
        solids.append(cyl(POST_OD, POST_H + EPS, (x, y, FLOOR - EPS)))
        cuts.append(cyl(pilot, POST_H, (x, y, FLOOR + 1.0)))
    return solids, cuts


def corner_xy():
    """Lid-screw boss centres.

    Nudged outward so each boss overlaps the walls rather than sitting tangent to
    them. A cylinder exactly touching a plane shares a single line with it, which is
    every bit as non-manifold as two coincident faces -- it is where the last eight
    bad edges came from.
    """
    inset = CORNER_OD / 2 - CORNER_OVERLAP
    return [
        (x, y)
        for x in (WALL + inset, WALL + INNER_X - inset)
        for y in (WALL + inset, WALL + INNER_Y - inset)
    ]


def ears():
    out = []
    for y in (WALL + 18, WALL + INNER_Y - 18):
        plate = box(EAR_LEN + WALL + EPS, EAR_W, EAR_T, (-EAR_LEN, y - EAR_W / 2, 0))
        hole = cyl(EAR_HOLE, EAR_T + 2 * EPS, (-EAR_LEN / 2, y, -EPS))
        out.append(difference([plate, hole]))
    return out


def tie_slots():
    """Zip-tie slots. Strain relief is what stops vibration working a connector loose."""
    slots = []
    for sx, sy in ((104, 24), (104, 54), (104, 84), (30, 118)):
        slots.append(box(12, 3.5, FLOOR + 2, (WALL + sx - 6, WALL + sy - 1.75, -1)))
    return slots


# -- parts -----------------------------------------------------------------------


def clean(mesh: trimesh.Trimesh) -> trimesh.Trimesh:
    """Tidy up after the booleans and refuse to emit anything a slicer will reject."""
    mesh.merge_vertices()
    mesh.remove_unreferenced_vertices()
    mesh.update_faces(mesh.nondegenerate_faces())
    mesh.update_faces(mesh.unique_faces())
    trimesh.repair.fix_normals(mesh)
    return mesh


def manifold_report(mesh: trimesh.Trimesh) -> tuple[bool, str]:
    """What a slicer checks: every edge shared by exactly two faces."""
    from collections import Counter

    counts = Counter(map(tuple, np.sort(mesh.edges_sorted, axis=1)))
    bad = sum(1 for c in counts.values() if c != 2)
    degenerate = int((mesh.area_faces < 1e-9).sum())
    ok = bad == 0 and degenerate == 0 and mesh.is_winding_consistent
    return ok, (
        f"non-manifold edges={bad}  degenerate={degenerate}  "
        f"winding={'ok' if mesh.is_winding_consistent else 'INCONSISTENT'}"
    )


def base() -> trimesh.Trimesh:
    # Order matters, and getting it wrong is silent. The cavity has to be cut out of the
    # shell BEFORE the posts are unioned in -- cutting it afterwards removes every post
    # standing inside it, leaving a bare floor that still passes every manifold check.
    shell = difference(
        [
            box(INNER_X + 2 * WALL, INNER_Y + 2 * WALL, INNER_Z + FLOOR),
            box(INNER_X, INNER_Y, INNER_Z + EPS, (WALL, WALL, FLOOR)),
        ]
    )

    solids = [shell, *ears()]
    cuts = list(tie_slots())

    for x, y in corner_xy():
        solids.append(cyl(CORNER_OD, INNER_Z + EPS, (x, y, FLOOR - EPS)))
        cuts.append(cyl(CORNER_PILOT, INNER_Z, (x, y, FLOOR + 2.0)))

    for args in (
        (PI_AT, PI_HOLE_DX, PI_HOLE_DY, PI_INSET, PI_INSET, PI_PILOT),
        (OPTO_AT, OPTO_HOLE_DX, OPTO_HOLE_DY, OPTO_INSET_X, OPTO_INSET_Y, OPTO_PILOT),
    ):
        post_solids, post_cuts = board_posts(*args)
        solids += post_solids
        cuts += post_cuts

    # Pi port access
    cuts.append(
        box(
            FRONT_SLOT_X[1] - FRONT_SLOT_X[0],
            WALL + 2 * EPS,
            SLOT_Z1 - SLOT_Z0,
            (WALL + FRONT_SLOT_X[0], -EPS, SLOT_Z0),
        )
    )
    cuts.append(
        box(
            WALL + 2 * EPS,
            LEFT_SLOT_Y[1] - LEFT_SLOT_Y[0],
            SLOT_Z1 - SLOT_Z0,
            (-EPS, WALL + LEFT_SLOT_Y[0], SLOT_Z0),
        )
    )

    # Tap harness entries, right-hand wall
    for y in CABLE_YS:
        cuts.append(cyl_x(CABLE_D, WALL + 2 * EPS, (WALL + INNER_X - EPS, WALL + y, CABLE_Z)))

    return clean(difference([union(solids), *cuts]))


def all_post_centres() -> list[tuple[float, float]]:
    """Every board post, for the geometry check in main()."""
    return hole_positions(PI_AT, PI_HOLE_DX, PI_HOLE_DY, PI_INSET, PI_INSET) + hole_positions(
        OPTO_AT, OPTO_HOLE_DX, OPTO_HOLE_DY, OPTO_INSET_X, OPTO_INSET_Y
    )


def posts_present(mesh: trimesh.Trimesh, centres) -> tuple[int, int]:
    """Count how many posts actually have material in them.

    A manifold check says nothing about whether the part has the features it is
    supposed to have. A box whose posts were accidentally cut away is perfectly
    watertight and completely useless, which is exactly what shipped once.

    Samples just off-centre: the post centre is hollow for most of its height because
    of the pilot hole.
    """
    probes = [[x + POST_OD / 2 - 1.2, y, FLOOR + POST_H / 2] for x, y in centres]
    inside = mesh.contains(np.array(probes))
    return int(inside.sum()), len(probes)


def lid() -> trimesh.Trimesh:
    plate = box(INNER_X + 2 * WALL, INNER_Y + 2 * WALL, LID_T)
    # The lip runs up into the plate rather than stopping flush against it.
    lip = box(
        INNER_X - 2 * LID_CLEAR,
        INNER_Y - 2 * LID_CLEAR,
        LID_LIP + EPS,
        (WALL + LID_CLEAR, WALL + LID_CLEAR, -LID_LIP),
    )
    solid = union([plate, lip])

    cuts = []
    for x, y in corner_xy():
        # Counterbore rather than a countersink: a cone primitive has a degenerate
        # apex, and 250 slivers in the first version is what that cost.
        cuts.append(cyl(3.4, LID_T + LID_LIP + 2 * EPS, (x, y, -LID_LIP - EPS)))
        cuts.append(cyl(6.5, 1.6 + EPS, (x, y, LID_T - 1.6)))
        # clearance so the lip does not foul the corner posts
        cuts.append(cyl(CORNER_OD + 0.6, LID_LIP + EPS, (x, y, -LID_LIP - EPS / 2)))

    return clean(difference([solid, *cuts]))


def coupon() -> trimesh.Trimesh:
    """A throwaway test plate carrying just the optocoupler board's post pattern.

    Fifteen minutes of printing to check the one dimension I am not certain of --
    whether the 40 x 37.5 hole pattern really is centred on the 72.15 x 45.45 outline.
    Far cheaper than discovering it after a ten-hour box print.
    """
    pad = 8.0
    plate_x = OPTO_W + 2 * pad
    plate_y = OPTO_D + 2 * pad
    solids = [box(plate_x, plate_y, 3.0)]
    cuts = []
    for i in (0, 1):
        for j in (0, 1):
            x = pad + OPTO_INSET_X + i * OPTO_HOLE_DX
            y = pad + OPTO_INSET_Y + j * OPTO_HOLE_DY
            solids.append(cyl(POST_OD, POST_H + EPS, (x, y, 3.0 - EPS)))
            cuts.append(cyl(OPTO_PILOT, POST_H, (x, y, 3.0 + 1.0)))
    # scratch outline so you can see whether the board overhangs
    for edge in ((pad, pad, plate_x - 2 * pad, 0.6), (pad, plate_y - pad, plate_x - 2 * pad, 0.6)):
        cuts.append(box(edge[2], edge[3], 0.6, (edge[0], edge[1], 3.0 - 0.6)))
    return clean(difference([union(solids), *cuts]))


def corner_test() -> trimesh.Trimesh:
    """The box corner that holds the Pi, cut straight out of the real base.

    An intersection rather than a reimplementation, so what you test-fit is exactly the
    geometry the full base has. Prints in a couple of hours instead of ten, and tells
    you whether the port slots actually line up with a real Pi 5 before you commit.
    """
    section = box(WALL + 100, WALL + 72, FLOOR + 30, (-1, -1, -1))
    return clean(trimesh.boolean.intersection([base(), section], engine="manifold"))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", default="hardware/stl")
    args = parser.parse_args()

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)

    failed = False
    parts = (
        ("enclosure-base", base),
        ("enclosure-lid", lid),
        ("port-test-corner", corner_test),
    )
    for name, builder in parts:
        part = builder()
        ok, report = manifold_report(part)
        size = part.extents
        path = out / f"{name}.stl"
        part.export(path)
        print(f"{path}  {size[0]:.1f} x {size[1]:.1f} x {size[2]:.1f} mm")
        print(f"    {report}  -> {'OK' if ok else 'WILL NOT SLICE'}")
        failed |= not ok

        if name in ("enclosure-base", "port-test-corner"):
            centres = all_post_centres()
            if name == "port-test-corner":  # only the Pi's four are in the section
                centres = hole_positions(PI_AT, PI_HOLE_DX, PI_HOLE_DY, PI_INSET, PI_INSET)
            found, total = posts_present(part, centres)
            print(
                f"    board posts present {found}/{total}"
                f"  -> {'OK' if found == total else 'MISSING POSTS'}"
            )
            failed |= found != total
    if failed:
        raise SystemExit("refusing to ship a mesh a slicer will reject")


if __name__ == "__main__":
    main()
