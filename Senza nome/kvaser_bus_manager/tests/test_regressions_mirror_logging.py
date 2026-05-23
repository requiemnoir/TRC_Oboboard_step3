#!/usr/bin/env python3
"""Regression tests for recent mirror/logging fixes."""

import os
import sys
import tempfile
import time
import unittest
from unittest.mock import MagicMock


BACKEND_DIR = os.path.join(os.path.dirname(__file__), '..', 'backend')
sys.path.insert(0, os.path.abspath(BACKEND_DIR))


def _have_mf4_deps():
    try:
        import numpy  # noqa: F401
        import asammdf  # noqa: F401
        return True
    except ImportError:
        return False


def _wait_for_merge(logger, timeout=10.0):
    merge_thread = getattr(logger, '_mf4_merge_thread', None)
    if merge_thread is not None and merge_thread.is_alive():
        merge_thread.join(timeout=timeout)


def _find_mf4_files(directory):
    mf4_files = []
    for name in os.listdir(directory):
        if not name.endswith('.mf4'):
            continue
        if '.tmp.' in name.lower() or 'error' in name.lower():
            continue
        full_path = os.path.join(directory, name)
        if os.path.isfile(full_path):
            mf4_files.append(full_path)
    merged = [path for path in mf4_files if '_part' not in os.path.basename(path)]
    return merged or mf4_files


@unittest.skipUnless(_have_mf4_deps(), 'numpy/asammdf not installed')
class TestLoggerEventRegression(unittest.TestCase):
    def test_event_markers_do_not_leak_into_raw_mf4(self):
        import asammdf

        from logger import BusLogger

        with tempfile.TemporaryDirectory() as tmpdir:
            logger = BusLogger(log_dir=tmpdir)
            logger.set_mf4_include_decoded(False)
            logger.start(formats=['mf4'])

            now_ms = int(time.time() * 1000)
            logger.log({
                'id': 0x0FD,
                'dlc': 8,
                'data': [0x00, 0x64, 0, 0, 0, 0, 0, 0],
                'flags': 0,
                'type': 'CAN',
                'channel': 100,
                'timestamp': now_ms,
            })
            logger.log({
                'timestamp': now_ms + 5,
                'type': 'EVENT',
                'id': 0,
                'dlc': 0,
                'data': [],
                'flags': 0,
                'decoded': {
                    'event': 'session_marker',
                    'details': {'source': 'test'},
                },
            })
            logger.log({
                'id': 0x0A8,
                'dlc': 8,
                'data': [0x0F, 0xA0, 0, 0, 0, 0, 0, 0],
                'flags': 0,
                'type': 'CAN',
                'channel': 101,
                'timestamp': now_ms + 10,
            })

            time.sleep(0.3)
            logger.stop()
            _wait_for_merge(logger)

            mf4_files = _find_mf4_files(tmpdir)
            self.assertGreater(len(mf4_files), 0)

            mdf = asammdf.MDF(mf4_files[0])
            try:
                ids = list(mdf.get('CAN_ID').samples)
                channels = list(mdf.get('Channel').samples)
                dlc = list(mdf.get('DLC').samples)

                self.assertEqual(ids, [0x0FD, 0x0A8])
                self.assertEqual(channels, [100, 101])
                self.assertEqual(dlc, [8, 8])
                self.assertNotIn(0, channels)
            finally:
                mdf.close()


class TestMirrorCallbackRegression(unittest.TestCase):
    @staticmethod
    def _make_capture(callback):
        from ethernet_capture import EthernetCapture

        cap = EthernetCapture.__new__(EthernetCapture)
        cap.mirror_callback = callback
        cap._mirror_rx_count = 0
        return cap

    def test_emit_mirror_frame_supports_legacy_callback_signature(self):
        received = []
        cap = self._make_capture(
            lambda channel_id, arb_id, data, flags=0, frame_type='CAN': received.append({
                'channel_id': channel_id,
                'arb_id': arb_id,
                'data': data,
                'flags': flags,
                'frame_type': frame_type,
            })
        )

        cap._emit_mirror_frame(0x123, b'\x01\x02\x03', 'CAN', channel_id=107, flags=4)

        self.assertEqual(len(received), 1)
        self.assertEqual(received[0]['channel_id'], 107)
        self.assertEqual(received[0]['arb_id'], 0x123)
        self.assertEqual(received[0]['data'], b'\x01\x02\x03')
        self.assertEqual(received[0]['flags'], 4)
        self.assertEqual(received[0]['frame_type'], 'CAN')
        self.assertEqual(cap._mirror_rx_count, 1)

    def test_emit_mirror_frame_propagates_capture_origin_when_supported(self):
        received = {}

        def callback(channel_id, arb_id, data, flags=0, frame_type='CAN', capture_origin=None):
            received.update({
                'channel_id': channel_id,
                'arb_id': arb_id,
                'data': data,
                'flags': flags,
                'frame_type': frame_type,
                'capture_origin': capture_origin,
            })

        cap = self._make_capture(callback)
        cap._emit_mirror_frame(0x321, b'\xAA\xBB', 'LIN', channel_id=150)

        self.assertEqual(received['channel_id'], 150)
        self.assertEqual(received['arb_id'], 0x321)
        self.assertEqual(received['data'], b'\xAA\xBB')
        self.assertEqual(received['frame_type'], 'LIN')
        self.assertEqual(received['capture_origin'], 'mirror')
        self.assertEqual(cap._mirror_rx_count, 1)


class TestFlexRayTimestampRegression(unittest.TestCase):
    def test_read_uses_timestamp_attribute_when_time_is_missing(self):
        from flexray_handler import FlexRayHandler

        frame = type('Frame', (), {
            'id': 0x44,
            'data': b'\x10\x20\x30\x40',
            'dlc': 4,
            'flags': 7,
            'timestamp': 1234.5,
        })()

        handler = FlexRayHandler(0)
        handler.is_open = True
        handler.ch = MagicMock()
        handler.ch.read.return_value = frame

        result = handler.read()

        self.assertIsNotNone(result)
        self.assertEqual(result['id'], 0x44)
        self.assertEqual(result['data'], [0x10, 0x20, 0x30, 0x40])
        self.assertEqual(result['dlc'], 4)
        self.assertEqual(result['flags'], 7)
        self.assertEqual(result['timestamp'], 1234.5)
        self.assertEqual(result['type'], 'FLEXRAY')


if __name__ == '__main__':
    unittest.main(verbosity=2)