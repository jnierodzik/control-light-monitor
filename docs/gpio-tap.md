# Reading the stack light electrically

A second sensor for the same machine: instead of watching the light with a camera, read
the wires that drive it. The camera stays — running both and cross-checking them is the
plan, not a transition.

**Status:** hardware identified, parts ordered, pipeline refactored and proven
sensor-agnostic. Not yet wired or implemented.

## Why bother, given the camera works

| | Camera | Electrical tap |
| --- | --- | --- |
| Calibration | ROIs, thresholds, exposure, lens position | none |
| Can drift | yes — bumped arm, changed lighting, dust on the lens | no |
| Ambient light | must be engineered around | irrelevant |
| Sample rate | 15 fps | 100 Hz+, trivially |
| Failure mode | silently wrong if the camera moves | wire is energised or it isn't |
| Install | printed arm, enclosure, 500mm ribbon in the splash zone | a harness and a small box |

The camera earns its place as an independent check, and as the thing that works if the
tap is ever unplugged. But the tap is the better primary sensor.

## The hardware

**LUBAN `LTA-502-2TJ`, 24VDC.** 50mm two-tier tower, red over green, with buzzer.
Matches **diagram 2** on the box, "continuous with buzzer".

Four flying leads into a **JST-XH, 2.5mm pitch, 4-circuit** connector (10.68mm across
the housing), which plugs into a header on the control box.

| Pin (left→right) | Wire | Function |
| --- | --- | --- |
| 1 | Gray | Common return |
| 2 | Green | Green segment, switched leg |
| 3 | Red | Red segment, switched leg |
| 4 | Purple | Buzzer, switched leg |

### This unit has no built-in flasher

Diagrams 3–5 on the box show models with a dedicated `(Gray) Flash` mode wire. Ours is
not one of them — gray here is the common return, and there is no separate Red/Black
power pair.

**Therefore: if red flashes, the machine's controller is pulsing the red wire.** The tap
sees those pulses directly, and the existing duty-cycle classifier handles them
unchanged. Nothing about `flash_window_s`, `flash_duty_range` or
`flash_min_transitions` becomes irrelevant — they all still apply, just to a cleaner
signal.

## Tap, don't intercept

```
IN SERIES  (rejected)

   panel ────────[ dongle ]────────  light
        lamp current flows through our electronics
        dongle fails → the operator loses their status light


PARALLEL TAP  (adopted)

   panel ─────────────┬───────────────  light
                      │
                  [ opto ] ──→ Pi GPIO
        original conductor untouched, full current as before
        tap fails → the light does not notice
```

The only reason to sit in series is to *change* what the light does. We only want to
read it, so being in the current path buys nothing and adds a failure mode to a working
machine.

### Harness, so nothing gets cut

A JST-XH male plug and female socket wired 1:1, with sense wires branched off Red, Green
and Purple. Unplug the light, insert the harness, plug the light into it. Reversible in
thirty seconds, no modification to the machine's wiring, and the signals pass through
unchanged.

### Isolation

Optocouplers, not a direct connection. Two reasons: the machine's 0V and the Pi's 0V are
not necessarily the same potential, and the Pi 5's GPIO is 3.3V with no tolerance for
surprises from an inductive industrial environment.

```
  machine side          |  isolated  |        Pi side
                        |            |
  Red wire ───[R]──▶|───┼────────────┼── OUT ──→ GPIO 17
  Gray (common) ────────┼────────────┼── GND ──→ Pi GND
                        |            |   VCC ──→ Pi 3.3V
```

**The isolation is only real if the two grounds stay apart.** Do not bond the machine's
gray to the Pi's ground anywhere. That single wire would undo the whole point.

Board: 4-channel, **24V in → 3.3V out**. Three channels used, one spare. Note the
direction — most cheap modules are sold the other way round, 3.3V → 24V, for driving
things rather than reading them.

## Software: it reuses the engine

The pipeline was already split at the right seam. `Detector.update_values()` takes
per-segment numbers and runs everything from there: lit thresholding, rolling duty
cycle, flash classification, state resolution, debounce, commit. `Detector.update()` is
now a thin camera-specific wrapper that measures ROIs and calls it.

```
camera ──→ measure ROI ──┐
                         ├──→ update_values ──→ modes ──→ state ──→ debounce ──→ MQTT
GPIO ────→ read pin ─────┘
```

A tap sample is just `255.0` when the lamp lead is energised and `0.0` when it is not,
against the same threshold of 90. `tests/test_sensor_agnostic.py` pins this down,
including a test that drives both detectors from the same scripted light and asserts
they commit identical state sequences — which is exactly the cross-check planned for the
real machine.

Everything downstream — MQTT topics, retained state, heartbeats, the rules engine,
SQLite, ntfy — is untouched and shared.

### Deployment shape

Two nodes, one broker. `stacklight` and `machine1`, same `shop/lights/` namespace, both
feeding the same rules engine. That was already the design for a second camera; a second
*sensor* costs nothing extra.

### Pi 5 GPIO gotcha

**`RPi.GPIO` does not work on a Pi 5.** The GPIO sits behind the RP1 south bridge and the
old `/dev/gpiomem` interface is gone. Use `gpiozero` (with the `lgpio` backend) or
`lgpio` directly. Both come from apt:

```bash
sudo apt install -y python3-gpiozero python3-lgpio
```

Same story as `picamera2` — system packages, so the venv still needs
`--system-site-packages`. The import will be lazy and optional, like the Pi camera, so
the package keeps importing on a laptop.

Pin choice: avoid GPIO 0/1 (ID EEPROM), 2/3 (I2C pull-ups) and 14/15 (UART). GPIO 17, 27
and 22 are unencumbered.

## Two things still to decide

### 1. Is the buzzer a "segment"?

The config model already allows arbitrary named channels with arbitrary modes, so the
buzzer could simply be one:

```yaml
segments:
  - {name: red,    pin: 17}
  - {name: green,  pin: 27}
  - {name: buzzer, pin: 22}
states:
  fault: {red: steady, green: "off", buzzer: steady}
```

No new machinery, and it adds a third dimension to the state map — which may well
distinguish some of the three patterns that currently resolve to `unknown`. The only
cost is calling a buzzer a "segment", which is a naming wart.

### 2. The tap cannot tell "machine off" from "machine idle"

This is the real gap, and it is the GPIO equivalent of the camera's `dark_reference`.

With only the four conductors in the light connector, every input reads inactive when:

* the machine is idle with both lamps off, **and**
* the machine is powered down, **and**
* the harness has fallen out.

All three look identical, and the third is a silent failure — the node would cheerfully
report `idle` forever while connected to nothing.

The camera does not have this problem: an unplugged camera stops producing frames, and a
moved one trips the dark reference.

Options:

* **Tap +24V as a fourth channel.** Needs access to the supply rail inside the control
  box, which is more invasive than the light connector. Gives a true presence signal,
  and the spare opto channel is already there for it.
* **Accept it**, and lean on the camera node as the cross-check — if the camera says
  `running` while the tap says `idle`, something is wrong with the tap.
* **Infer it**: if the machine genuinely never sits with both lamps dark while powered,
  then "all inputs inactive" *is* "powered down" and there is nothing to fix. Needs
  observation of the real machine to confirm.

Worth resolving before the tap becomes the primary sensor. Until then the camera covers
it.

## Bench plan

Nothing touches the machine until all of this passes on the bench.

1. Spare light, bench 24V supply, toggle switches on each leg. Confirm the light behaves
   and note the polarity you wired.
2. Opto board between those legs and the Pi. Confirm each GPIO follows its lamp.
3. Run the node against it. Confirm the same MQTT payloads the camera node produces.
4. Script the switches to imitate a flash and confirm the classifier reports `flashing`.
5. Only then build the harness and go near the machine.

The bench rig needs no connectors — the spare light's leads go straight to a terminal
block. The JST-XH harness is for the real install.
