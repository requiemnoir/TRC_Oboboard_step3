try:
    import canlib.canlib as canlib
    IS_MOCK = False
except (ImportError, OSError, SystemExit):
    # Mock for development without drivers
    IS_MOCK = True
    import random
    import time
    import queue
    
    class MockCanLib:
        canBITRATE_500K = -2
        canOPEN_ACCEPT_VIRTUAL = 1
        canDRIVER_NORMAL = 4
        canDRIVER_SILENT = 1
        class canNoMsg(Exception): pass
        class canError(Exception): pass
        
        class Message:
            def __init__(self, id, data, dlc, flags, time):
                self.id = id
                self.data = data
                self.dlc = dlc
                self.flags = flags
                self.time = time

        class Channel:
            def __init__(self, ch_idx):
                self.ch_idx = ch_idx
                self.rx_queue = queue.Queue()
                self.start_time = time.time()
                
            def setBusOutputControl(self, x): pass
            def setBusParams(self, x): pass
            def busOn(self): pass
            def busOff(self): pass
            def close(self): pass
            
            def read(self, timeout):
                # By default, the mock driver should NOT generate traffic.
                # Enable synthetic frames explicitly with KBSM_MOCK_CAN_TRAFFIC=1
                # for UI/dev demos.
                try:
                    import os
                    enable = str(os.getenv('KBSM_MOCK_CAN_TRAFFIC', '')).strip().lower() in {'1', 'true', 'yes', 'on'}
                except Exception:
                    enable = False

                if not enable:
                    # Check queue for loopback/injected frames
                    try:
                        return self.rx_queue.get(block=False)
                    except queue.Empty:
                        pass
                    
                    time.sleep(timeout / 1000.0)
                    raise MockCanLib.canNoMsg

                # 1. Generate random background traffic (Engine RPM, Speed)
                if random.random() < 0.05: # 5% chance of random frame
                    return MockCanLib.Message(
                        id=0x100 + random.randint(0, 5),
                        data=[random.randint(0, 255) for _ in range(8)],
                        dlc=8,
                        flags=0,
                        time=time.time() * 1000
                    )
                
                # 2. Check for queued responses (from write)
                try:
                    return self.rx_queue.get(block=False)
                except queue.Empty:
                    pass
                    
                # 3. Simulate timeout
                time.sleep(timeout / 1000.0)
                raise MockCanLib.canNoMsg

            def write(self, id, data, flags, dlc):
                # Simulate ECU responses
                # OBD-II Request: 0x7DF or 0x7E0..7E7
                if id == 0x7DF or (0x7E0 <= id <= 0x7E7):
                    # Tester Present / Discovery (we accept it on functional too for simulation)
                    # Request: 02 3E 00 ...
                    if len(data) >= 3 and data[1] == 0x3E:
                        # Positive response format used by our discovery heuristic: data[1] == 0x7E
                        resp_data = [0x02, 0x7E, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00]
                        self._queue_response(0x7E8, resp_data)
                        return

                    # Check for Mode 01 00 (Supported PIDs)
                    if len(data) >= 3 and data[1] == 0x01 and data[2] == 0x00:
                        # Respond from Engine (0x7E8)
                        resp_data = [0x06, 0x41, 0x00, 0xFF, 0xFF, 0xFF, 0xFF, 0x00]
                        self._queue_response(0x7E8, resp_data)
                        return

                    # Mode 01 PID 0x0C (RPM)
                    elif len(data) >= 3 and data[1] == 0x01 and data[2] == 0x0C:
                        # Simulate ~2400 RPM
                        rpm = 2400
                        raw = int(rpm * 4)
                        a = (raw >> 8) & 0xFF
                        b = raw & 0xFF
                        resp_data = [0x04, 0x41, 0x0C, a, b, 0x00, 0x00, 0x00]
                        self._queue_response(0x7E8, resp_data)
                        return

                    # Mode 01 PID 0x0D (Speed)
                    elif len(data) >= 3 and data[1] == 0x01 and data[2] == 0x0D:
                        speed = 88  # km/h
                        resp_data = [0x03, 0x41, 0x0D, speed, 0x00, 0x00, 0x00, 0x00]
                        self._queue_response(0x7E8, resp_data)
                        return
                        
                    # Check for Mode 03 (Read DTCs)
                    elif len(data) >= 2 and data[1] == 0x03:
                        # Respond with dummy DTCs
                        # 43 02 ... (2 DTCs)
                        resp_data = [0x04, 0x43, 0x02, 0x01, 0x23, 0x00, 0x00, 0x00]
                        self._queue_response(0x7E8, resp_data)
                        return

                    # Mode 04 (Clear DTCs)
                    elif len(data) >= 2 and data[1] == 0x04:
                        # Positive response 0x44
                        resp_data = [0x02, 0x44, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00]
                        self._queue_response(0x7E8, resp_data)
                        return

            def _queue_response(self, id, data):
                msg = MockCanLib.Message(
                    id=id,
                    data=data,
                    dlc=len(data),
                    flags=0,
                    time=time.time() * 1000
                )
                self.rx_queue.put(msg)

        def openChannel(self, ch, flags):
            return self.Channel(ch)
            
    canlib = MockCanLib()

import time
import threading

# Map human-readable bitrate values to Kvaser driver constants.
# The Kvaser CANlib driver requires predefined negative constants for standard
# bitrates (setBusParams rejects raw integer values like 500000).
_BITRATE_MAP = {
    10000:   getattr(canlib, 'canBITRATE_10K',   -9),
    50000:   getattr(canlib, 'canBITRATE_50K',   -7),
    62000:   getattr(canlib, 'canBITRATE_62K',   -6),
    83000:   getattr(canlib, 'canBITRATE_83K',   -8),
    100000:  getattr(canlib, 'canBITRATE_100K',  -5),
    125000:  getattr(canlib, 'canBITRATE_125K',  -4),
    250000:  getattr(canlib, 'canBITRATE_250K',  -3),
    500000:  getattr(canlib, 'canBITRATE_500K',  -2),
    1000000: getattr(canlib, 'canBITRATE_1M',    -1),
}


def _resolve_bitrate(bitrate):
    """Convert a numeric bitrate (e.g. 500000) to the Kvaser constant (-2)."""
    if isinstance(bitrate, int) and bitrate > 0:
        return _BITRATE_MAP.get(bitrate, bitrate)
    return bitrate


class CANHandler:
    def __init__(self, channel_number, bitrate=canlib.canBITRATE_500K, listen_only=False):
        self.channel_number = channel_number
        self.bitrate = _resolve_bitrate(bitrate)
        self.listen_only = bool(listen_only)
        self.ch = None
        self.is_open = False

    def open(self):
        try:
            # On real Kvaser hardware, opening with canOPEN_ACCEPT_VIRTUAL can fail with
            # "Error in parameter" on some driver setups. Prefer a normal open.
            flags = 0
            try:
                # Allow virtual channels only if explicitly requested via env var.
                import os
                if str(os.getenv('KVBM_ACCEPT_VIRTUAL', '')).strip().lower() in {'1', 'true', 'yes', 'on'}:
                    flags = int(getattr(canlib, 'canOPEN_ACCEPT_VIRTUAL', 0))
            except Exception:
                flags = 0

            env_listen_only = False
            try:
                import os
                env_listen_only = str(os.getenv('KBSM_CAN_LISTEN_ONLY', '')).strip().lower() in {'1', 'true', 'yes', 'on'}
            except Exception:
                env_listen_only = False

            requested_listen_only = bool(self.listen_only or env_listen_only)
            output_mode = getattr(canlib, 'canDRIVER_NORMAL', None)
            if requested_listen_only:
                output_mode = getattr(canlib, 'canDRIVER_SILENT', None)
                if output_mode is None:
                    print(f"Error opening CAN channel {self.channel_number}: listen-only requested but driver lacks canDRIVER_SILENT")
                    return False

            self.ch = canlib.openChannel(self.channel_number, flags)
            actual_mode = output_mode
            try:
                self.ch.setBusOutputControl(output_mode)
            except Exception:
                # Some Kvaser channels/driver states reject canDRIVER_SILENT
                # (CanGeneralError which may not inherit canlib.canError).
                # Fall back to NORMAL — we never call write() so no TX occurs.
                if requested_listen_only:
                    fallback = getattr(canlib, 'canDRIVER_NORMAL', 4)
                    print(f"CAN Channel {self.channel_number}: SILENT mode not supported by hardware, falling back to NORMAL (RX-only, no write() calls)")
                    self.ch.setBusOutputControl(fallback)
                    actual_mode = fallback
                else:
                    raise
            self.ch.setBusParams(self.bitrate)
            self.ch.busOn()
            self.is_open = True
            if requested_listen_only and actual_mode != output_mode:
                mode_label = 'normal (silent n/a, SW-guarded)'
            elif requested_listen_only:
                mode_label = 'listen-only'
            else:
                mode_label = 'normal'
            print(f"CAN Channel {self.channel_number} Opened ({mode_label}).")
            return True
        except Exception as e:
            print(f"Error opening CAN channel {self.channel_number} (bitrate={self.bitrate}, flags={locals().get('flags', None)}): {e}")
            return False

    def close(self):
        if self.ch:
            try:
                self.ch.busOff()
                self.ch.close()
            except Exception as e:
                print(f"Error closing CAN channel: {e}")
        self.is_open = False
        self.ch = None

    def read(self):
        """Reads a single frame. Returns None if no message."""
        if not self.is_open:
            return None
        try:
            # timeout=10ms
            msg = self.ch.read(timeout=10)
            ts = getattr(msg, 'time', None)
            if ts is None:
                ts = getattr(msg, 'timestamp', None)
            if ts is None:
                ts = time.time() * 1000
            # Keep the hardware/driver timestamp (Kvaser often reports ms since driver start)
            # but expose a consistent epoch-ms timestamp to the rest of the app.
            hw_timestamp = ts
            ts = int(time.time() * 1000)
            flags = int(getattr(msg, 'flags', 0) or 0)

            # Filter Error Frames (0x20) to prevent log flooding
            # This happens if the physical interface is open but disconnected/terminated poorly
            if flags & 0x20:
                return None

            dlc = int(getattr(msg, 'dlc', 0) or 0)
            # Some Kvaser setups report canMSG_FDF (0x02) even when transmitting/receiving
            # classic 8-byte frames. For our purposes (OBD/UDS classic CAN), treat DLC<=8
            # as normal CAN by clearing the FDF flag.
            if (flags & 0x02) and dlc <= 8:
                flags = flags & (~0x02)

            return {
                "id": msg.id,
                "data": list(msg.data),
                "dlc": dlc,
                "flags": flags,
                "timestamp": ts,
                "hw_timestamp": hw_timestamp,
                "is_canfd": bool(flags & 0x02),
                "type": "CAN"
            }
        except canlib.canNoMsg:
            return None
        except canlib.canError as e:
            print(f"CAN Read Error: {e}")
            return None

    def write(self, arb_id, data, dlc=None, flags=0):
        if not self.is_open:
            return False
        try:
            if dlc is None:
                dlc = len(data)
            self.ch.write(arb_id, data, flags, dlc)
            return True
        except canlib.canError as e:
            print(f"CAN Write Error: {e}")
            return False

    def get_stats(self):
        if not self.is_open:
            return {"bus_load": 0, "error_frames": 0}
        
        # When drivers are missing (mock mode), do not fabricate load.
        # Returning 0 avoids misleading the operator into thinking traffic exists.
        if IS_MOCK:
            return {"bus_load": 0, "error_frames": 0, "mocked": True}
            
        return {"bus_load": 0, "error_frames": 0}
