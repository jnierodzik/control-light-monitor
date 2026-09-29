# Claude Code project prompt — stack light monitor

Paste everything below the line into Claude Code in an empty directory.

---

I want to build a Raspberry Pi camera system that watches the stack light (tower
light) on a machine in my shop and pushes a notification to my phone when the
machine's state changes. Set up the project from scratch.

## Context

The machine is an industrial machine. It has a two-segment stack
light on a vertical pole: **red on top, green below it**. I don't yet know the
full meaning of every pattern, so the software must not hard-code assumptions
beyond "there are N segments, each can be off / steady / flashing."

Hardware (on order, not in hand yet):

- Raspberry Pi 5, 2GB, running Raspberry Pi OS Bookworm or later, 64-bit, headless
- Raspberry Pi Camera Module 3, standard (IMX708, IR-cut filter, 66° H x 41° V FOV,
  10cm minimum focus)
- 500mm camera cable, camera mounted on a printed arm clamped to the light's own pole

**Because the hardware has not arrived, everything must be developable and testable
on a Linux dev machine with no camera attached.** This is a hard requirement, not a
nice-to-have. See "Camera abstraction" below.

## The core technique — read this before designing anything

Do not attempt color classification on a normally-exposed image. Instead:

1. **Lock the camera manually.** Disable auto-exposure, auto-white-balance, and
   autofocus. Set a fixed exposure time, analogue gain, and lens position.
2. **Underexpose heavily** so the room goes nearly black and only an energized LED
   segment registers as bright pixels.
3. **Identify segments by fixed position, not hue.** The segments are at known,
   unchanging locations in the frame. Red is simply "the top rectangle." This makes
   the system immune to hue shifts, amber-vs-red ambiguity, and ambient light.
4. The decision per segment reduces to: mean brightness inside a rectangle, compared
   against a threshold. That's it. Color is at most a secondary sanity check.

Everything else in this project is plumbing around that idea.

## Architecture

Edge nodes are dumb and self-contained; they publish semantic state, not pixels.
A separate rules process decides what to notify about. Keep these as separate
processes so a second camera can be added later by deploying another node with a
different config file and changing nothing centrally.

```
[node: pi + camera] --MQTT--> [mosquitto] --> [rules engine] --> ntfy
[node 2: future]    --MQTT-->      ^               |
                                heartbeats         +--> SQLite (state history)
```

MQTT topics, namespaced per node id:

- `shop/lights/<node_id>/state` — retained JSON, published on committed transitions
  and refreshed periodically
- `shop/lights/<node_id>/health` — heartbeat every 15s with uptime, frame rate,
  and per-segment raw brightness values
- `shop/lights/<node_id>/snapshot` — occasional JPEG for debugging, rate limited

State payload shape (adjust if you have a better idea, but keep raw values in it —
they're essential for tuning):

```json
{
  "node_id": "machine1",
  "state": "fault",
  "segments": {
    "red":   {"mode": "steady", "value": 142.3},
    "green": {"mode": "off",    "value": 3.1}
  },
  "since": "2026-09-01T14:22:03Z",
  "confidence": "ok"
}
```

## Camera abstraction

Define a `FrameSource` interface with at least three implementations:

- `PiCameraSource` — real `picamera2`, manual controls
- `FileSource` — replays a directory of PNG/JPEG frames, or a video file, on a loop
- `SyntheticSource` — generates frames programmatically: a black background with
  colored rectangles at configurable positions that can be toggled on/off/flashing
  on a schedule, plus optional noise and ambient light bleed

`SyntheticSource` is what makes the whole system testable in CI and on my laptop.
The state machine tests should drive it through scripted sequences (off → steady
green → flashing red → off) and assert the correct MQTT publishes come out.

Note: `picamera2` is not pip-installable in the normal way. On the Pi it comes from
`apt install python3-picamera2`, so the venv must be created with
`--system-site-packages`. Make the Pi camera import lazy and optional so the package
imports cleanly on a machine without it.

## Configuration

Per-node YAML. Something like:

```yaml
node_id: machine1
mqtt:
  host: localhost
  port: 1883
  base_topic: shop/lights
camera:
  source: picamera        # picamera | file | synthetic
  resolution: [1280, 720]
  exposure_us: 800
  analogue_gain: 1.0
  awb_enable: false
  lens_position: 3.5      # fixed focus, dioptres
  fps: 15
segments:
  - name: red
    roi: [412, 88, 46, 60]     # x, y, w, h
    threshold: 90
  - name: green
    roi: [412, 152, 46, 60]
    threshold: 90
dark_reference:
  roi: [300, 400, 40, 40]      # must ALWAYS read dark
  threshold: 60
detection:
  debounce_s: 1.5
  flash_window_s: 2.0
  flash_duty_range: [0.2, 0.8]
  flash_min_transitions: 2
states:
  fault:     {red: steady, green: off}
  running:   {red: off, green: steady}
  attention: {red: flashing}
  idle:      {red: off, green: off}
```

The `dark_reference` region is a drift and tamper detector. If a region that should
never be lit starts reading bright, the camera has been bumped or the lighting
situation changed. When that trips, publish `confidence: "degraded"` and a state of
`unknown` rather than a confidently wrong answer.

State names must be resolved from config, not hard-coded. If the observed segment
pattern matches no configured state, publish `unknown` rather than guessing.

## Detection logic

- Sample at the configured fps
- Per segment, compute mean brightness over the ROI each frame
- A segment is "lit" if mean > threshold
- Over a rolling `flash_window_s`, compute lit duty cycle and transition count.
  Duty inside `flash_duty_range` with at least `flash_min_transitions` means
  `flashing`; otherwise `steady` or `off`. Do not collapse flashing into steady —
  stack lights commonly use flash to mean something different.
- A resolved overall state must hold for `debounce_s` before it is committed and
  published
- Publish on committed transitions only, plus a periodic retained refresh

## Calibration web UI

Build this early — it is the difference between a pleasant project and a miserable
one. A small Flask app on the node, served on a configurable port:

- Live MJPEG stream of the current camera source
- Draggable, resizable ROI rectangles overlaid on the stream, one per configured
  segment plus the dark reference
- Live numeric readout per region: current mean brightness, threshold, lit/unlit
- Sliders for exposure time and gain that apply to the live camera immediately, so
  I can dial in the underexposure visually
- A save button that writes the adjusted ROIs, thresholds, and camera settings back
  to the YAML config
- A "capture sample" button that saves the current frame to a directory, so I can
  build a `FileSource` corpus for regression tests

## Rules engine and notifications

Separate process, subscribes to MQTT. Requirements:

- ntfy for push (assume a topic URL in config; support self-hosted or ntfy.sh)
- Include the snapshot JPEG as an image attachment on notifications where available
- Per-rule cooldown and dedupe so a flapping state doesn't spam me
- Escalation: if still in a given state after N minutes, re-notify at higher priority
- Watchdog: if no heartbeat from a node for 60s, notify that the monitor itself is
  down. A silent monitor is worse than no monitor.
- Optional quiet hours
- Log every committed state transition to SQLite with timestamps, so I can later
  compute dwell times, throughput, and fault rates

## Project requirements

- Python 3.11+, `uv` or plain venv, `pyproject.toml`
- Dependencies: `picamera2` (system, optional), `numpy`, `paho-mqtt`, `flask`,
  `pyyaml`, `pillow`. Prefer numpy over OpenCV for the hot path; if you want OpenCV
  for the calibration overlay only, make it an optional extra.
- `ruff` and `pytest`, with tests that actually exercise the state machine via
  `SyntheticSource`
- systemd unit files for the node and the rules engine, with `Restart=always`
- A `Makefile` or `justfile` with targets for `dev`, `test`, `lint`, `calibrate`,
  and `deploy`
- README covering first-boot setup, calibration workflow, and how to add a second node
- Structured logging, configurable level, no print statements in library code

## Build order

Do these in order and stop after each for me to review:

1. Project skeleton, `pyproject.toml`, config loading and validation, `FrameSource`
   interface with `SyntheticSource` implemented
2. Detection engine (ROI sampling, thresholding, flash detection, debounce, state
   resolution) with tests driven by `SyntheticSource`
3. MQTT publisher, heartbeats, retained state
4. Calibration web UI
5. Rules engine, ntfy, SQLite logging, watchdog
6. `PiCameraSource`, systemd units, deployment docs

Start with step 1. Ask me about anything ambiguous rather than guessing — especially
about the state semantics, which I'll be able to answer better once I've watched the
machine run.
