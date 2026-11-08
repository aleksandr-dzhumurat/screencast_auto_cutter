"""Animated CLI progress spinner for long-running pipeline stages."""

from __future__ import annotations

import sys
import threading
import time
from typing import Self, TextIO

_FRAMES = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"


def _fmt_elapsed(seconds: float) -> str:
    m, s = divmod(int(seconds), 60)
    if m:
        return f"{m}m{s:02d}s"
    return f"{s}s"


class Spinner:
    """Thread-animated progress indicator for blocking operations."""

    def __init__(
        self,
        message: str = "",
        stream: TextIO | None = None,
        interval: float = 0.1,
        enabled: bool | None = None,
    ) -> None:
        self.message = message
        self.stream = stream or sys.stderr
        self.interval = max(0.05, interval)
        self._is_tty = bool(getattr(self.stream, "isatty", lambda: False)())
        self._active = self._is_tty if enabled is None else bool(enabled)
        self._running = False
        self._start: float | None = None
        self._progress: float | None = None
        self._frame = 0
        self._last_len = 0
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()

    # -- lifecycle ---------------------------------------------------------
    def start(self) -> Spinner:
        if self._running:
            return self
        self._running = True
        self._start = time.monotonic()
        if self._active:
            self._thread = threading.Thread(target=self._spin, daemon=True)
            self._thread.start()
            self._render()
        return self

    def stop(self, final_message: str | None = None) -> None:
        if not self._running:
            if final_message is not None:
                self._emit(final_message)
            return
        self._running = False
        if self._thread is not None:
            self._stop_event.set()
            self._thread.join()
            self._thread = None
        with self._lock:
            self._clear()
        if final_message is not None:
            elapsed = _fmt_elapsed(time.monotonic() - (self._start or time.monotonic()))
            self._emit(f"{final_message} ({elapsed})")

    def __enter__(self) -> Self:
        return self.start()

    def __exit__(self, exc_type, exc, tb) -> bool:
        self.stop()
        return False

    # -- updates from the caller --------------------------------------------
    def update(self, message: str) -> None:
        """Change the message shown next to the spinner."""
        self.message = message
        self._render()

    def set_progress(self, fraction: float | None) -> None:
        """Show a completion percentage (0..1), or None to hide it."""
        if fraction is None:
            self._progress = None
        else:
            self._progress = max(0.0, min(1.0, float(fraction)))
        self._render()

    def say(self, message: str) -> None:
        """Print a clean line while the spinner is running."""
        with self._lock:
            self._clear()
            self._emit(message)

    # -- internals -----------------------------------------------------------
    def _spin(self) -> None:
        while not self._stop_event.wait(self.interval):
            self._render()

    def _render(self) -> None:
        with self._lock:
            if not self._running or not self._active:
                return
            frame = _FRAMES[self._frame % len(_FRAMES)]
            self._frame += 1
            parts = [frame] + ([self.message] if self.message else [])
            if self._progress is not None:
                parts.append(f"{int(self._progress * 100):3d}%")
            if self._start is not None:
                parts.append(_fmt_elapsed(time.monotonic() - self._start))
            line = " ".join(parts)
            self._clear()
            self.stream.write(line)
            self.stream.flush()
            self._last_len = len(line)

    def _clear(self) -> None:
        if self._last_len:
            self.stream.write("\r" + " " * self._last_len + "\r")
            self.stream.flush()
            self._last_len = 0

    def _emit(self, line: str) -> None:
        self.stream.write(line + "\n")
        self.stream.flush()

