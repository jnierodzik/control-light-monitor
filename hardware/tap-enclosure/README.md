# Tap node enclosure

Raspberry Pi 5 with the Active Cooler, plus the NOYITO 4-channel optocoupler
(24V → 3.3V, board marked `MT-301R4P-P`). Parametric OpenSCAD: [`case.scad`](case.scad).

```bash
openscad -D 'part="base"' -o base.stl case.scad
openscad -D 'part="lid"'  -o lid.stl  case.scad
```

`part="assembly"` shows both boards as ghosts for checking fit.

Outside: about 104 × 118 × 37.5 mm, plus 12 mm mounting ears on each side.

## Hardware

| Qty | Part | Use |
| --- | --- | --- |
| 4 | M3 heat-set insert (≤4.0mm OD, 5–6mm long) | lid posts |
| 4 | M3 × 8 button head | lid |
| 4 | M2.5 × 6 self-tapping, or machine screw + insert (`pi_hole_d = 3.5`) | Pi |
| 4 | M3 × 6 self-tapping | opto board |
| 1 | PG7 cable gland (3–6.5mm cable) | 24V sense wires in |

## Notes

* **Measure the opto board before printing.** Its size comes from the listing
  (72 × 45 mm, holes 40 × 37 mm). Clones vary. Adjust `opto_hole_dx/dy`.
* The Active Cooler draws air in through the lid slots and blows it out through
  the fins, toward the left wall vents. Do not cover the lid.
* `inner_h = 33` is there so straight Dupont jumpers fit on the GPIO header.
  With right-angle jumpers it can drop to about 26.
* The microSD and power button are on the Pi's left edge, which faces a 13mm
  gap rather than a wall slot. Take the lid off to reach them.
* Keep the gray (machine common) wire on the opto's input side only. The
  layout puts the 24V wiring and the Pi on opposite sides of the box. The point
  is to keep the two grounds apart (see `docs/gpio-tap.md`).
* Print both parts flat, no supports. PETG is better than PLA near a machine
  that gets warm.
