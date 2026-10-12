# Reading the stack light electrically

A second sensor for the same machine: instead of watching the light with a camera, read
the wires that drive it.

> **Hard constraint: the tap node is camera-agnostic and must work with no camera at
> all.** Not a camera plus a tap — a complete, standalone monitor. No camera hardware, no
> camera config section, no `picamera2`, no calibration step. A Pi with an optocoupler
> board and nothing else must be a working node.
>
> Running a camera node alongside it is *optional* and useful for validation, but nothing
> in the tap's design may depend on one existing.

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

### Polarity, measured on the bench

Confirmed 2026-10-06 with a spare LTA-502TJ and a 24V supply: **gray is the negative /
common side, and each colour wire is a switched +24V leg.** Gray to the supply's `-`,
green to `+`, and the green lamp lights steadily.

So for the optocoupler inputs: the **lamp leg goes to `+`, gray goes to `-`** on each
channel.

The lamp is also steady when energised, with no self-flashing, which is the final
confirmation of box diagram 2.

### The optocoupler board sources; it does not sink

The `MT-301R4P-P` drives its output **HIGH** when the field input is energised and holds
it low through an on-board pull-down when idle. The `-P` suffix is PNP output. Config is
`active_low: false`, `pull: down`.

This was configured backwards at first, and the symptom is worth remembering because it
did not look like a polarity error. With the Pi pulling up (~50k) against the board's
pull-down (~27k), every output sat at **1.158 V** — not a logic level at all, but a
divider landing almost exactly on the Pi's input threshold. Readings flipped at random:
one night the pins read dark with a single unexplained 13-second excursion, the next
morning they read permanently lit with nothing connected. It looked like interference.

A voltmeter on the idle output settled it in twenty seconds: three channels reading an
identical 1.158 V is a divider, not noise. With `pull: down` the idle level is a solid
0 V and the ambiguity is gone.

**Verified end to end on 2026-10-08**, real 24V on real lamp leads:

| Lamp energised | Values (red/green/buzzer) | Committed state |
| --- | --- | --- |
| none | 0 / 0 / 0 | `clear` |
| red | **255** / 0 / 0 | `error` |
| green | 0 / **255** / 0 | `done` |
| buzzer | 0 / 0 / **255** | `clear` (buzzer names no state) |

No crosstalk between channels in either direction.

### The buzzer line is a steady signal

Measured on the bench: with the purple lead energised, the buzzer beeps on a cadence
but the optocoupler's input LED stays **solid**. The pulsing is generated inside the
buzzer module; the wire is held continuously on.

So the buzzer tap reads `steady` when sounding, never `flashing`. The concern that a
short-duty beep might fall below `flash_duty_range` and classify as `off` does not
arise. `buzzer: "on"` and `buzzer: steady` would behave identically.

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
        machine side         |  barrier  |        Pi side
                             |           |
  Red leg (+24V) ──▶ IN+ ────┤  ▶|   ▷   ├──── OUT ──→ GPIO 17
  Gray (common)  ──▶ IN- ────┤           ├──── VCC ──→ Pi 3.3V
                             |           ├──── GND ──→ Pi GND
     nothing crosses ────────┘           └──────── nothing crosses
```

Each channel takes a lamp leg on `IN+` and gray on `IN-`. Gray is the *machine's*
common and it stops at the optocoupler's input. The Pi's `GND` is a different net
entirely, and the two never meet.

**The isolation is only real if the two grounds stay apart.** Do not bond the machine's
gray to the Pi's ground anywhere. That single wire would undo the whole point — the tap
would still appear to work, which is what makes the mistake worth naming here rather
than leaving to be inferred from the diagram.

(An earlier version of this diagram drew gray crossing to `Pi GND`, contradicting the
warning directly beneath it. The bench rig was always wired as above; the drawing was
wrong, not the hardware.)

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

`machine1` is a complete node on its own: optocoupler board, Pi, broker, rules engine,
notifications. No camera anywhere in the chain.

Running `stacklight` alongside it is optional. Both publish into the same
`shop/lights/` namespace and feed the same rules engine, so a second node costs nothing
structurally — but the tap must never *require* it.

When both are running, cross-check on **per-segment modes for red and green**, not on
resolved state names. The two nodes may legitimately have different segment sets (the
camera cannot see the buzzer), so comparing what each sensor *measured* is both more
meaningful and more robust than comparing what their configs *concluded*.

### What standalone requires in the code

* `camera:` becomes optional in config, required only when `sensor: camera`. It is
  currently mandatory, so this is a real change.
* `Node` must run without a `FrameSource`. Snapshot publishing becomes camera-only and
  skips cleanly rather than erroring.
* No `picamera2` and no snapshot encoding on the import path for a GPIO node.
* The GPIO path's tests must not reach for `SyntheticSource`. (They already don't —
  `drive_pins` in `tests/test_sensor_agnostic.py` feeds the detector directly. Only the
  explicit camera-versus-tap comparison test uses a camera, which is correct.)

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

### 2. Disconnection detection — settled

**Decision: the connector is the only interface. Nothing is tapped inside the control
box.** Closed, not revisited.

My earlier framing of this as a critical gap was wrong, and the reason is the harness
topology. The pass-through harness is *physically* in the light's circuit even though
the sense electronics are not. So:

| Failure | Visible? |
| --- | --- |
| Harness unplugged at the box | **The stack light stops working.** The operator notices, because they rely on it |
| Harness unplugged at the light | Same — light dead, immediately obvious |
| Node process or Pi dies | Heartbeat watchdog in the rules engine |
| Machine unpowered | Looks like `clear`. But the operator knows whether they switched it on |
| **Sense wire breaks, opto channel dies, wrong pin** | **Silent.** Nothing to see |

Only the last row is genuinely silent, and it is narrow: the harness is intact and
passing signals to a working light, but our branch off it has failed. A single channel
failing is a partial loss (errors stop arriving, completions keep working), which is
harder to spot than a total one.

**Mitigation, in software, no extra wiring:** a staleness rule in the rules engine —
"no light activity since X" — flagged during working hours. It cannot distinguish "tap
is dead" from "machine was not used today", but combined with the operator knowing
whether they ran a batch, it closes the gap in practice.

That belongs with the rules engine. Nothing in the node needs to change for it.

Note for later: because the lamp current does *not* flow through our electronics, the
controller still drives the lamp lead whether or not a lamp is attached. The tap would
therefore report an error correctly even with the light itself removed.

## Verified in production

2026-10-11, on the machine itself, through the pass-through harness:

```
21:53:28  clear   -> running    green steady      no notification, by design
21:54:17  running -> done       green flashing    "Machine run complete" delivered
```

Delivered on the first attempt. The buzzer stayed silent throughout, confirming the
corrected harness map. Run duration falls out of the transition log for free: 49s.

What each piece of the chain proved in that one cycle:

- The parallel tap reads the controller's green output without disturbing it.
- The optocouplers isolate correctly under real machine load.
- `steady` and `flashing` are distinguished reliably on a live signal -- measured 0.50
  duty, 4 transitions in the 2s window, no detector tuning required.
- Debounce, state resolution, MQTT publish, rule evaluation and push delivery all work
  end to end on hardware, in about a second.

Not yet exercised on the real system: the `error` path. Red has only ever been tested
with a bench supply. It resolves from the same two-segment table and shares the whole
downstream chain, so the risk is low, but it is untested and should not be described
otherwise until a genuine fault occurs.

## Undelivered notifications

The annunciator behaviour that shapes the detector also shapes delivery, and it took
until the services were running on the Pi to notice.

A state indicator would have let delivery failures sort themselves out: the light keeps
asserting what it is, so if one push is lost the next one carries the same information.
An annunciator does not. The lamp latches, the node publishes `error` exactly once — on
the transition — and then nothing further happens until an operator intervenes. There is
no second transition, so nothing about the machine will ever trigger a second attempt.

So a single failed POST used to mean the alert was gone. The cooldown was correctly not
claimed, which sounds like the escape hatch but isn't: the next notification requires a
state change, and a latched lamp produces none. The only trace was a `delivered=0` row in
SQLite, next to a phone that never buzzed and a system that needed a human. Not academic
on this unit — its Wi-Fi has been intermittent throughout, and power saving is still on.

The fix is a queue in the engine, retried on the 5-second tick it already runs:

- **Lateness is not a reason to drop an alert.** A latched red lamp is just as true ten
  minutes on. The retry says how late it is (`delayed 4m12s; attempt 9`) so a stale
  alert can't read as fresh, but it still goes out. `max_age_s` is only a backstop.
- **Being overtaken by reality is a reason to drop it.** A queued `error` whose node has
  since committed a different state is discarded, as is a queued watchdog alert for a
  node whose heartbeats resumed. That is a statement about the machine rather than the
  clock, which makes it the right test.
- **Duplicates collapse.** A light flapping through an outage would otherwise queue one
  alert per transition and deliver the pile the instant the network returned. One entry
  per node, kind and state, keeping the newest content and the *original* first-attempt
  time — so repeating cannot keep an alert alive past `max_age_s`.
- **The cooldown starts when the alert arrives**, not when it was first attempted. The
  operator's clock starts at the buzz.
- **The queue is bounded** (`max_pending`), oldest discarded first, because during a long
  outage the newest alerts are the ones describing the shop as it is now.

Only the first failure and the final outcome are written to history, with the attempt
count. A half-hour outage should not write a row every thirty seconds for an alert
nobody has seen yet.

What this does **not** fix: the Pi hosts the broker, the node and the rules engine, so a
total failure of the Pi takes the retry queue with it — the queue is in memory, and
nothing survives to resend. That is the same gap the watchdog has, and it still wants an
off-box check. The retry covers the case that actually keeps happening, which is the
network between a working Pi and ntfy.

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
