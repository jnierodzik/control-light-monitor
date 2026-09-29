# stacklight-monitor

Watch a machine's stack light with a Raspberry Pi camera and push a phone notification
when the machine's state changes.

Currently monitoring an **industrial machine**: a two-segment tower light,
red over green.

## How it reads the light

The camera does **not** try to classify colour in a normally exposed photograph. That
approach falls apart the moment the shop lights change, or when amber and red get
confused. Instead:

1. **Lock the camera.** Auto-exposure, auto-white-balance and autofocus are all
   disabled, with a fixed exposure time, analogue gain and lens position.
2. **Underexpose heavily.** The room goes nearly black; only an energised LED registers
   as bright pixels.
3. **Identify segments by position, not hue.** The segments sit at fixed, known
   rectangles in the frame. "Red" just means *the top rectangle*.
4. **Threshold.** Per segment, per frame: mean brightness inside the rectangle versus a
   threshold. Colour is at most a secondary sanity check.

Everything else in this repository is plumbing around that idea.

## Architecture

```
[node: pi + camera] --MQTT--> [mosquitto] --> [rules engine] --> ntfy
[node 2: future]    --MQTT-->      ^               |
                                heartbeats         +--> SQLite (state history)
```

Nodes are dumb and self-contained. They publish semantic state, not pixels. A separate
rules process decides what is worth a notification. Adding a second camera means
deploying another node with its own config file and changing nothing centrally.

Topics, namespaced per node id:

| Topic | Payload |
| --- | --- |
| `shop/lights/<node_id>/state` | Retained JSON; published on committed transitions and refreshed periodically |
| `shop/lights/<node_id>/health` | Heartbeat with uptime, frame rate, per-segment raw brightness |
| `shop/lights/<node_id>/snapshot` | Occasional JPEG for debugging, rate limited |

## Build status

This project is being built in reviewable steps.

- [x] **1. Skeleton, config, `FrameSource` + `SyntheticSource`**
- [ ] 2. Detection engine (ROI sampling, flash detection, debounce, state resolution)
- [ ] 3. MQTT publisher, heartbeats, retained state
- [ ] 4. Calibration web UI
- [ ] 5. Rules engine, ntfy, SQLite logging, watchdog
- [ ] 6. `PiCameraSource`, systemd units, deployment

## Development, with no camera and no Pi

The hardware is not required to work on this. `SyntheticSource` draws frames
programmatically — a black background with coloured rectangles that can be switched
off, steady or flashing on a schedule, with optional sensor noise, ambient light bleed
and LED glow. Brightness responds to exposure and gain the way a real sensor does, so
even the exposure controls can be exercised on a laptop.

```bash
make setup                      # uv sync --extra dev, on Python 3.11
make test
make lint
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

`dark_reference` names a region that must **always** read dark. If it starts reading
bright, the camera has been bumped or the lighting situation changed, and every other
reading in the frame is suspect. When it trips, the node publishes
`confidence: "degraded"` and a state of `unknown` rather than a confidently wrong
answer.

### The brightness metric

`detection.metric` controls how an ROI's pixels collapse to one number:

| Metric | Meaning | Full-brightness red LED reads |
| --- | --- | --- |
| `value` (default) | Mean of the per-pixel max channel | ~255 |
| `luma` | Rec. 601 luminance | ~103 |
| `mean_rgb` | Plain mean of R, G and B | ~85 |

`value` is the default precisely because it is hue-independent: a saturated red segment
and a saturated green segment both read near 255, so one threshold works for both. A
plain RGB average would put a fully lit red segment at ~85 — *below* a threshold of 90.

## Camera abstraction

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

`picamera2` is not pip-installable in the normal way. It comes from apt, and the venv
must be able to see it:

```bash
sudo apt install -y python3-picamera2
python3 -m venv --system-site-packages .venv
.venv/bin/pip install -e '.[dev]'
```

The Pi camera import is lazy and optional, so this package imports cleanly on a machine
with no `picamera2` at all. Asking for `camera.source: picamera` there fails with a
message telling you to use `synthetic` instead, rather than an `ImportError`.

Full first-boot setup, the calibration workflow, and how to add a second node are
documented as those steps land.

## Layout

```
src/stacklight/
  config.py          strict YAML parsing, validation, state resolution
  types.py           SegmentMode / Confidence vocabulary
  clock.py           real and virtual clocks
  logging_setup.py   structured text/JSON logging
  cli.py             validate / states / synth
  sources/
    base.py          FrameSource interface, Frame, CameraControls
    synthetic.py     programmatic frames, scriptable
    file.py          image directory / image / video replay
config/              per-node YAML
tests/
```
