"""Record the MQTT frames the integration already receives, for offline decoding.

Why this exists
---------------
``mqtt_listener_enabled`` + ``mqtt_topic_discovery`` subscribe to the CAN topics,
and against ``#`` that means the whole broker, including the car's 1 Hz
``CanPack1s`` broadcast. 0.4.30 did the expensive work per frame - an HA event, a
full coordinator update that re-renders every entity, and the recorder rows that
follow - and that wedged Home Assistant. 0.4.32 coalesced it to one update per
second.

Coalescing is right for the diagnostic sensors and wrong for a recording: a
recording whose per-frame timing has been smoothed away cannot answer the
questions it exists to answer (rate, gaps, drops). What a recording needs is the
bytes and their arrival times, and neither of those needs Home Assistant.

So this module is the whole hot path: ``submit()`` puts the topic and payload on a
bounded queue and returns. It does not decode, does not log per frame, does not
touch the event loop and never blocks. A single writer thread does the framing,
the file I/O, rotation and flushing - off the loop, out of the callback. The bytes
are interpreted later, by ``tools/xpeng_can_decode.py``, against the file.

Why it cannot wedge Home Assistant the way 0.4.30 did
-----------------------------------------------------
Every unbounded thing in that failure is bounded here:

* the queue has a fixed size; when it is full the frame is **dropped and counted**,
  never queued behind an unbounded backlog;
* nothing per-frame allocates a decoded copy of the payload;
* the writer owns all the file I/O, so a slow disk cannot stall the MQTT callback
  thread - it fills the queue, and the drop counter records it;
* the counter is the point: a capture that silently lost frames would be worse
  than no capture, so drops are recorded in the journal as notes and reported on a
  diagnostic sensor.

Nothing here imports Home Assistant, so it runs - and is tested - without one.
"""

from __future__ import annotations

import os
import queue
import threading
import time
from typing import Any, Callable, Iterable

try:  # normal package import, i.e. inside Home Assistant
    from .can_journal import JournalError, JournalWriter, utc_now
except ImportError:  # loaded as a top-level module by tests/ (the repo's convention)
    from can_journal import JournalError, JournalWriter, utc_now  # type: ignore

#: Frames buffered between the MQTT callback thread and the writer. At the 1 Hz CAN
#: rate this is hours of slack; against ``#`` it is seconds, which is the point -
#: the bound is what turns "Home Assistant is behind" into a counted drop.
DEFAULT_QUEUE_FRAMES = 20_000

#: One journal part. A capture that outgrows this rolls to ``-part2`` and so on
#: rather than becoming one unrecoverable file.
DEFAULT_ROTATE_BYTES = 256 * 1024 * 1024

#: Hard stop for the whole capture. Reaching it ends the capture and says so.
DEFAULT_MAX_BYTES = 2048 * 1024 * 1024

#: Frames handled per wake-up. The queue is the buffer; batching just keeps the
#: number of write() calls proportional to the data rather than to the frame count.
_BATCH_FRAMES = 512

#: How often a note about drops is recorded. One note per drop would be the flood
#: this module exists to avoid; none at all would hide the loss.
_DROP_NOTE_EVERY = 1000


def capture_filename(when: time.struct_time | None = None) -> str:
    """``xpeng-can-20261016T045424Z.xpgcan`` - sortable, and UTC like the rest."""
    return "xpeng-can-" + time.strftime("%Y%m%dT%H%M%SZ", when or time.gmtime()) + ".xpgcan"


class CaptureRecorder:
    """A bounded-queue writer for raw MQTT frames.

    Lifecycle: ``start()``, any number of ``submit()`` calls from the MQTT
    callback thread, then ``stop()``. ``submit()`` is safe before ``start()`` and
    after ``stop()`` - the first is a counted drop rather than a queue nobody will
    drain, so a callback racing a config-entry reload cannot raise into paho.
    """

    def __init__(
        self,
        directory: str,
        *,
        topics: Iterable[str] = (),
        queue_frames: int = DEFAULT_QUEUE_FRAMES,
        rotate_bytes: int = DEFAULT_ROTATE_BYTES,
        max_bytes: int = DEFAULT_MAX_BYTES,
        flush_interval: float = 1.0,
        clock: Callable[[], int] = time.time_ns,
    ) -> None:
        self.directory = directory
        self.topics = list(topics)
        self.rotate_bytes = max(0, int(rotate_bytes))
        self.max_bytes = max(0, int(max_bytes))
        self.flush_interval = max(0.0, float(flush_interval))
        self._clock = clock

        self._queue: queue.Queue[tuple] = queue.Queue(maxsize=max(1, int(queue_frames)))
        self._thread: threading.Thread | None = None
        self._closing = threading.Event()
        # Distinct from _closing: stop() means "drain what was accepted", a cap
        # means "stop now". Conflating them lets the writer keep draining a backlog
        # straight past the cap, which is what makes a size limit meaningless.
        self._hard_stop = threading.Event()
        self._stop_reason = ""
        self._writer: JournalWriter | None = None
        self._parts: list[str] = []
        self._part = 1
        self._summary: dict[str, Any] = {}

        self.started_utc: str | None = None
        self.finished_utc: str | None = None
        self.last_error: str = ""

        self.frames = 0          # written to the journal
        self.dropped = 0         # refused because the queue was full
        self.payload_bytes = 0   # written payload bytes
        self._dropped_since_note = 0

    # -- read-only views ---------------------------------------------------- #

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    @property
    def current_file(self) -> str:
        return self._writer.path if self._writer else (self._parts[-1] if self._parts else "")

    @property
    def parts(self) -> list[str]:
        return list(self._parts)

    def stats(self) -> dict[str, Any]:
        """What the diagnostic sensor reports. Cheap enough to call from the loop."""
        return {
            "running": self.running,
            "directory": self.directory,
            "current_file": self.current_file,
            "parts": len(self._parts),
            "frames": self.frames,
            "dropped": self.dropped,
            "payload_bytes": self.payload_bytes,
            "queued": self._queue.qsize(),
            "queue_capacity": self._queue.maxsize,
            "started_utc": self.started_utc,
            "finished_utc": self.finished_utc,
            "topics": len(self.topics),
            "last_error": self.last_error,
        }

    # -- hot path ----------------------------------------------------------- #

    def submit(
        self,
        topic: str,
        payload: bytes,
        *,
        qos: int = 0,
        retain: bool = False,
        ts_ns: int | None = None,
    ) -> bool:
        """Enqueue one frame. Never blocks, never raises. False means dropped.

        Called from paho's network thread, so the work here is the enqueue and - on
        the drop path - an integer increment and, occasionally, another enqueue.
        """
        if not self.running or self._closing.is_set() or self._hard_stop.is_set():
            self.dropped += 1
            return False
        try:
            self._queue.put_nowait(
                ("frame", topic, payload, int(qos), bool(retain), ts_ns or self._clock())
            )
            return True
        except queue.Full:
            self.dropped += 1
            self._dropped_since_note += 1
            if self._dropped_since_note >= _DROP_NOTE_EVERY:
                self._dropped_since_note = 0
                self._note("frames_dropped", total=self.dropped)
            return False

    # -- lifecycle ---------------------------------------------------------- #

    def start(self) -> None:
        """Open the first part and start the writer thread.

        Raises whatever ``makedirs`` or the first ``open`` raises; the caller
        decides about fallback directories, because only it knows what else is
        writable.
        """
        if self.running:
            return
        os.makedirs(self.directory, exist_ok=True)
        self._closing.clear()
        self._hard_stop.clear()
        self._stop_reason = ""
        self._open_part()
        self.started_utc = utc_now()
        self._thread = threading.Thread(target=self._run, name="xpeng-capture", daemon=True)
        self._thread.start()

    def stop(self, timeout: float = 10.0, reason: str = "stopped") -> dict[str, Any]:
        """Drain what is queued, close the part, and return the summary.

        Blocking is fine here - the integration calls it from an executor - but it
        is bounded, because an unload that hangs is worse than a truncated tail,
        which the journal format already tolerates.
        """
        self._stop_reason = reason
        thread = self._thread
        if thread is not None:
            self._closing.set()
            thread.join(timeout=timeout)
            if thread.is_alive() and not self.last_error:
                self.last_error = (
                    f"the writer did not finish within {timeout}s; the capture may "
                    "have a truncated tail"
                )
        self._thread = None
        self.finished_utc = utc_now()
        self._summary.update(
            frames=self.frames,
            dropped=self.dropped,
            payload_bytes=self.payload_bytes,
            parts=list(self._parts),
        )
        return dict(self._summary)

    # -- writer thread ------------------------------------------------------ #

    def _part_path(self, part: int) -> str:
        name = capture_filename()
        if part > 1:
            stem, ext = os.path.splitext(name)
            name = f"{stem}-part{part}{ext}"
        return os.path.join(self.directory, name)

    def _open_part(self) -> None:
        path = self._part_path(self._part)
        self._writer = JournalWriter(
            path,
            {
                "tool": "xpeng integration",
                "recorder": "custom_components/xpeng/capture.py",
                "source": "the integration's own MQTT connection",
                "subscribed": self.topics,
                "part": self._part,
                "opened_utc": utc_now(),
            },
            flush_interval=self.flush_interval,
        )
        self._parts.append(path)

    def _close_part(self, reason: str) -> dict[str, Any] | None:
        writer, self._writer = self._writer, None
        if writer is None:
            return None
        try:
            summary = writer.close(
                reason=reason,
                frames=self.frames,
                dropped=self.dropped,
                payload_bytes=self.payload_bytes,
            )
            self._summary = summary
            return summary
        except (JournalError, OSError) as err:
            self.last_error = f"{type(err).__name__}: {err}"
            return None

    def note(self, event: str, **fields: Any) -> None:
        """Record something that is not a frame: a refusal, a rotation, a reload.

        Public because the integration records subscription results here, and a
        topic the broker refuses is the most useful single thing a capture can say:
        from outside, a refused subscription and a silent one look identical.
        Non-blocking and never raises, like ``submit()``.
        """
        self._note(event, **fields)

    def _note(self, event: str, **fields: Any) -> None:
        """Queue a note. Only the writer thread ever touches the file."""
        try:
            self._queue.put_nowait(("note", event, fields))
        except queue.Full:
            # Losing the note is acceptable: the counters it would have described
            # are in the summary and on the sensor either way.
            pass

    def _run(self) -> None:
        try:
            while not self._hard_stop.is_set():
                try:
                    item = self._queue.get(timeout=0.2)
                except queue.Empty:
                    self._maybe_flush()
                    if self._closing.is_set():
                        break
                    continue

                self._handle(item)
                for _ in range(_BATCH_FRAMES - 1):
                    try:
                        self._handle(self._queue.get_nowait())
                    except queue.Empty:
                        break
                    if self._closing.is_set():
                        break

                self._maybe_rotate()
                self._maybe_flush()
                if self._hard_stop.is_set():
                    break
                if self._closing.is_set() and self._queue.empty():
                    break
        except Exception as err:  # noqa: BLE001 - a dead writer must be visible, not silent
            self.last_error = f"{type(err).__name__}: {err}"
        finally:
            self._close_part(reason=self._stop_reason or "writer stopped")

    def _handle(self, item: tuple) -> None:
        writer = self._writer
        if writer is None or writer.closed:
            return
        if item[0] == "note":
            _tag, event, fields = item
            writer.note(event, **fields)
            return
        _tag, topic, payload, qos, retain, ts_ns = item
        try:
            writer.message(topic, payload, ts_ns=ts_ns, qos=qos, retain=retain)
        except JournalError as err:
            self.last_error = str(err)
            return
        self.frames += 1
        self.payload_bytes += len(payload)
        # Checked per frame rather than per batch: a batch is up to 512 frames, so
        # checking afterwards overshoots a 512-byte cap by 16 kB and makes the
        # counter meaningless on exactly the captures that need it bounded.
        if self.max_bytes and self.payload_bytes >= self.max_bytes:
            writer.note(
                "capture_cap_reached", max_bytes=self.max_bytes, frames=self.frames
            )
            self._stop_reason = "capture size cap reached"
            self._hard_stop.set()
            self._closing.set()

    def _maybe_rotate(self) -> None:
        if self._writer is None or self._writer.closed:
            return
        if not self.rotate_bytes or self._writer.payload_bytes < self.rotate_bytes:
            return
        self._close_part(reason="rotated")
        self._part += 1
        self._open_part()
        if self._writer is not None:
            self._writer.note("rotation", part=self._part, frames=self.frames)

    def _maybe_flush(self) -> None:
        if self._writer is not None and not self._writer.closed:
            self._writer.flush()


def open_recorder(
    preferred: str,
    fallback: str,
    *,
    topics: Iterable[str] = (),
    **kwargs: Any,
) -> tuple[CaptureRecorder, str]:
    """Build a started recorder, falling back to ``fallback`` if ``preferred`` is unusable.

    ``/share`` exists on Home Assistant OS and Supervised, and not on a Container
    install - so a path that cannot be created is a normal configuration rather
    than an error, and the fallback (under the config directory) always exists.
    Returns the recorder and the directory it actually opened.
    """
    for directory in (preferred, fallback):
        if not directory:
            continue
        recorder = CaptureRecorder(directory, topics=topics, **kwargs)
        try:
            recorder.start()
        except OSError:
            continue
        return recorder, directory
    raise OSError(f"neither {preferred!r} nor {fallback!r} could be written to")
