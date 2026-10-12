# Pi 5 + NOYITO opto: stacking enclosure

Parts, from the bottom up:
1. **base.stl**: the optocoupler layer. Four M3 standoffs on a 40 × 37 mm pattern for the 72 × 45 × 18 mm board (centred), a terminal-block window in each end wall (the terminals run along the board's short edges), vent slots in the long walls, and heat-set inserts that anchor the whole stack.
2. **pi.stl**: the Raspberry Pi 5 + Active Cooler layer. It has front cutouts (USB-C, 2× micro-HDMI) and end cutouts (Ethernet, USB 3, USB 2). The left wall has a micro-SD slot and a power-button window. There are exhaust slots too. A 4.6 mm wire slot in the floor lines up with a ~6 mm channel beside the GPIO header.
3. **lid.stl**: honeycomb vent across the whole top, counterbored for the bolt heads, and a skirt that locates it. The STL is already flipped for printing.

Outer size 102.8 × 76.8 × 67 mm.

## Hardware (fastener scheme is locked)
| Qty | Item | Where | Fastening |
|---|---|---|---|
| 4 | M3 heat-set insert, M3 × L4 × OD4.2 | base corner collars (4.0 mm hole) | heat-set: the stack gets opened repeatedly |
| 4 | M3 × 40 socket head cap screw | lid → Pi layer → base inserts | machine thread into the inserts |
| 4 | M2.5 × 6 socket head cap screw | Pi 5 → standoffs (2.2 mm hole) | self-tapped: the board is fitted once |
| 4 | M3 × 6 socket head cap screw | opto board → standoffs (2.6 mm hole) | self-tapped: the board is fitted once |
| 4 | 10 mm rubber bump-on feet (optional) | base underside recesses | adhesive |

## Printing (Bambu, PETG/ASA, 0.2 mm, 3–4 walls)
- base and pi: print upright as exported, with no supports. The 43 mm terminal windows are bridges. If it sags, add "support blocker off / support on build plate only" for that window.
- lid: print as exported (top face on the plate) to get a clean honeycomb face.

## Assembly order
1. Press the 4 M3 inserts into the base corner collars (PETG ~220–230 °C), flush with the collar top.
2. Screw the opto module to the base. Each terminal row faces an end window. Either end works; the output jumpers go along the back wall to the floor slot in the Pi layer.
3. Wire the field inputs (24 V) through the window at the input end. Wire the outputs and run jumpers up toward the Pi.
4. Feed the jumpers through the floor slot of the Pi layer. Set the Pi layer onto the base; the collars locate it.
5. Screw the Pi 5 (with the cooler fitted) to its standoffs. Plug the jumpers onto the GPIO header.
6. Fit the lid and run the 4 × M3 × 40 bolts through to the inserts.

## Tune before printing (all at the top of the .scad)
- `op_L / op_W / op_hole_dx / op_hole_dy`: set to the 72 × 45 mm board with a 40 × 37 mm hole pattern.
- `term_w / term_z0 / term_h`: the terminal window size.
- `pwr_btn_y`: power-button centre, measured at 18.15 mm from the USB-C-side board edge (window is `pwr_w` = 6.5 mm wide).
- `H_opto / H_pi`: layer heights. Adding a layer is one more `layer_shell()` module, and the bolt length goes up by that layer's height.

Render a part: `openscad -D 'part="base"' -o base.stl pi5_stack_enclosure.scad` (`base` | `pi` | `lid` | `assembly` | `exploded`)

---

## Note added when this was brought into the repo

**The opto hole pattern differs from what we measured on the board.** The `.scad` sets
`op_hole_dy = 37`, and the MT-301R4P-P in this build measures **37.5** mm between hole
centres on that axis (`op_hole_dx = 40` matches). That figure came from a printed test
plate checked against the real board, so it is the trusted one.

0.5 mm sounds negligible but lands each post 0.25 mm off centre, and an M3 through a
3.2 mm board hole has only about 0.1 mm of play per side. If the board has already been
fitted and the screws went in without force, ignore this. If a reprint is ever needed,
change that one value first.

`op_L`/`op_W` are set to 72 × 45 against a measured 72.15 × 45.45 — a snug fit rather
than a wrong one, and not worth changing on its own.

**The `.scad` is the source of truth.** The STLs beside it are build output and can be
regenerated:

```
openscad -D 'part="base"' -o base.stl pi5_stack_enclosure.scad
```

`part` is one of `base` | `pi` | `lid` | `assembly` | `exploded`.

**Verified on import:** all three STLs are watertight with zero open edges.
