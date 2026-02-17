"""Shared stream capture layer — one ffmpeg/cv2 process per camera."""

import logging
import os
import subprocess
import threading
import time

import cv2
import numpy as np

logger = logging.getLogger(__name__)

_FFMPEG_CAPTURE_SIZE = (640, 480)


class _PipeReader:
    """Reads raw BGR frames from an ffmpeg subprocess pipe."""

    def __init__(self, proc: subprocess.Popen, width: int, height: int):
        self._proc = proc
        self._width = width
        self._height = height
        self._frame_size = width * height * 3

    def read(self) -> tuple[bool, np.ndarray | None]:
        data = self._proc.stdout.read(self._frame_size)
        if not data or len(data) != self._frame_size:
            return False, None
        frame = np.frombuffer(data, dtype=np.uint8).reshape(
            (self._height, self._width, 3)
        )
        return True, frame.copy()

    def release(self):
        try:
            self._proc.stdout.close()
        except Exception:
            pass
        try:
            self._proc.terminate()
            self._proc.wait(timeout=5)
        except Exception:
            try:
                self._proc.kill()
            except Exception:
                pass


class StreamCapture:
    """
    Owns the video stream for a single camera.

    Continuously reads frames in a background thread and keeps the latest
    one available.  Both the motion detector and snapshot capture read from
    this single instance, avoiding UDP port conflicts on SDP/SRTP streams.
    """

    def __init__(
        self,
        camera_name: str,
        sdp_file: str | None = None,
        rtsp_url: str | None = None,
        fps: int = 2,
        reconnect_delay: int = 5,
        max_reconnect_delay: int = 60,
    ):
        self.camera_name = camera_name
        self.sdp_file = sdp_file
        self.rtsp_url = rtsp_url
        self._fps = fps
        self._reconnect_delay = reconnect_delay
        self._max_reconnect_delay = max_reconnect_delay

        # Frame storage — protected by _lock
        self._lock = threading.Lock()
        self._latest_frame: np.ndarray | None = None
        self._frame_event = threading.Event()  # set once first frame arrives

        # Lifecycle
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._connected = threading.Event()

    @property
    def is_connected(self) -> bool:
        return self._connected.is_set()

    def start(self) -> None:
        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._capture_loop,
            daemon=True,
            name=f"capture-{self.camera_name}",
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop_event.set()
        self._frame_event.set()  # unblock waiters

    def get_frame(self, timeout: float = 5.0) -> np.ndarray | None:
        """Return a copy of the latest raw BGR frame, or None."""
        if self._latest_frame is not None:
            with self._lock:
                return self._latest_frame.copy() if self._latest_frame is not None else None
        self._frame_event.wait(timeout=timeout)
        with self._lock:
            return self._latest_frame.copy() if self._latest_frame is not None else None

    def get_jpeg(self, quality: int = 90, timeout: float = 5.0) -> bytes | None:
        """Return the latest frame encoded as JPEG bytes, or None."""
        frame = self.get_frame(timeout=timeout)
        if frame is None:
            return None
        _, jpeg = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, quality])
        return jpeg.tobytes()

    def wait_for_frame(self, timeout: float = 10.0) -> bool:
        """Block until at least one frame has been captured."""
        return self._frame_event.wait(timeout=timeout)

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _capture_loop(self) -> None:
        delay = self._reconnect_delay

        while not self._stop_event.is_set():
            cap = self._open_stream()
            if cap is None:
                logger.warning(
                    "capture/%s: failed to open stream, retrying in %ds",
                    self.camera_name, delay,
                )
                self._connected.clear()
                self._stop_event.wait(timeout=delay)
                delay = min(delay * 2, self._max_reconnect_delay)
                continue

            logger.info("capture/%s: stream connected", self.camera_name)
            self._connected.set()
            delay = self._reconnect_delay
            is_pipe = isinstance(cap, _PipeReader)
            frame_interval = 1.0 / self._fps
            consecutive_failures = 0
            max_failures = 30

            try:
                while not self._stop_event.is_set():
                    ret, frame = cap.read()
                    if not ret:
                        consecutive_failures += 1
                        if consecutive_failures >= max_failures:
                            logger.warning(
                                "capture/%s: %d consecutive read failures, reconnecting",
                                self.camera_name, consecutive_failures,
                            )
                            break
                        if not is_pipe:
                            time.sleep(frame_interval)
                        continue

                    consecutive_failures = 0
                    with self._lock:
                        self._latest_frame = frame
                    self._frame_event.set()

                    if not is_pipe:
                        time.sleep(frame_interval)
            finally:
                cap.release()
                self._connected.clear()

    def _open_stream(self):
        if self.sdp_file:
            return self._open_ffmpeg_pipe()
        if self.rtsp_url:
            return self._open_cv2()
        return None

    def _open_ffmpeg_pipe(self) -> _PipeReader | None:
        w, h = _FFMPEG_CAPTURE_SIZE
        cmd = [
            "ffmpeg",
            "-protocol_whitelist", "file,udp,srtp,rtp",
            "-i", self.sdp_file,
            "-f", "rawvideo",
            "-pix_fmt", "bgr24",
            "-s", f"{w}x{h}",
            "-r", str(self._fps),
            "-loglevel", "warning",
            "pipe:1",
        ]
        try:
            proc = subprocess.Popen(
                cmd,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
            )
            return _PipeReader(proc, w, h)
        except Exception as e:
            logger.warning("capture/%s: ffmpeg start failed: %s", self.camera_name, e)
            return None

    def _open_cv2(self) -> cv2.VideoCapture | None:
        os.environ["OPENCV_FFMPEG_CAPTURE_OPTIONS"] = "loglevel;quiet"
        cap = cv2.VideoCapture(self.rtsp_url, cv2.CAP_FFMPEG)
        cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)
        if not cap.isOpened():
            logger.warning("capture/%s: VideoCapture failed to open", self.camera_name)
            return None
        return cap
