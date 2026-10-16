#!/usr/bin/env python3
"""Read an ``.xpgcan`` capture offline: counts, shapes, hexdumps, plugins.

This is the other half of ``xpeng_can_capture.py``, and the reason the capture is
raw bytes rather than parsed rows. Everything here runs against a file, with no
broker, no credentials, no car and no Home Assistant - so a decoder can be
written, thrown away and rewritten without costing another capture.

What it deliberately does not do is claim to know XPENG's CanPack framing. The
topic names say ``CanPack1s``/``CanPackV2``; the byte layout behind them is not
documented anywhere in this repository, and a tool that guessed at offsets would
be worth less than one that shows you the bytes. So:

``shapes``   is the starting point for an unknown stream - it groups payloads by
             their exact bytes and prints the distinct ones by frequency, which
             turns "a 1 Hz firehose" into "eleven message shapes, one of which is
             94% of the traffic". Decoding starts there, not at a hexdump.
``frames``   is the structural view: split a payload at a fixed stride and show
             the words. It is an aid to spotting a header or a counter, and it
             does not name any field.
``dump``     is the fidelity view: exactly what arrived, when.
``export``   is the hand-off to pandas/jq, as newline-delimited JSON.
``--decoder`` is the hook: point it at your own ``module.py:function`` and its
             output is what gets printed, so the decoder can live outside this
             repository entirely.

Usage
-----
    xpeng_can_decode.py summary capture.xpgcan
    xpeng_can_decode.py shapes  capture.xpgcan --topic 'CanPack1s'
    xpeng_can_decode.py dump    capture.xpgcan --topic 'CanPack1s' --limit 5
    xpeng_can_decode.py export  capture.xpgcan --out frames.jsonl --text

Stdlib only.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import importlib.util
import json
import sys
from pathlib import Path

TOOLS_DIR = Path(__file__).resolve().parent
if str(TOOLS_DIR) not in sys.path:
    sys.path.insert(0, str(TOOLS_DIR))

import xpeng_can_journal as journal  # noqa: E402


# -- helpers ---------------------------------------------------------------- #


def parse_when(value: str, *, end: bool = False) -> int | None:
    """Accept an ISO-8601 stamp or epoch seconds, and return epoch nanoseconds.

    A bare date (``2026-10-16``) means the whole day, which is what someone
    asking ``--from 2026-10-16`` actually wants.
    """
    value = (value or "").strip()
    if not value:
        return None
    try:
        seconds = float(value)
        return int(seconds * 1e9)
    except ValueError:
        pass

    import calendar
    import time as _time

    text = value.replace("Z", "").replace("T", " ")
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            parsed = _time.strptime(text, fmt)
        except ValueError:
            continue
        seconds = calendar.timegm(parsed)
        if fmt == "%Y-%m-%d" and end:
            seconds += 86_399
        return int(seconds * 1e9)
    raise SystemExit(f"could not read {value!r} as a date, a timestamp or epoch seconds")


def car_ids(topics: list[str]) -> dict[str, list[str]]:
    """Which vehicle identities appear in a topic list.

    ``Xmart/Car/<vid>/...`` is the car relayed by the platform relay; the
    ``Xmart/Tbox.../<iccid>/...`` families are keyed by SIM. Both end up here
    because the question - "did this capture contain anyone else's car?" - is the
    same question for both.
    """
    found: dict[str, list[str]] = {"car_vid": [], "tbox": [], "tbox_d21b": []}
    for topic in topics:
        parts = topic.split("/")
        if len(parts) >= 3 and parts[0] == "Xmart":
            if parts[1] == "Car" and parts[2] not in ("+", "#"):
                found["car_vid"].append(parts[2])
            elif parts[1] == "Tbox" and parts[2] not in ("+", "#"):
                found["tbox"].append(parts[2])
            elif parts[1] == "TboxD21B" and parts[2] not in ("+", "#"):
                found["tbox_d21b"].append(parts[2])
    return {key: sorted(set(value)) for key, value in found.items() if value}


def topic_suffix(topic: str) -> str:
    """``Xmart/Car/94434/State/CanPack1s`` -> ``State/CanPack1s``: drop the id."""
    parts = topic.split("/")
    if len(parts) > 3 and parts[0] == "Xmart" and parts[1] in ("Car", "Tbox", "TboxD21B"):
        return "/".join(parts[3:])
    return topic


# -- subcommands ------------------------------------------------------------ #


def cmd_summary(args: argparse.Namespace) -> int:
    reader = journal.JournalReader(args.capture)
    scan = reader.scan()
    report = scan.as_dict(args.capture)

    if args.json:
        print(json.dumps(report, indent=2, ensure_ascii=False))
        return 0

    meta = report["meta"] or {}
    print(f"capture   : {report['capture_file']}")
    print(f"attached  : {report['file_bytes'] / 1e6:.3f} MB, {report['messages']} messages, "
          f"{report['payload_bytes'] / 1e6:.3f} MB of payload")
    if report["first_utc"]:
        print(f"window    : {report['first_utc']} .. {report['last_utc']} "
              f"({report['duration_s']}s, {report['rate_hz']} msg/s overall)")
    print(f"ended     : {report['end_record'].get('reason') if report['end_record'] else 'NOT recorded'}"
          + ("" if report["end_record"] else "  <- killed or truncated; the frames above are still real"))

    identity = meta.get("identity") or {}
    if identity:
        print(f"identity  : VIN {identity.get('vin') or '?'} | uid {identity.get('uid') or '?'} "
              f"| device {identity.get('device_id') or '?'}")
    if meta.get("subscribed"):
        print(f"topics    : {len(meta['subscribed'])} requested "
              f"({len(meta.get('wide_topics') or [])} with a wildcard)")

    if report["truncated"]:
        print(f"[!] truncated: the last record is incomplete ({report['trailing_bytes']} bytes "
              "of its body are missing). Normal after a kill; the earlier frames are intact.")
    if report["malformed_records"]:
        print(f"[!] {report['malformed_records']} record(s) could not be parsed")

    ids = car_ids(list(scan.stats))
    if ids:
        print("car ids   : " + ", ".join(f"{k}={v}" for k, v in ids.items()))
        if len(ids.get("car_vid", [])) > 1:
            print("            more than one vid: this capture contains other vehicles' traffic")

    print()
    print(f"{'count':>9}  {'rate':>10}  {'bytes':>10}  topic")
    for entry in sorted(report["topics"], key=lambda t: -t["count"])[: args.top]:
        rate = f"{entry['rate_hz']:.3f} Hz" if entry.get("rate_hz") else "n/a"
        print(f"{entry['count']:>9}  {rate:>10}  {entry['bytes']:>10}  {entry['topic']}")
    if len(report["topics"]) > args.top:
        print(f"... and {len(report['topics']) - args.top} more (--top to see further)")

    gaps = [n for n in report["notes"] if n.get("event") in
            ("disconnect", "reconnect", "mint_failed", "subscribe_refused", "connection_lost")]
    if gaps:
        print()
        print(f"{len(gaps)} transport note(s) - these explain holes in the data:")
        for note in gaps[: args.top]:
            detail = {k: v for k, v in note.items() if k not in ("event", "utc")}
            print(f"  {note.get('utc')}  {note.get('event')}  "
                  f"{json.dumps(detail, ensure_ascii=False)[:120]}")
    refused = [n for n in report["notes"] if n.get("event") == "subscribe_refused"]
    if refused and not report["messages"]:
        print()
        print("Every subscription was refused and nothing arrived. That is the broker")
        print("answering your ACL, not a broken tool - see 'notes' for the grants.")
    return 0


def cmd_topics(args: argparse.Namespace) -> int:
    scan = journal.JournalReader(args.capture).scan()
    for topic in scan.topics:
        stat = scan.stats.get(topic)
        count = stat.count if stat else 0
        print(f"{count:>9}  {topic}")
    return 0


def cmd_notes(args: argparse.Namespace) -> int:
    reader = journal.JournalReader(args.capture)
    scan = reader.scan()
    for note in scan.notes:
        print(json.dumps(note, ensure_ascii=False))
    end = scan.end_record
    if end:
        print(json.dumps({"event": "end_record", "reason": end.get("reason"),
                          "utc": end.get("ended_utc")}, ensure_ascii=False))
    elif scan.truncated:
        print(json.dumps({"event": "no_end_record", "truncated": True,
                          "trailing_bytes": scan.trailing_bytes}, ensure_ascii=False))
    return 0


def cmd_shapes(args: argparse.Namespace) -> int:
    """Reduce a stream to the distinct shapes it carries.

    Exact-byte grouping alone is close to useless on a CAN broadcast, because a
    rolling counter or a checksum makes every frame unique - the fixture for this
    tool gives six payloads that are 15/16 identical and six "distinct" shapes.
    So each (topic, length) group also carries a **byte-class pattern**: the value
    of every offset that never changed across the whole group, and ``..`` where it
    did. That is the view that says "16 bytes, counter at offset 4, everything else
    fixed", which is the actual starting point for a decoder.

    Known exact payloads are still counted, capped so a pathological stream cannot
    turn a capture reader into a memory hog.
    """
    MAX_EXACT = 4096
    reader = journal.JournalReader(args.capture)
    groups: dict[tuple[str, int], dict] = {}
    total = 0

    def group_for(topic: str, payload: bytes) -> dict:
        key = (topic, len(payload))
        entry = groups.get(key)
        if entry is None:
            entry = groups[key] = {
                "topic": topic,
                "length": len(payload),
                "count": 0,
                "first_utc": None,
                "const": list(payload),
                "exact": {},
                "overflow": 0,
            }
        return entry

    for message in reader.messages(topic_filter=args.topic):
        total += 1
        entry = group_for(message.topic, message.payload)
        entry["count"] += 1
        if entry["first_utc"] is None:
            entry["first_utc"] = message.utc
        const = entry["const"]
        for index, byte in enumerate(message.payload):
            if const[index] is not None and const[index] != byte:
                const[index] = None
        digest = hashlib.sha1(message.payload).hexdigest()[:12]
        if digest in entry["exact"]:
            entry["exact"][digest][0] += 1
        elif len(entry["exact"]) < MAX_EXACT:
            entry["exact"][digest] = [1, message.payload[: args.sample_bytes]]
        else:
            entry["overflow"] += 1

    if not groups:
        print("no messages matched" + (f" {args.topic!r}" if args.topic else ""))
        return 1

    ordered = sorted(groups.values(), key=lambda g: (-g["count"], g["topic"]))

    if args.json:
        payload = []
        for entry in ordered:
            payload.append({
                "topic": entry["topic"],
                "length": entry["length"],
                "count": entry["count"],
                "first_utc": entry["first_utc"],
                "pattern": pattern_string(entry["const"]),
                "constant_bytes": [v for v in entry["const"] if v is not None],
                "distinct_exact": len(entry["exact"]),
                "overflow_exact": entry["overflow"],
                "exact": [
                    {"digest": d, "count": c, "sample_hex": sample.hex()}
                    for d, (c, sample) in sorted(entry["exact"].items(), key=lambda kv: -kv[1][0])
                ],
            })
        print(json.dumps(payload, indent=2))
        return 0

    print(f"{total} messages in {len(groups)} (topic, length) shape(s)")
    print()
    for entry in ordered[: args.max_shapes]:
        distinct = len(entry["exact"]) + (1 if entry["overflow"] else 0)
        print(f"{entry['count']:>8}  {entry['length']:>5}B  {entry['topic']}")
        print(f"          {distinct} distinct exact, first {entry['first_utc']}")
        print(f"          pattern  {pattern_string(entry['const'])}")
        if args.exact:
            for digest, (count, sample) in sorted(
                entry["exact"].items(), key=lambda kv: -kv[1][0]
            )[: args.exact]:
                share = 100.0 * count / entry["count"]
                print(f"            {count:>7} ({share:5.1f}%)  {sample.hex(' ')}")
        print()
    if len(ordered) > args.max_shapes:
        print(f"... and {len(ordered) - args.max_shapes} more shape(s)")

    sizes: dict[int, int] = {}
    for entry in ordered:
        sizes[entry["length"]] = sizes.get(entry["length"], 0) + entry["count"]
    print("payload sizes: " + ", ".join(
        f"{size}B x{count}" for size, count in sorted(sizes.items(), key=lambda kv: -kv[1])[:10]
    ))
    return 0


def pattern_string(const: list[int | None], group: int = 4) -> str:
    """Render a byte-class pattern, e.g. ``aa 55 01 00  .. 00 00 00``."""
    chunks = []
    for index in range(0, len(const), group):
        parts = [
            f"{value:02x}" if value is not None else ".."
            for value in const[index : index + group]
        ]
        chunks.append(" ".join(parts))
    return "  ".join(chunks)


def cmd_dump(args: argparse.Namespace) -> int:
    reader = journal.JournalReader(args.capture)
    shown = 0
    binary_shown = 0
    for message in reader.messages(
        topic_filter=args.topic,
        limit=args.limit,
        since_ns=parse_when(args.since),
        until_ns=parse_when(args.until, end=True),
    ):
        header = f"{message.utc} {message.topic} ({message.size}B q{message.qos}{'R' if message.retain else ''})"
        if args.format == "hex":
            print(header)
            print(indent_hex(message.payload))
        elif args.format == "json":
            print(json.dumps({
                "utc": message.utc,
                "topic": message.topic,
                "qos": message.qos,
                "retain": message.retain,
                "len": message.size,
                "payload_b64": base64.b64encode(message.payload).decode("ascii"),
            }))
        elif args.format == "raw":
            sys.stdout.buffer.write(message.payload)
            sys.stdout.buffer.flush()
        else:
            text = message.payload[: args.max_bytes].decode("utf-8", errors="replace")
            printable = sum(1 for ch in text if ch.isprintable() or ch in "\r\n\t")
            ratio = printable * 100 // max(1, len(text))
            print(f"{header}  {ratio}% printable")
            print("  " + text.replace("\n", "\n  ")[: args.max_bytes])
            if ratio < 60:
                binary_shown += 1
        shown += 1
    if shown == 0:
        print("no messages matched", file=sys.stderr)
        return 1
    if args.format != "raw":
        print(f"\n{shown} message(s) shown"
              + (f" (limit {args.limit})" if args.limit else ""))
    if binary_shown:
        # Text mode on a CAN pack is the single most common wrong turn here, and
        # the mojibake alone does not say what to do about it.
        print(f"{binary_shown} of those payloads are mostly binary; --format hex "
              "shows the bytes, and `shapes` shows their structure.", file=sys.stderr)
    return 0


def indent_hex(payload: bytes, width: int = 16) -> str:
    lines = []
    for offset in range(0, len(payload), width):
        chunk = payload[offset : offset + width]
        hex_part = " ".join(f"{byte:02x}" for byte in chunk)
        text = "".join(chr(b) if 32 <= b < 127 else "." for b in chunk)
        lines.append(f"  {offset:04x}  {hex_part:<{width * 3 - 1}}  {text}")
    return "\n".join(lines)


def cmd_frames(args: argparse.Namespace) -> int:
    """Structural view: a fixed stride, no field names."""
    reader = journal.JournalReader(args.capture)
    shown = 0
    for message in reader.messages(topic_filter=args.topic, limit=args.limit):
        print(f"{message.utc} {message.topic} ({message.size}B)")
        payload = message.payload
        if args.stride and len(payload) % args.stride == 0:
            for index in range(0, len(payload), args.stride):
                word = payload[index : index + args.stride]
                extra = ""
                if args.stride == 8:
                    import struct

                    be, le = struct.unpack(">Q", word)[0], struct.unpack("<Q", word)[0]
                    extra = f"  u64be={be:<20} u64le={le}"
                elif args.stride == 4:
                    import struct

                    be, le = struct.unpack(">I", word)[0], struct.unpack("<I", word)[0]
                    extra = f"  u32be={be:<12} u32le={le}"
                print(f"  word {index // args.stride:>3}  {word.hex(' ')}{extra}")
        else:
            print(indent_hex(payload))
        shown += 1
    if shown == 0:
        print("no messages matched", file=sys.stderr)
        return 1
    return 0


def cmd_export(args: argparse.Namespace) -> int:
    reader = journal.JournalReader(args.capture)
    written = 0
    out = open(args.out, "w", encoding="utf-8") if args.out else sys.stdout
    try:
        for message in reader.messages(topic_filter=args.topic, limit=args.limit):
            row = {
                "ts_ns": message.ts_ns,
                "utc": message.utc,
                "topic": message.topic,
                "qos": message.qos,
                "retain": message.retain,
                "len": message.size,
            }
            if args.text:
                try:
                    row["text"] = message.payload.decode("utf-8")
                    row["encoding"] = "utf-8"
                except UnicodeDecodeError:
                    row["payload_b64"] = base64.b64encode(message.payload).decode("ascii")
                    row["encoding"] = "base64"
            else:
                row["payload_b64"] = base64.b64encode(message.payload).decode("ascii")
                row["encoding"] = "base64"
            out.write(json.dumps(row, ensure_ascii=False) + "\n")
            written += 1
    finally:
        if args.out:
            out.close()
    if args.out:
        print(f"{written} row(s) -> {args.out}", file=sys.stderr)
    return 0


def cmd_decode(args: argparse.Namespace) -> int:
    """Run a user-supplied decoder over the capture and print what it returns."""
    module_path, _, func_name = args.decoder.partition(":")
    func_name = func_name or "decode"
    path = Path(module_path).expanduser()
    if not path.is_file():
        raise SystemExit(f"decoder module {path} does not exist")
    spec = importlib.util.spec_from_file_location("xpeng_user_decoder", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    func = getattr(module, func_name, None)
    if not callable(func):
        raise SystemExit(f"{path} has no callable {func_name}() - expected signature "
                         "decode(topic, payload, ts_ns) -> dict")

    reader = journal.JournalReader(args.capture)
    shown = 0
    for message in reader.messages(topic_filter=args.topic, limit=args.limit):
        result = func(message.topic, message.payload, message.ts_ns)
        if result is None and args.skip_none:
            continue
        print(json.dumps({"utc": message.utc, "topic": message.topic, "decoded": result},
                         ensure_ascii=False, default=str))
        shown += 1
    if shown == 0:
        print("the decoder returned nothing for every message", file=sys.stderr)
        return 1
    return 0


# -- cli -------------------------------------------------------------------- #


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Read an .xpgcan capture offline.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Start with `shapes` for an unfamiliar stream: it reduces a firehose to "
            "its distinct payloads, which is where decoding actually begins."
        ),
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("summary", help="counts, rates, identity, integrity, transport notes")
    p.add_argument("capture")
    p.add_argument("--json", action="store_true")
    p.add_argument("--top", type=int, default=15, help="rows to show (default: 15)")
    p.set_defaults(func=cmd_summary)

    p = sub.add_parser("topics", help="every topic in the capture, with counts")
    p.add_argument("capture")
    p.set_defaults(func=cmd_topics)

    p = sub.add_parser("notes", help="the transport's own events, as JSON lines")
    p.add_argument("capture")
    p.set_defaults(func=cmd_notes)

    p = sub.add_parser(
        "shapes",
        help="distinct payload shapes per topic: constant bytes, and where they vary",
    )
    p.add_argument("capture")
    p.add_argument("--topic", default=None,
                   help="topic selector: MQTT filter (Xmart/Car/+/...), a path prefix, or a substring")
    p.add_argument("--max-shapes", type=int, default=20)
    p.add_argument("--exact", type=int, default=0,
                   help="also list this many most-common exact payloads per shape")
    p.add_argument("--sample-bytes", type=int, default=64)
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_shapes)

    p = sub.add_parser("dump", help="the frames themselves")
    p.add_argument("capture")
    p.add_argument("--topic", default=None,
                   help="topic selector: MQTT filter (Xmart/Car/+/...), a path prefix, or a substring")
    p.add_argument("--limit", type=int, default=0, help="stop after N (0 = all)")
    p.add_argument("--format", choices=("text", "hex", "json", "raw"), default="hex")
    p.add_argument("--max-bytes", type=int, default=2000, help="text mode truncation")
    p.add_argument("--since", default="", help="ISO date/time or epoch seconds")
    p.add_argument("--until", default="", help="ISO date/time or epoch seconds")
    p.set_defaults(func=cmd_dump)

    p = sub.add_parser("frames", help="structural view at a fixed stride (names nothing)")
    p.add_argument("capture")
    p.add_argument("--topic", default=None, help="topic selector (filter, prefix or substring)")
    p.add_argument("--limit", type=int, default=3)
    p.add_argument("--stride", type=int, default=8, help="bytes per word (0 = hexdump)")
    p.set_defaults(func=cmd_frames)

    p = sub.add_parser("export", help="newline-delimited JSON for pandas/jq")
    p.add_argument("capture")
    p.add_argument("--out", default="", help="file to write (default: stdout)")
    p.add_argument("--topic", default=None, help="topic selector (filter, prefix or substring)")
    p.add_argument("--limit", type=int, default=0)
    p.add_argument("--text", action="store_true", help="decode UTF-8 payloads as text")
    p.set_defaults(func=cmd_export)

    p = sub.add_parser("decode", help="run your own decoder module over the capture")
    p.add_argument("capture")
    p.add_argument("--decoder", required=True, help="path/to/module.py[:function]")
    p.add_argument("--topic", default=None, help="topic selector (filter, prefix or substring)")
    p.add_argument("--limit", type=int, default=0)
    p.add_argument("--skip-none", action="store_true")
    p.set_defaults(func=cmd_decode)

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except journal.JournalError as err:
        print(f"capture: {err}", file=sys.stderr)
        return 2
    except BrokenPipeError:
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
