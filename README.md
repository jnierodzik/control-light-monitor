# stacklight-monitor

Watch a machine's stack light and push a phone notification when it needs you.

Currently monitoring an **industrial machine**: a two-segment tower light,
red over green, with a buzzer.

There are two ways to read a light here, and they share everything downstream of the
reading. **The live installation uses the electrical tap.** The camera came first and
still works; see [the camera path](#the-camera-path).

## How it reads the light

### The electrical tap

The light is driven by four wires between the control box and the lamp: a common return
and one switched +24V leg per element. Reading those legs directly is both simpler and
more reliable than looking at the light, so that is what the live node does.

1. **A pass-through harness.** A cut extension lead rejoins each conductor through a
   lever connector, with a third wire branching off to the sensing side. Nothing on the
   machine is cut or modified, and pulling the harness restores the original wiring in
   about thirty seconds.
2. **A parallel tap, never in series.** The lamp current does not flow through anything
   we built. If the tap fails, the operator's status light does not notice.
3. **Optocouplers for isolation.** The machine's 0V and the Pi's 0V are not necessarily
   the same potential, and a Pi 5's GPIO has no tolerance for surprises from an
   industrial environment. Light crosses the barrier; electricity does not.
4. **Sampled at 100 Hz.** Each leg is a clean binary signal, so "is this lamp lit" needs
   no interpretation at all.

Full design note, including the measured wiring and what went wrong on the way:
[`docs/gpio-tap.md`](docs/gpio-tap.md).

### The camera path

The original approach, still supported and tested. It deliberately does **not** classify
colour in a normally exposed photograph — that falls apart the moment the shop lights
change, or when amber and red get confused. Instead:

1. **Lock the camera.** Auto-exposure, auto-white-balance and autofocus are all
   disabled, with a fixed exposure time, analogue gain and lens position.
2. **Underexpose heavily.** The room goes nearly black; only an energised LED registers
   as bright pixels.
3. **Identify segments by position, not hue.** The segments sit at fixed, known
   rectangles in the frame. "Red" just means *the top rectangle*.
4. **Threshold.** Per segment, per frame: mean brightness inside the rectangle versus a
   threshold. Colour is at most a secondary sanity check.

### What they share

Everything after the reading. Both produce a per-segment brightness number, and from
there the same code decides lit or unlit, steady or flashing, debounces the result,
resolves it to a named state and publishes it. A node declares which sensor it is in
config, and a config that mixes the two is rejected rather than quietly ignored.

## Architecture

```
[node: pi + tap   ] --MQTT--> [mosquitto] --> [rules engine] --> ntfy
[node 2: pi + cam ] --MQTT-->      ^               |
                                heartbeats         +--> SQLite (state history)
```

Nodes are dumb and self-contained. They publish semantic state, not pixels. A separate
rules process decides what is worth a notification. Adding a second machine means
deploying another node with its own config file and changing nothing centrally -- and it
does not have to read its light the same way the first one does.

Topics, namespaced per node id:

| Topic | Retain | QoS | Payload |
| --- | --- | --- | --- |
| `shop/lights/<node_id>/state` | yes | 1 | JSON; published on committed transitions and refreshed every `state_refresh_s` |
| `shop/lights/<node_id>/health` | no | 0 | Heartbeat with uptime, sample rate, per-segment raw value |
| `shop/lights/<node_id>/snapshot` | yes | 0 | **Camera nodes only.** Annotated debug JPEG; on every transition, plus rate-limited routine ones |
| `shop/lights/<node_id>/availability` | yes | 1 | `online` / `offline`, also registered as the MQTT last will |

`availability` is not in the original design and is the one topic I added. The rules
engine's 60s heartbeat timeout is the backstop for a dead node, but a last will flips
this topic to `offline` the moment the broker notices the socket is gone — seconds
instead of a minute. Both paths matter: the will does not fire if the broker itself
dies, so the heartbeat timeout still earns its place.

## Status

Running on a machine since 2026-10-11, reading the light electrically.

Verified on the machine, through the harness:

```
21:53:28  clear   -> running    green steady      no notification, by design
21:54:17  running -> done       green flashing    "Machine run complete" delivered
```

- [x] Config, detection engine, MQTT publisher, heartbeats, retained state
- [x] Camera source (`PiCameraSource`) and the synthetic/file sources behind it
- [x] **Electrical tap** -- harness, optocouplers, GPIO sampler, measured and installed
- [x] Rules engine -- ntfy, cooldowns, escalation, watchdog, retry, SQLite history
- [x] systemd units and `make deploy`
- [ ] Calibration web UI -- only useful for a camera node, shelved
- [ ] Off-box watchdog -- see [Failure behaviour](#failure-behaviour)

Not yet exercised on the real machine: the `error` path. Red has only been energised
from a bench supply. It resolves from the same table and shares the whole downstream
chain, so the risk is low, but it is untested and is not described here as verified.

## Development, with no hardware at all

Neither a Pi, a camera nor a light is required to work on this, and that is a hard
requirement rather than a convenience -- it is what makes "does it spam me when the
light flaps" a test rather than an afternoon of watching a phone.

For the tap, `FakePinReader` stands in for the GPIO, driven by a virtual clock. For the
camera, `SyntheticSource` draws frames
programmatically — a black background with coloured rectangles that can be switched
off, steady or flashing on a schedule, with optional sensor noise, ambient light bleed
and LED glow. Brightness responds to exposure and gain the way a real sensor does, so
even the exposure controls can be exercised on a laptop.

```bash
make setup                      # uv sync --extra dev, on Python 3.11
make test
make lint
```

Watch the detection engine run a scripted light sequence, start to finish, in a few
seconds of wall time:

```bash
make dev
```

Run the whole node pipeline — capture, detect, publish — without touching a broker:

```bash
make dry-run
```

Inspect a config without any hardware:

```bash
uv run stacklight validate config/dev-synthetic.yaml
```

See exactly how every possible segment pattern resolves to a state — worth running
after any edit to the `states:` block:

```bash
uv run stacklight states config/machine1.yaml
```

Watch the engine against a live source, with raw brightness beside each resolved mode.
This is the tuning tool:

```bash
uv run stacklight detect config/machine1.yaml --interval 1
```

Render synthetic frames to PNG to eyeball what the detector will be fed:

```bash
uv run stacklight synth config/dev-synthetic.yaml --mode red=flashing --frames 20
```

## Configuration

One YAML file per node; see [`config/machine1.yaml`](config/machine1.yaml) for the annotated
production config and [`config/dev-synthetic.yaml`](config/dev-synthetic.yaml) for the
laptop equivalent.

Validation is strict: unknown keys are errors, not warnings, and ROIs are bounds
checked against the configured resolution. A typo in a key name should not cost you an
afternoon up a ladder.

### What the machine's light actually means

Worth stating plainly, because it is not what you would assume, and because we got it
wrong first:

| Pattern | State | Meaning |
| --- | --- | --- |
| both dark | `clear` | powered off, or idle between runs |
| green **steady** | `running` | a cycle is in progress |
| green **flashing** | `done` | the cycle finished and is waiting to be dismissed |
| red lit | `error` | a fault, latched until the operator acts |

The distinction that matters is **steady versus flashing on one wire.** Green solid
means running; green blinking means finished. The light carries no internal flasher --
this unit is wired for continuous operation only -- so the blinking is the controller
pulsing the green leg, and the audible click at completion is that relay.

**This was originally documented as a pure annunciator**: dark when off, idle and
running alike, lighting only to ask for attention. That is true of red and wrong about
green, and the error was not harmless. Both green modes matched `green: "on"` and
resolved to `done`, so the config fired a completion alert the moment a cycle *started*,
and then produced no transition at all when the cycle genuinely finished -- a false
alert at the start and silence at the end. It was only caught by watching the real
machine. There is now a regression test that fails if the two are collapsed again.

A consequence for the history log: because `running` and `done` are distinct states,
**run duration falls out of the transition log for free** -- the interval between them.
The dwell on a latched `error` means something different again: it is how long the light
sat waiting for the operator, which is response time rather than machine time.

### The `on` mode

Red means error whether it is solid or blinking. Rather than depend on knowing which,
states match `on`, which accepts `steady` or `flashing`:

```yaml
states:
  error: {red: "on"}                    # error wins if both were ever lit at once
  done:  {red: "off", green: "on"}
  clear: {red: "off", green: "off"}
```

The flash mode is still measured and published on every payload, so the blink behaviour
is observable — it just isn't depended on. If flashing later turns out to mean something
distinct from solid, splitting `error` in two is a one-line config change.

Precedence is structural rather than ordered: `error` constrains only red while `done`
requires red off, so there is no tie for the ambiguity checker to reject.

> `"on"` and `"off"` must both be quoted. YAML 1.1 reads them bare as booleans. Config
> load rejects the boolean with a message saying exactly this.

### Segments and states

Segment names, ROIs and thresholds are entirely config-driven. Nothing in the code
knows that this particular machine has a red segment and a green one.

States are defined as constraints on segments:

```yaml
states:
  fault:     {red: steady,   green: "off"}
  running:   {red: "off",    green: steady}
  attention: {red: flashing}
  idle:      {red: "off",    green: "off"}
```

A segment a state does not mention is a **wildcard**. `attention` matches whenever red
is flashing, whatever green is doing. When several states match, the most specific one
wins (most segments constrained). Config load fails if two states are equally specific
for some pattern, so ties never get silently broken by file order.

A pattern that matches no configured state resolves to `unknown`. The system never
guesses — `unknown` is a legitimate, publishable answer. `unknown` is reserved and
cannot be defined as a state name.

> Note: `off` must be quoted in YAML. Unquoted, YAML 1.1 parses it as the boolean
> `false`.

### The dark reference

*Camera path only.*

`dark_reference` names a region that must **always** read dark. If it starts reading
bright, the camera has been bumped or the lighting situation changed, and every other
reading in the frame is suspect. When it trips, the node publishes
`confidence: "degraded"` and a state of `unknown` rather than a confidently wrong
answer.

### The brightness metric

*Camera path only -- a tapped segment is already binary.*

`detection.metric` controls how an ROI's pixels collapse to one number:

| Metric | Meaning | Full-brightness red LED reads |
| --- | --- | --- |
| `value` (default) | Mean of the per-pixel max channel | ~255 |
| `luma` | Rec. 601 luminance | ~103 |
| `mean_rgb` | Plain mean of R, G and B | ~85 |

`value` is the default precisely because it is hue-independent: a saturated red segment
and a saturated green segment both read near 255, so one threshold works for both. A
plain RGB average would put a fully lit red segment at ~85 — *below* a threshold of 90.

## What the node publishes

State, on every committed transition and as a periodic retained refresh:

```json
{
  "node_id": "machine1",
  "state": "fault",
  "segments": {
    "red":   {"mode": "steady", "value": 213.5, "threshold": 90.0, "lit": true,  "duty": 1.0},
    "green": {"mode": "off",    "value": 4.8,   "threshold": 90.0, "lit": false, "duty": 0.0}
  },
  "since": "2026-09-01T14:22:03Z",
  "confidence": "ok",
  "reasons": [],
  "ts": "2026-09-01T14:23:03Z",
  "seq": 18503
}
```

`since` is when the pattern first appeared, not when debounce expired, so dwell times
computed from it reflect the machine rather than the monitor. `ts` is when this
particular payload was built, so a stale retained message is recognisable as stale.

The health heartbeat adds `uptime_s`, `fps`, `fps_configured`, `frames`, `metric`,
`source`, `version`, the dark reference reading, and per-segment `transitions` and
`settled` — the detail needed to tune `flash_window_s` without shelling into the node.
`fps` is `null` rather than `0.0` on the very first heartbeat, so "not measurable yet"
cannot be mistaken for a stalled camera.

### Snapshots

*Camera path only. A GPIO node has no frame to publish.*

A raw underexposed frame is almost entirely black and makes a poor debug image, so
snapshots are optionally annotated — each ROI outlined and labelled with its measured
value, coloured by lit/unlit, with the dark reference in magenta and a header carrying
state and confidence. A misaligned ROI becomes obvious at a glance instead of something
to infer from numbers. `snapshot_gain` brightens the JPEG only; the detector always sees
the untouched frame.

One snapshot goes out on every committed transition regardless of the rate limit, so a
push notification carries an image of the state that actually triggered it rather than
one from minutes earlier. Debounce already caps transitions at one per `debounce_s`, so
that cannot run away. One is also published at startup, to confirm camera aim on a fresh
deploy without waiting for the light to change.

### Failure behaviour

The camera is the thing that cannot be paused, so paho owns the socket on its own thread
and publishes fail fast rather than stalling the capture loop. A node whose broker is
down keeps sampling and detecting; state publishes that fail are marked pending and
retried on the next frame, and the reconnect callback resends the retained state so the
broker's copy is never left stale. A broker that is unreachable at startup is a warning,
not a fatal error — it may come up later, and a node that samples and retries is more
useful than one that exits.

The same reasoning applies one hop further out, to the push itself. See
[Undelivered notifications](docs/gpio-tap.md#undelivered-notifications) — for a latching
light this is the difference between an alert arriving late and an alert never arriving.

## Detection

Per frame, per segment: mean brightness inside the ROI, compared against a threshold.
A segment is *lit* when the mean exceeds it. Over a rolling `flash_window_s` the engine
tracks the lit **duty cycle** and the **transition count**, and the mode falls out of
those two numbers:

| Condition | Mode |
| --- | --- |
| Transitions ≥ `flash_min_transitions` **and** duty inside `flash_duty_range` | `flashing` |
| Duty ≥ the top of `flash_duty_range` | `steady` |
| Duty ≤ the bottom of `flash_duty_range` | `off` |
| Otherwise (mid-range duty, too few transitions) | previous mode is held |

`flashing` is never collapsed into `steady`. The last row matters more than it looks:
that case happens while the window is still draining after a real change, and holding
the previous mode there means a segment coming on reads `off` until the evidence is
solid, rather than flickering on the way up.

The resulting segment pattern resolves to a state name, which then has to hold for
`debounce_s` before it is committed. Only committed transitions get published.

### Latency

The window has to drain before a duty cycle clears the steady or off boundary, and only
then does debounce start. So with the machine's 2s window, an 0.8 duty ceiling and a 1.5s
debounce, a light change takes **1.9–3.1s** to reach a notification. `stacklight
validate` prints the figure for whatever config you hand it. Shorten `flash_window_s`
for faster response, at the cost of being unable to see slower flashes.

### Two tuning consequences worth knowing

**A single blink looks exactly like a flash.** One 0.5s pulse puts two transitions and a
24% duty cycle in a 2s window — indistinguishable from flashing, given
`flash_min_transitions: 2`. It holds that way for longer than the debounce, so it
commits. It does *not* become `running`; green-flashing matches no configured state, so
it lands on `unknown`. Raising `flash_min_transitions` to 3 rejects single pulses, at the
cost of needing roughly 0.75Hz or faster to see a real flash in a 2s window. Left at 2
until the machine's actual flash rate is known.

**A flash slower than the window cannot be seen.** At 0.2Hz against a 2s window the
engine reads alternating `steady` and `off`, never `flashing`. If the light turns out to
blink that slowly, widen `flash_window_s`.

### Confidence

A reading is `degraded`, and its state forced to `unknown`, when either:

* **Warming up** — the rolling window does not yet hold a full `flash_window_s`. Nothing
  commits during warm-up, so a restart never publishes a spurious `unknown` that means
  only "started a moment ago".
* **The dark reference is bright** — see below. This one *does* commit, because a bumped
  camera is news.

The dark reference only trips when it stays bright, not on a single frame: a reflection
or a passing torch is not a moved camera. Raw brightness values stay in the payload
throughout, so a degraded node can be diagnosed without shelling into it.

## Source abstraction

| Source | Use |
| --- | --- |
| `PiCameraSource` | Real `picamera2` with manual locked controls (step 6) |
| `FileSource` | Replays a directory of images, a single image, or a video file, on a loop |
| `SyntheticSource` | Draws frames programmatically from a script of timed segment modes |

Two design rules keep hardware-free testing honest:

* **Frames carry their own timestamp**, taken from the source's clock. Nothing
  downstream calls `time.monotonic()` itself. Tests inject a `VirtualClock` that
  fast-forwards, so a scripted minute of light behaviour runs in milliseconds and is
  deterministic.
* **Camera controls are part of the `FrameSource` interface**, not of the Pi backend,
  because the calibration UI drives exposure and gain live against whatever source is
  running.

Any production config can be replayed on a laptop without editing the file:

```bash
uv run stacklight synth config/machine1.yaml --frames 20
```

`SyntheticSource` mirrors the detection ROIs when `camera.synthetic.segments` is
omitted, so the geometry you calibrate is the geometry you test.

## On the Pi

**Setting up from scratch: [`docs/pi-setup.md`](docs/pi-setup.md)** — parts check
(including the 22-pin cable the Pi 5 needs), flashing for headless boot, and a
stock-tools procedure for proving the underexposure technique on your actual light before
any of this code runs on the Pi.

**What it is built from: [`docs/parts.md`](docs/parts.md)** — every part, with the
reason it is that part rather than a similar-looking one, and the measured facts about
the light's connector and wiring.


`picamera2` is not pip-installable in the normal way. It comes from apt, and the venv
must be able to see it:

```bash
make bootstrap PI=stacklight.local PI_USER=youruser
make deploy    PI=stacklight.local PI_USER=youruser NODE=machine1
```

which amounts to:

```bash
sudo apt install -y python3-picamera2
python3 -m venv --system-site-packages .venv
.venv/bin/pip install -e /opt/stacklight
```

The systemd unit is templated per node (`stacklight-node@machine1`), so adding a second
camera is a second config file and a second `systemctl enable` — nothing central changes.
`make logs`, `make restart` and `make probe` cover the day-to-day.

### What the camera is locked to

*Camera path only.*

Everything automatic is switched off, because the detector needs the same frame under the
same conditions every time: `AeEnable` and `AwbEnable` false, `ExposureTime` and
`AnalogueGain` pinned, `AfMode` manual with an explicit `LensPosition`, and
`FrameDurationLimits` pinned at *both* ends — a single limit would let the camera
lengthen exposure to chase a brightness target. `ColourGains` are stated explicitly too:
with AWB off the ISP keeps whatever gains it last had, so leaving them unset makes colour
balance undefined between runs.

The camera's own frame timing paces the loop rather than a sleep, since
`FrameDurationLimits` already pins the rate and `capture_array` blocks for the next
frame; sleeping on top of that would wait out a frame already in hand and halve the
effective rate. What the sensor actually *accepted* is read back and logged at startup,
because a sensor silently clamps an out-of-range exposure, and finding that out from a
log line beats inferring it from puzzling brightness values.

Note that changing `camera.resolution` can change the sensor mode and therefore the field
of view, which invalidates calibrated ROIs. The mode the ISP chose is logged at startup.

`camera.swap_rb` defaults to true: picamera2 delivers an `"RGB888"` stream as B-G-R in
memory, so the channels are swapped back. Detection is indifferent — the default metric
takes the maximum channel — but snapshot colours are not, so set it false if red and blue
come out inverted.

The Pi camera import is lazy and optional, so this package imports cleanly on a machine
with no `picamera2` at all. Asking for `camera.source: picamera` there fails with a
message telling you to use `synthetic` instead, rather than an `ImportError`.

Full first-boot setup, the calibration workflow, and how to add a second node are
documented as those steps land.

## Layout

```
src/stacklight/
  config.py          strict YAML parsing, validation, state resolution
  detector.py        rolling-window mode classification, debounce, commit
  metrics.py         ROI -> one brightness number (the hot path)
  node.py            the capture-detect-publish loop
  publisher.py       Publisher interface, paho client, recording stand-in
  payloads.py        MQTT JSON documents
  snapshot.py        annotated debug JPEG encoding
  types.py           SegmentMode / Confidence vocabulary
  clock.py           real and virtual clocks
  logging_setup.py   structured text/JSON logging
  cli.py             validate / states / synth / detect / node
  sources/
    base.py          FrameSource interface, Frame, CameraControls
    synthetic.py     programmatic frames, scriptable
    file.py          image directory / image / video replay
config/              per-node YAML
hardware/enclosure/  printed case: OpenSCAD source plus rendered STLs
docs/                design notes, parts list, Pi setup
tests/
```
