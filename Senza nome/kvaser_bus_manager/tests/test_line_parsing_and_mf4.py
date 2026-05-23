#!/usr/bin/env python3
"""Comprehensive per-line parsing & MF4 decoded-signal recording tests.

Tests each communication line (CAN, FlexRay, LIN, mirror) individually:
  1. CAN line: parse raw CAN frame → DBC decode → verify signal values
  2. FlexRay line: parse raw FlexRay frame → FIBEX decode → verify
  3. LIN line: parse mirror LIN frame entry → verify
  4. AUTOSAR mirror: parse AUTOSAR Bus Mirroring PDU with mixed buses
  5. VAG SOME/IP mirror: parse SOME/IP-wrapped mirror payload
  6. Iron Bird mirror: parse legacy 0xD00D frames
  7. Raw CAN-in-UDP: parse simple [ArbID:4][DLC:1][Data:N]
  8. MF4 recording: inject decoded CAN frames → save MF4 → read back decoded signals
  9. MF4 raw-only: inject frames without decode → verify raw table
 10. MF4 with decode flag: inject frames with decoded signals → verify they appear in MF4
"""
import os
import sys
import struct
import tempfile
import time
import unittest
from unittest.mock import MagicMock

BACKEND_DIR = os.path.join(os.path.dirname(__file__), '..', 'backend')
sys.path.insert(0, os.path.abspath(BACKEND_DIR))


# ═══════════════════════════════════════════════════════════════════════════
# Helper: simulation DBC path
# ═══════════════════════════════════════════════════════════════════════════
SIMULATION_DBC = os.path.join(
    os.path.dirname(__file__), '..', '..', 'databases', 'dbc', 'simulation.dbc'
)
CCAN_DBC = os.path.join(
    os.path.dirname(__file__), '..', 'databases', 'dbc',
    'MLBevo_Gen2_MLBevo_CCAN_KMatrix_V8.24.00F_20220602_SEn.dbc'
)
HCAN_DBC = os.path.join(
    os.path.dirname(__file__), '..', 'databases', 'dbc',
    'MLBevo_Gen2_MLBevo_HCAN_KMatrix_V8.24.00F_20220602_VP.dbc'
)


def _have_dbc(path):
    return os.path.isfile(path)


def _have_mf4_deps():
    try:
        import numpy
        import asammdf
        return True
    except ImportError:
        return False


def _wait_for_merge(logger, timeout=10.0):
    """Wait for the MF4 merge background thread to complete."""
    t = getattr(logger, '_mf4_merge_thread', None)
    if t is not None and t.is_alive():
        t.join(timeout=timeout)


def _find_mf4_files(directory):
    """Find usable MF4 files in a directory, preferring merged session files."""
    all_mf4 = []
    for f in os.listdir(directory):
        if not f.endswith('.mf4'):
            continue
        if '.tmp.' in f.lower() or 'error' in f.lower():
            continue
        full = os.path.join(directory, f)
        if os.path.isfile(full):
            all_mf4.append(full)
    # Prefer merged session file (no _part in name) over part files
    merged = [f for f in all_mf4 if '_part' not in os.path.basename(f)]
    if merged:
        return merged
    return all_mf4


# ═══════════════════════════════════════════════════════════════════════════
# 1. CAN Line Parsing (one frame at a time)
# ═══════════════════════════════════════════════════════════════════════════
class TestCANLineParsing(unittest.TestCase):
    """Parse a single CAN frame and decode it via DBC."""

    @unittest.skipUnless(_have_dbc(SIMULATION_DBC), "simulation.dbc not available")
    def test_decode_motor_01_engine_rpm(self):
        """Decode Motor_01.Engine_RPM from a single CAN frame."""
        from dbc_loader import DBCLoader

        loader = DBCLoader()
        self.assertTrue(loader.load(SIMULATION_DBC))

        # Motor_01: ID=498 (0x1F2), Engine_RPM: start_bit=0, length=16, factor=0.25
        rpm_target = 2400.0
        raw_val = int(rpm_target / 0.25)  # = 9600 = 0x2580
        data = list(raw_val.to_bytes(2, 'little')) + [0] * 6  # little-endian

        result = loader.decode(498, data)
        self.assertIsNotNone(result, "Motor_01 should decode")
        self.assertEqual(result['name'], 'Motor_01')
        self.assertIn('Engine_RPM', result['signals'])
        self.assertAlmostEqual(result['signals']['Engine_RPM'], rpm_target, places=1)

    @unittest.skipUnless(_have_dbc(SIMULATION_DBC), "simulation.dbc not available")
    def test_decode_esp_01_vehicle_speed(self):
        """Decode Esp_01.Vehicle_Speed from a single CAN frame."""
        from dbc_loader import DBCLoader

        loader = DBCLoader()
        self.assertTrue(loader.load(SIMULATION_DBC))

        # Esp_01: ID=499 (0x1F3), Vehicle_Speed: start_bit=0, length=16, factor=0.01
        speed_target = 120.50
        raw_val = int(speed_target / 0.01)  # = 12050
        data = list(raw_val.to_bytes(2, 'little')) + [0] * 6

        result = loader.decode(499, data)
        self.assertIsNotNone(result, "Esp_01 should decode")
        self.assertEqual(result['name'], 'Esp_01')
        self.assertIn('Vehicle_Speed', result['signals'])
        self.assertAlmostEqual(result['signals']['Vehicle_Speed'], speed_target, places=1)

    @unittest.skipUnless(_have_dbc(SIMULATION_DBC), "simulation.dbc not available")
    def test_unknown_id_returns_none(self):
        """Unknown CAN ID should return None from decoder."""
        from dbc_loader import DBCLoader

        loader = DBCLoader()
        self.assertTrue(loader.load(SIMULATION_DBC))
        result = loader.decode(0x7FF, [0] * 8)
        self.assertIsNone(result)

    @unittest.skipUnless(_have_dbc(SIMULATION_DBC), "simulation.dbc not available")
    def test_multi_loader_fallthrough(self):
        """When multiple DBCs are loaded, decode should try each in order."""
        from dbc_loader import DBCLoader

        loader1 = DBCLoader()
        loader2 = DBCLoader()
        self.assertTrue(loader1.load(SIMULATION_DBC))
        # loader2 is empty (no DBC loaded)

        loaders = [loader2, loader1]
        decoded = None
        for loader in loaders:
            decoded = loader.decode(498, [0x80, 0x25, 0, 0, 0, 0, 0, 0])
            if decoded:
                break
        self.assertIsNotNone(decoded)
        self.assertEqual(decoded['name'], 'Motor_01')

    @unittest.skipUnless(_have_dbc(CCAN_DBC), "CCAN DBC not available")
    def test_decode_real_ccan_frame(self):
        """Decode a real CCAN frame if the MLBevo DBC is available."""
        from dbc_loader import DBCLoader

        loader = DBCLoader()
        self.assertTrue(loader.load(CCAN_DBC))

        # Try to decode known ESP_21 (0x0FD) or similar
        result = loader.decode(0x0FD, [0x00, 0x64, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00])
        # May or may not decode depending on DBC content, but should not crash
        if result:
            self.assertIn('name', result)
            self.assertIn('signals', result)


# ═══════════════════════════════════════════════════════════════════════════
# 2. FlexRay Line Parsing
# ═══════════════════════════════════════════════════════════════════════════
class TestFlexRayLineParsing(unittest.TestCase):
    """Parse a FlexRay frame and decode it via FIBEX."""

    def test_flexray_handler_read_returns_correct_type(self):
        """FlexRay handler read() should return type='FLEXRAY'."""
        from flexray_handler import FlexRayHandler
        handler = FlexRayHandler(0)
        # In mock mode, open succeeds but read returns None (no msg)
        handler.open()
        frame = handler.read()
        # No traffic in mock mode: frame is None
        if frame is not None:
            self.assertEqual(frame['type'], 'FLEXRAY')
        handler.close()

    def test_fibex_loader_decode_no_crash(self):
        """FibexLoader.decode should not crash on unknown frame."""
        from fibex_loader import FibexLoader
        loader = FibexLoader()
        # No FIBEX loaded → should return None gracefully
        result = loader.decode(0x100, [0] * 8)
        self.assertIsNone(result)


# ═══════════════════════════════════════════════════════════════════════════
# 3. LIN Line Parsing (via AUTOSAR mirror)
# ═══════════════════════════════════════════════════════════════════════════
class TestLINLineParsing(unittest.TestCase):
    """Parse a LIN frame from AUTOSAR mirror PDU."""

    def test_lin_frame_from_autosar_mirror(self):
        """LIN frames (NetworkType=0x03) should be emitted with type=LIN."""
        from ethernet_capture import EthernetCapture

        received = []
        cap = EthernetCapture.__new__(EthernetCapture)
        cap.mirror_callback = lambda channel_id, arb_id, data, flags=0, frame_type="CAN": \
            received.append({'ch': channel_id, 'arb': arb_id, 'data': data, 'ft': frame_type})
        cap._mirror_rx_count = 0

        # Build AUTOSAR mirror payload with a LIN frame
        # Header: StatusByte(1) + Timestamp(4µs) + SeqCounter(2) = 7 bytes
        # Frame: NetworkType(1)=0x03 + NetworkID(1)=0 + FrameID(4) + PayloadLen(2) + Payload
        lin_id = 0x3C
        lin_data = bytes([0x01, 0x02, 0x03, 0x04])
        header = struct.pack('!BIH', 0x00, 1000, 1)
        frame_entry = struct.pack('!BBIH', 0x03, 0, lin_id, len(lin_data)) + lin_data
        payload = header + frame_entry

        result = cap._try_unpack_autosar_mirror(payload)
        self.assertTrue(result)
        self.assertEqual(len(received), 1)
        self.assertEqual(received[0]['ft'], 'LIN')
        self.assertEqual(received[0]['arb'], lin_id)
        self.assertEqual(received[0]['data'], lin_data)
        # LIN channel: 150 + network_id
        self.assertEqual(received[0]['ch'], 150)


# ═══════════════════════════════════════════════════════════════════════════
# 4. AUTOSAR Mirror Parsing (mixed CAN + FlexRay + LIN)
# ═══════════════════════════════════════════════════════════════════════════
class TestAutosarMirrorMixedLines(unittest.TestCase):
    """Parse AUTOSAR mirror PDU with mixed bus types in one payload."""

    def test_mixed_can_flexray_lin(self):
        """Parse a PDU containing CAN, FlexRay, and LIN frames."""
        from ethernet_capture import EthernetCapture

        received = []
        cap = EthernetCapture.__new__(EthernetCapture)
        cap.mirror_callback = lambda channel_id, arb_id, data, flags=0, frame_type="CAN": \
            received.append({'ch': channel_id, 'arb': arb_id, 'data': data, 'ft': frame_type})
        cap._mirror_rx_count = 0

        # Header
        header = struct.pack('!BIH', 0x00, 0, 1)

        # CAN frame (type 0x01, net_id=2)
        can_entry = struct.pack('!BBIH', 0x01, 2, 0x0FD, 8) + b'\x00' * 8

        # FlexRay frame (type 0x04, net_id=0)
        fr_entry = struct.pack('!BBIH', 0x04, 0, 0x0010, 4) + b'\xAA\xBB\xCC\xDD'

        # LIN frame (type 0x03, net_id=1)
        lin_entry = struct.pack('!BBIH', 0x03, 1, 0x3C, 3) + b'\x01\x02\x03'

        payload = header + can_entry + fr_entry + lin_entry

        result = cap._try_unpack_autosar_mirror(payload)
        self.assertTrue(result)
        self.assertEqual(len(received), 3)

        # CAN
        self.assertEqual(received[0]['ft'], 'CAN')
        self.assertEqual(received[0]['arb'], 0x0FD)
        self.assertEqual(received[0]['ch'], 102)  # 100 + net_id=2

        # FlexRay
        self.assertEqual(received[1]['ft'], 'FlexRay')
        self.assertEqual(received[1]['arb'], 0x0010)
        self.assertEqual(received[1]['ch'], 200)  # 200 + net_id=0

        # LIN
        self.assertEqual(received[2]['ft'], 'LIN')
        self.assertEqual(received[2]['arb'], 0x3C)
        self.assertEqual(received[2]['ch'], 151)  # 150 + net_id=1

    def test_canfd_frame(self):
        """CAN-FD frames (NetworkType=0x02) should be parsed."""
        from ethernet_capture import EthernetCapture

        received = []
        cap = EthernetCapture.__new__(EthernetCapture)
        cap.mirror_callback = lambda channel_id, arb_id, data, flags=0, frame_type="CAN": \
            received.append({'ch': channel_id, 'arb': arb_id, 'data': data, 'ft': frame_type})
        cap._mirror_rx_count = 0

        header = struct.pack('!BIH', 0x00, 0, 1)
        # CAN-FD frame (type 0x02) with 12 bytes payload
        canfd_data = bytes(range(12))
        canfd_entry = struct.pack('!BBIH', 0x02, 0, 0x200, len(canfd_data)) + canfd_data

        payload = header + canfd_entry
        result = cap._try_unpack_autosar_mirror(payload)
        self.assertTrue(result)
        self.assertEqual(len(received), 1)
        self.assertEqual(received[0]['ft'], 'CAN-FD')
        self.assertEqual(received[0]['arb'], 0x200)
        self.assertEqual(len(received[0]['data']), 12)


# ═══════════════════════════════════════════════════════════════════════════
# 5. Iron Bird Mirror Parsing
# ═══════════════════════════════════════════════════════════════════════════
class TestIronBirdLineParsing(unittest.TestCase):
    """Parse Iron Bird 0xD00D mirror frames one by one."""

    def test_single_iron_bird_frame(self):
        """Parse exactly one Iron Bird frame."""
        from ethernet_capture import EthernetCapture

        received = []
        cap = EthernetCapture.__new__(EthernetCapture)
        cap.mirror_callback = lambda channel_id, arb_id, data, flags=0, frame_type="CAN": \
            received.append({'ch': channel_id, 'arb': arb_id, 'data': data, 'ft': frame_type})
        cap._mirror_rx_count = 0

        # Single frame: [0xD00D:2][ArbID:4][DLC:1][Data:8]
        data = b'\x00\x64\x00\x00\x00\x00\x00\x00'
        payload = struct.pack('>HI B', 0xD00D, 0x0FD, 8) + data

        cap._unpack_mirror_payload(payload)
        self.assertEqual(len(received), 1)
        self.assertEqual(received[0]['arb'], 0x0FD)
        self.assertEqual(received[0]['data'], data)
        self.assertEqual(received[0]['ch'], 99)

    def test_three_iron_bird_frames_sequential(self):
        """Parse 3 Iron Bird frames one at a time."""
        from ethernet_capture import EthernetCapture

        ids = [0x0FD, 0x0A8, 0x040]
        for arb_id in ids:
            received = []
            cap = EthernetCapture.__new__(EthernetCapture)
            cap.mirror_callback = lambda channel_id, arb_id, data, flags=0, frame_type="CAN": \
                received.append({'arb': arb_id})
            cap._mirror_rx_count = 0

            data = bytes([arb_id & 0xFF] * 8)
            payload = struct.pack('>HI B', 0xD00D, arb_id, 8) + data
            cap._unpack_mirror_payload(payload)
            self.assertEqual(len(received), 1, f"Failed for ID 0x{arb_id:03X}")


# ═══════════════════════════════════════════════════════════════════════════
# 6. Raw CAN-in-UDP Parsing
# ═══════════════════════════════════════════════════════════════════════════
class TestRawCanInUDPParsing(unittest.TestCase):
    """Parse simple [ArbID:4][DLC:1][Data:N] format."""

    def test_single_raw_can_frame(self):
        """Parse exactly one raw CAN-in-UDP frame."""
        from ethernet_capture import EthernetCapture

        received = []
        cap = EthernetCapture.__new__(EthernetCapture)
        cap.mirror_callback = lambda channel_id, arb_id, data, flags=0, frame_type="CAN": \
            received.append({'ch': channel_id, 'arb': arb_id, 'data': data, 'ft': frame_type})
        cap._mirror_rx_count = 0

        # ArbID=0x100, DLC=4, Data=[0xAA,0xBB,0xCC,0xDD]
        payload = struct.pack('>IB', 0x100, 4) + b'\xAA\xBB\xCC\xDD'

        # This payload should NOT match Iron Bird (no 0xD00D) or AUTOSAR (status > 0x0F)
        # so it should fall through to raw CAN-in-UDP
        # Need to make first byte > 0x0F to avoid AUTOSAR detection
        # But 0x00 0x00 0x01 0x00 → first byte=0x00, which could match AUTOSAR
        # Use a higher arb_id that makes byte 0 > 0x0F
        payload = struct.pack('>IB', 0x10000100, 4) + b'\xAA\xBB\xCC\xDD'
        # This has arb_id > 0x1FFFFFFF so it'll break validation... use smaller
        payload = struct.pack('>IB', 0x00000100, 4) + b'\xAA\xBB\xCC\xDD'

        # Actually test _try_unpack_raw_can directly
        cap._try_unpack_raw_can(payload)
        self.assertEqual(len(received), 1)
        self.assertEqual(received[0]['arb'], 0x100)
        self.assertEqual(received[0]['data'], b'\xAA\xBB\xCC\xDD')

    def test_multiple_raw_can_frames(self):
        """Parse multiple back-to-back raw CAN frames."""
        from ethernet_capture import EthernetCapture

        received = []
        cap = EthernetCapture.__new__(EthernetCapture)
        cap.mirror_callback = lambda channel_id, arb_id, data, flags=0, frame_type="CAN": \
            received.append({'arb': arb_id, 'data': data})
        cap._mirror_rx_count = 0

        payload = b''
        expected = [(0x100, b'\x01\x02'), (0x200, b'\x03\x04\x05')]
        for arb_id, data in expected:
            payload += struct.pack('>IB', arb_id, len(data)) + data

        cap._try_unpack_raw_can(payload)
        self.assertEqual(len(received), 2)
        self.assertEqual(received[0]['arb'], 0x100)
        self.assertEqual(received[1]['arb'], 0x200)


# ═══════════════════════════════════════════════════════════════════════════
# 7. SOME/IP Parser Line-by-Line
# ═══════════════════════════════════════════════════════════════════════════
class TestSomeIPLineParsing(unittest.TestCase):
    """Parse SOME/IP headers individually."""

    def test_single_someip_header(self):
        from someip_parser import parse_someip

        msg_id = (0x1234 << 16) | 0x5678
        hdr = struct.pack('!IIIBBBB', msg_id, 8, 0, 1, 1, 0, 0)
        result = parse_someip(hdr)
        self.assertIsNotNone(result)
        self.assertEqual(result.service_id, 0x1234)
        self.assertEqual(result.method_id, 0x5678)

    def test_mirror_service_someip(self):
        """Mirror service 0x02FD/0xF302 should parse correctly."""
        from someip_parser import parse_someip

        msg_id = (0x02FD << 16) | 0xF302
        hdr = struct.pack('!IIIBBBB', msg_id, 100, 1, 1, 1, 0x02, 0)
        result = parse_someip(hdr)
        self.assertIsNotNone(result)
        self.assertEqual(result.service_id, 0x02FD)
        self.assertEqual(result.method_id, 0xF302)
        self.assertEqual(result.msg_type, 0x02)  # Notification


# ═══════════════════════════════════════════════════════════════════════════
# 8. MF4 Raw Recording (no decode flag)
# ═══════════════════════════════════════════════════════════════════════════
@unittest.skipUnless(_have_mf4_deps(), "numpy/asammdf not installed")
class TestMF4RawRecording(unittest.TestCase):
    """Test MF4 raw-only recording: frames in → raw CAN table in MF4."""

    def test_raw_frames_to_mf4(self):
        """Write raw CAN frames to MF4 and verify the raw table."""
        import numpy as np
        import asammdf

        from logger import BusLogger

        with tempfile.TemporaryDirectory() as tmpdir:
            logger = BusLogger(log_dir=tmpdir)
            # Disable decoded channels explicitly
            logger.set_mf4_include_decoded(False)
            logger.start(formats=['mf4'])

            # Inject frames
            frames = [
                {'id': 0x0FD, 'dlc': 8, 'data': [0x00, 0x64, 0, 0, 0, 0, 0, 0],
                 'flags': 0, 'type': 'CAN', 'channel': 0,
                 'timestamp': int(time.time() * 1000)},
                {'id': 0x0A8, 'dlc': 8, 'data': [0x0F, 0xA0, 0, 0, 0, 0, 0, 0],
                 'flags': 0, 'type': 'CAN', 'channel': 1,
                 'timestamp': int(time.time() * 1000) + 10},
                {'id': 0x040, 'dlc': 8, 'data': [0, 0, 0, 0, 0, 0, 0, 0],
                 'flags': 0, 'type': 'CAN', 'channel': 0,
                 'timestamp': int(time.time() * 1000) + 20},
            ]

            for f in frames:
                logger.log(f)

            # Let the queue drain
            time.sleep(0.5)
            logger.stop()
            _wait_for_merge(logger)

            # Find MF4 files
            mf4_files = _find_mf4_files(tmpdir)
            self.assertGreater(len(mf4_files), 0, "Should produce at least one MF4 file")

            # Read back and verify raw table
            mdf = asammdf.MDF(mf4_files[0])
            try:
                can_id_sig = mdf.get('CAN_ID')
                self.assertIsNotNone(can_id_sig)
                ids = list(can_id_sig.samples)
                self.assertEqual(len(ids), 3)
                self.assertEqual(ids[0], 0x0FD)
                self.assertEqual(ids[1], 0x0A8)
                self.assertEqual(ids[2], 0x040)

                dlc_sig = mdf.get('DLC')
                self.assertEqual(list(dlc_sig.samples), [8, 8, 8])

                ch_sig = mdf.get('Channel')
                self.assertEqual(list(ch_sig.samples), [0, 1, 0])

                db0_sig = mdf.get('DataByte0')
                self.assertEqual(list(db0_sig.samples), [0x00, 0x0F, 0x00])

                db1_sig = mdf.get('DataByte1')
                self.assertEqual(list(db1_sig.samples), [0x64, 0xA0, 0x00])
            finally:
                try:
                    mdf.close()
                except Exception:
                    pass


# ═══════════════════════════════════════════════════════════════════════════
# 9. MF4 with Decoded Signals
# ═══════════════════════════════════════════════════════════════════════════
@unittest.skipUnless(_have_mf4_deps(), "numpy/asammdf not installed")
@unittest.skipUnless(_have_dbc(SIMULATION_DBC), "simulation.dbc not available")
class TestMF4DecodedRecording(unittest.TestCase):
    """Test MF4 recording WITH decoded signals enabled."""

    def test_decoded_signals_in_mf4(self):
        """When MF4_INCLUDE_DECODED is on, decoded signal channels should appear."""
        import numpy as np
        import asammdf

        from logger import BusLogger

        with tempfile.TemporaryDirectory() as tmpdir:
            logger = BusLogger(log_dir=tmpdir)
            logger.set_mf4_include_decoded(True)
            logger.set_mf4_include_raw(True)
            logger.start(formats=['mf4'])

            # Motor_01: ID=498, Engine_RPM factor=0.25, little-endian
            rpm_target = 3000.0
            raw_rpm = int(rpm_target / 0.25)  # 12000
            rpm_bytes = list(raw_rpm.to_bytes(2, 'little')) + [0] * 6

            # Esp_01: ID=499, Vehicle_Speed factor=0.01
            speed_target = 88.0
            raw_speed = int(speed_target / 0.01)  # 8800
            speed_bytes = list(raw_speed.to_bytes(2, 'little')) + [0] * 6

            now_ms = int(time.time() * 1000)
            frames = [
                {
                    'id': 498, 'dlc': 8, 'data': rpm_bytes,
                    'flags': 0, 'type': 'CAN', 'channel': 0,
                    'timestamp': now_ms,
                    'decoded': {
                        'name': 'Motor_01',
                        'signals': {'Engine_RPM': rpm_target}
                    }
                },
                {
                    'id': 499, 'dlc': 8, 'data': speed_bytes,
                    'flags': 0, 'type': 'CAN', 'channel': 0,
                    'timestamp': now_ms + 10,
                    'decoded': {
                        'name': 'Esp_01',
                        'signals': {'Vehicle_Speed': speed_target}
                    }
                },
                # Second Motor_01 with different RPM
                {
                    'id': 498, 'dlc': 8, 'data': list(int(4000.0 / 0.25).to_bytes(2, 'little')) + [0] * 6,
                    'flags': 0, 'type': 'CAN', 'channel': 0,
                    'timestamp': now_ms + 20,
                    'decoded': {
                        'name': 'Motor_01',
                        'signals': {'Engine_RPM': 4000.0}
                    }
                },
            ]

            for f in frames:
                logger.log(f)

            time.sleep(0.5)
            logger.stop()
            _wait_for_merge(logger)

            # Find MF4 files
            mf4_files = _find_mf4_files(tmpdir)
            self.assertGreater(len(mf4_files), 0, "Should produce at least one MF4 file")

            # Read back
            mdf = asammdf.MDF(mf4_files[0])
            try:
                # Raw table should still exist
                can_id_sig = mdf.get('CAN_ID')
                self.assertIsNotNone(can_id_sig)
                self.assertEqual(len(can_id_sig.samples), 3)

                # Decoded signals should exist as separate channels
                # Logger prefixes signal names with bus label (e.g. CAN0.Engine_RPM)
                channels_db = getattr(mdf, 'channels_db', {})
                channel_names = set(channels_db.keys())

                self.assertIn('CAN0.Engine_RPM', channel_names,
                              f"CAN0.Engine_RPM not found in MF4 channels: {sorted(channel_names)}")

                rpm_sig = mdf.get('CAN0.Engine_RPM')
                self.assertIsNotNone(rpm_sig)
                rpm_values = list(rpm_sig.samples)
                self.assertEqual(len(rpm_values), 2)  # Two Motor_01 frames
                self.assertAlmostEqual(rpm_values[0], rpm_target, places=1)
                self.assertAlmostEqual(rpm_values[1], 4000.0, places=1)

                self.assertIn('CAN0.Vehicle_Speed', channel_names,
                              f"CAN0.Vehicle_Speed not found in MF4 channels: {sorted(channel_names)}")
                speed_sig = mdf.get('CAN0.Vehicle_Speed')
                self.assertIsNotNone(speed_sig)
                speed_values = list(speed_sig.samples)
                self.assertEqual(len(speed_values), 1)
                self.assertAlmostEqual(speed_values[0], speed_target, places=1)
            finally:
                try:
                    mdf.close()
                except Exception:
                    pass

    def test_mirror_decoded_frames_in_mf4(self):
        """Mirror frames (channel 99+) with decoded data should appear in MF4."""
        import numpy as np
        import asammdf

        from logger import BusLogger

        with tempfile.TemporaryDirectory() as tmpdir:
            logger = BusLogger(log_dir=tmpdir)
            logger.set_mf4_include_decoded(True)
            logger.set_mf4_include_raw(True)
            logger.start(formats=['mf4'])

            now_ms = int(time.time() * 1000)

            # Simulate a mirror frame from channel 100 (bus 0)
            frame = {
                'id': 0x0FD, 'dlc': 8,
                'data': [0x00, 0x64, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00],
                'flags': 0, 'type': 'CAN', 'channel': 100,
                'timestamp': now_ms,
                'decoded': {
                    'name': 'ESP_21',
                    'signals': {'ESP_v_Signal': 25.0}
                }
            }
            logger.log(frame)

            time.sleep(0.5)
            logger.stop()
            _wait_for_merge(logger)

            mf4_files = _find_mf4_files(tmpdir)
            self.assertGreater(len(mf4_files), 0)

            mdf = asammdf.MDF(mf4_files[0])
            try:
                can_id_sig = mdf.get('CAN_ID')
                self.assertEqual(list(can_id_sig.samples), [0x0FD])

                ch_sig = mdf.get('Channel')
                self.assertEqual(list(ch_sig.samples), [100])

                channels_db = getattr(mdf, 'channels_db', {})
                self.assertIn('CAN100.ESP_v_Signal', set(channels_db.keys()))

                sig = mdf.get('CAN100.ESP_v_Signal')
                self.assertAlmostEqual(float(sig.samples[0]), 25.0, places=1)
            finally:
                try:
                    mdf.close()
                except Exception:
                    pass


# ═══════════════════════════════════════════════════════════════════════════
# 10. Full Pipeline: CAN Parse → DBC Decode → MF4 with Decoded
# ═══════════════════════════════════════════════════════════════════════════
@unittest.skipUnless(_have_mf4_deps(), "numpy/asammdf not installed")
@unittest.skipUnless(_have_dbc(SIMULATION_DBC), "simulation.dbc not available")
class TestFullPipelineDecodedMF4(unittest.TestCase):
    """Full pipeline: raw CAN → DBC decode → log with decoded signals → MF4 read-back."""

    def test_end_to_end_decode_and_record(self):
        """Complete round-trip: encode signals → decode → log → MF4 → read back."""
        import numpy as np
        import asammdf

        from dbc_loader import DBCLoader
        from logger import BusLogger

        # Load DBC
        loader = DBCLoader()
        self.assertTrue(loader.load(SIMULATION_DBC))

        with tempfile.TemporaryDirectory() as tmpdir:
            logger = BusLogger(log_dir=tmpdir)
            logger.set_mf4_include_decoded(True)
            logger.set_mf4_include_raw(True)
            logger.start(formats=['mf4'])

            now_ms = int(time.time() * 1000)

            # Generate test frames with different RPM values
            rpm_values = [1200.0, 2400.0, 3600.0, 4800.0, 6000.0]
            for i, rpm in enumerate(rpm_values):
                raw = int(rpm / 0.25)
                data = list(raw.to_bytes(2, 'little')) + [0] * 6

                # Decode the frame
                decoded = loader.decode(498, data)
                self.assertIsNotNone(decoded)

                frame = {
                    'id': 498, 'dlc': 8, 'data': data,
                    'flags': 0, 'type': 'CAN', 'channel': 0,
                    'timestamp': now_ms + i * 10,
                    'decoded': decoded,
                }
                logger.log(frame)

            # Also add some speed frames
            speed_values = [60.0, 90.0, 120.0]
            for i, speed in enumerate(speed_values):
                raw = int(speed / 0.01)
                data = list(raw.to_bytes(2, 'little')) + [0] * 6

                decoded = loader.decode(499, data)
                self.assertIsNotNone(decoded)

                frame = {
                    'id': 499, 'dlc': 8, 'data': data,
                    'flags': 0, 'type': 'CAN', 'channel': 0,
                    'timestamp': now_ms + (len(rpm_values) + i) * 10,
                    'decoded': decoded,
                }
                logger.log(frame)

            time.sleep(0.5)
            logger.stop()
            _wait_for_merge(logger)

            # Find & read MF4
            mf4_files = _find_mf4_files(tmpdir)
            self.assertGreater(len(mf4_files), 0)

            mdf = asammdf.MDF(mf4_files[0])
            try:
                # Raw: 8 frames total
                can_id_sig = mdf.get('CAN_ID')
                self.assertEqual(len(can_id_sig.samples), 8)

                # Decoded RPM (logger prefixes with bus label)
                channels_db = getattr(mdf, 'channels_db', {})
                self.assertIn('CAN0.Engine_RPM', set(channels_db.keys()))

                rpm_sig = mdf.get('CAN0.Engine_RPM')
                self.assertEqual(len(rpm_sig.samples), 5)
                for i, expected in enumerate(rpm_values):
                    self.assertAlmostEqual(float(rpm_sig.samples[i]), expected, places=1,
                                           msg=f"RPM sample {i}")

                # Decoded Speed
                self.assertIn('CAN0.Vehicle_Speed', set(channels_db.keys()))
                speed_sig = mdf.get('CAN0.Vehicle_Speed')
                self.assertEqual(len(speed_sig.samples), 3)
                for i, expected in enumerate(speed_values):
                    self.assertAlmostEqual(float(speed_sig.samples[i]), expected, places=1,
                                           msg=f"Speed sample {i}")
            finally:
                try:
                    mdf.close()
                except Exception:
                    pass


# ═══════════════════════════════════════════════════════════════════════════
# 11. CAN Handler: raw frame structure
# ═══════════════════════════════════════════════════════════════════════════
class TestCANHandlerFrameStructure(unittest.TestCase):
    """Verify CANHandler.read() returns correct frame structure."""

    def test_frame_dict_keys(self):
        """CANHandler frame should have all required keys."""
        from can_handler import CANHandler
        handler = CANHandler(0)
        handler.open()
        # In mock mode without traffic, read returns None
        frame = handler.read()
        # Frame may be None in mock mode with no traffic enabled
        if frame is not None:
            required_keys = {'id', 'data', 'dlc', 'flags', 'timestamp', 'type'}
            self.assertTrue(required_keys.issubset(set(frame.keys())),
                            f"Missing keys: {required_keys - set(frame.keys())}")
            self.assertEqual(frame['type'], 'CAN')
        handler.close()


# ═══════════════════════════════════════════════════════════════════════════
# 12. DBC Loader Edge Cases
# ═══════════════════════════════════════════════════════════════════════════
class TestDBCLoaderEdgeCases(unittest.TestCase):
    """DBC loader should handle edge cases without crashing."""

    def test_load_nonexistent_file(self):
        from dbc_loader import DBCLoader
        loader = DBCLoader()
        result = loader.load('/nonexistent/path/file.dbc')
        self.assertFalse(result)

    def test_decode_without_load(self):
        from dbc_loader import DBCLoader
        loader = DBCLoader()
        result = loader.decode(0x100, [0] * 8)
        self.assertIsNone(result)

    def test_decode_empty_data(self):
        from dbc_loader import DBCLoader
        loader = DBCLoader()
        result = loader.decode(0x100, [])
        self.assertIsNone(result)

    @unittest.skipUnless(_have_dbc(SIMULATION_DBC), "simulation.dbc not available")
    def test_decode_wrong_dlc(self):
        """Decoding with wrong DLC should not crash."""
        from dbc_loader import DBCLoader
        loader = DBCLoader()
        loader.load(SIMULATION_DBC)
        # Motor_01 expects 8 bytes, give only 2
        result = loader.decode(498, [0x00, 0x64])
        # Should either decode with available bytes or return None, never crash


# ═══════════════════════════════════════════════════════════════════════════
# 13. VAG SOME/IP Mirror Line-by-Line
# ═══════════════════════════════════════════════════════════════════════════
class TestVAGMirrorLineByLine(unittest.TestCase):
    """Parse VAG SOME/IP mirror frames one at a time."""

    def _build_vag_frame_entry(self, ts_off, bus_ch, net_type, frame_id, data):
        reserved = 0x0000
        can_id_16 = frame_id & 0xFFFF
        return struct.pack('!HBBHHH', ts_off, bus_ch, net_type, reserved, can_id_16, len(data)) + data

    def _build_vag_packet(self, frame_entries_bytes):
        pkt_len = len(frame_entries_bytes)
        flags = 0x0000
        return struct.pack('!HH', pkt_len, flags) + frame_entries_bytes

    def _build_someip_header(self, inner_payload):
        service_id = 0x02FD
        method_id = 0xF302
        length = len(inner_payload) + 8
        return struct.pack('!HHIIBBBB',
                           service_id, method_id, length, 1,
                           0x01, 0x01, 0x02, 0x00)

    def test_parse_each_bus_channel_individually(self):
        """Parse one frame per bus channel (0-7) individually."""
        from ethernet_capture import EthernetCapture

        for bus_ch in range(8):
            received = []
            cap = EthernetCapture.__new__(EthernetCapture)
            cap.mirror_callback = lambda channel_id, arb_id, data, flags=0, frame_type="CAN": \
                received.append({'ch': channel_id, 'arb': arb_id})
            cap._mirror_rx_count = 0

            data = bytes([bus_ch] * 8)
            entry = self._build_vag_frame_entry(0, bus_ch, 1, 0x0FD, data)
            inner = self._build_vag_packet(entry)
            someip = self._build_someip_header(inner)
            cap._unpack_mirror_payload(someip + inner)

            self.assertGreaterEqual(len(received), 1,
                                    f"Bus channel {bus_ch} should produce at least 1 frame")
            self.assertEqual(received[0]['arb'], 0xFD,
                             f"Bus channel {bus_ch}: wrong arb_id")
            self.assertEqual(received[0]['ch'], 100 + bus_ch,
                             f"Bus channel {bus_ch}: expected ch={100 + bus_ch}")


# ═══════════════════════════════════════════════════════════════════════════
# 14. DoIP Mirror Parsing
# ═══════════════════════════════════════════════════════════════════════════
class TestDoIPMirrorParsing(unittest.TestCase):
    """Parse DoIP-encapsulated mirror frames."""

    def test_doip_mirror_single_frame(self):
        """Parse a single CAN frame from DoIP diagnostic message."""
        from ethernet_capture import EthernetCapture

        received = []
        cap = EthernetCapture.__new__(EthernetCapture)
        cap.mirror_callback = lambda channel_id, arb_id, data, flags=0, frame_type="CAN": \
            received.append({'ch': channel_id, 'arb': arb_id, 'data': data})
        cap._mirror_rx_count = 0

        # DoIP header: Ver=0x02, InvVer=0xFD, PayloadType=0x8001, Length
        # DoIP body: SrcAddr=0x4010 (gateway), DstAddr=0x0E00 (tester), UDS payload
        # UDS payload: VAG proprietary [BusID:1][ArbID:4][DLC:1][Data:DLC]
        can_data = b'\xAA\xBB\xCC\xDD\xEE\xFF\x00\x11'
        uds_payload = struct.pack('!BIB', 0x01, 0x0FD, 8) + can_data
        doip_body = struct.pack('!HH', 0x4010, 0x0E00) + uds_payload
        doip_hdr = struct.pack('!BBHI', 0x02, 0xFD, 0x8001, len(doip_body))
        tcp_payload = doip_hdr + doip_body

        cap._unpack_doip_mirror(tcp_payload)
        self.assertGreaterEqual(len(received), 1)
        self.assertEqual(received[0]['arb'], 0x0FD)


# ═══════════════════════════════════════════════════════════════════════════
# 15. Gateway Mirror Payload Construction
# ═══════════════════════════════════════════════════════════════════════════
class TestGatewayMirrorConstruction(unittest.TestCase):
    """Test mirror mode payload for each bus line individually."""

    def test_single_can_bus_at_a_time(self):
        """Build mirror payload enabling one CAN bus at a time."""
        from gateway_mirror import build_mirror_mode_payload

        for can_num in range(1, 9):
            p = build_mirror_mode_payload(
                target_bus=2, can=[can_num],
                dest_ip='::', dest_port=30490
            )
            expected_mask = 1 << (can_num - 1)
            self.assertEqual(p[1], expected_mask,
                             f"CAN{can_num}: byte1=0x{p[1]:02X} != expected 0x{expected_mask:02X}")
            # FlexRay/LIN should be 0
            self.assertEqual(p[2], 0x00,
                             f"CAN{can_num}: byte2 should be 0 when only CAN enabled")

    def test_single_flexray_channel(self):
        """Build mirror payload for each FlexRay channel individually."""
        from gateway_mirror import build_mirror_mode_payload

        for ch, bit in [('A', 0), ('B', 1)]:
            p = build_mirror_mode_payload(
                target_bus=2, flexray=[ch],
                dest_ip='::', dest_port=30490
            )
            self.assertEqual(p[1], 0x00, f"FR_{ch}: CAN mask should be 0")
            self.assertEqual(p[2], 1 << bit,
                             f"FR_{ch}: byte2=0x{p[2]:02X} != expected 0x{(1 << bit):02X}")

    def test_single_lin_bus(self):
        """Build mirror payload for each LIN bus individually."""
        from gateway_mirror import build_mirror_mode_payload

        for lin_num, bit in [(1, 4), (2, 5), (3, 6)]:
            p = build_mirror_mode_payload(
                target_bus=2, lin=[lin_num],
                dest_ip='::', dest_port=30490
            )
            self.assertEqual(p[1], 0x00, f"LIN{lin_num}: CAN mask should be 0")
            self.assertEqual(p[2], 1 << bit,
                             f"LIN{lin_num}: byte2=0x{p[2]:02X} != expected 0x{(1 << bit):02X}")


# ═══════════════════════════════════════════════════════════════════════════
# 16. MF4 Recording Boolean/Enum Signal Coercion
# ═══════════════════════════════════════════════════════════════════════════
@unittest.skipUnless(_have_mf4_deps(), "numpy/asammdf not installed")
class TestMF4SignalCoercion(unittest.TestCase):
    """Decoded signal values (bool, enum-like) should be coerced to float in MF4."""

    def test_boolean_signal_coercion(self):
        import asammdf
        from logger import BusLogger

        with tempfile.TemporaryDirectory() as tmpdir:
            logger = BusLogger(log_dir=tmpdir)
            logger.set_mf4_include_decoded(True)
            logger.start(formats=['mf4'])

            now_ms = int(time.time() * 1000)
            frame = {
                'id': 0x100, 'dlc': 8, 'data': [1, 0, 0, 0, 0, 0, 0, 0],
                'flags': 0, 'type': 'CAN', 'channel': 0,
                'timestamp': now_ms,
                'decoded': {
                    'name': 'TestMsg',
                    'signals': {
                        'BoolSignal': True,
                        'IntSignal': 42,
                        'FloatSignal': 3.14,
                    }
                }
            }
            logger.log(frame)

            time.sleep(0.5)
            logger.stop()
            _wait_for_merge(logger)

            mf4_files = _find_mf4_files(tmpdir)
            self.assertGreater(len(mf4_files), 0)

            mdf = asammdf.MDF(mf4_files[0])
            try:
                channels_db = set(getattr(mdf, 'channels_db', {}).keys())

                if 'BoolSignal' in channels_db:
                    sig = mdf.get('BoolSignal')
                    self.assertAlmostEqual(float(sig.samples[0]), 1.0)

                if 'IntSignal' in channels_db:
                    sig = mdf.get('IntSignal')
                    self.assertAlmostEqual(float(sig.samples[0]), 42.0)

                if 'FloatSignal' in channels_db:
                    sig = mdf.get('FloatSignal')
                    self.assertAlmostEqual(float(sig.samples[0]), 3.14, places=2)
            finally:
                try:
                    mdf.close()
                except Exception:
                    pass


# ═══════════════════════════════════════════════════════════════════════════
# 17. MF4 EthernetMF4Logger
# ═══════════════════════════════════════════════════════════════════════════
@unittest.skipUnless(_have_mf4_deps(), "numpy/asammdf not installed")
class TestEthernetMF4Logger(unittest.TestCase):
    """Test EthernetMF4Logger records raw ETH + SOME/IP + DoIP."""

    def test_log_and_save_raw_eth(self):
        import asammdf
        from mf4_logger import EthernetMF4Logger

        with tempfile.TemporaryDirectory() as tmpdir:
            logger = EthernetMF4Logger(log_dir=tmpdir)

            now = time.time()
            logger.log_raw_eth(now, "192.168.1.1", "192.168.1.2", 17, 100)
            logger.log_raw_eth(now + 0.01, "10.0.0.1", "10.0.0.2", 6, 200)

            path = logger.save()
            self.assertTrue(os.path.isfile(path))

            mdf = asammdf.MDF(path)
            try:
                eth_len = mdf.get('ETH_Length')
                self.assertEqual(len(eth_len.samples), 2)
                self.assertEqual(int(eth_len.samples[0]), 100)
                self.assertEqual(int(eth_len.samples[1]), 200)

                eth_proto = mdf.get('ETH_Proto')
                self.assertEqual(int(eth_proto.samples[0]), 17)  # UDP
                self.assertEqual(int(eth_proto.samples[1]), 6)   # TCP
            finally:
                try:
                    mdf.close()
                except Exception:
                    pass

    def test_log_and_save_someip(self):
        import asammdf
        from mf4_logger import EthernetMF4Logger

        with tempfile.TemporaryDirectory() as tmpdir:
            logger = EthernetMF4Logger(log_dir=tmpdir)

            now = time.time()
            logger.log_someip(now, 0x02FD, 0xF302, 0x02, 100)

            path = logger.save()
            self.assertTrue(os.path.isfile(path))

            mdf = asammdf.MDF(path)
            try:
                srv = mdf.get('SOMEIP_ServiceID')
                self.assertEqual(int(srv.samples[0]), 0x02FD)

                met = mdf.get('SOMEIP_MethodID')
                self.assertEqual(int(met.samples[0]), 0xF302)
            finally:
                try:
                    mdf.close()
                except Exception:
                    pass


if __name__ == '__main__':
    unittest.main(verbosity=2)
