import os
import sys


BACKEND_DIR = os.path.join(os.path.dirname(__file__), "..", "backend")
sys.path.insert(0, os.path.abspath(BACKEND_DIR))


from bus_manager import BusManager


class _SocketIOStub:
    def __init__(self):
        self.events = []

    def emit(self, name, payload):
        self.events.append((name, payload))


class _LoggerStub:
    def log(self, *_args, **_kwargs):
        return None


def test_all_flexray_frames_bypass_batching_by_default():
    socketio = _SocketIOStub()
    manager = BusManager(socketio=socketio, logger=_LoggerStub())
    manager.set_timeline_live_enabled(True)
    manager._ui_emit_interval_s = 60.0
    manager._ui_emit_batch_max = 999

    frame = {
        "channel": 201,
        "id": 29,
        "type": "FlexRay",
        "flags": 12,
        "decoded": {
            "name": "WBA_03 + Getriebe_HYB_11",
            "signals": {
                "GE_Sollgang": 4.0,
            },
        },
    }

    manager._emit_ui_frame(frame)

    assert socketio.events == [("timeline_bus_data", frame)]
    stats = manager.get_ui_stream_stats()
    assert stats["timeline"]["offered_frames"] == 1
    assert stats["timeline"]["emitted_frames"] == 1
    assert stats["timeline"]["immediate_frames"] == 1
    assert stats["config"]["priority_all_flexray"] is True


def test_priority_flexray_frame_bypasses_batching_and_updates_stats():
    socketio = _SocketIOStub()
    manager = BusManager(socketio=socketio, logger=_LoggerStub())
    manager.set_timeline_live_enabled(True)
    manager._ui_emit_interval_s = 60.0
    manager._ui_emit_batch_max = 999

    frame = {
        "channel": 201,
        "id": 8,
        "type": "FlexRay",
        "flags": 4,
        "decoded": {
            "name": "Klemmen_Status_01 + HVLM_03 (+3 more)",
            "signals": {
                "ZAS_Kl_15": 1.0,
                "ZAS_Kl_15_txt": "ein",
            },
        },
    }

    manager._emit_ui_frame(frame)

    assert socketio.events == [("timeline_bus_data", frame)]
    stats = manager.get_ui_stream_stats()
    assert stats["timeline"]["offered_frames"] == 1
    assert stats["timeline"]["emitted_frames"] == 1
    assert stats["timeline"]["immediate_frames"] == 1
    assert stats["timeline"]["sampling_ratio"] == 1.0
    assert stats["queue"]["pending_frames"] == 0


def test_stream_stats_report_sampling_ratio_when_queue_drops_frames():
    socketio = _SocketIOStub()
    manager = BusManager(socketio=socketio, logger=_LoggerStub())
    manager.set_timeline_live_enabled(True)
    manager._ui_emit_interval_s = 60.0
    manager._ui_emit_batch_max = 999
    manager._ui_emit_queue_max = 2
    manager._ui_priority_all_flexray = False

    frames = [
        {"channel": 201, "id": 29, "type": "FlexRay", "flags": idx, "decoded": {"name": "WBA_03 + Getriebe_HYB_11", "signals": {"GE_Sollgang": 0.0}}}
        for idx in range(3)
    ]

    for frame in frames:
        manager._emit_ui_frame(frame)

    if manager._ui_emit_timer is not None:
        manager._ui_emit_timer.cancel()
        manager._ui_emit_timer = None

    manager._flush_ui_frames()

    emitted_events = [event for event, _payload in socketio.events]
    assert emitted_events == ["timeline_bus_data_batch"]
    stats = manager.get_ui_stream_stats()
    assert stats["timeline"]["offered_frames"] == 3
    assert stats["timeline"]["dropped_frames"] == 1
    assert stats["timeline"]["emitted_frames"] == 2
    assert stats["timeline"]["sampling_ratio"] == 0.6667
    assert stats["timeline"]["drop_ratio"] == 0.3333