import time
import base64

from flask import Response

from shared_frame_buffer import SharedFrameBuffer


# 1x1 black JPEG (valid image payload for MJPEG keepalive when camera is disconnected)
_FALLBACK_JPEG = base64.b64decode(
    b"/9j/4AAQSkZJRgABAQAAAQABAAD/2wBDAAgGBgcGBQgHBwcJCQgKDBQNDAsLDBkSEw8UHRofHh0aHBwgJC4nICIsIxwcKDcpLDAxNDQ0Hyc5PTgyPC4zNDL/2wBDAQkJCQwLDBgNDRgyIRwhMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjL/wAARCAABAAEDASIAAhEBAxEB/8QAHwAAAQUBAQEBAQEAAAAAAAAAAAECAwQFBgcICQoL/8QAtRAAAgEDAwIEAwUFBAQAAAF9AQIDAAQRBRIhMUEGE1FhByJxFDKBkaEII0KxwRVS0fAkM2JyggkKFhcYGRolJicoKSo0NTY3ODk6Q0RFRkdISUpTVFVWV1hZWmNkZWZnaGlqc3R1dnd4eXqDhIWGh4iJipKTlJWWl5iZmqKjpKWmp6ipqrKztLW2t7i5usLDxMXGx8jJytLT1NXW19jZ2uHi4+Tl5ufo6erx8vP09fb3+Pn6/8QAHwEAAwEBAQEBAQEBAQAAAAAAAAECAwQFBgcICQoL/8QAtREAAgECBAQDBAcFBAQAAQJ3AAECAxEEBSExBhJBUQdhcRMiMoEIFEKRobHBCSMzUvAVYnLRChYkNOEl8RcYGRomJygpKjU2Nzg5OkNERUZHSElKU1RVVldYWVpjZGVmZ2hpanN0dXZ3eHl6goOEhYaHiImKkpOUlZaXmJmaoqOkpaanqKmqsrO0tba3uLm6wsPExcbHyMnK0tPU1dbX2Nna4uPk5ebn6Onq8vP09fb3+Pn6/9oADAMBAAIRAxEAPwD5/ooooA//2Q=="
)


class MJPEGStreamer:
    def __init__(self, frame_buffer: SharedFrameBuffer):
        self._buffer = frame_buffer

    def response(self) -> Response:
        return Response(
            self._generator(),
            mimetype='multipart/x-mixed-replace; boundary=frame',
            headers={
                'Cache-Control': 'no-store, no-cache, must-revalidate, max-age=0',
                'Pragma': 'no-cache',
            },
        )

    def _generator(self):
        last_seq = 0
        while True:
            try:
                seq, jpeg, ts_s = self._buffer.get_latest(last_seq=last_seq, timeout_s=1.0)
                if jpeg is None or seq == last_seq:
                    # Keep the connection stable for <img> by always yielding a valid JPEG part.
                    jpeg = _FALLBACK_JPEG
                    ts_s = time.time()
                else:
                    last_seq = seq

                header = (
                    b"--frame\r\n"
                    b"Content-Type: image/jpeg\r\n"
                    + f"X-Timestamp: {ts_s:.6f}\r\n".encode('ascii')
                    + f"Content-Length: {len(jpeg)}\r\n\r\n".encode('ascii')
                )
                yield header
                yield jpeg
                yield b"\r\n"

                # If we're in fallback mode, avoid a tight loop.
                if last_seq == 0:
                    time.sleep(0.2)
            except GeneratorExit:
                return
            except Exception:
                # avoid tight-loop on error
                time.sleep(0.2)
                continue
