"""Time source. On real hardware replace with GPS/RTC-backed trusted time."""
import time


class Clock:
    trusted = True

    def now_ms(self) -> int:
        return int(time.time() * 1000)


class FakeClock(Clock):
    """Controllable clock for tests and the demo."""

    def __init__(self, start_ms=None, trusted=True):
        self._now = start_ms if start_ms is not None else int(time.time() * 1000)
        self.trusted = trusted

    def now_ms(self) -> int:
        return self._now

    def advance(self, seconds: float):
        self._now += int(seconds * 1000)
