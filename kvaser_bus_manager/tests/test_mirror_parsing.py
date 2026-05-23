#!/usr/bin/env python3
"""Test suite for Gateway Mirror parsing and message decoding.

Validates the complete mirror data chain:
  1. ARXML catalog parsing → real vehicle CAN frame IDs
  2. Gateway mirror payload construction (UDS DID 0x096F)
  3. AUTOSAR Bus Mirroring PDU format decoding
  4. Iron Bird custom format decoding (legacy)
  5. SOME/IP header parsing
  6. Port alignment between mirror config and capture

Uses real MLBevo CAN frame definitions from the ARXML database.
"""
import os
import sys
import struct
import unittest

# Ensure backend is on the path
BACKEND_DIR = os.path.join(os.path.dirname(__file__), '..', 'backend')
sys.path.insert(0, os.path.abspath(BACKEND_DIR))

from someip_parser import parse_someip, SomeIpHeader
from gateway_mirror import (
    build_mirror_mode_payload,
    build_mirror_mode_write_request,
    default_mirror_definition,
    _encode_ip_16,
)
from ethernet_capture import DEFAULT_MIRROR_PORT

# ─── Known VAG MLBevo CAN Frame IDs (from ARXML) ───────────────────────────
# These are verified against:
#   MLBevo_Gen1_Autosar_V8.21.05F_20210616_EICR.arxml
KNOWN_FRAMES = {
    'ESP_21':       {'id': 0x0FD, 'dlc': 8, 'bus': 'CCAN'},
    'ESP_03':       {'id': 0x103, 'dlc': 8, 'bus': 'CCAN'},
    'ESP_10':       {'id': 0x116, 'dlc': 8, 'bus': 'ECAN'},
    'Motor_11':     {'id': 0x0A7, 'dlc': 8, 'bus': 'ECAN'},
    'Motor_12':     {'id': 0x0A8, 'dlc': 8, 'bus': 'CCAN'},
    'Motor_20':     {'id': 0x121, 'dlc': 8, 'bus': 'ECAN'},
    'Getriebe_11':  {'id': 0x0AD, 'dlc': 8, 'bus': 'ECAN'},
    'Bremse_EV_01': {'id': 0x0B3, 'dlc': 8, 'bus': 'ECAN'},
    'Airbag_01':    {'id': 0x040, 'dlc': 8, 'bus': 'CCAN'},
    'Kombi_01':     {'id': 0x30B, 'dlc': 8, 'bus': 'CCAN'},
}

# ─── AUTOSAR Bus Mirroring Status Byte Definitions ─────────────────────────
# Per AUTOSAR SWS_BusMirroring (SRS_BusMirroring_xxxxx):
#   Status byte layout for CAN frames:
#     bit 7:   FrameID type (0=Standard, 1=Extended)
#     bit 6-4: NetworkType (0=CAN, 1=LIN, 2=FlexRay, 3=Ethernet)
#     bit 3-0: NetworkID (index of the source bus, 0-based)
MIRROR_NETWORK_CAN     = 0b000
MIRROR_NETWORK_LIN     = 0b001
MIRROR_NETWORK_FLEXRAY = 0b010
MIRROR_NETWORK_ETH     = 0b011

# VAG MLBevo bus indices (typical mapping from DID 0x2A20):
#   0 = ECAN, 1 = HCAN, 2 = ICAN, 3 = KCAN, 4 = CCAN, 5 = DiagCAN
VAG_BUS_INDEX = {
    'ECAN': 0,
    'HCAN': 1,
    'ICAN': 2,
    'KCAN': 3,
    'CCAN': 4,
    'DiagCAN': 5,
}


def build_autosar_mirror_pdu(frames, *, timestamp_us=None):
    """Build an AUTOSAR Bus Mirroring PDU payload.

    AUTOSAR Mirror PDU format (per SWS_BusMirroring):
      [2 bytes] Sequence Counter (uint16 big-endian)
      [N * mirror_element] Mirror data elements

    Each CAN mirror element:
      [4 bytes] Timestamp (uint32 big-endian, microseconds relative)
      [1 byte]  Status (NetworkType[6:4] | NetworkID[3:0] | FrameIDType[7])
      [1 byte]  Payload length (DLC, 0-8 for classic CAN, 0-64 for CAN-FD)
      [4 bytes] CAN ID (uint32 big-endian, bits 28:0 = frame ID)
      [N bytes] CAN data payload

    Args:
        frames: list of dicts with keys: arb_id, data, bus (optional), timestamp_us (optional)
        timestamp_us: base timestamp in microseconds (default: 0)
    """
    seq_counter = 1
    pdu = struct.pack('>H', seq_counter)

    base_ts = timestamp_us or 0
    for i, fr in enumerate(frames):
        arb_id = int(fr.get('arb_id', 0))
        data = bytes(fr.get('data', b''))
        dlc = len(data)
        bus = str(fr.get('bus', 'CCAN'))
        ts = int(fr.get('timestamp_us', base_ts + i * 1000))

        # Status byte
        network_type = MIRROR_NETWORK_CAN
        network_id = VAG_BUS_INDEX.get(bus, 0) & 0x0F
        frame_id_type = 1 if arb_id > 0x7FF else 0
        status = (frame_id_type << 7) | (network_type << 4) | network_id

        # CAN ID: standard = bits 10:0, extended = bits 28:0
        can_id_field = arb_id & 0x1FFFFFFF

        pdu += struct.pack('>I', ts & 0xFFFFFFFF)   # Timestamp
        pdu += struct.pack('B', status)               # Status
        pdu += struct.pack('B', dlc)                  # Payload length
        pdu += struct.pack('>I', can_id_field)        # CAN ID
        pdu += data                                   # Payload

    return pdu


def build_iron_bird_payload(frames):
    """Build Iron Bird protocol payload (legacy custom format).

    Format: repeating blocks of [Magic:2][ArbID:4][DLC:1][Data:8] = 15 bytes
    Magic = 0xD00D
    """
    payload = b''
    for fr in frames:
        arb_id = int(fr.get('arb_id', 0))
        data = bytes(fr.get('data', b''))
        dlc = len(data)
        # Pad data to 8 bytes
        padded = data + b'\x00' * (8 - len(data))
        payload += struct.pack('>HI B', 0xD00D, arb_id, dlc) + padded[:8]
    return payload


def parse_autosar_mirror_pdu(payload):
    """Parse AUTOSAR Bus Mirroring PDU and return list of decoded frames.

    Returns list of dicts: {arb_id, data, dlc, bus_type, network_id, timestamp_us, extended}
    """
    if len(payload) < 2:
        return []

    seq_counter = struct.unpack('>H', payload[0:2])[0]
    offset = 2
    frames = []

    while offset + 10 <= len(payload):  # min element: 4+1+1+4+0 = 10 bytes
        ts = struct.unpack('>I', payload[offset:offset + 4])[0]
        status = payload[offset + 4]
        dlc = payload[offset + 5]
        can_id = struct.unpack('>I', payload[offset + 6:offset + 10])[0]

        frame_id_type = (status >> 7) & 1
        network_type = (status >> 4) & 0x07
        network_id = status & 0x0F

        # Validate DLC
        if dlc > 64:  # CAN-FD max
            break

        if offset + 10 + dlc > len(payload):
            break

        data = payload[offset + 10:offset + 10 + dlc]

        # Resolve bus type
        bus_types = {0: 'CAN', 1: 'LIN', 2: 'FLEXRAY', 3: 'ETHERNET'}
        bus_type = bus_types.get(network_type, 'UNKNOWN')

        frames.append({
            'arb_id': can_id,
            'data': data,
            'dlc': dlc,
            'bus_type': bus_type,
            'network_id': network_id,
            'timestamp_us': ts,
            'extended': bool(frame_id_type),
            'seq_counter': seq_counter,
        })

        offset += 10 + dlc

    return frames


# ════════════════════════════════════════════════════════════════════════════
# Test Cases
# ════════════════════════════════════════════════════════════════════════════

class TestSomeIpParser(unittest.TestCase):
    """Test the corrected SOME/IP header parser."""

    def test_basic_parse(self):
        msg_id = (0x1234 << 16) | 0x0001
        length = 12
        req_id = (0x00AB << 16) | 0x00CD
        hdr = struct.pack('!IIIBBBB', msg_id, length, req_id, 0x01, 0x02, 0x03, 0x04)
        hdr += b'\x00' * 4  # payload

        result = parse_someip(hdr)
        self.assertIsNotNone(result)
        self.assertEqual(result.service_id, 0x1234)
        self.assertEqual(result.method_id, 0x0001)
        self.assertEqual(result.length, length)
        self.assertEqual(result.client_id, 0x00AB)
        self.assertEqual(result.session_id, 0x00CD)
        self.assertEqual(result.proto_ver, 0x01)
        self.assertEqual(result.iface_ver, 0x02)
        self.assertEqual(result.msg_type, 0x03)
        self.assertEqual(result.ret_code, 0x04)

    def test_short_payload_returns_none(self):
        self.assertIsNone(parse_someip(b'\x00' * 15))
        self.assertIsNone(parse_someip(b''))

    def test_exactly_16_bytes(self):
        hdr = struct.pack('!IIIBBBB', 0, 8, 0, 1, 1, 0, 0)
        result = parse_someip(hdr)
        self.assertIsNotNone(result)

    def test_notification_type(self):
        """SOME/IP Notification: msg_type=0x02"""
        msg_id = (0xFFFF << 16) | 0x8001
        hdr = struct.pack('!IIIBBBB', msg_id, 8, 0, 1, 1, 0x02, 0)
        result = parse_someip(hdr)
        self.assertIsNotNone(result)
        self.assertEqual(result.service_id, 0xFFFF)
        self.assertEqual(result.method_id, 0x8001)
        self.assertEqual(result.msg_type, 0x02)


class TestGatewayMirrorPayload(unittest.TestCase):
    """Test mirror mode payload construction (UDS DID 0x096F)."""

    def test_basic_payload_length(self):
        payload = build_mirror_mode_payload(
            target_bus='ethernet',
            can=[2],
            dest_ip='fe80::1',
            dest_port=30490,
        )
        self.assertEqual(len(payload), 21, "Mirror mode payload must be 21 bytes")

    def test_target_bus_byte(self):
        p_eth = build_mirror_mode_payload(target_bus='ethernet', dest_ip='::', dest_port=0)
        self.assertEqual(p_eth[0], 2, "ethernet = 0x02")

        p_off = build_mirror_mode_payload(target_bus='not_active', dest_ip='::', dest_port=0)
        self.assertEqual(p_off[0], 0, "not_active = 0x00")

        p_diag = build_mirror_mode_payload(target_bus='can_diagnostic', dest_ip='::', dest_port=0)
        self.assertEqual(p_diag[0], 1, "can_diagnostic = 0x01")

    def test_can_bus_mask(self):
        p = build_mirror_mode_payload(target_bus=2, can=[1, 3, 5], dest_ip='::', dest_port=0)
        # CAN1=bit0, CAN3=bit2, CAN5=bit4
        expected_mask = (1 << 0) | (1 << 2) | (1 << 4)
        self.assertEqual(p[1], expected_mask)

    def test_flexray_lin_mask(self):
        p = build_mirror_mode_payload(
            target_bus=2,
            flexray=['A', 'B'],
            lin=[1, 3],
            dest_ip='::',
            dest_port=0,
        )
        # FR_A=bit0, FR_B=bit1, LIN1=bit4, LIN3=bit6
        expected = (1 << 0) | (1 << 1) | (1 << 4) | (1 << 6)
        self.assertEqual(p[2], expected)

    def test_ipv4_mapping(self):
        p = build_mirror_mode_payload(target_bus=2, dest_ip='192.168.1.100', dest_port=30490)
        # IPv4-mapped IPv6: first 10 bytes zero, then 0xFFFF, then IPv4
        ip_bytes = p[3:19]
        self.assertEqual(ip_bytes[:10], b'\x00' * 10)
        self.assertEqual(ip_bytes[10:12], b'\xff\xff')
        self.assertEqual(ip_bytes[12:16], bytes([192, 168, 1, 100]))

    def test_ipv6_encoding(self):
        p = build_mirror_mode_payload(
            target_bus=2,
            dest_ip='fe80::1363:5912:4983:1837',
            dest_port=30490,
        )
        ip_bytes = p[3:19]
        self.assertEqual(len(ip_bytes), 16)
        self.assertEqual(ip_bytes[0:2], b'\xfe\x80')

    def test_port_encoding(self):
        p = build_mirror_mode_payload(target_bus=2, dest_ip='::', dest_port=30490)
        port_bytes = p[19:21]
        port_val = struct.unpack('>H', port_bytes)[0]
        self.assertEqual(port_val, 30490)

    def test_write_request_did(self):
        req = build_mirror_mode_write_request(
            did=0x096F,
            target_bus='ethernet',
            dest_ip='::1',
            dest_port=30490,
        )
        self.assertEqual(req.did, 0x096F)
        self.assertEqual(len(req.payload), 21)


class TestPortAlignment(unittest.TestCase):
    """Verify port alignment between mirror config and capture."""

    def test_default_mirror_port_matches_config(self):
        """DEFAULT_MIRROR_PORT in ethernet_capture must match the standard mirror port."""
        self.assertEqual(DEFAULT_MIRROR_PORT, 30490,
                         "Capture must listen on 30490 to match gateway mirror dest_port")

    def test_default_mirror_definition_structure(self):
        d = default_mirror_definition()
        self.assertIn('did', d)
        self.assertIn('fields', d)
        self.assertIn('port', d['fields'])
        self.assertEqual(d['fields']['port']['byte_offset'], 19)
        self.assertEqual(d['fields']['port']['length'], 2)


class TestAutosarMirrorPduParsing(unittest.TestCase):
    """Test AUTOSAR Bus Mirroring PDU format parsing with real VAG frame IDs."""

    def test_single_can_frame(self):
        """Parse a single ESP_21 CAN frame from mirror PDU."""
        esp_data = bytes([0x00, 0x64, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00])
        pdu = build_autosar_mirror_pdu([
            {'arb_id': KNOWN_FRAMES['ESP_21']['id'], 'data': esp_data, 'bus': 'CCAN'}
        ])

        frames = parse_autosar_mirror_pdu(pdu)
        self.assertEqual(len(frames), 1)
        f = frames[0]
        self.assertEqual(f['arb_id'], 0x0FD)
        self.assertEqual(f['dlc'], 8)
        self.assertEqual(f['data'], esp_data)
        self.assertEqual(f['bus_type'], 'CAN')
        self.assertEqual(f['network_id'], VAG_BUS_INDEX['CCAN'])
        self.assertFalse(f['extended'])

    def test_multiple_frames(self):
        """Parse multiple real vehicle CAN frames in one mirror PDU."""
        test_frames = [
            {'arb_id': KNOWN_FRAMES['ESP_21']['id'],
             'data': b'\x00\x64\x00\x00\x00\x00\x00\x00',
             'bus': 'CCAN'},
            {'arb_id': KNOWN_FRAMES['Motor_12']['id'],
             'data': b'\x0F\xA0\x00\x00\x00\x00\x00\x00',
             'bus': 'CCAN'},
            {'arb_id': KNOWN_FRAMES['Getriebe_11']['id'],
             'data': b'\x03\x00\x00\x00\x00\x00\x00\x00',
             'bus': 'ECAN'},
            {'arb_id': KNOWN_FRAMES['Airbag_01']['id'],
             'data': b'\x00\x00\x00\x00\x00\x00\x00\x00',
             'bus': 'CCAN'},
        ]

        pdu = build_autosar_mirror_pdu(test_frames)
        parsed = parse_autosar_mirror_pdu(pdu)

        self.assertEqual(len(parsed), 4)

        # Verify ESP_21
        self.assertEqual(parsed[0]['arb_id'], 0x0FD)
        self.assertEqual(parsed[0]['network_id'], VAG_BUS_INDEX['CCAN'])

        # Verify Motor_12
        self.assertEqual(parsed[1]['arb_id'], 0x0A8)
        self.assertEqual(parsed[1]['data'], b'\x0F\xA0\x00\x00\x00\x00\x00\x00')

        # Verify Getriebe_11 on ECAN
        self.assertEqual(parsed[2]['arb_id'], 0x0AD)
        self.assertEqual(parsed[2]['network_id'], VAG_BUS_INDEX['ECAN'])

        # Verify Airbag_01
        self.assertEqual(parsed[3]['arb_id'], 0x040)

    def test_extended_frame_id(self):
        """Verify extended (29-bit) CAN IDs are parsed correctly."""
        ext_id = 0x18FEF100  # J1939-style extended ID
        pdu = build_autosar_mirror_pdu([
            {'arb_id': ext_id, 'data': b'\xAA\xBB\xCC\xDD', 'bus': 'CCAN'}
        ])
        parsed = parse_autosar_mirror_pdu(pdu)
        self.assertEqual(len(parsed), 1)
        self.assertEqual(parsed[0]['arb_id'], ext_id)
        self.assertTrue(parsed[0]['extended'])
        self.assertEqual(parsed[0]['dlc'], 4)

    def test_empty_payload(self):
        parsed = parse_autosar_mirror_pdu(b'')
        self.assertEqual(parsed, [])

    def test_too_short_payload(self):
        parsed = parse_autosar_mirror_pdu(b'\x00')
        self.assertEqual(parsed, [])

    def test_different_bus_types(self):
        """Verify bus type identification from status byte."""
        for bus, idx in VAG_BUS_INDEX.items():
            pdu = build_autosar_mirror_pdu([
                {'arb_id': 0x100, 'data': b'\x00', 'bus': bus}
            ])
            parsed = parse_autosar_mirror_pdu(pdu)
            self.assertEqual(len(parsed), 1, f"Failed for bus {bus}")
            self.assertEqual(parsed[0]['network_id'], idx,
                             f"Wrong network_id for bus {bus}")


class TestIronBirdParsing(unittest.TestCase):
    """Test Iron Bird (legacy custom) mirror format."""

    def test_single_frame(self):
        """Parse a single frame in Iron Bird format."""
        payload = build_iron_bird_payload([
            {'arb_id': 0x0FD, 'data': b'\x00\x64\x00\x00\x00\x00\x00\x00'}
        ])
        self.assertEqual(len(payload), 15, "Iron Bird block = 15 bytes")

        # Manually parse like ethernet_capture does
        magic, arb_id, dlc = struct.unpack('>HIB', payload[0:7])
        self.assertEqual(magic, 0xD00D)
        self.assertEqual(arb_id, 0x0FD)
        self.assertEqual(dlc, 8)

    def test_multiple_frames(self):
        frames = [
            {'arb_id': 0x0FD, 'data': b'\x00\x64\x00\x00\x00\x00\x00\x00'},
            {'arb_id': 0x0A8, 'data': b'\x0F\xA0\x00\x00\x00\x00\x00\x00'},
        ]
        payload = build_iron_bird_payload(frames)
        self.assertEqual(len(payload), 30, "2 frames * 15 bytes")

        # Parse first
        magic1, arb1, dlc1 = struct.unpack('>HIB', payload[0:7])
        self.assertEqual(arb1, 0x0FD)

        # Parse second
        magic2, arb2, dlc2 = struct.unpack('>HIB', payload[15:22])
        self.assertEqual(arb2, 0x0A8)

    def test_short_dlc(self):
        """Frame with less than 8 bytes of data."""
        payload = build_iron_bird_payload([
            {'arb_id': 0x100, 'data': b'\xAA\xBB'}
        ])
        magic, arb_id, dlc = struct.unpack('>HIB', payload[0:7])
        self.assertEqual(dlc, 2)
        data_bytes = payload[7:15]
        self.assertEqual(data_bytes[:2], b'\xAA\xBB')


class TestArxmlIntegration(unittest.TestCase):
    """Integration test: parse real ARXML and verify frame IDs match."""

    ARXML_PATH = os.path.join(
        os.path.dirname(__file__), '..', 'databases', 'arxml',
        'MLBevo_Gen1_Autosar_V8.21.05F_20210616_EICR.arxml'
    )

    @unittest.skipUnless(
        os.path.isfile(os.path.join(
            os.path.dirname(__file__), '..', 'databases', 'arxml',
            'MLBevo_Gen1_Autosar_V8.21.05F_20210616_EICR.arxml'
        )),
        "ARXML file not available"
    )
    def test_arxml_frame_ids_match_known(self):
        """Verify the ARXML parser extracts correct CAN IDs for known frames."""
        from arxml_parser import parse_arxml

        cat = parse_arxml(self.ARXML_PATH)
        self.assertGreater(len(cat.frames), 100, "Should parse many frames")

        for name, expected in KNOWN_FRAMES.items():
            # Find matching frame (may have suffix like _XIX_MLBevo_CCAN)
            matches = [
                f for f in cat.frames.values()
                if name in f.short_name and f.frame_id > 0
            ]
            self.assertTrue(
                len(matches) > 0,
                f"Frame '{name}' not found in ARXML catalog"
            )
            found_id = matches[0].frame_id
            self.assertEqual(
                found_id, expected['id'],
                f"Frame '{name}': ARXML ID=0x{found_id:03X} != expected 0x{expected['id']:03X}"
            )

    @unittest.skipUnless(
        os.path.isfile(os.path.join(
            os.path.dirname(__file__), '..', 'databases', 'arxml',
            'MLBevo_Gen1_Autosar_V8.21.05F_20210616_EICR.arxml'
        )),
        "ARXML file not available"
    )
    def test_build_and_parse_mirror_with_arxml_ids(self):
        """Full round-trip: ARXML IDs → build mirror PDU → parse → verify."""
        from arxml_parser import parse_arxml

        cat = parse_arxml(self.ARXML_PATH)

        # Build mirror data using real ARXML frame IDs
        mirror_frames = []
        for name, expected in KNOWN_FRAMES.items():
            matches = [f for f in cat.frames.values()
                       if name in f.short_name and f.frame_id > 0]
            if matches:
                arxml_frame = matches[0]
                fake_data = bytes([i & 0xFF for i in range(arxml_frame.frame_length or 8)])
                bus = expected['bus']
                mirror_frames.append({
                    'arb_id': arxml_frame.frame_id,
                    'data': fake_data[:8],  # Classic CAN max 8
                    'bus': bus,
                })

        self.assertGreater(len(mirror_frames), 5)

        # Build AUTOSAR mirror PDU
        pdu = build_autosar_mirror_pdu(mirror_frames)
        self.assertGreater(len(pdu), 2)

        # Parse it back
        parsed = parse_autosar_mirror_pdu(pdu)
        self.assertEqual(len(parsed), len(mirror_frames))

        # Verify each frame
        for i, (orig, decoded) in enumerate(zip(mirror_frames, parsed)):
            self.assertEqual(
                decoded['arb_id'], orig['arb_id'],
                f"Frame {i}: ID mismatch"
            )
            self.assertEqual(
                decoded['data'], orig['data'],
                f"Frame {i}: data mismatch"
            )
            self.assertEqual(
                decoded['network_id'],
                VAG_BUS_INDEX.get(orig['bus'], 0),
                f"Frame {i}: bus index mismatch"
            )


class TestVagMirrorParsing(unittest.TestCase):
    """Test VAG / MLBevo proprietary bus mirroring format (SOME/IP-wrapped)."""

    def _build_vag_frame_entry(self, ts_off, bus_ch, net_type, frame_id, data):
                """Build a single VAG mirror frame entry for unit tests.

                The live-capture parser uses a resync scanner with a conservative, checksum-free
                layout candidate to safely extract classic CAN frames:
                    [TsOffset:2][BusCh:1][NetType:1][Reserved:2][CAN_ID:2][DLC:2][Data:DLC]

                This builder intentionally emits that layout so the tests exercise the same
                logic the real mirror path uses.
                """
                reserved = 0x0000
                can_id_16 = frame_id & 0xFFFF
                return struct.pack('!HBBHHH', ts_off, bus_ch, net_type, reserved, can_id_16, len(data)) + data

    def _build_vag_packet(self, frame_entries_bytes):
        """Wrap frame entries in a 4-byte VAG packet header."""
        pkt_len = len(frame_entries_bytes)
        flags = 0x0000
        return struct.pack('!HH', pkt_len, flags) + frame_entries_bytes

    def _build_someip_header(self, inner_payload):
        """Build a 16-byte SOME/IP header for Service 0x02FD, Method 0xF302."""
        service_id = 0x02FD
        method_id = 0xF302
        length = len(inner_payload) + 8  # SOME/IP length field includes 8 bytes after it
        req_id = 0x00000001
        proto_ver = 0x01
        iface_ver = 0x01
        msg_type = 0x02  # notification
        return_code = 0x00
        return struct.pack('!HHIIBBBB',
                           service_id, method_id, length, req_id,
                           proto_ver, iface_ver, msg_type, return_code)

    def _make_capture(self, received):
        """Create a minimal EthernetCapture with a mock mirror_callback."""
        from ethernet_capture import EthernetCapture
        cap = EthernetCapture.__new__(EthernetCapture)
        cap.mirror_callback = lambda channel_id, arb_id, data, flags=0, frame_type="CAN": \
            received.append({'ch': channel_id, 'arb': arb_id, 'data': data, 'ft': frame_type})
        cap._mirror_count = 0
        cap._mirror_errors = 0
        cap._mirror_rx_count = 0
        return cap

    def test_single_can_frame(self):
        """Parse a single CAN frame from VAG mirror format."""
        received = []
        cap = self._make_capture(received)

        data = b'\x00\x64\x00\x00\x00\x00\x00\x00'
        entry = self._build_vag_frame_entry(0x0000, 0, 1, 0x00FD, data)
        inner = self._build_vag_packet(entry)
        someip = self._build_someip_header(inner)
        cap._unpack_mirror_payload(someip + inner)

        self.assertEqual(len(received), 1)
        self.assertEqual(received[0]['arb'], 0xFD)
        self.assertEqual(received[0]['data'], data)
        self.assertEqual(received[0]['ch'], 100)  # bus_ch=0 → CAN1=100
        self.assertEqual(received[0]['ft'], 'CAN')

    def test_multiple_can_frames_different_buses(self):
        """Parse multiple CAN frames on different buses."""
        received = []
        cap = self._make_capture(received)

        entries = b''
        test_data = [
            (0, 1, 0x00FD, b'\x01' * 8),   # CAN1 ESP_21
            (3, 1, 0x00A8, b'\x02' * 8),   # CAN4 Motor_12
            (7, 1, 0x0116, b'\x03' * 8),   # CAN8 Bremse_5
        ]
        for bus, net, fid, dat in test_data:
            entries += self._build_vag_frame_entry(0, bus, net, fid, dat)
        inner = self._build_vag_packet(entries)
        someip = self._build_someip_header(inner)
        cap._unpack_mirror_payload(someip + inner)

        self.assertEqual(len(received), 3)
        self.assertEqual(received[0]['arb'], 0xFD)
        self.assertEqual(received[0]['ch'], 100)
        self.assertEqual(received[1]['arb'], 0xA8)
        self.assertEqual(received[1]['ch'], 103)
        self.assertEqual(received[2]['arb'], 0x116)
        self.assertEqual(received[2]['ch'], 107)

    def test_status_blocks_skipped(self):
        """Bus-status blocks (NetworkType=0) should be skipped."""
        received = []
        cap = self._make_capture(received)
        # adapt received format for this test
        cap.mirror_callback = lambda channel_id, arb_id, data, flags=0, frame_type="CAN": \
            received.append({'arb': arb_id})

        # Status block with 34 bytes of status data
        status_entry = self._build_vag_frame_entry(0, 0, 0, 0x0000, b'\x00' * 34)
        # Real CAN frame
        can_entry = self._build_vag_frame_entry(0, 1, 1, 0x00A7, b'\xAA' * 8)
        inner = self._build_vag_packet(status_entry + can_entry)
        someip = self._build_someip_header(inner)
        cap._unpack_mirror_payload(someip + inner)

        self.assertEqual(len(received), 1)
        self.assertEqual(received[0]['arb'], 0xA7)  # Only the CAN frame

    def test_empty_vag_payload(self):
        """Empty VAG payload (only header, no frames) should return False."""
        from ethernet_capture import EthernetCapture
        cap = EthernetCapture.__new__(EthernetCapture)
        cap.mirror_callback = None
        cap._mirror_count = 0
        cap._mirror_errors = 0
        cap._mirror_rx_count = 0

        # Just the 4-byte packet header, no frames
        result = cap._try_unpack_vag_mirror(struct.pack('!HH', 0, 0))
        self.assertFalse(result)

    def test_known_arxml_ids_via_vag_format(self):
        """All known CAN IDs from ARXML should parse correctly via VAG format."""
        received = []
        cap = self._make_capture(received)
        # adapt callback
        cap.mirror_callback = lambda channel_id, arb_id, data, flags=0, frame_type="CAN": \
            received.append({'arb': arb_id, 'data': data})

        entries = b''
        expected_ids = []
        for name, info in KNOWN_FRAMES.items():
            fid = info['id']
            data = bytes([fid & 0xFF] * info['dlc'])
            entries += self._build_vag_frame_entry(0, 0, 1, fid, data)
            expected_ids.append(fid)

        inner = self._build_vag_packet(entries)
        someip = self._build_someip_header(inner)
        cap._unpack_mirror_payload(someip + inner)

        self.assertEqual(len(received), len(KNOWN_FRAMES))
        parsed_ids = [r['arb'] for r in received]
        for eid in expected_ids:
            self.assertIn(eid, parsed_ids, f"CAN ID 0x{eid:03X} not found in parsed output")


class TestVagMirrorGoldenSample(unittest.TestCase):
    """Regression tests using a real captured mirror packet."""

    def test_parse_real_someip_payload_extracts_known_ids(self):
        """Parse a real SOME/IP mirror UDP payload and extract plausible CAN frames."""
        import os

        # backend is already on path at top of file
        from ethernet_capture import EthernetCapture

        sample_path = os.path.join(os.path.dirname(__file__), 'data', 'vag_mirror_single_payload.bin')
        self.assertTrue(os.path.exists(sample_path), f"Missing golden sample: {sample_path}")

        payload = open(sample_path, 'rb').read()
        self.assertGreater(len(payload), 64)

        received = []
        cap = EthernetCapture.__new__(EthernetCapture)
        cap.mirror_callback = lambda channel_id, arb_id, data, flags=0, frame_type="CAN": \
            received.append((channel_id, arb_id, data, frame_type))
        cap._mirror_rx_count = 0
        cap._mirror_count = 0
        cap._mirror_errors = 0

        cap._unpack_mirror_payload(payload)

        # Must decode at least a handful of frames in this known-good capture.
        self.assertGreaterEqual(len(received), 5)

        ids = {arb for _, arb, _, ft in received if ft == 'CAN'}
        fr_slots = {arb for _, arb, _, ft in received if ft == 'FlexRay'}

        # These CAN IDs are reliably present in the sample capture.
        for must in (0x3D5, 0x0086, 0x0108):
            self.assertIn(must, ids, f"Expected CAN ID 0x{must:03X} not found")

        # FlexRay slots should also be extracted (the payload contains mixed traffic).
        self.assertGreater(len(fr_slots), 0, "Expected FlexRay frames in golden sample")


class TestMirrorEndToEnd(unittest.TestCase):
    """End-to-end test: simulate mirror callback chain."""

    def test_callback_receives_frames(self):
        """Simulate EthernetCapture mirror callback with real CAN IDs."""
        received = []

        def mock_callback(channel_id, arb_id, data, flags=0, frame_type="CAN"):
            received.append({
                'channel_id': channel_id,
                'arb_id': arb_id,
                'data': data,
                'flags': flags,
                'frame_type': frame_type,
            })

        # Build Iron Bird payload (current format in ethernet_capture.py)
        test_frames = [
            {'arb_id': 0x0FD, 'data': b'\x00\x64\x00\x00\x00\x00\x00\x00'},
            {'arb_id': 0x0A8, 'data': b'\x0F\xA0\x00\x00\x00\x00\x00\x00'},
            {'arb_id': 0x040, 'data': b'\x00\x00\x00\x00\x00\x00\x00\x00'},
        ]
        payload = build_iron_bird_payload(test_frames)

        # Simulate the parsing loop from ethernet_capture._process_packet
        block_size = 15
        offset = 0
        while offset + block_size <= len(payload):
            magic, arb_id, dlc = struct.unpack('>HIB', payload[offset:offset + 7])
            if magic == 0xD00D:
                data_bytes = payload[offset + 7:offset + 7 + 8]
                real_data = data_bytes[:min(dlc, 8)]
                mock_callback(
                    channel_id=99,
                    arb_id=arb_id,
                    data=real_data,
                    flags=0,
                    frame_type="CAN"
                )
            offset += block_size

        self.assertEqual(len(received), 3)
        self.assertEqual(received[0]['arb_id'], 0x0FD)  # ESP_21
        self.assertEqual(received[1]['arb_id'], 0x0A8)  # Motor_12
        self.assertEqual(received[2]['arb_id'], 0x040)  # Airbag_01
        # All should be on virtual mirror channel
        for r in received:
            self.assertEqual(r['channel_id'], 99)
            self.assertEqual(r['frame_type'], 'CAN')


if __name__ == '__main__':
    unittest.main(verbosity=2)
