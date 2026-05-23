import os
import sys
import types


BACKEND_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', 'backend'))
if BACKEND_DIR not in sys.path:
    sys.path.insert(0, BACKEND_DIR)


def test_can_handler_uses_silent_mode_when_requested(monkeypatch):
    import can_handler

    calls = {}

    class FakeChannel:
        def setBusOutputControl(self, value):
            calls['output_mode'] = value

        def setBusParams(self, value):
            calls['bitrate'] = value

        def busOn(self):
            calls['bus_on'] = True

        def busOff(self):
            calls['bus_off'] = True

        def close(self):
            calls['closed'] = True

    fake_canlib = types.SimpleNamespace(
        canBITRATE_500K=-2,
        canDRIVER_NORMAL=4,
        canDRIVER_SILENT=1,
        canOPEN_ACCEPT_VIRTUAL=32,
        canError=RuntimeError,
        canNoMsg=RuntimeError,
        openChannel=lambda channel, flags: calls.setdefault('open', {'channel': channel, 'flags': flags}) or FakeChannel(),
    )

    def fake_open_channel(channel, flags):
        calls['open'] = {'channel': channel, 'flags': flags}
        return FakeChannel()

    fake_canlib.openChannel = fake_open_channel

    monkeypatch.setattr(can_handler, 'canlib', fake_canlib)
    monkeypatch.setenv('KBSM_CAN_LISTEN_ONLY', '1')

    handler = can_handler.CANHandler(1, bitrate=500000)

    assert handler.open() is True
    assert calls['open'] == {'channel': 1, 'flags': 0}
    assert calls['output_mode'] == fake_canlib.canDRIVER_SILENT
    assert calls['bitrate'] == fake_canlib.canBITRATE_500K
    assert calls['bus_on'] is True


def test_can_handler_fails_if_silent_mode_is_unavailable(monkeypatch):
    import can_handler

    fake_canlib = types.SimpleNamespace(
        canBITRATE_500K=-2,
        canDRIVER_NORMAL=4,
        canOPEN_ACCEPT_VIRTUAL=32,
        canError=RuntimeError,
        canNoMsg=RuntimeError,
    )

    monkeypatch.setattr(can_handler, 'canlib', fake_canlib)
    monkeypatch.delenv('KBSM_CAN_LISTEN_ONLY', raising=False)

    handler = can_handler.CANHandler(0, bitrate=500000, listen_only=True)

    assert handler.open() is False


def test_can_handler_falls_back_to_normal_on_silent_rejection(monkeypatch):
    """When hardware rejects canDRIVER_SILENT, fall back to NORMAL and succeed."""
    import can_handler

    calls = {}

    class FakeChannel:
        def setBusOutputControl(self, value):
            if value == 1:  # canDRIVER_SILENT
                raise RuntimeError("Error in parameter (-1)")
            calls['output_mode'] = value

        def setBusParams(self, value):
            calls['bitrate'] = value

        def busOn(self):
            calls['bus_on'] = True

        def busOff(self):
            pass

        def close(self):
            pass

    fake_canlib = types.SimpleNamespace(
        canBITRATE_500K=-2,
        canDRIVER_NORMAL=4,
        canDRIVER_SILENT=1,
        canOPEN_ACCEPT_VIRTUAL=32,
        canError=RuntimeError,
        canNoMsg=RuntimeError,
        openChannel=lambda channel, flags: FakeChannel(),
    )

    monkeypatch.setattr(can_handler, 'canlib', fake_canlib)
    monkeypatch.setenv('KBSM_CAN_LISTEN_ONLY', '1')

    handler = can_handler.CANHandler(1, bitrate=500000)

    assert handler.open() is True
    # Fell back to NORMAL after SILENT was rejected
    assert calls['output_mode'] == fake_canlib.canDRIVER_NORMAL
    assert calls['bus_on'] is True