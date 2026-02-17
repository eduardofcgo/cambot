#!/usr/bin/env python3
"""Live multi-camera viewer — shows all cameras tiled in a single window.

Usage:
    python scripts/multiview.py                    # uses config/cameras.yaml
    python scripts/multiview.py -c path/to.yaml    # custom config
    python scripts/multiview.py --cols 3            # force 3-column grid
    python scripts/multiview.py --width 1280 --height 720   # window size

Requires opencv-python (NOT headless):
    pip install opencv-python
"""

from __future__ import annotations

import argparse
import math
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

import cv2
import numpy as np
import yaml

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CONFIG = PROJECT_ROOT / "config" / "cameras.yaml"

STREAM_SIZE = (640, 480)  # per-camera decode resolution


class FfmpegStream:
    """Reads raw BGR frames from an ffmpeg subprocess (for SDP/SRTP)."""

    def __init__(self, sdp_file: str, width: int, height: int, fps: int = 10):
        self._width = width
        self._height = height
        self._frame_size = width * height * 3
        cmd = [
            "ffmpeg",
            "-protocol_whitelist", "file,udp,srtp,rtp",
            "-i", sdp_file,
            "-f", "rawvideo",
            "-pix_fmt", "bgr24",
            "-s", f"{width}x{height}",
            "-r", str(fps),
            "-loglevel", "warning",
            "pipe:1",
        ]
        self._proc = subprocess.Popen(
            cmd, stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
        )

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


class CameraFeed:
    """Continuously reads frames from a single camera in a background thread."""

    def __init__(self, name: str, display_name: str,
                 rtsp_url: str | None, sdp_file: str | None):
        self.name = name
        self.display_name = display_name
        self.rtsp_url = rtsp_url
        self.sdp_file = sdp_file

        self._frame: np.ndarray | None = None
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._loop, daemon=True,
                                        name=f"feed-{name}")

    def start(self):
        self._thread.start()

    def stop(self):
        self._stop.set()

    @property
    def frame(self) -> np.ndarray | None:
        with self._lock:
            return self._frame

    def _open(self):
        w, h = STREAM_SIZE
        if self.sdp_file:
            return FfmpegStream(self.sdp_file, w, h)
        elif self.rtsp_url:
            os.environ["OPENCV_FFMPEG_CAPTURE_OPTIONS"] = "loglevel;quiet"
            cap = cv2.VideoCapture(self.rtsp_url, cv2.CAP_FFMPEG)
            cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)
            if not cap.isOpened():
                return None
            return cap
        return None

    def _loop(self):
        reconnect_delay = 5
        while not self._stop.is_set():
            cap = self._open()
            if cap is None:
                self._stop.wait(timeout=reconnect_delay)
                reconnect_delay = min(reconnect_delay * 2, 60)
                continue

            reconnect_delay = 5
            while not self._stop.is_set():
                ret, frame = cap.read()
                if not ret:
                    break
                with self._lock:
                    self._frame = frame

            cap.release()


def make_placeholder(width: int, height: int, text: str) -> np.ndarray:
    """Dark frame with centered text for offline cameras."""
    img = np.zeros((height, width, 3), dtype=np.uint8)
    font = cv2.FONT_HERSHEY_SIMPLEX
    scale = 0.5
    thickness = 1
    (tw, th), _ = cv2.getTextSize(text, font, scale, thickness)
    x = (width - tw) // 2
    y = (height + th) // 2
    cv2.putText(img, text, (x, y), font, scale, (0, 0, 200), thickness,
                cv2.LINE_AA)
    return img


def draw_label(frame: np.ndarray, text: str) -> None:
    """Draw a semi-transparent label at the top-left of a frame."""
    font = cv2.FONT_HERSHEY_SIMPLEX
    scale = 0.5
    thickness = 1
    (tw, th), baseline = cv2.getTextSize(text, font, scale, thickness)
    pad = 4
    overlay = frame.copy()
    cv2.rectangle(overlay, (0, 0), (tw + pad * 2, th + baseline + pad * 2),
                  (0, 0, 0), -1)
    cv2.addWeighted(overlay, 0.6, frame, 0.4, 0, frame)
    cv2.putText(frame, text, (pad, th + pad), font, scale, (255, 255, 255),
                thickness, cv2.LINE_AA)


def load_cameras(config_path: Path) -> list[dict]:
    if not config_path.exists():
        print(f"Error: config not found at {config_path}", file=sys.stderr)
        sys.exit(1)
    with open(config_path) as f:
        config = yaml.safe_load(f)
    cams = config.get("cameras", [])
    return [c for c in cams if c.get("enabled", True)]


def main():
    parser = argparse.ArgumentParser(description="Live multi-camera viewer")
    parser.add_argument("-c", "--config", type=Path, default=DEFAULT_CONFIG,
                        help="Path to cameras.yaml")
    parser.add_argument("--cols", type=int, default=0,
                        help="Number of columns in grid (0 = auto)")
    parser.add_argument("--width", type=int, default=1280,
                        help="Window width in pixels")
    parser.add_argument("--height", type=int, default=720,
                        help="Window height in pixels")
    parser.add_argument("--fps", type=int, default=15,
                        help="Target display refresh rate")
    args = parser.parse_args()

    cameras = load_cameras(args.config)
    if not cameras:
        print("No enabled cameras found in config.", file=sys.stderr)
        sys.exit(1)

    n = len(cameras)
    cols = args.cols if args.cols > 0 else math.ceil(math.sqrt(n))
    rows = math.ceil(n / cols)

    cell_w = args.width // cols
    cell_h = args.height // rows

    feeds: list[CameraFeed] = []
    for cam in cameras:
        feed = CameraFeed(
            name=cam["name"],
            display_name=cam.get("display_name", cam["name"]),
            rtsp_url=cam.get("rtsp_url"),
            sdp_file=cam.get("sdp_file"),
        )
        feed.start()
        feeds.append(feed)

    print(f"Multiview: {n} camera(s) in {cols}x{rows} grid "
          f"({args.width}x{args.height})")
    print("Press 'q' or ESC to quit.")

    window = "Cambot Multiview"
    cv2.namedWindow(window, cv2.WINDOW_NORMAL)
    cv2.resizeWindow(window, args.width, args.height)

    frame_delay = 1.0 / args.fps

    try:
        while True:
            canvas = np.zeros((args.height, args.width, 3), dtype=np.uint8)

            for idx, feed in enumerate(feeds):
                row = idx // cols
                col = idx % cols
                x = col * cell_w
                y = row * cell_h

                frame = feed.frame
                if frame is not None:
                    cell = cv2.resize(frame, (cell_w, cell_h))
                else:
                    cell = make_placeholder(cell_w, cell_h,
                                            f"{feed.display_name}: connecting...")

                draw_label(cell, feed.display_name)
                canvas[y:y + cell_h, x:x + cell_w] = cell

            cv2.imshow(window, canvas)

            key = cv2.waitKey(int(frame_delay * 1000)) & 0xFF
            if key in (ord("q"), 27):  # q or ESC
                break
    finally:
        for feed in feeds:
            feed.stop()
        cv2.destroyAllWindows()


if __name__ == "__main__":
    main()
