"""Smoke test per RawLogger: ciclo start/log/stop, drop=0 a regime."""
from __future__ import annotations
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from raw_logger import RawLogger     # noqa: E402
from mirror_parser import RawFrame   # noqa: E402


def _mk_frame(arb_id: int, ftype: str = 'CAN', ch: int = 101) -> RawFrame:
    return RawFrame(
        ts_ns=time.time_ns(), ts_pkt=time.time(),
        frame_type=ftype, channel_id=ch, arb_id=arb_id,
        flags=0, dlc=8, data=b'\x01\x02\x03\x04\x05\x06\x07\x08',
    )


def test_logger_basic_lifecycle():
    """Avvia + N frame + stop produce MF4 con frame_count corretto."""
    with tempfile.TemporaryDirectory() as td:
        rl = RawLogger(log_dir=td, chunk_interval_s=2.0, flush_interval_s=1.0,
                       flush_interval_frames=500, chunk_max_frames=50_000)
        rl.start()
        for i in range(2000):
            rl.log(_mk_frame(0x100 + (i % 50)))
        stats = rl.stop(timeout_s=10.0)
        assert stats['frame_count'] == 2000
        assert stats['dropped_count'] == 0
        files = sorted(Path(td).glob('session_*.mf4'))
        assert files, 'no MF4 produced'
        print(f'  test_basic: {stats["frame_count"]} frame in {len(files)} part')


def test_logger_no_drop_under_burst():
    """Burst di 50k frame, queue_max alto → drop=0."""
    with tempfile.TemporaryDirectory() as td:
        rl = RawLogger(log_dir=td, queue_max=131072, put_timeout_ms=50,
                       chunk_interval_s=10.0)
        rl.start()
        for i in range(50_000):
            rl.log(_mk_frame(0x200 + (i % 100), ftype='FlexRay', ch=200))
        stats = rl.stop(timeout_s=15.0)
        assert stats['dropped_count'] == 0, f'drop atteso 0, got {stats["dropped_count"]}'
        assert stats['frame_count'] == 50_000
        print(f'  test_no_drop: {stats["frame_count"]:,} frame, drop=0')


def test_logger_intermediate_disabled_when_flush_ge_chunk():
    """flush_interval_s >= chunk_interval_s → intermediate disabled."""
    with tempfile.TemporaryDirectory() as td:
        rl = RawLogger(log_dir=td, chunk_interval_s=5.0, flush_interval_s=10.0)
        assert rl._intermediate_enabled is False
        # E con 0
        rl2 = RawLogger(log_dir=td, chunk_interval_s=10.0, flush_interval_s=0.0)
        assert rl2._intermediate_enabled is False
        # Normale: enabled
        rl3 = RawLogger(log_dir=td, chunk_interval_s=10.0, flush_interval_s=2.0)
        assert rl3._intermediate_enabled is True


def test_logger_force_flush_during_session():
    """force_flush() funziona durante sessione attiva."""
    with tempfile.TemporaryDirectory() as td:
        rl = RawLogger(log_dir=td, chunk_interval_s=60.0)
        rl.start()
        for i in range(500):
            rl.log(_mk_frame(0x300 + i))
        time.sleep(0.5)   # dà tempo al worker
        ok = rl.force_flush(timeout_s=3.0)
        assert ok, 'force_flush ha ritornato False'
        # Dovrebbe esserci un MF4 con i frame scritti finora
        files = sorted(Path(td).glob('session_*.mf4'))
        assert files
        rl.stop(timeout_s=5.0)


if __name__ == '__main__':
    test_logger_basic_lifecycle()
    test_logger_no_drop_under_burst()
    test_logger_intermediate_disabled_when_flush_ge_chunk()
    test_logger_force_flush_during_session()
    print('OK — 4 test PASS')
