import threading
import time
from typing import Optional, Tuple


class SharedFrameBuffer:
    """Thread-safe single-slot buffer for the latest JPEG frame.

    Designed for low-latency MJPEG: only the newest frame is retained.
    """

    def __init__(self):
        self._cond = threading.Condition()
        self._seq = 0
        self._jpeg: Optional[bytes] = None
        self._timestamp_s: float = 0.0

    def update(self, jpeg_bytes: bytes, timestamp_s: Optional[float] = None) -> int:
        if timestamp_s is None:
            timestamp_s = time.time()
        with self._cond:
            self._jpeg = jpeg_bytes
            self._timestamp_s = float(timestamp_s)
            self._seq += 1
            self._cond.notify_all()
            return self._seq

    def get_latest(
        self,
        last_seq: int = 0,
        timeout_s: Optional[float] = None,
    ) -> Tuple[int, Optional[bytes], float]:
        """Wait until a newer frame than last_seq is available.

        Returns (seq, jpeg_bytes_or_none, timestamp_s).
        """
        end_time = None
        if timeout_s is not None:
            end_time = time.time() + float(timeout_s)

        with self._cond:
            while self._seq <= last_seq:
                remaining = None
                if end_time is not None:
                    remaining = end_time - time.time()
                    if remaining <= 0:
                        break
                self._cond.wait(timeout=remaining)

            return self._seq, self._jpeg, self._timestamp_s

    def snapshot(self) -> Tuple[int, Optional[bytes], float]:
        with self._cond:
            return self._seq, self._jpeg, self._timestamp_s
