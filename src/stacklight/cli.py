"""Command line entry point.

Subcommands here are diagnostic: they let a config be checked, its state table
inspected, and synthetic frames rendered, all without a camera or a broker.
"""

from __future__ import annotations

import argparse
import itertools
import logging
import sys
from pathlib import Path

from . import __version__
from .config import NodeConfig, load_config, resolve_state
from .detector import Detector, Reading, detection_latency_bounds
from .errors import StackLightError
from .logging_setup import configure_logging
from .types import UNKNOWN_STATE, SegmentMode


def _describe(config: NodeConfig) -> str:
    lens = config.camera.lens_position if config.camera.lens_position is not None else "auto"
    best, worst = detection_latency_bounds(config.detection)
    lines = [
        f"node_id:     {config.node_id}",
        f"config:      {config.path}",
        f"camera:      {config.camera.source} "
        f"{config.camera.width}x{config.camera.height} @ {config.camera.fps:g}fps",
        f"exposure:    {config.camera.exposure_us}us  gain {config.camera.analogue_gain:g}  "
        f"awb {'on' if config.camera.awb_enable else 'off'}  "
        f"lens {lens}",
        f"metric:      {config.detection.metric}",
        f"latency:     {best:.1f}-{worst:.1f}s from light change to committed state "
        f"(window {config.detection.flash_window_s:g}s + debounce "
        f"{config.detection.debounce_s:g}s)",
        f"mqtt:        {config.mqtt.host}:{config.mqtt.port} -> {config.state_topic}",
        "segments:",
    ]
    for seg in config.segments:
        lines.append(
            f"  {seg.name:<10} roi={seg.roi.as_list()} threshold={seg.threshold:g} "
            f"color={list(seg.render_color)}"
        )
    if config.dark_reference is not None:
        lines.append(
            f"  {'[dark ref]':<10} roi={config.dark_reference.roi.as_list()} "
            f"threshold={config.dark_reference.threshold:g}"
        )
    else:
        lines.append("  [dark ref] not configured -- drift and tamper detection is disabled")
    lines.append("states:")
    for spec in config.states:
        constraints = ", ".join(f"{k}={v}" for k, v in spec.constraints.items())
        lines.append(f"  {spec.name:<10} {constraints}")
    return "\n".join(lines)


def _cmd_validate(args: argparse.Namespace) -> int:
    config = load_config(args.config)
    print(_describe(config))
    print("\nconfig OK")
    return 0


def _cmd_states(args: argparse.Namespace) -> int:
    """Print how every possible segment pattern resolves.

    Worth a look after editing ``states:`` -- it makes the wildcard semantics concrete
    and shows exactly which patterns fall through to 'unknown'.
    """
    config = load_config(args.config)
    names = config.segment_names
    # Columns must fit the widest mode name ("flashing") as well as the segment names.
    width = max(*(len(n) for n in names), *(len(m.value) for m in SegmentMode))
    header = "  ".join(f"{n:<{width}}" for n in names)
    print(f"{header}  -> state")
    print("-" * (len(header) + 12))
    unknown = 0
    for combo in itertools.product(SegmentMode, repeat=len(names)):
        observed = dict(zip(names, combo, strict=True))
        state = resolve_state(config.states, observed)
        if state == UNKNOWN_STATE:
            unknown += 1
        row = "  ".join(f"{m.value:<{width}}" for m in combo)
        print(f"{row}  -> {state}")
    total = len(SegmentMode) ** len(names)
    print(f"\n{total - unknown}/{total} patterns are named; {unknown} resolve to 'unknown'")
    return 0


def _cmd_synth(args: argparse.Namespace) -> int:
    """Render synthetic frames to disk, to eyeball what the detector will be fed."""
    from PIL import Image

    from .sources import create_source

    config = load_config(args.config)
    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)

    source = create_source(config, override="synthetic")
    if args.mode:
        for spec in args.mode:
            name, _, mode = spec.partition("=")
            source.set_mode(name.strip(), SegmentMode(mode.strip()))

    written = 0
    with source:
        for frame in source.frames(limit=args.frames):
            path = out_dir / f"frame_{frame.seq:05d}.png"
            Image.fromarray(frame.data).save(path)
            written += 1
    print(f"wrote {written} frame(s) to {out_dir}")
    return 0


#: A scripted sequence that exercises every branch of the engine, for --demo.
_DEMO_SCRIPT = [
    ("all off", 4.0, {}),
    ("steady green", 5.0, {"green": SegmentMode.STEADY}),
    ("flashing red", 5.0, {"red": SegmentMode.FLASHING}),
    ("steady red", 5.0, {"red": SegmentMode.STEADY}),
    ("all off", 4.0, {}),
]


def _reading_row(reading: Reading, names: tuple[str, ...], t0: float) -> str:
    values = "  ".join(f"{reading.segments[n].value:6.1f}" for n in names)
    modes = " ".join(f"{reading.segments[n].mode.value:<8}" for n in names)
    dark = "    -" if reading.dark_reference is None else f"{reading.dark_reference.value:5.1f}"
    conf = reading.confidence.value
    if reading.reasons:
        conf += "(" + ",".join(reading.reasons) + ")"
    return (
        f"{reading.t - t0:7.2f}  {values}  {dark}  | {modes} | "
        f"{reading.candidate_state:<9} {reading.state or '-':<9} {conf}"
    )


def _cmd_detect(args: argparse.Namespace) -> int:
    """Run the detection engine and print what it sees.

    This is the tuning tool: watch the raw brightness numbers next to the mode each
    segment resolves to, and see exactly when a state commits.
    """
    from .clock import VirtualClock
    from .sources import create_source
    from .sources.synthetic import ScriptStep, SyntheticSource

    config = load_config(args.config)
    # --fast drives the source from a virtual clock, so a 25-second sequence finishes
    # immediately instead of taking 25 seconds.
    clock = VirtualClock() if args.fast else None
    source = create_source(config, override=args.source, clock=clock)
    if args.fast and not isinstance(source, SyntheticSource):
        print("error: --fast only makes sense for a synthetic source", file=sys.stderr)
        return 2

    if args.demo:
        if not isinstance(source, SyntheticSource):
            print(
                "error: --demo needs a synthetic source (pass --source synthetic)", file=sys.stderr
            )
            return 2
        source.set_script(
            [ScriptStep(seconds, modes) for _, seconds, modes in _DEMO_SCRIPT], loop=False
        )
        print("demo script:")
        offset = 0.0
        for label, seconds, _ in _DEMO_SCRIPT:
            print(f"  t={offset:5.1f}s  {label}")
            offset += seconds
        print()

    names = config.segment_names
    best, worst = detection_latency_bounds(config.detection)
    print(f"expected latency {best:.1f}-{worst:.1f}s after each light change\n")
    header_values = "  ".join(f"{n:>6}" for n in names)
    header_modes = " ".join(f"{n:<8}" for n in names)
    header = (
        f"{'t':>7}  {header_values}   dark  | {header_modes} | "
        f"{'candidate':<9} {'state':<9} confidence"
    )
    print(header)
    print("-" * len(header))

    detector = Detector(config)
    every = max(1, round(config.camera.fps * args.interval))
    frames = args.frames if args.frames else round(args.seconds * config.camera.fps)

    t0: float | None = None
    with source:
        if args.mode:
            for spec in args.mode:
                name, _, mode = spec.partition("=")
                source.set_mode(name.strip(), SegmentMode(mode.strip()))
        for frame in source.frames(limit=frames):
            reading = detector.update(frame)
            if t0 is None:
                t0 = reading.t
            if reading.changed:
                print(f"{_reading_row(reading, names, t0)}   <-- COMMITTED {reading.state}")
            elif frame.seq % every == 0:
                print(_reading_row(reading, names, t0))
    return 0


def _cmd_node(args: argparse.Namespace) -> int:
    """Run the node: capture, detect, publish to MQTT."""
    import signal

    from .node import Node, availability_will
    from .publisher import MqttPublisher, Publisher, RecordingPublisher
    from .sources import create_source

    config = load_config(args.config)
    configure_logging(args.log_level or config.logging.level, config.logging.format)
    log = logging.getLogger("stacklight.node")
    source = create_source(config, override=args.source)

    node: Node
    publisher: Publisher
    if args.dry_run:
        print(f"dry run: nothing will be sent to {config.mqtt.host}:{config.mqtt.port}")
        publisher = RecordingPublisher()
    else:
        publisher = MqttPublisher(
            config,
            will=availability_will(config),
            # A transition published into a dead socket is gone, and the broker's
            # retained copy is stale until something replaces it.
            on_reconnect=lambda: node.republish_state(),
        )
    node = Node(config, source, publisher)

    def handle_signal(signum: int, _frame: object) -> None:
        log.info("shutting down", extra={"signal": signal.Signals(signum).name})
        node.stop()

    signal.signal(signal.SIGINT, handle_signal)
    signal.signal(signal.SIGTERM, handle_signal)

    with publisher:
        if isinstance(publisher, MqttPublisher) and not publisher.wait_until_connected(
            args.connect_timeout
        ):
            # Not fatal: the broker may come up later, and a node that samples the light
            # and keeps retrying is more useful than one that exits.
            log.warning(
                "broker not reachable yet, continuing and retrying in the background",
                extra={"host": config.mqtt.host, "port": config.mqtt.port},
            )
        with node:
            node.run(max_frames=args.frames, max_seconds=args.seconds)

    if isinstance(publisher, RecordingPublisher):
        counts: dict[str, int] = {}
        for message in publisher.messages:
            counts[message.topic] = counts.get(message.topic, 0) + 1
        print(f"\ndry run published {len(publisher.messages)} message(s):")
        for topic, count in sorted(counts.items()):
            print(f"  {count:5d}  {topic}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="stacklight", description=__doc__)
    parser.add_argument("--version", action="version", version=f"stacklight {__version__}")
    parser.add_argument("--log-level", default=None, help="override the configured log level")
    sub = parser.add_subparsers(dest="command", required=True)

    p_validate = sub.add_parser("validate", help="load and validate a node config")
    p_validate.add_argument("config", help="path to the node YAML config")
    p_validate.set_defaults(func=_cmd_validate)

    p_states = sub.add_parser("states", help="show how every segment pattern resolves")
    p_states.add_argument("config")
    p_states.set_defaults(func=_cmd_states)

    p_synth = sub.add_parser("synth", help="render synthetic frames to PNG files")
    p_synth.add_argument("config")
    p_synth.add_argument("--frames", type=int, default=10)
    p_synth.add_argument("--out", default="samples/synthetic")
    p_synth.add_argument(
        "--mode",
        action="append",
        metavar="SEGMENT=MODE",
        help="force a segment mode, e.g. --mode red=flashing (repeatable)",
    )
    p_synth.set_defaults(func=_cmd_synth)

    p_detect = sub.add_parser("detect", help="run the detection engine and print readings")
    p_detect.add_argument("config")
    p_detect.add_argument(
        "--source",
        choices=["picamera", "file", "synthetic"],
        default=None,
        help="override camera.source from the config",
    )
    p_detect.add_argument(
        "--demo",
        action="store_true",
        help="drive a synthetic source through a scripted light sequence",
    )
    p_detect.add_argument(
        "--fast",
        action="store_true",
        help="run a synthetic source on a virtual clock, as fast as possible",
    )
    p_detect.add_argument("--seconds", type=float, default=25.0)
    p_detect.add_argument("--frames", type=int, default=None)
    p_detect.add_argument(
        "--interval",
        type=float,
        default=1.0,
        help="seconds between status lines (transitions always print)",
    )
    p_detect.add_argument("--mode", action="append", metavar="SEGMENT=MODE")
    p_detect.set_defaults(func=_cmd_detect)

    p_node = sub.add_parser("node", help="run the node: capture, detect, publish")
    p_node.add_argument("config")
    p_node.add_argument(
        "--source",
        choices=["picamera", "file", "synthetic"],
        default=None,
        help="override camera.source from the config",
    )
    p_node.add_argument(
        "--dry-run",
        action="store_true",
        help="run the full pipeline but record publishes instead of sending them",
    )
    p_node.add_argument("--frames", type=int, default=None, help="stop after N frames")
    p_node.add_argument("--seconds", type=float, default=None, help="stop after N seconds")
    p_node.add_argument("--connect-timeout", type=float, default=5.0)
    p_node.set_defaults(func=_cmd_node)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    configure_logging(args.log_level or "INFO")
    try:
        return int(args.func(args))
    except StackLightError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


def node_main(argv: list[str] | None = None) -> int:
    """Entry point for the ``stacklight-node`` console script and the systemd unit."""
    return main(["node", *(argv if argv is not None else sys.argv[1:])])


if __name__ == "__main__":
    raise SystemExit(main())
