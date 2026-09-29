"""Command line entry point.

Subcommands here are diagnostic: they let a config be checked, its state table
inspected, and synthetic frames rendered, all without a camera or a broker.
"""

from __future__ import annotations

import argparse
import itertools
import sys
from pathlib import Path

from . import __version__
from .config import NodeConfig, load_config, resolve_state
from .errors import StackLightError
from .logging_setup import configure_logging
from .types import UNKNOWN_STATE, SegmentMode


def _describe(config: NodeConfig) -> str:
    lens = config.camera.lens_position if config.camera.lens_position is not None else "auto"
    lines = [
        f"node_id:     {config.node_id}",
        f"config:      {config.path}",
        f"camera:      {config.camera.source} "
        f"{config.camera.width}x{config.camera.height} @ {config.camera.fps:g}fps",
        f"exposure:    {config.camera.exposure_us}us  gain {config.camera.analogue_gain:g}  "
        f"awb {'on' if config.camera.awb_enable else 'off'}  "
        f"lens {lens}",
        f"metric:      {config.detection.metric}",
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
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    configure_logging(args.log_level or "INFO")
    try:
        return int(args.func(args))
    except StackLightError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
