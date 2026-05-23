import os
import shutil
import subprocess
import threading
import time
from typing import Dict, Optional


class VideoRecorder:
    """FFmpeg-based A/V recorder for a UVC webcam.

    Records to MP4 (H.264 + AAC when possible). Intended to be started/stopped
    by the app when CAN logging starts/stops.

    Config via env:
      - CAM_DEVICE: v4l2 device index (e.g. 0) or path (/dev/video0)
      - CAM_FPS: output FPS (default 10)
      - CAM_WIDTH / CAM_HEIGHT: optional capture size
      - CAM_AUDIO_DEVICE: ALSA device (default 'default')
      - CAM_AUDIO_ENABLE: '1' to enable audio (default 1)
      - CAM_FFMPEG_VCODEC: e.g. 'libx264' (default libx264)
      - CAM_FFMPEG_ACODEC: e.g. 'aac' (default aac)
    """

    def __init__(self, frame_source=None):
        # frame_source: callable -> (frame_bgr, ts_s) where frame is a numpy ndarray
        self._frame_source = frame_source

        self._proc: Optional[subprocess.Popen] = None
        self._output_path: Optional[str] = None
        self._last_error: Optional[str] = None

        self._stop_evt = threading.Event()
        self._writer_thread: Optional[threading.Thread] = None
        self._pre_roll_bytes = None

    def is_available(self) -> bool:
        return shutil.which('ffmpeg') is not None

    def status(self) -> Dict:
        return {
            'ffmpeg_available': self.is_available(),
            'recording': self._proc is not None and self._proc.poll() is None,
            'output_path': self._output_path,
            'last_error': self._last_error,
        }

    def start(self, output_path: str, *, pre_roll_bytes=None) -> bool:
        if self._proc is not None and self._proc.poll() is None:
            return True

        self._last_error = None
        self._output_path = output_path
        self._pre_roll_bytes = pre_roll_bytes

        ffmpeg = shutil.which('ffmpeg')
        if not ffmpeg:
            self._last_error = 'ffmpeg not found'
            return False

        fps = os.getenv('CAM_FPS') or '10'
        try:
            fps_f = float(fps)
        except Exception:
            fps_f = 10.0

        width = os.getenv('CAM_WIDTH')
        height = os.getenv('CAM_HEIGHT')
        size = None
        if width and height:
            try:
                size = f'{int(width)}x{int(height)}'
            except Exception:
                size = None

        audio_enable = str(os.getenv('CAM_AUDIO_ENABLE', '1')).strip().lower() not in {'0', 'false', 'no', 'off'}
        audio_dev = os.getenv('CAM_AUDIO_DEVICE') or 'default'

        vcodec = os.getenv('CAM_FFMPEG_VCODEC') or 'libx264'
        acodec = os.getenv('CAM_FFMPEG_ACODEC') or 'aac'

        # Prefer recording from the already-open OpenCV frames (no /dev/video contention).
        # Fallback to direct v4l2 capture only when frame_source is not provided.
        record_source = (os.getenv('CAM_RECORD_SOURCE') or 'pipe').strip().lower()
        use_pipe = self._frame_source is not None and record_source in {'pipe', 'frames', 'opencv'}

        initial_frame_bytes = None

        def _build_cmd(*, with_audio: bool, size_str: Optional[str]) -> list:
            if use_pipe:
                cmd_local = [
                    ffmpeg,
                    '-hide_banner',
                    '-loglevel', 'error',
                    '-nostdin',
                    '-y',
                    '-f', 'rawvideo',
                    '-pix_fmt', 'bgr24',
                    '-s', str(size_str),
                    '-r', str(fps_f),
                    '-i', 'pipe:0',
                ]
                if with_audio:
                    cmd_local += ['-thread_queue_size', '512', '-f', 'alsa', '-i', audio_dev]
                cmd_local += [
                    '-c:v', vcodec,
                    '-preset', 'veryfast',
                    '-tune', 'zerolatency',
                    '-pix_fmt', 'yuv420p',
                ]
                if with_audio:
                    cmd_local += ['-c:a', acodec, '-b:a', '128k']
                else:
                    cmd_local += ['-an']
                cmd_local += ['-movflags', '+faststart', output_path]
                return cmd_local

            cam_dev = os.getenv('CAM_DEVICE') or '0'
            if cam_dev.isdigit():
                cam_dev = f'/dev/video{cam_dev}'

            cmd_local = [
                ffmpeg,
                '-hide_banner',
                '-loglevel', 'error',
                '-nostdin',
                '-y',
                '-f', 'v4l2',
                '-framerate', str(fps_f),
            ]
            if size_str:
                cmd_local += ['-video_size', str(size_str)]
            cmd_local += ['-i', cam_dev]
            if with_audio:
                cmd_local += ['-f', 'alsa', '-i', audio_dev]

            cmd_local += [
                '-c:v', vcodec,
                '-preset', 'veryfast',
                '-tune', 'zerolatency',
                '-pix_fmt', 'yuv420p',
            ]
            if with_audio:
                cmd_local += ['-c:a', acodec, '-b:a', '128k']
            else:
                cmd_local += ['-an']
            cmd_local += ['-movflags', '+faststart', output_path]
            return cmd_local

        if use_pipe:
            # Wait briefly for a frame to determine size.
            frame = None
            try:
                startup_timeout_s = float(os.getenv('CAM_RECORD_START_TIMEOUT_S', '1.5') or 1.5)
            except Exception:
                startup_timeout_s = 1.5
            startup_timeout_s = max(0.05, min(startup_timeout_s, 5.0))
            deadline = time.time() + startup_timeout_s
            while time.time() < deadline:
                try:
                    frame, _ts = self._frame_source()
                except Exception:
                    frame = None
                if frame is not None:
                    break
                time.sleep(0.02)

            if frame is None:
                self._last_error = 'no camera frames available'
                return False

            try:
                h = int(frame.shape[0])
                w = int(frame.shape[1])
            except Exception:
                self._last_error = 'invalid frame shape'
                return False

            size = size or f'{w}x{h}'
            try:
                initial_frame_bytes = frame.tobytes()
            except Exception:
                initial_frame_bytes = None

            # cmd built later via _build_cmd()
        else:
            # cmd built later via _build_cmd()
            pass

        try:
            os.makedirs(os.path.dirname(output_path), exist_ok=True)
        except Exception:
            pass

        def _spawn(cmd_to_run) -> Optional[subprocess.Popen]:
            try:
                self._stop_evt.clear()
                return subprocess.Popen(
                    cmd_to_run,
                    stdin=subprocess.PIPE if use_pipe else subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.PIPE,
                )
            except Exception as e:
                self._last_error = str(e)
                return None

        def _read_stderr(proc: Optional[subprocess.Popen]) -> str:
            if proc is None:
                return ''
            err = b''
            try:
                err = proc.stderr.read() if proc.stderr else b''
            except Exception:
                err = b''
            return (err.decode('utf-8', errors='replace') or '').strip()

        # Attempt with requested audio; if ffmpeg exits quickly due to ALSA, retry video-only.
        for attempt_audio in ([audio_enable, False] if audio_enable else [False]):
            cmd_attempt = _build_cmd(with_audio=bool(attempt_audio), size_str=size)
            proc = _spawn(cmd_attempt)
            if proc is None:
                return False

            # In pipe mode, push one frame immediately so ffmpeg opens all inputs.
            if use_pipe and proc.stdin is not None and initial_frame_bytes is not None:
                try:
                    proc.stdin.write(initial_frame_bytes)
                    proc.stdin.flush()
                except Exception:
                    pass

            # Give ffmpeg a moment to fail fast.
            try:
                ffmpeg_warmup_s = float(os.getenv('CAM_RECORD_FFMPEG_WARMUP_S', '0.15') or 0.15)
            except Exception:
                ffmpeg_warmup_s = 0.15
            ffmpeg_warmup_s = max(0.05, min(ffmpeg_warmup_s, 2.0))
            time.sleep(ffmpeg_warmup_s)
            if proc.poll() is not None:
                self._last_error = _read_stderr(proc) or 'ffmpeg exited'
                try:
                    if proc.stdin is not None:
                        proc.stdin.close()
                except Exception:
                    pass
                if attempt_audio is audio_enable and audio_enable:
                    # Retry without audio.
                    continue
                return False

            # Started successfully (possibly after retrying without audio)
            self._last_error = None
            self._proc = proc
            if use_pipe:
                self._writer_thread = threading.Thread(
                    target=self._pipe_writer_loop,
                    args=(fps_f,),
                    name='ffmpeg-video-writer',
                    daemon=True,
                )
                self._writer_thread.start()
            return True

        return False

    def _pipe_writer_loop(self, fps: float) -> None:
        proc = self._proc
        if proc is None or proc.stdin is None or self._frame_source is None:
            return

        period = 1.0 / max(0.1, float(fps))
        next_t = time.time()
        last_frame = None

        # Pre-roll frames (already in bgr24 bytes)
        try:
            if self._pre_roll_bytes and proc.stdin is not None:
                for b in self._pre_roll_bytes:
                    if self._stop_evt.is_set():
                        break
                    try:
                        proc.stdin.write(b)
                    except Exception:
                        break
                try:
                    proc.stdin.flush()
                except Exception:
                    pass
        except Exception:
            pass
        last_ts = 0.0

        while not self._stop_evt.is_set():
            if proc.poll() is not None:
                break

            now = time.time()
            if now < next_t:
                time.sleep(min(0.05, next_t - now))
                continue
            next_t = now + period

            try:
                frame, ts = self._frame_source()
            except Exception:
                frame, ts = None, 0.0

            if frame is None:
                frame = last_frame
                ts = last_ts

            if frame is None:
                continue

            last_frame = frame
            last_ts = ts

            try:
                proc.stdin.write(frame.tobytes())
                proc.stdin.flush()
            except Exception:
                break

        # Capture any ffmpeg error output for later debugging.
        try:
            if self._last_error is None and proc.poll() is not None:
                err = b''
                try:
                    err = proc.stderr.read() if proc.stderr else b''
                except Exception:
                    err = b''
                msg = (err.decode('utf-8', errors='replace') or '').strip()
                if msg:
                    self._last_error = msg
        except Exception:
            pass

    def stop(self) -> None:
        if self._proc is None:
            return
        try:
            self._stop_evt.set()
            try:
                if self._proc.stdin is not None:
                    self._proc.stdin.close()
            except Exception:
                pass

            try:
                if self._writer_thread and self._writer_thread.is_alive():
                    self._writer_thread.join(timeout=0.35)
            except Exception:
                pass

            if self._proc.poll() is None:
                try:
                    self._proc.wait(timeout=0.8)
                except Exception:
                    self._proc.terminate()
                    try:
                        self._proc.wait(timeout=0.8)
                    except Exception:
                        self._proc.kill()
        finally:
            self._proc = None
            self._writer_thread = None

    def __del__(self):
        try:
            self.stop()
        except Exception:
            pass
