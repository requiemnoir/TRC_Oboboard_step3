import time
import threading

class Diagnostics:
    def __init__(self):
        self._lock = threading.Lock()
        # Use monotonic clock for rates/uptime to avoid issues when wall clock changes (NTP).
        self._start_mono = time.monotonic()
        self.frame_count = 0
        self.error_count = 0
        self.byte_count = 0
        self._last_calc_mono = time.monotonic()
        self.current_load = 0.0

        # Per-channel counters (optional; frame['channel'] may be missing)
        self.frame_count_by_ch = {}
        self.byte_count_by_ch = {}
        self.error_count_by_ch = {}
        self.current_load_by_ch = {}
        self.frame_type_by_ch = {}

    @staticmethod
    def _normalize_frame_type(frame_type) -> str:
        try:
            return str(frame_type or 'CAN').strip().upper()
        except Exception:
            return 'CAN'

    def _default_bps_for_frame_type(self, frame_type: str, channel=None, fallback=500000) -> int:
        ft = self._normalize_frame_type(frame_type)
        if ft in {'FLEXRAY', 'FLEX', 'FR'}:
            # Automotive FlexRay physical bitrate is typically 10 Mbit/s.
            return 10_000_000
        if ft == 'LIN':
            return 19_200
        try:
            ch = int(channel)
        except Exception:
            ch = None
        if ch is not None and ch >= 200:
            return 10_000_000
        if ch is not None and 150 <= ch < 200:
            return 19_200
        return self._bitrate_to_bps(fallback)

    @staticmethod
    def _bitrate_to_bps(bitrate) -> int:
        """Map Kvaser bitrate constants (negative) or raw bps to integer bps."""
        try:
            b = int(bitrate)
        except Exception:
            return 500000

        # Kvaser canlib bitrate constants used in this project
        if b == -1:
            return 1000000
        if b == -2:
            return 500000
        if b == -3:
            return 250000
        if b == -4:
            return 125000
        # If the caller provides an actual bps value, use it.
        if b > 0:
            return b
        return 500000

    def update(self, frame):
        with self._lock:
            frame_type = self._normalize_frame_type(frame.get('type'))
            self.frame_count += 1
            self.byte_count += frame['dlc']
            # Kvaser canlib: 0x04 is canMSG_EXT (extended frame), not an error.
            # Error frames use canMSG_ERROR_FRAME (0x20).
            is_can_like = frame_type in {'CAN', 'CAN-FD'}
            if is_can_like and (int(frame.get('flags', 0) or 0) & 0x20):
                self.error_count += 1

            # Per-channel stats (best effort)
            try:
                ch = int(frame.get('channel'))
            except Exception:
                ch = None
            if ch is not None:
                self.frame_count_by_ch[ch] = int(self.frame_count_by_ch.get(ch, 0)) + 1
                self.byte_count_by_ch[ch] = int(self.byte_count_by_ch.get(ch, 0)) + int(frame.get('dlc', 0) or 0)
                self.frame_type_by_ch[ch] = frame_type
                if is_can_like and (int(frame.get('flags', 0) or 0) & 0x20):
                    self.error_count_by_ch[ch] = int(self.error_count_by_ch.get(ch, 0)) + 1

    def calculate_load(self, bitrate=500000, bitrate_by_channel=None):
        now_mono = time.monotonic()
        with self._lock:
            delta = now_mono - self._last_calc_mono
            if delta >= 1.0:
                # Bits per second approx: (bytes * 8) + overhead
                # CAN frame overhead is roughly 47 bits + stuffing. Let's approx 50 bits per frame for simplicity.
                # Per-channel load calculation
                try:
                    bps_map = bitrate_by_channel if isinstance(bitrate_by_channel, dict) else {}
                except Exception:
                    bps_map = {}
                new_loads = {}
                for ch, fc in list(self.frame_count_by_ch.items()):
                    try:
                        bc = int(self.byte_count_by_ch.get(ch, 0))
                        fc = int(fc)
                        frame_type = self.frame_type_by_ch.get(ch, 'CAN')
                        bits_ch = (bc * 8) + (fc * 50)
                        if ch in bps_map:
                            bps = self._bitrate_to_bps(bps_map.get(ch, bitrate))
                        else:
                            bps = self._default_bps_for_frame_type(frame_type, channel=ch, fallback=bitrate)
                        load = (bits_ch / delta) / float(bps) * 100.0
                        if load > 100:
                            load = 100.0
                        if load < 0:
                            load = 0.0
                        new_loads[int(ch)] = round(float(load), 2)
                    except Exception:
                        continue
                if new_loads:
                    self.current_load = round(max(float(v) for v in new_loads.values()), 2)
                else:
                    bits = (self.byte_count * 8) + (self.frame_count * 50)
                    self.current_load = (bits / delta) / float(self._bitrate_to_bps(bitrate)) * 100.0
                    if self.current_load > 100:
                        self.current_load = 100.0
                    if self.current_load < 0:
                        self.current_load = 0.0

                self.current_load_by_ch = new_loads
                self.byte_count = 0
                self.frame_count = 0
                self.frame_count_by_ch = {}
                self.byte_count_by_ch = {}
                self.frame_type_by_ch = {}
                self._last_calc_mono = now_mono
            
        return {
            "bus_load": round(self.current_load, 2),
            "errors": self.error_count,
            "uptime": round(now_mono - self._start_mono, 0),
            "bus_load_by_channel": dict(self.current_load_by_ch) if isinstance(self.current_load_by_ch, dict) else {},
            "errors_by_channel": dict(self.error_count_by_ch) if isinstance(self.error_count_by_ch, dict) else {},
        }
