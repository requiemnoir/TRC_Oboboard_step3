"""Smoke test per reliability.py (disk snapshot + retention)."""
from __future__ import annotations
import os
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from reliability import disk_snapshot, is_disk_low, enforce_logs_retention   # noqa: E402


def test_disk_snapshot_valid_path():
    with tempfile.TemporaryDirectory() as td:
        snap = disk_snapshot(td)
        assert 'free_mb' in snap and snap['free_mb'] > 0
        assert 'total_mb' in snap and snap['total_mb'] > 0
        assert 'used_percent' in snap
        assert 'error' not in snap


def test_disk_snapshot_invalid_path():
    """Path inesistente → error key, no crash."""
    snap = disk_snapshot('/non/esistente/path/foo')
    assert 'error' in snap


def test_is_disk_low_threshold_logic():
    with tempfile.TemporaryDirectory() as td:
        snap = disk_snapshot(td)
        # threshold gigante → low=True
        assert is_disk_low(td, min_free_mb=1e15) is True
        # threshold zero → low=False
        assert is_disk_low(td, min_free_mb=1) is False
        # path bad → False (graceful)
        assert is_disk_low('/non/esistente/bar', min_free_mb=100) is False


def test_retention_disabled():
    with tempfile.TemporaryDirectory() as td:
        result = enforce_logs_retention(td, enabled=False)
        assert result['ok'] is True
        assert result['enabled'] is False


def test_retention_deletes_old_files():
    """File con mtime molto vecchio + > max_total_mb → eliminati."""
    with tempfile.TemporaryDirectory() as td:
        # Crea 5 file 1 MB ciascuno con mtime old
        for i in range(5):
            p = Path(td) / f'old_{i}.mf4'
            p.write_bytes(b'\x00' * 1_000_000)
            old_time = time.time() - 30 * 86400   # 30 giorni fa
            os.utime(p, (old_time, old_time))
        result = enforce_logs_retention(td, enabled=True, max_age_days=7,
                                         max_total_mb=50, grace_s=0.0)
        assert result['ok'] is True
        # max_age=7 giorni, files sono di 30 giorni → cancellati
        assert result['deleted_files'] == 5, f'attesi 5 cancellati, got {result["deleted_files"]}'


def test_retention_grace_protects_fresh():
    """File freschi (mtime now) sono protetti dal grace_s."""
    with tempfile.TemporaryDirectory() as td:
        for i in range(3):
            p = Path(td) / f'fresh_{i}.mf4'
            p.write_bytes(b'\x00' * 50_000_000)   # 50 MB
        result = enforce_logs_retention(td, enabled=True, max_age_days=0,
                                         max_total_mb=50, grace_s=30.0)
        assert result['ok'] is True
        # grace=30s → freschi protetti, non cancellati
        assert result['deleted_files'] == 0


if __name__ == '__main__':
    test_disk_snapshot_valid_path()
    test_disk_snapshot_invalid_path()
    test_is_disk_low_threshold_logic()
    test_retention_disabled()
    test_retention_deletes_old_files()
    test_retention_grace_protects_fresh()
    print('OK — 6 test PASS')
