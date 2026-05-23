import os
import threading
import time
from typing import Callable, Dict, Optional
from collections import deque

try:
    import cv2  # type: ignore
except Exception:
    cv2 = None

from shared_frame_buffer import SharedFrameBuffer


class CameraManager:
    """USB (UVC) webcam manager.

    Pipeline:
      - Capture thread: grabs frames from VideoCapture and keeps only the latest raw frame.
      - Encode thread: throttles to output_fps, JPEG-encodes latest raw frame, updates SharedFrameBuffer.

    This keeps latency low (drops old frames) and supports multiple MJPEG clients.
    """

    def __init__(
        self,
        frame_buffer: SharedFrameBuffer,
        device: Optional[str] = None,
        output_fps: float = 10.0,
        width: Optional[int] = None,
        height: Optional[int] = None,
        jpeg_quality: int = 80,
        process_frame: Optional[Callable] = None,
        event_sink: Optional[Callable[[str, Dict], None]] = None,
        motion_callback: Optional[Callable[[Dict], None]] = None,
        custom_matcher=None,
    ):
        self._buffer = frame_buffer
        self._device = device
        self._output_fps = float(output_fps) if output_fps else 10.0
        self._width = width
        self._height = height
        self._jpeg_quality = int(jpeg_quality)
        self._process_frame = process_frame
        self._event_sink = event_sink
        self._motion_callback = motion_callback
        self._custom_matcher = custom_matcher

        self._running = False
        self._connected = False
        self._last_error: Optional[str] = None

        self._cap = None

        self._raw_lock = threading.Condition()
        self._raw_seq = 0
        self._raw_frame = None
        self._raw_ts_s = 0.0

        # Video pre-roll buffer (raw bgr24 bytes)
        self._preroll_enabled = float(os.getenv('CAM_PREROLL_S', '0') or 0) > 0
        self._preroll_s = float(os.getenv('CAM_PREROLL_S', '0') or 0)
        self._preroll_fps = float(os.getenv('CAM_PREROLL_FPS', '5') or 5)
        self._preroll_max_mb = float(os.getenv('CAM_PREROLL_MAX_MB', '64') or 64)
        self._preroll_lock = threading.Lock()
        self._preroll = deque()
        self._preroll_last_store_s = 0.0

        self._capture_thread: Optional[threading.Thread] = None
        self._encode_thread: Optional[threading.Thread] = None

        self._stats_lock = threading.Lock()
        self._jpeg_fps_est = 0.0
        self._last_jpeg_time_s = 0.0

        # Motion detection (env-controlled)
        self._motion_enabled = str(os.getenv('CAM_MOTION_ENABLE', '0')).strip().lower() in {'1', 'true', 'yes', 'on'}
        self._motion_threshold = float(os.getenv('CAM_MOTION_THRESHOLD', '5000') or 5000)
        self._motion_cooldown_s = float(os.getenv('CAM_MOTION_COOLDOWN_S', '2.0') or 2.0)
        self._motion_fps = float(os.getenv('CAM_MOTION_FPS', '4.0') or 4.0)
        self._last_motion_s = 0.0
        self._last_motion_eval_s = 0.0
        self._prev_gray = None

        # YOLO trigger (optional; env-controlled; loads lazily)
        self._yolo_enabled = str(os.getenv('CAM_YOLO_ENABLE', '0')).strip().lower() in {'1', 'true', 'yes', 'on'}
        self._yolo_conf = float(os.getenv('CAM_YOLO_CONF', '0.5') or 0.5)
        self._yolo_imgsz = int(os.getenv('CAM_YOLO_IMGSZ', '320') or 320)
        self._yolo_fps = float(os.getenv('CAM_YOLO_FPS', '1.0') or 1.0)
        self._yolo_cooldown_s = float(os.getenv('CAM_YOLO_COOLDOWN_S', os.getenv('CAM_MOTION_COOLDOWN_S', '2.0')) or 2.0)
        self._yolo_classes_raw = (os.getenv('CAM_YOLO_CLASSES') or '').strip()
        self._yolo_model_name = (os.getenv('CAM_YOLO_MODEL') or 'yolov8n.pt').strip()

        self._yolo_model = None
        self._yolo_last_run_s = 0.0
        self._yolo_last_trigger_s = 0.0
        self._yolo_last_error: Optional[str] = None
        self._yolo_prev_present = False

        # YOLO overlay (draw last detections on MJPEG frames)
        self._yolo_overlay = str(os.getenv('CAM_YOLO_OVERLAY', '1')).strip().lower() in {'1', 'true', 'yes', 'on'}
        self._yolo_overlay_ttl_s = float(os.getenv('CAM_YOLO_OVERLAY_TTL_S', '1.2') or 1.2)
        self._yolo_last_boxes = []  # list of dicts: {x1,y1,x2,y2,cls,name,conf}
        self._yolo_last_boxes_ts_s = 0.0

        self._yolo_lock = threading.Lock()
        self._yolo_req_lock = threading.Condition()
        self._yolo_req_frame = None
        self._yolo_req_ts_s = 0.0
        self._yolo_req_id = 0
        self._yolo_done_id = 0
        self._yolo_thread: Optional[threading.Thread] = None

        # Custom object matcher (optional)
        self._custom_enabled = str(os.getenv('CAM_CUSTOM_ENABLE', '0')).strip().lower() in {'1', 'true', 'yes', 'on'}
        self._custom_fps = float(os.getenv('CAM_CUSTOM_FPS', '1.0') or 1.0)
        self._custom_cooldown_s = float(os.getenv('CAM_CUSTOM_COOLDOWN_S', '2.0') or 2.0)
        self._custom_threshold = int(os.getenv('CAM_CUSTOM_THRESHOLD', '20') or 20)
        self._custom_objects_raw = (os.getenv('CAM_CUSTOM_OBJECTS') or '').strip()
        self._custom_last_run_s = 0.0
        self._custom_last_trigger_s = 0.0
        self._custom_last_error: Optional[str] = None
        self._custom_lock = threading.Lock()

    @staticmethod
    def _env_int(name: str, default: Optional[int]) -> Optional[int]:
        val = os.getenv(name)
        if val is None or val == '':
            return default
        try:
            return int(val)
        except Exception:
            return default

    @staticmethod
    def _env_float(name: str, default: float) -> float:
        val = os.getenv(name)
        if val is None or val == '':
            return default
        try:
            return float(val)
        except Exception:
            return default

    def start(self) -> None:
        if self._running:
            return
        self._running = True
        self._capture_thread = threading.Thread(target=self._capture_loop, name='camera-capture', daemon=True)
        self._encode_thread = threading.Thread(target=self._encode_loop, name='camera-encode', daemon=True)
        self._capture_thread.start()
        self._encode_thread.start()
        self._ensure_yolo_thread()

    def _ensure_yolo_thread(self) -> None:
        if not self._running or not self._yolo_enabled or self._motion_callback is None:
            return
        if self._yolo_thread is not None and self._yolo_thread.is_alive():
            return
        self._yolo_thread = threading.Thread(target=self._yolo_loop, name='camera-yolo', daemon=True)
        self._yolo_thread.start()

    def stop(self) -> None:
        self._running = False
        try:
            with self._raw_lock:
                self._raw_lock.notify_all()
        except Exception:
            pass
        try:
            with self._yolo_req_lock:
                self._yolo_req_lock.notify_all()
        except Exception:
            pass

        if self._capture_thread and self._capture_thread.is_alive():
            self._capture_thread.join(timeout=2)
        if self._encode_thread and self._encode_thread.is_alive():
            self._encode_thread.join(timeout=2)
        if self._yolo_thread and self._yolo_thread.is_alive():
            self._yolo_thread.join(timeout=2)

        self._release_capture()

    def status(self) -> Dict:
        seq, jpeg, ts = self._buffer.snapshot()
        age_ms = None
        if jpeg is not None and ts:
            age_ms = max(0, int((time.time() - ts) * 1000))

        with self._stats_lock:
            fps = float(self._jpeg_fps_est)

        return {
            'available': cv2 is not None,
            'connected': bool(self._connected),
            'device': self._device or os.getenv('CAM_DEVICE') or '0',
            'width': self._width,
            'height': self._height,
            'output_fps': self._output_fps,
            'jpeg_quality': self._jpeg_quality,
            'last_frame_seq': seq,
            'last_frame_age_ms': age_ms,
            'jpeg_fps_est': fps,
            'last_error': self._last_error,
            'yolo_enabled': bool(self._yolo_enabled),
            'yolo_model': self._yolo_model_name,
            'yolo_conf': self._yolo_conf,
            'yolo_imgsz': self._yolo_imgsz,
            'yolo_fps': self._yolo_fps,
            'yolo_cooldown_s': self._yolo_cooldown_s,
            'yolo_classes': self._yolo_classes_raw,
            'yolo_last_error': self._yolo_last_error,
            'yolo_last_boxes_n': int(len(self._yolo_last_boxes)) if self._yolo_last_boxes else 0,
            'yolo_last_boxes_age_ms': (None if not self._yolo_last_boxes_ts_s else max(0, int((time.time() - float(self._yolo_last_boxes_ts_s)) * 1000))),
            'custom_enabled': bool(self._custom_enabled),
            'custom_fps': self._custom_fps,
            'custom_cooldown_s': self._custom_cooldown_s,
            'custom_threshold': self._custom_threshold,
            'custom_objects': self._custom_objects_raw,
            'custom_last_error': self._custom_last_error,
            'preroll_enabled': bool(self._preroll_enabled),
            'preroll_s': self._preroll_s,
            'preroll_fps': self._preroll_fps,
        }

    def set_preroll_runtime(self, *, seconds: Optional[float] = None, fps: Optional[float] = None) -> None:
        try:
            with self._preroll_lock:
                if seconds is not None:
                    self._preroll_s = max(0.0, float(seconds))
                    self._preroll_enabled = self._preroll_s > 0
                if fps is not None:
                    self._preroll_fps = max(0.1, float(fps))
                # Trim immediately
                self._trim_preroll_locked(now_s=time.time())
        except Exception:
            pass

    def get_preroll_bytes(self) -> list:
        """Return list of bgr24 frame bytes for the configured pre-roll window."""
        try:
            with self._preroll_lock:
                self._trim_preroll_locked(now_s=time.time())
                return [b for (_ts, b, _sz) in list(self._preroll)]
        except Exception:
            return []

    def _trim_preroll_locked(self, *, now_s: float) -> None:
        # Drop by time
        try:
            cutoff = now_s - max(0.0, float(self._preroll_s))
            while self._preroll and self._preroll[0][0] < cutoff:
                self._preroll.popleft()
        except Exception:
            pass

        # Drop by approximate memory
        try:
            limit_bytes = int(max(1.0, float(self._preroll_max_mb)) * 1024 * 1024)
            total = 0
            for _ts, _b, sz in self._preroll:
                total += int(sz)
            while self._preroll and total > limit_bytes:
                _ts, _b, sz = self._preroll.popleft()
                total -= int(sz)
        except Exception:
            pass

    def set_custom_runtime(
        self,
        *,
        enabled: bool,
        objects_raw: str = '',
        fps: Optional[float] = None,
        cooldown_s: Optional[float] = None,
        threshold: Optional[int] = None,
    ) -> None:
        try:
            with self._custom_lock:
                self._custom_enabled = bool(enabled)
                self._custom_objects_raw = (objects_raw or '').strip()
                if fps is not None:
                    self._custom_fps = float(fps)
                if cooldown_s is not None:
                    self._custom_cooldown_s = float(cooldown_s)
                if threshold is not None:
                    self._custom_threshold = int(threshold)
        except Exception:
            pass

    def get_latest_raw_frame(self):
        """Return a copy of the latest raw BGR frame and its timestamp.

        Returns:
            (frame_bgr, timestamp_s) or (None, 0.0)
        """
        try:
            with self._raw_lock:
                if self._raw_frame is None:
                    return None, 0.0
                return self._raw_frame.copy(), float(self._raw_ts_s)
        except Exception:
            return None, 0.0

    def set_yolo_runtime(
        self,
        *,
        enabled: bool,
        rearm: bool = False,
        classes_raw: str = '',
        model_name: Optional[str] = None,
        conf: Optional[float] = None,
        imgsz: Optional[int] = None,
        fps: Optional[float] = None,
        cooldown_s: Optional[float] = None,
    ) -> None:
        """Update YOLO settings at runtime (used by the web UI)."""
        should_ensure_thread = False
        try:
            with self._yolo_lock:
                prev_enabled = bool(self._yolo_enabled)
                self._yolo_enabled = bool(enabled)
                # When (re)arming YOLO, reset presence edge state so a detection
                # already in view can still generate a trigger.
                if self._yolo_enabled and (rearm or (not prev_enabled)):
                    self._yolo_prev_present = False
                    self._yolo_last_trigger_s = 0.0
                if (not self._yolo_enabled) and prev_enabled:
                    self._yolo_prev_present = False
                    self._yolo_last_boxes = []
                    self._yolo_last_boxes_ts_s = 0.0
                self._yolo_classes_raw = (classes_raw or '').strip()
                if model_name is not None:
                    new_name = (str(model_name) or '').strip() or 'yolov8n.pt'
                    if new_name != self._yolo_model_name:
                        self._yolo_model_name = new_name
                        self._yolo_model = None
                if conf is not None:
                    self._yolo_conf = float(conf)
                if imgsz is not None:
                    self._yolo_imgsz = int(imgsz)
                if fps is not None:
                    self._yolo_fps = float(fps)
                if cooldown_s is not None:
                    self._yolo_cooldown_s = float(cooldown_s)
                should_ensure_thread = bool(self._yolo_enabled)
        except Exception:
            pass
        if should_ensure_thread:
            try:
                self._ensure_yolo_thread()
            except Exception:
                pass

    def yolo_test(
        self,
        *,
        conf: Optional[float] = None,
        imgsz: Optional[int] = None,
        classes_raw: Optional[str] = None,
        model_name: Optional[str] = None,
        max_det: int = 50,
    ) -> Dict:
        """Run a single YOLO inference on the latest frame and return detections.

        Intended for API/UI validation (debugging). Does not require trigger to be armed.
        """
        t0 = time.time()
        frame, ts_s = self.get_latest_raw_frame()
        if frame is None:
            return {
                'ok': False,
                'error': 'no frame available',
                'frame_ts_s': ts_s,
                'elapsed_ms': int((time.time() - t0) * 1000),
            }

        try:
            with self._yolo_lock:
                eff_conf = float(conf) if conf is not None else float(self._yolo_conf)
                eff_imgsz = int(imgsz) if imgsz is not None else int(self._yolo_imgsz)
                eff_classes_raw = (classes_raw if classes_raw is not None else self._yolo_classes_raw) or ''
                eff_model_name = (model_name if model_name is not None else self._yolo_model_name) or 'yolov8n.pt'

                if model_name is not None and eff_model_name != self._yolo_model_name:
                    self._yolo_model_name = eff_model_name
                    self._yolo_model = None

            model = self._ensure_yolo_model()
            if model is None:
                return {
                    'ok': False,
                    'error': self._yolo_last_error or 'yolo model not available',
                    'frame_ts_s': float(ts_s),
                    'elapsed_ms': int((time.time() - t0) * 1000),
                }

            class_filter = None
            if classes_raw is not None:
                prev = self._yolo_classes_raw
                try:
                    self._yolo_classes_raw = eff_classes_raw
                    class_filter = self._parse_yolo_class_filter()
                finally:
                    self._yolo_classes_raw = prev
            else:
                class_filter = self._parse_yolo_class_filter()

            t_infer0 = time.time()
            results = model.predict(
                frame,
                conf=eff_conf,
                imgsz=eff_imgsz,
                verbose=False,
                max_det=int(max_det) if max_det else 50,
            )
            infer_ms = int((time.time() - t_infer0) * 1000)

            detections = []
            for r in results:
                names_map = getattr(r, 'names', {}) or {}
                boxes = getattr(r, 'boxes', None)
                if boxes is None:
                    continue

                for b in boxes:
                    try:
                        cls_id = int(getattr(b, 'cls', [0])[0]) if hasattr(getattr(b, 'cls', None), '__len__') else int(b.cls)
                    except Exception:
                        try:
                            cls_id = int(float(b.cls))
                        except Exception:
                            cls_id = -1

                    try:
                        score = float(getattr(b, 'conf', [0.0])[0]) if hasattr(getattr(b, 'conf', None), '__len__') else float(b.conf)
                    except Exception:
                        try:
                            score = float(b.conf)
                        except Exception:
                            score = 0.0

                    name = None
                    try:
                        name = str(names_map.get(cls_id)) if cls_id in names_map else None
                    except Exception:
                        name = None

                    detections.append({'cls': cls_id, 'name': name, 'conf': score})

            if class_filter is not None and detections:
                filtered = []
                for d in detections:
                    if d.get('cls') in class_filter['ids']:
                        filtered.append(d)
                        continue
                    n = (d.get('name') or '').lower().strip()
                    if n and n in class_filter['names']:
                        filtered.append(d)
                detections = filtered

            self._yolo_last_error = None
            return {
                'ok': True,
                'frame_ts_s': float(ts_s),
                'model': eff_model_name,
                'conf': eff_conf,
                'imgsz': eff_imgsz,
                'classes_raw': eff_classes_raw,
                'detections': detections,
                'count': len(detections),
                'infer_ms': infer_ms,
                'elapsed_ms': int((time.time() - t0) * 1000),
            }
        except Exception as e:
            self._yolo_last_error = str(e)
            return {
                'ok': False,
                'error': str(e),
                'frame_ts_s': float(ts_s),
                'elapsed_ms': int((time.time() - t0) * 1000),
            }

    def _parse_yolo_class_filter(self):
        raw = (self._yolo_classes_raw or '').strip()
        if not raw:
            return None
        names = set()
        ids = set()
        for part in raw.split(','):
            p = part.strip()
            if not p:
                continue
            if p.isdigit():
                try:
                    ids.add(int(p))
                except Exception:
                    continue
            else:
                names.add(p.lower())
        return {'names': names, 'ids': ids}

    def _ensure_yolo_model(self):
        if self._yolo_model is not None:
            return self._yolo_model

        try:
            from ultralytics import YOLO  # type: ignore
        except Exception as e:
            self._yolo_last_error = f'ultralytics not available: {e}'
            return None

        try:
            model_name = (self._yolo_model_name or 'yolov8n.pt').strip()
            # Make relative model paths robust regardless of cwd.
            if not os.path.isabs(model_name):
                here = os.path.dirname(os.path.abspath(__file__))
                candidate = os.path.join(here, model_name)
                if os.path.exists(candidate):
                    model_name = candidate
            self._yolo_model = YOLO(model_name)
            self._yolo_last_error = None
            return self._yolo_model
        except Exception as e:
            self._yolo_last_error = str(e)
            self._yolo_model = None
            return None

    def _emit_event(self, name: str, details: Dict) -> None:
        if not self._event_sink:
            return
        try:
            self._event_sink(name, details)
        except Exception:
            pass

    def _set_connected(self, connected: bool, err: Optional[str] = None) -> None:
        if self._connected == connected and (err is None or err == self._last_error):
            return
        self._connected = connected
        self._last_error = err
        self._emit_event('camera_connected' if connected else 'camera_disconnected', {
            'connected': bool(connected),
            'error': err,
        })

    def _open_capture(self):
        if cv2 is None:
            self._set_connected(False, 'opencv not installed')
            return None

        dev = self._device or os.getenv('CAM_DEVICE')
        if dev is None or dev == '':
            dev = '0'

        cap = None
        try:
            # Avoid hammering OpenCV (which can spam stderr) when the device node
            # does not exist.
            try:
                if dev.isdigit():
                    dev_path = f'/dev/video{dev}'
                else:
                    dev_path = str(dev)
                if dev_path.startswith('/dev/video') and not os.path.exists(dev_path):
                    self._set_connected(False, f'camera device not found: {dev_path}')
                    return None
            except Exception:
                pass

            if dev.isdigit():
                cap = cv2.VideoCapture(int(dev))
            else:
                cap = cv2.VideoCapture(dev)

            if self._width:
                cap.set(cv2.CAP_PROP_FRAME_WIDTH, int(self._width))
            if self._height:
                cap.set(cv2.CAP_PROP_FRAME_HEIGHT, int(self._height))

            # Keep capture side free-running; encode thread will throttle.
            ok = cap.isOpened()
            if not ok:
                try:
                    cap.release()
                except Exception:
                    pass
                self._set_connected(False, f'cannot open camera device {dev}')
                return None

            self._set_connected(True, None)
            return cap
        except Exception as e:
            try:
                if cap is not None:
                    cap.release()
            except Exception:
                pass
            self._set_connected(False, str(e))
            return None

    def _release_capture(self) -> None:
        try:
            if self._cap is not None:
                self._cap.release()
        except Exception:
            pass
        self._cap = None
        self._set_connected(False, self._last_error)

    def _capture_loop(self) -> None:
        reconnect_min_s = self._env_float('CAM_RECONNECT_MIN_S', 1.0)
        reconnect_max_s = self._env_float('CAM_RECONNECT_MAX_S', 120.0)
        reconnect_delay_s = max(0.2, float(reconnect_min_s))
        next_attempt_s = 0.0
        while self._running:
            if self._cap is None:
                # Honor reconnect backoff window.
                now_s = time.time()
                if next_attempt_s and now_s < next_attempt_s:
                    time.sleep(min(0.5, max(0.05, next_attempt_s - now_s)))
                    continue
                self._cap = self._open_capture()
                if self._cap is None:
                    next_attempt_s = time.time() + reconnect_delay_s
                    reconnect_delay_s = min(float(reconnect_max_s), float(reconnect_delay_s) * 1.7)
                    continue

            try:
                ok, frame = self._cap.read()
                if not ok or frame is None:
                    self._release_capture()
                    next_attempt_s = time.time() + reconnect_delay_s
                    reconnect_delay_s = min(float(reconnect_max_s), float(reconnect_delay_s) * 1.7)
                    continue

                # Successful read: reset backoff.
                reconnect_delay_s = max(0.2, float(reconnect_min_s))
                next_attempt_s = 0.0

                if self._process_frame is not None:
                    try:
                        frame = self._process_frame(frame)
                    except Exception:
                        # Keep raw frame if processing fails
                        pass

                # Motion detection (lightweight, drops frames; no impact on MJPEG pipeline)
                if self._motion_enabled and self._motion_callback is not None and cv2 is not None:
                    try:
                        now_m = time.time()
                        min_motion_period = 1.0 / max(0.1, float(self._motion_fps))
                        if (now_m - self._last_motion_eval_s) >= min_motion_period:
                            self._last_motion_eval_s = now_m
                            gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
                            gray = cv2.GaussianBlur(gray, (5, 5), 0)
                            if self._prev_gray is not None:
                                diff = cv2.absdiff(self._prev_gray, gray)
                                _, thresh = cv2.threshold(diff, 25, 255, cv2.THRESH_BINARY)
                                motion_score = float(cv2.countNonZero(thresh))
                                if motion_score >= self._motion_threshold and (now_m - self._last_motion_s) >= self._motion_cooldown_s:
                                    self._last_motion_s = now_m
                                    try:
                                        self._motion_callback({
                                            'timestamp_s': now_m,
                                            'trigger': 'motion',
                                            'motion_score': motion_score,
                                        })
                                    except Exception:
                                        pass
                            self._prev_gray = gray
                    except Exception:
                        pass

                # YOLO trigger (optional)
                if self._yolo_enabled and self._motion_callback is not None:
                    try:
                        now_y = time.time()
                        min_period = 1.0 / max(0.1, float(self._yolo_fps))
                        if (now_y - self._yolo_last_run_s) >= min_period:
                            self._yolo_last_run_s = now_y
                            with self._yolo_req_lock:
                                self._yolo_req_frame = frame.copy()
                                self._yolo_req_ts_s = now_y
                                self._yolo_req_id += 1
                                self._yolo_req_lock.notify_all()
                    except Exception as e:
                        self._yolo_last_error = str(e)

                # YOLO overlay (draw cached boxes for a short TTL; no extra inference)
                if self._yolo_overlay and cv2 is not None:
                    try:
                        now_o = time.time()
                        ttl = max(0.2, float(self._yolo_overlay_ttl_s))
                        with self._yolo_lock:
                            age = now_o - float(self._yolo_last_boxes_ts_s or 0.0)
                            boxes = list(self._yolo_last_boxes) if age <= ttl else []
                        if boxes:
                            for bb in boxes:
                                x1 = int(bb.get('x1', 0))
                                y1 = int(bb.get('y1', 0))
                                x2 = int(bb.get('x2', 0))
                                y2 = int(bb.get('y2', 0))
                                if x2 <= x1 or y2 <= y1:
                                    continue
                                cv2.rectangle(frame, (x1, y1), (x2, y2), (0, 255, 0), 2)
                                name = str(bb.get('name') or bb.get('cls') or '')
                                conf = bb.get('conf')
                                label = f"{name} {conf:.2f}" if isinstance(conf, (int, float)) else str(name)
                                y_text = max(0, y1 - 7)
                                cv2.putText(frame, label, (x1, y_text), cv2.FONT_HERSHEY_SIMPLEX, 0.5, (0, 255, 0), 2)
                    except Exception:
                        pass

                # Custom object matcher trigger (optional)
                if self._custom_enabled and self._motion_callback is not None and self._custom_matcher is not None:
                    try:
                        now_c = time.time()
                        min_period = 1.0 / max(0.1, float(self._custom_fps))
                        if (now_c - self._custom_last_run_s) >= min_period:
                            self._custom_last_run_s = now_c
                            objects_raw = ''
                            threshold = 20
                            cooldown_s = 2.0
                            with self._custom_lock:
                                objects_raw = self._custom_objects_raw
                                threshold = int(self._custom_threshold)
                                cooldown_s = float(self._custom_cooldown_s)

                            names = [x.strip() for x in (objects_raw or '').split(',') if x.strip()]
                            det = self._custom_matcher.detect(frame, names_filter=names or None, threshold=threshold)
                            if det and det.get('matched') and (now_c - self._custom_last_trigger_s) >= cooldown_s:
                                self._custom_last_trigger_s = now_c
                                try:
                                    self._motion_callback({
                                        'timestamp_s': now_c,
                                        'trigger': 'custom',
                                        'match': det,
                                    })
                                except Exception:
                                    pass
                            self._custom_last_error = None
                    except Exception as e:
                        self._custom_last_error = str(e)

                now_s = time.time()
                with self._raw_lock:
                    self._raw_frame = frame
                    self._raw_ts_s = now_s
                    self._raw_seq += 1
                    self._raw_lock.notify_all()

                # Pre-roll store (throttled)
                if self._preroll_enabled and self._preroll_s > 0:
                    try:
                        min_period = 1.0 / max(0.1, float(self._preroll_fps))
                        with self._preroll_lock:
                            if (now_s - self._preroll_last_store_s) >= min_period:
                                self._preroll_last_store_s = now_s
                                b = frame.tobytes()
                                self._preroll.append((now_s, b, len(b)))
                                self._trim_preroll_locked(now_s=now_s)
                    except Exception:
                        pass

            except Exception as e:
                self._set_connected(False, str(e))
                self._release_capture()
                time.sleep(reconnect_delay_s)

    def _yolo_loop(self) -> None:
        while self._running:
            frame = None
            ts_s = 0.0
            req_id = 0
            try:
                with self._yolo_req_lock:
                    while self._running and self._yolo_req_id == self._yolo_done_id:
                        self._yolo_req_lock.wait(timeout=0.5)
                    if not self._running:
                        return
                    req_id = int(self._yolo_req_id)
                    frame = self._yolo_req_frame
                    ts_s = float(self._yolo_req_ts_s or time.time())
                if frame is None:
                    continue
                self._run_yolo_inference(frame, ts_s)
                with self._yolo_req_lock:
                    self._yolo_done_id = max(self._yolo_done_id, req_id)
            except Exception as e:
                self._yolo_last_error = str(e)

    def _run_yolo_inference(self, frame, now_y: float) -> None:
        model = self._ensure_yolo_model()
        if model is None:
            return

        class_filter = self._parse_yolo_class_filter()
        results = model.predict(frame, conf=float(self._yolo_conf), imgsz=int(self._yolo_imgsz), verbose=False)
        detections = []
        boxes_for_overlay = []

        for r in results:
            try:
                names_map = getattr(r, 'names', {}) or {}
                boxes = getattr(r, 'boxes', None)
                if boxes is None:
                    continue

                for b in boxes:
                    try:
                        cls_id = int(getattr(b, 'cls', [0])[0]) if hasattr(getattr(b, 'cls', None), '__len__') else int(b.cls)
                    except Exception:
                        try:
                            cls_id = int(float(b.cls))
                        except Exception:
                            cls_id = -1

                    try:
                        conf = float(getattr(b, 'conf', [0.0])[0]) if hasattr(getattr(b, 'conf', None), '__len__') else float(b.conf)
                    except Exception:
                        try:
                            conf = float(b.conf)
                        except Exception:
                            conf = 0.0

                    name = None
                    try:
                        name = str(names_map.get(cls_id)) if cls_id in names_map else None
                    except Exception:
                        name = None

                    detections.append({
                        'cls': cls_id,
                        'name': name,
                        'conf': conf,
                    })

                    try:
                        xyxy = getattr(b, 'xyxy', None)
                        if xyxy is not None:
                            if hasattr(xyxy, 'tolist'):
                                arr = xyxy.tolist()
                            else:
                                arr = list(xyxy)
                            if isinstance(arr, list) and len(arr) == 1 and isinstance(arr[0], (list, tuple)):
                                arr = arr[0]
                            if isinstance(arr, (list, tuple)) and len(arr) >= 4:
                                x1, y1, x2, y2 = [int(float(v)) for v in arr[:4]]
                                boxes_for_overlay.append({
                                    'x1': x1, 'y1': y1, 'x2': x2, 'y2': y2,
                                    'cls': cls_id,
                                    'name': name,
                                    'conf': float(conf),
                                })
                    except Exception:
                        pass
            except Exception:
                continue

        should_trigger = False
        if detections:
            if class_filter is None:
                should_trigger = True
            else:
                for det in detections:
                    if det.get('cls') in class_filter['ids']:
                        should_trigger = True
                        break
                    name = (det.get('name') or '').lower().strip()
                    if name and name in class_filter['names']:
                        should_trigger = True
                        break

        present = bool(should_trigger)

        try:
            if self._yolo_overlay:
                if class_filter is not None and boxes_for_overlay:
                    filtered_boxes = []
                    for bb in boxes_for_overlay:
                        if bb.get('cls') in class_filter['ids']:
                            filtered_boxes.append(bb)
                            continue
                        name = (bb.get('name') or '').lower().strip()
                        if name and name in class_filter['names']:
                            filtered_boxes.append(bb)
                    boxes_for_overlay = filtered_boxes
                with self._yolo_lock:
                    self._yolo_last_boxes = list(boxes_for_overlay)
                    self._yolo_last_boxes_ts_s = float(now_y)
                self._yolo_last_error = None
        except Exception:
            pass

        self._yolo_prev_present = present
        try:
            self._motion_callback({
                'timestamp_s': now_y,
                'trigger': 'yolo',
                'present': bool(present),
                'detections': detections,
            })
        except Exception:
            pass

    def _encode_loop(self) -> None:
        if cv2 is None:
            return

        target_period = 1.0 / max(0.1, float(self._output_fps))
        last_out = 0.0
        last_seen_raw_seq = 0

        encode_params = [int(cv2.IMWRITE_JPEG_QUALITY), int(self._jpeg_quality)]

        while self._running:
            # Throttle output
            now = time.time()
            sleep_for = (last_out + target_period) - now
            if sleep_for > 0:
                time.sleep(min(sleep_for, 0.05))
                continue

            # Wait for a raw frame
            with self._raw_lock:
                if self._raw_seq <= last_seen_raw_seq:
                    self._raw_lock.wait(timeout=0.2)
                raw_seq = self._raw_seq
                frame = self._raw_frame
                ts_s = self._raw_ts_s

            if frame is None or raw_seq == last_seen_raw_seq:
                continue

            last_seen_raw_seq = raw_seq

            try:
                ok, enc = cv2.imencode('.jpg', frame, encode_params)
                if not ok:
                    continue
                jpeg = enc.tobytes()
                self._buffer.update(jpeg, timestamp_s=ts_s)

                with self._stats_lock:
                    if self._last_jpeg_time_s:
                        dt = max(1e-6, now - self._last_jpeg_time_s)
                        inst = 1.0 / dt
                        self._jpeg_fps_est = (self._jpeg_fps_est * 0.8) + (inst * 0.2)
                    self._last_jpeg_time_s = now

                last_out = now
            except Exception as e:
                self._last_error = str(e)
                # keep running; capture thread will continue
                last_out = now
