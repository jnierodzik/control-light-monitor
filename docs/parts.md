# Parts

What this build is made of, and why each part is the one chosen. This exists because
the list previously lived only in a chat log: when it came time to build the harness,
nothing in the repo could say what had actually been ordered.

Links are deliberately omitted — listings churn and a dead link is worse than none.
The part numbers below are what to search for.

## The machine's own hardware (not purchased)

| Part | Detail |
| --- | --- |
| System | industrial machine |
| Light stack | LUBAN `LTA-502-2TJ`, 24VDC, 50mm two-tier, red over green, with buzzer |
| Connector | JST-XH, 2.5mm pitch, 4-circuit. Light carries the female housing; the control box carries the male header |

Wiring, measured 2026-10-06 — see [gpio-tap.md](gpio-tap.md#polarity-measured-on-the-bench):

| Pin (left→right) | Wire | Function |
| --- | --- | --- |
| 1 | Gray | Common return (machine 0V) |
| 2 | Green | Green segment, switched +24V |
| 3 | Red | Red segment, switched +24V |
| 4 | Purple | Buzzer, switched +24V |

## Compute

| Part | Notes |
| --- | --- |
| Raspberry Pi 5 | RPi.GPIO does **not** work on Pi 5 (RP1 south bridge). Uses gpiozero + lgpio from apt |
| Active Cooler | Mounts in its own dedicated holes, not the corner mounting holes |
| Official power supply | — |
| microSD card | Raspberry Pi OS Lite, 64-bit |

## The tap

| Part | Qty | Notes |
| --- | --- | --- |
| `MT-301R4P-P` optocoupler board | 1 | 4-channel, **24V in → 3.3V out**. Three channels used, one spare |
| JST-XH 4-pin extension lead, male-to-female | 1 | Becomes the pass-through harness. Gets cut |
| Wago `221-413` lever connectors | 4 | 3-conductor. One per tapped wire |
| Dupont jumper wires | — | Opto board to Pi header |
| Zip ties | — | Strain relief |

### On the optocoupler board

Direction matters and is easy to get backwards: most cheap modules are sold **3.3V →
24V**, for *driving* things. We are *reading*, so we need 24V → 3.3V.

The `-P` suffix means a PNP (sourcing) output: it drives HIGH when the field input is
energised and holds low through an on-board pull-down. Hence `active_low: false` and
`pull: down` in the node config. Getting this backwards produced a 1.158V reading on
every idle channel — a divider sitting on the input threshold, not a logic level. The
story is in [gpio-tap.md](gpio-tap.md#the-optocoupler-board-sources-it-does-not-sink).

### Channel map

The board has no shared common: each channel is an independent pair, `1-/1+` through
`4-/4+`, on the green input terminals. The machine has exactly one common (gray), so the
three `-` terminals are bridged together and gray feeds the chain. That bridge is what
makes one return serve three channels, and it lives entirely on the machine side of the
barrier — it is not a connection to the Pi's ground.

Confirmed as wired, 2026-10-08:

| Input `+` | Lamp leg | Output | GPIO | Segment |
| --- | --- | --- | --- | --- |
| `1+` | Red | `O1` | 17 | `red` |
| `2+` | Green | `O2` | 27 | `green` |
| `3+` | Purple | `O3` | 22 | `buzzer` |
| `4+` | — | `O4` | — | spare |

Gray (cable position 1) goes to the bridged `-` chain.
The blue terminals are 3.3V and ground from the Pi.

This map was verified by behaviour long before it was written down — energising one lamp
at a time and watching which segment reported. Recorded here because "it works" is not
something the next person can check against.

### Harness map, measured at the system

Confirmed 2026-10-09. The Wago labels W1-W4 run in the same order as the connector read
right to left:

| Wago | Signal | Sense wire | Terminal |
| --- | --- | --- | --- |
| W1 | **Purple, buzzer** | none | — |
| W2 | Red lamp | red | `1+` |
| W3 | Green lamp | green | `2+` |
| W4 | **Gray, common** | black | `-` hub |

Purple is left untapped. This machine never drives it -- the clicking at completion is a
flasher relay in the controller, not the buzzer -- so the channel carries no information,
and `buzzer` is named in no state.

**An earlier version of this table had W1 as the common and W4 as the buzzer, and it was
wrong.** It was derived from voltage readings taken with the probe referenced to W1, and
those readings are consistent with *either* assignment: with the buzzer unenergised,
purple sits at essentially gray potential, so measuring from purple looks exactly like
measuring from the common. Every number was correct and the conclusion still did not
follow.

What it cost: wired that way, the tap's return current left the green channel, went out
through purple, and reached gray only by passing through the buzzer. About 10mA -- too
little to trigger the optocoupler, so the buzzer channel read 0.0 the whole time, and
enough to sound the buzzer whenever green was lit. A machine that beeped continuously
through every run, with the instrument insisting nothing was happening.

What actually settled it was not a measurement but a substitution: move the common to the
other candidate and see whether the beeping stops. Two configurations, one audible
difference. A diode-mode continuity test was tried first and was useless -- a 24V lamp
module is a series string with its own driver, so the meter's 2V test reads OL on every
pair regardless of how the thing is wired.

### Sense wire colours

The four wires from the harness Wagos to the optocoupler inputs are cut from 22 AWG
spool stock. The light's own wires are gray / green / red / purple; the spools on hand
are white, green, red, blue, black and yellow. So:

| Signal | Sense wire | Goes to |
| --- | --- | --- |
| Gray, common | **black** | the bridged `-` chain |
| Green lamp | **green** | `2+` |
| Red lamp | **red** | `1+` |
| Purple, buzzer | **blue** | `3+` |

White and yellow are unused, and are the colours to reach for if a fourth channel is
ever tapped.

Two of these match the light's own colours exactly, which is the point: a wire whose
colour states its job does not have to be traced. Black for the common is the one that
matters most — it is the universal convention for a return, and using it for a signal
here would invite exactly the polarity mistake that can damage an optocoupler input.
Blue standing in for purple is the only substitution, and the only thing to remember.

Note that the harness's own conductors are **not** colour-coded this way: the extension
lead is three black wires and one red, where red marks cable position 1. The sense wires
are the colour-coded layer, and they start at the Wagos.

### On the extension lead

Verified 2026-10-08 before cutting:

- Male on one end, female on the other, 4 circuits — so it chains between the box's
  male header and the light's female housing, and only in that one orientation.
- **Straight through, pin 1→1 through 4→4**, confirmed by continuity. Some adapter
  leads are deliberately crossed; this one is not.
- Dry-fitted in line with the system powered: the light behaved identically.
- **Its own wire colours are black and red, and mean nothing here.** The red is a pin-1
  position stripe, not a signal. The harness is tracked by *position*, never by colour —
  the light's gray/green/red/purple do not appear on this cable.

Cutting this lead is what keeps the machine unmodified. It is sacrificial and cheap: if
the harness is ever in doubt, unplug it and the light goes straight back on the box.

### On the Wagos

`221-413` is the 3-conductor size, which is exactly what a parallel tap needs: the wire
in from the box, the wire out to the light, and a sense wire to the optocoupler. Four
wires to tap, four connectors.

Strip length is **11mm**, per Wago's own spec. The lead's conductors are fine stranded
and sit near the bottom of the 221's accepted range, so the lever must close fully.

## Bench and assembly tools

| Part | Notes |
| --- | --- |
| Multimeter | COCZOW `PM8233D+`. Used for polarity, idle levels, and continuity |
| Alligator test leads | Easier than holding probes on a 2.5mm pitch connector |
| Wire stripper | Must handle fine wire. The lead's conductors are small enough that a general-purpose stripper will nick them |

## Enclosure

Generated by [`hardware/enclosure.py`](../hardware/enclosure.py), not purchased. STLs in
`hardware/stl/`. Shelved until the unit is running at the system.
