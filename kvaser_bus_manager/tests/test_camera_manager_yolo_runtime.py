import os
import sys


BACKEND_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', 'backend'))
if BACKEND_DIR not in sys.path:
    sys.path.insert(0, BACKEND_DIR)

from camera_manager import CameraManager
from shared_frame_buffer import SharedFrameBuffer


def test_set_yolo_runtime_starts_worker_when_camera_is_already_running():
    manager = CameraManager(
        frame_buffer=SharedFrameBuffer(),
        motion_callback=lambda _details: None,
    )
    manager._running = True

    try:
        assert manager._yolo_thread is None

        manager.set_yolo_runtime(enabled=True, classes_raw='person')

        assert manager._yolo_enabled is True
        assert manager._yolo_thread is not None
        assert manager._yolo_thread.is_alive()
    finally:
        manager.stop()


def test_set_yolo_runtime_disable_clears_stale_overlay_boxes():
    manager = CameraManager(
        frame_buffer=SharedFrameBuffer(),
        motion_callback=lambda _details: None,
    )
    manager._yolo_enabled = True
    manager._yolo_last_boxes = [{'x1': 1, 'y1': 2, 'x2': 3, 'y2': 4}]
    manager._yolo_last_boxes_ts_s = 123.0

    manager.set_yolo_runtime(enabled=False)

    assert manager._yolo_enabled is False
    assert manager._yolo_last_boxes == []
    assert manager._yolo_last_boxes_ts_s == 0.0
