#!/usr/bin/env python3
"""Append-only binary journal for raw XPENG MQTT frames.

It lives in the integration rather than in ``tools/`` because both sides need it:
the integration writes captures from the MQTT connection it already holds (see
``capture.py``), and ``tools/xpeng_can_decode.py`` reads them back offline. The
offline tools load this file by path, the way they already load ``xpeng_api.py``
and ``mqtt_client.py``, so there is one format and one implementation of it.

It is deliberately stdlib-only and imports nothing from Home Assistant: the
decoder and the tests have to be able to load it without a Home Assistant to run.

Why a journal at all, and why binary
------------------------------------
The MQTT listener inside the integration is a *diagnostic*: it keeps a 4 KB
preview of the last message, a count and a rolling text log, and it coalesces
the expensive half to one event per second (``LISTENER_UPDATE_INTERVAL``). That
is the right shape for "is anything arriving on this topic?", and the wrong
shape for "record the CAN stream so I can decode it offline":

* a text log cannot hold arbitrary bytes faithfully - the listener already
  decodes every payload as UTF-8 with ``errors="replace"``, which turns a binary
  CAN pack into a lossy string before anything else sees it;
* one line per frame is the flood that wedged Home Assistant on 0.4.30, and any
  downstream consumer has to re-parse it;
* per-frame arrival time is what a rate analysis needs, and a coalesced or
  line-timestamped log has already thrown it away.

So a capture is an append-only binary file, written by the recorder and read by
an offline decoder. Nothing in the write path parses, decodes or interprets the
payload; the bytes go in exactly as the broker delivered them.

Layout, version 1
-----------------
Little-endian throughout::

    header  : magic(8) | version(u16) | flags(u16) | meta_len(u32) | meta(json)
    record* : type(u8) | len(u32) | body(len)

    MSG   (1): ts_ns(u64) | topic_id(u16) | qos(u8) | flags(u8) | n(u32) | payload
    TOPIC (2): topic_id(u16) | n(u16) | topic(utf-8)
    NOTE  (3): ts_ns(u64) | n(u32) | json(utf-8)
    END   (4): ts_ns(u64) | n(u32) | json(utf-8)

Design notes that are load-bearing:

* **Topic ids, not topic strings.** Against ``Xmart/Car/+/State/CanPack1s`` the
  topic repeats on every frame and is longer than many payloads. A TOPIC record
  is emitted the first time a topic is seen and the id is reused. The table
  lives only in the file, so a capture is self-contained.
* **A truncated tail is normal, not corruption.** The file is flushed on an
  interval, so a crash can leave a partial record. ``JournalReader`` stops at
  the first record that does not fit and marks the capture truncated rather than
  raising - a capture that recorded 40 of 60 seconds is worth having.
* **``END`` is in-band.** The ``.index.json`` sidecar is for humans and other
  tools; the END record means the summary survives losing it.
* ``ts_ns`` is ``time.time_ns()`` at arrival in the *reader* process; MQTT 3.1.1
  carries no broker timestamp. Anything rate-shaped therefore measures our
  arrival, which is what a self-DoS question is actually about.

This module is stdlib-only and imports neither paho nor the integration, so the
offline decoder and the tests can use it without a network stack installed.
"""

from __future__ import annotations

import json
import os
import struct
import time
from dataclasses import dataclass
from typing import Any, BinaryIO, Iterator

MAGIC = b"XPENGCAP"
VERSION = 1
FLAG_HEADER_NONE = 0

REC_MSG = 1
REC_TOPIC = 2
REC_NOTE = 3
REC_END = 4

RECORD_NAMES = {
    REC_MSG: "msg",
    REC_TOPIC: "topic",
    REC_NOTE: "note",
    REC_END: "end",
}

#: Message flag bits in the MSG body.
MSG_FLAG_RETAIN = 1 << 0
MSG_FLAG_DUP = 1 << 1

MAX_TOPIC_ID = 0xFFFF
MAX_TOPIC_LEN = 0xFFFF
MAX_BODY_LEN = 0xFFFFFFFF

_HEADER = struct.Struct("<8sHHI")
_RECORD_HEAD = struct.Struct("<BI")
_MSG = struct.Struct("<QHBBI")
_TOPIC = struct.Struct("<HH")
_NOTE = struct.Struct("<QI")


def utc_now() -> str:
    """ISO-8601 UTC, the shape the rest of the repo reports timestamps in."""
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def ns_to_utc(ts_ns: int) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(ts_ns / 1e9))


class JournalError(RuntimeError):
    """A capture could not be written, or is not a capture at all."""


@dataclass
class CaptureMessage:
    """One frame, as read back from a capture."""

    ts_ns: int
    topic: str
    payload: bytes
    qos: int = 0
    retain: bool = False
    dup: bool = False

    @property
    def utc(self) -> str:
        return ns_to_utc(self.ts_ns)

    @property
    def size(self) -> int:
        return len(self.payload)


@dataclass
class TopicStat:
    """Per-topic counters, kept live by the writer and recomputed by the reader."""

    topic: str
    count: int = 0
    bytes: int = 0
    first_ns: int = 0
    last_ns: int = 0

    def add(self, ts_ns: int, size: int) -> None:
        if self.count == 0:
            self.first_ns = ts_ns
        self.count += 1
        self.bytes += size
        self.last_ns = ts_ns

    def as_dict(self) -> dict[str, Any]:
        span = max(0, self.last_ns - self.first_ns) / 1e9 if self.count > 1 else 0.0
        return {
            "topic": self.topic,
            "count": self.count,
            "bytes": self.bytes,
            "mean_bytes": round(self.bytes / self.count, 1) if self.count else 0.0,
            "first_utc": ns_to_utc(self.first_ns) if self.first_ns else None,
            "last_utc": ns_to_utc(self.last_ns) if self.last_ns else None,
            # Computed over the topic's own observed span, so a topic that
            # appeared late is not diluted by the ones present from the start.
            "rate_hz": round(self.count / span, 4) if span > 0 else None,
        }


@dataclass
class ScanResult:
    """Everything a single pass over a capture can tell you."""

    meta: dict[str, Any] = None  # type: ignore[assignment]
    topics: list[str] = None  # type: ignore[assignment]
    stats: dict[str, TopicStat] = None  # type: ignore[assignment]
    notes: list[dict[str, Any]] = None  # type: ignore[assignment]
    end_record: dict[str, Any] | None = None
    messages: int = 0
    payload_bytes: int = 0
    truncated: bool = False
    trailing_bytes: int = 0
    first_ns: int = 0
    last_ns: int = 0
    malformed: int = 0

    def as_dict(self, path: str) -> dict[str, Any]:
        span = (self.last_ns - self.first_ns) / 1e9 if self.messages else 0.0
        topics = [self.stats[t].as_dict() for t in sorted(self.stats)]
        return {
            "capture_file": os.path.abspath(path),
            "journal_version": (self.meta or {}).get("journal_version", VERSION),
            "meta": self.meta or {},
            "messages": self.messages,
            "payload_bytes": self.payload_bytes,
            "file_bytes": os.path.getsize(path) if os.path.exists(path) else 0,
            "topic_count": len(topics),
            "topics": topics,
            "notes": self.notes or [],
            "end_record": self.end_record,
            "truncated": self.truncated,
            "trailing_bytes": self.trailing_bytes,
            "malformed_records": self.malformed,
            "first_utc": ns_to_utc(self.first_ns) if self.first_ns else None,
            "last_utc": ns_to_utc(self.last_ns) if self.last_ns else None,
            "duration_s": round(span, 3),
            "rate_hz": round(self.messages / span, 4) if span > 0 else None,
        }


class JournalWriter:
    """Append raw frames to a capture file.

    One writer, one file, one thread. ``message()`` is expected to be called from
    the MQTT client's callback thread, so the only work it does is a dict lookup,
    a pack and a buffered write - no parsing, no logging, and no decoded copy of
    the payload.
    """

    def __init__(
        self,
        path: str,
        meta: dict[str, Any] | None = None,
        *,
        flush_interval: float = 1.0,
    ) -> None:
        self.path = path
        self.flush_interval = max(0.0, flush_interval)
        self._meta = dict(meta or {})
        self._meta.setdefault("created_utc", utc_now())
        self._meta.setdefault("journal_version", VERSION)

        parent = os.path.dirname(os.path.abspath(path))
        if parent:
            os.makedirs(parent, exist_ok=True)

        self._fh: BinaryIO = open(path, "wb")
        meta_bytes = json.dumps(self._meta, ensure_ascii=False).encode("utf-8")
        self._fh.write(_HEADER.pack(MAGIC, VERSION, FLAG_HEADER_NONE, len(meta_bytes)))
        self._fh.write(meta_bytes)

        self._topic_ids: dict[str, int] = {}
        self._stats: dict[str, TopicStat] = {}
        self._notes: list[dict[str, Any]] = []
        self._records = 0
        self._messages = 0
        self._bytes = 0  # payload bytes, i.e. what the broker delivered
        self._opened_ns = time.time_ns()
        self._last_flush = time.monotonic()
        self._closed = False
        self._summary: dict[str, Any] = {}

    # -- read-only views ---------------------------------------------------- #

    @property
    def topics(self) -> list[str]:
        return list(self._topic_ids)

    @property
    def message_count(self) -> int:
        return self._messages

    @property
    def payload_bytes(self) -> int:
        return self._bytes

    @property
    def notes(self) -> list[dict[str, Any]]:
        return list(self._notes)

    @property
    def closed(self) -> bool:
        return self._closed

    def stat(self, topic: str) -> TopicStat | None:
        return self._stats.get(topic)

    # -- writing ------------------------------------------------------------ #

    def _write_record(self, rec_type: int, body: bytes) -> None:
        self._fh.write(_RECORD_HEAD.pack(rec_type, len(body)))
        self._fh.write(body)
        self._records += 1

    def _define_topic(self, topic: str) -> int:
        topic_id = len(self._topic_ids)
        if topic_id > MAX_TOPIC_ID:
            raise JournalError(f"more than {MAX_TOPIC_ID} distinct topics in one capture")
        raw = topic.encode("utf-8")
        if len(raw) > MAX_TOPIC_LEN:
            raise JournalError(f"topic longer than {MAX_TOPIC_LEN} bytes: {topic[:60]!r}")
        self._write_record(REC_TOPIC, _TOPIC.pack(topic_id, len(raw)) + raw)
        self._topic_ids[topic] = topic_id
        return topic_id

    def message(
        self,
        topic: str,
        payload: bytes,
        *,
        ts_ns: int | None = None,
        qos: int = 0,
        retain: bool = False,
        dup: bool = False,
    ) -> None:
        """Append one frame. The payload is stored verbatim."""
        if self._closed:
            raise JournalError("journal is closed")
        ts_ns = time.time_ns() if ts_ns is None else int(ts_ns)
        size = len(payload)
        if size > MAX_BODY_LEN:
            raise JournalError(f"payload of {size} bytes exceeds the format's {MAX_BODY_LEN}")

        topic_id = self._topic_ids.get(topic)
        if topic_id is None:
            topic_id = self._define_topic(topic)

        flags = (MSG_FLAG_RETAIN if retain else 0) | (MSG_FLAG_DUP if dup else 0)
        body = _MSG.pack(ts_ns, topic_id, int(qos) & 0xFF, flags, size) + bytes(payload)
        self._write_record(REC_MSG, body)

        self._messages += 1
        self._bytes += size
        stat = self._stats.get(topic)
        if stat is None:
            stat = self._stats[topic] = TopicStat(topic=topic)
        stat.add(ts_ns, size)

        if self.flush_interval and time.monotonic() - self._last_flush >= self.flush_interval:
            self.flush()

    def note(self, event: str, **fields: Any) -> None:
        """Record something that is not a frame: a reconnect, a refusal, a drop.

        A capture that silently lost a minute to a credential rotation is worse
        than no capture, so the transport's own events belong in the file next to
        the frames they explain.
        """
        if self._closed:
            raise JournalError("journal is closed")
        ts_ns = time.time_ns()
        entry = {"event": event, "utc": ns_to_utc(ts_ns), **fields}
        raw = json.dumps(entry, ensure_ascii=False).encode("utf-8")
        self._write_record(REC_NOTE, _NOTE.pack(ts_ns, len(raw)) + raw)
        self._notes.append(entry)

    def marker(self, text: str) -> None:
        """A free-text note, for ``--mark`` and for operator actions."""
        self.note("marker", text=text)

    def flush(self) -> None:
        if not self._closed:
            self._fh.flush()
            self._last_flush = time.monotonic()

    # -- closing ------------------------------------------------------------ #

    def summary(self, reason: str = "", **extra: Any) -> dict[str, Any]:
        """The capture's own account of itself, as written to the index sidecar."""
        ended_ns = time.time_ns()
        topics = [self._stats[t].as_dict() for t in sorted(self._stats)]
        return {
            "capture_file": os.path.abspath(self.path),
            "journal_version": VERSION,
            "meta": self._meta,
            "reason": reason or "closed",
            "started_utc": ns_to_utc(self._opened_ns),
            "ended_utc": ns_to_utc(ended_ns),
            "duration_s": round((ended_ns - self._opened_ns) / 1e9, 3),
            "records": self._records,
            "messages": self._messages,
            "payload_bytes": self._bytes,
            "topic_count": len(topics),
            "topics": topics,
            "notes": self._notes,
            **extra,
        }

    def close(self, reason: str = "", **extra: Any) -> dict[str, Any]:
        """Write the END record and the index sidecar, and return the summary.

        Idempotent: the recorder calls this from a signal handler *and* from its
        normal exit path, and a second call must not append a second END.
        """
        if self._closed:
            return self._summary

        summary = self.summary(reason, **extra)
        raw = json.dumps(summary, ensure_ascii=False).encode("utf-8")
        self._write_record(REC_END, _NOTE.pack(time.time_ns(), len(raw)) + raw)
        self._fh.flush()
        self._fh.close()
        self._closed = True

        # file_bytes can only be known once the END record is on disk.
        summary["file_bytes"] = os.path.getsize(self.path)
        self._summary = summary

        index_path = self.path + ".index.json"
        try:
            with open(index_path, "w", encoding="utf-8") as handle:
                json.dump(summary, handle, indent=2, ensure_ascii=False)
                handle.write("\n")
        except OSError:
            # Losing the human-readable index is not worth failing a capture over;
            # the END record in the file carries the same summary.
            pass
        return summary

    def __enter__(self) -> "JournalWriter":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        if not self._closed:
            self.close(reason="exception" if exc_type else "closed")
        return None


class JournalReader:
    """Read a capture back. Never raises on a truncated tail.

    ``path`` may be a ``.xpgcan`` file or its ``.index.json`` sidecar; the
    sidecar resolves to the capture beside it, because reaching for the sidecar
    and being told "that is not a capture" is a pointless stumble.
    """

    def __init__(self, path: str) -> None:
        self.path = str(path)
        if self.path.endswith(".index.json"):
            candidate = self.path[: -len(".index.json")]
            if os.path.exists(candidate):
                self.path = candidate
        self.meta: dict[str, Any] = {}
        self.version = VERSION
        self.truncated = False
        self.trailing_bytes = 0
        self.malformed = 0

    # -- records ------------------------------------------------------------ #

    def iter_records(self) -> Iterator[tuple[int, bytes]]:
        """Yield ``(record_type, body)`` in file order, stopping at a bad tail."""
        self.truncated = False
        self.trailing_bytes = 0
        self.malformed = 0
        try:
            handle = open(self.path, "rb")
        except OSError as err:
            raise JournalError(f"{self.path}: cannot be read ({err.strerror})") from err
        with handle:
            head = handle.read(_HEADER.size)
            if len(head) < _HEADER.size:
                raise JournalError(
                    f"{self.path}: too short to be a capture "
                    f"({len(head)} bytes, expected at least {_HEADER.size})"
                )
            magic, version, _flags, meta_len = _HEADER.unpack(head)
            if magic != MAGIC:
                raise JournalError(
                    f"{self.path}: bad magic {magic!r}, expected {MAGIC!r}. "
                    "Is this an .xpgcan capture, or an index.json beside one?"
                )
            self.version = version
            raw_meta = handle.read(meta_len)
            if len(raw_meta) < meta_len:
                raise JournalError(f"{self.path}: header says {meta_len} meta bytes, file has fewer")
            try:
                self.meta = json.loads(raw_meta.decode("utf-8"))
            except (UnicodeDecodeError, ValueError) as err:
                raise JournalError(f"{self.path}: header metadata is not JSON: {err}") from err

            while True:
                record_head = handle.read(_RECORD_HEAD.size)
                if not record_head:
                    return  # clean EOF
                if len(record_head) < _RECORD_HEAD.size:
                    self.truncated = True
                    self.trailing_bytes = len(record_head)
                    return
                rec_type, body_len = _RECORD_HEAD.unpack(record_head)
                body = handle.read(body_len)
                if len(body) < body_len:
                    self.truncated = True
                    self.trailing_bytes = len(body)
                    return
                yield rec_type, body

    def messages(
        self,
        *,
        topic_filter: str | None = None,
        topics_only: list[str] | None = None,
        limit: int | None = None,
        since_ns: int | None = None,
        until_ns: int | None = None,
    ) -> Iterator[CaptureMessage]:
        """Yield frames, resolving topic ids as the TOPIC records go past.

        ``topic_filter`` is an MQTT-style pattern (``+`` one level, ``#`` the
        rest); ``topics_only`` is an exact allow-list. Both are applied here
        rather than by the caller so a filtered dump is still one pass.
        """
        table: dict[int, str] = {}
        yielded = 0
        for rec_type, body in self.iter_records():
            if rec_type == REC_TOPIC:
                if len(body) < _TOPIC.size:
                    self.malformed += 1
                    continue
                topic_id, name_len = _TOPIC.unpack_from(body, 0)
                name = body[_TOPIC.size : _TOPIC.size + name_len].decode("utf-8", "replace")
                table[topic_id] = name
                continue
            if rec_type != REC_MSG:
                continue
            if len(body) < _MSG.size:
                self.malformed += 1
                continue
            ts_ns, topic_id, qos, flags, payload_len = _MSG.unpack_from(body, 0)
            payload = body[_MSG.size : _MSG.size + payload_len]
            topic = table.get(topic_id, f"<undefined-topic-{topic_id}>")

            if topics_only and topic not in topics_only:
                continue
            if topic_filter and not topic_matches(topic_filter, topic):
                continue
            if since_ns is not None and ts_ns < since_ns:
                continue
            if until_ns is not None and ts_ns > until_ns:
                continue

            yield CaptureMessage(
                ts_ns=ts_ns,
                topic=topic,
                payload=payload,
                qos=qos,
                retain=bool(flags & MSG_FLAG_RETAIN),
                dup=bool(flags & MSG_FLAG_DUP),
            )
            yielded += 1
            # Falsy limit means "no limit": 0 is what a CLI passes for "all", and
            # reading it as a count would silently return exactly one frame.
            if limit and yielded >= limit:
                return

    def scan(self) -> ScanResult:
        """One full pass: counters, notes, the END record and the meta block."""
        result = ScanResult(stats={}, notes=[], topics=[])
        table: dict[int, str] = {}
        saw_end = False
        for rec_type, body in self.iter_records():
            if rec_type == REC_TOPIC:
                if len(body) < _TOPIC.size:
                    self.malformed += 1
                    continue
                topic_id, name_len = _TOPIC.unpack_from(body, 0)
                name = body[_TOPIC.size : _TOPIC.size + name_len].decode("utf-8", "replace")
                table[topic_id] = name
                if name not in result.topics:
                    result.topics.append(name)
                continue
            if rec_type == REC_NOTE:
                if len(body) < _NOTE.size:
                    self.malformed += 1
                    continue
                _ts, blen = _NOTE.unpack_from(body, 0)
                try:
                    result.notes.append(json.loads(body[_NOTE.size : _NOTE.size + blen]))
                except (UnicodeDecodeError, ValueError):
                    self.malformed += 1
                continue
            if rec_type == REC_END:
                if len(body) < _NOTE.size:
                    self.malformed += 1
                    continue
                _ts, blen = _NOTE.unpack_from(body, 0)
                try:
                    result.end_record = json.loads(body[_NOTE.size : _NOTE.size + blen])
                except (UnicodeDecodeError, ValueError):
                    self.malformed += 1
                saw_end = True
                continue
            if rec_type != REC_MSG:
                self.malformed += 1
                continue
            if len(body) < _MSG.size:
                self.malformed += 1
                continue
            ts_ns, topic_id, _qos, _flags, payload_len = _MSG.unpack_from(body, 0)
            # A MSG naming a topic we never saw a TOPIC record for is malformed;
            # say so with an obviously synthetic name rather than letting it look
            # like a real topic someone can go and subscribe to.
            name = table.get(topic_id, f"<undefined-topic-{topic_id}>")
            stat = result.stats.get(name)
            if stat is None:
                stat = result.stats[name] = TopicStat(topic=name)
            stat.add(ts_ns, payload_len)
            result.messages += 1
            result.payload_bytes += payload_len
            if not result.first_ns:
                result.first_ns = ts_ns
            result.last_ns = ts_ns

        result.truncated = self.truncated
        result.trailing_bytes = self.trailing_bytes
        result.malformed += self.malformed
        # A capture that never got its END record is the normal outcome of a
        # power cut or a kill, and is worth saying out loud rather than inferring.
        result.meta = dict(self.meta or {})
        result.meta["end_record_present"] = saw_end
        return result


def topic_matches(pattern: str, topic: str) -> bool:
    """Whether a ``--topic`` selector picks out a topic.

    Three shapes, because three things get typed:

    * ``Xmart/Car/+/State/CanPack1s`` or ``Xmart/#`` - an MQTT filter, matched
      level by level. ``+`` is one level, ``#`` is the rest of the subtree.
    * ``Xmart/Car/94434`` - a path prefix, which is what someone who has just
      read a topic out of ``topics`` types when they mean that car.
    * ``MsgV2`` - a bare substring, which is what someone types when they do not
      remember the path at all.

    A pattern without a wildcard is never required to be an exact match: being
    strict about it only ever produces an empty result that looks like a broken
    capture.
    """
    if not pattern:
        return True
    if "+" in pattern or "#" in pattern:
        if pattern == topic:
            return True
        pattern_levels = pattern.split("/")
        topic_levels = topic.split("/")
        for index, part in enumerate(pattern_levels):
            if part == "#":
                return True
            if index >= len(topic_levels):
                return False
            if part == "+":
                continue
            if part != topic_levels[index]:
                return False
        return len(pattern_levels) == len(topic_levels)
    if "/" in pattern:
        trimmed = pattern.rstrip("/")
        return topic == trimmed or topic.startswith(trimmed + "/")
    return pattern in topic


#: Suffix used for the human-readable index beside a capture.
INDEX_SUFFIX = ".index.json"

__all__ = [
    "MAGIC",
    "VERSION",
    "REC_MSG",
    "REC_TOPIC",
    "REC_NOTE",
    "REC_END",
    "RECORD_NAMES",
    "MSG_FLAG_RETAIN",
    "MSG_FLAG_DUP",
    "MAX_TOPIC_ID",
    "MAX_TOPIC_LEN",
    "MAX_BODY_LEN",
    "INDEX_SUFFIX",
    "JournalError",
    "CaptureMessage",
    "TopicStat",
    "ScanResult",
    "JournalWriter",
    "JournalReader",
    "topic_matches",
    "is_capture",
    "utc_now",
    "ns_to_utc",
]


def is_capture(path: str) -> bool:
    """True when ``path`` starts with the capture magic, without reading it all."""
    try:
        with open(path, "rb") as handle:
            return handle.read(len(MAGIC)) == MAGIC
    except OSError:
        return False
