"""Motion detection + person counting using OpenCV MOG2 and YOLO."""

import logging
import queue
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone

import cv2
import numpy as np

from cambot.capture import StreamCapture

logger = logging.getLogger(__name__)


@dataclass
class MotionConfig:
    enabled: bool = False
    threshold: float = 1.0  # % of frame area that must change
    cooldown: int = 60  # seconds between motion events per camera
    fps: int = 2  # frames to process per second
    resolution: tuple[int, int] = (320, 240)  # resize for MOG2 analysis
    min_contour_area: int = 500  # minimum contour area in pixels
    warmup_frames: int = 30  # frames before detection starts
    history: int = 500  # MOG2 history parameter
    var_threshold: int = 16  # MOG2 varThreshold parameter
    reconnect_delay: int = 5  # initial reconnect wait (seconds)
    max_reconnect_delay: int = 60  # max reconnect backoff (seconds)
    person_detection: bool = True  # run YOLO when motion detected
    person_confidence: float = 0.4  # YOLO confidence threshold
    yolo_model: str = "yolov8n"  # model variant


@dataclass
class MotionEvent:
    camera_name: str
    timestamp: datetime
    motion_percentage: float
    contour_count: int
    person_count: int = 0
    previous_person_count: int = 0
    snapshot: bytes | None = None
    trigger: str = "motion"  # "motion", "person_change", or "both"


@dataclass
class CameraState:
    person_count: int = 0
    last_person_change_at: datetime | None = None
    last_motion_at: datetime | None = None


class CameraMotionDetector:
    """Per-camera worker thread that detects motion and counts people."""

    def __init__(
        self,
        camera_name: str,
        stream: StreamCapture,
        config: MotionConfig,
        event_queue: queue.Queue,
        stop_event: threading.Event,
        yolo_loader,
    ):
        self.camera_name = camera_name
        self._stream = stream
        self.config = config
        self._event_queue = event_queue
        self._stop_event = stop_event
        self._yolo_loader = yolo_loader
        self._enabled = threading.Event()
        if config.enabled:
            self._enabled.set()
        self._last_event_time: float = 0
        self._state = CameraState()
        self._thread: threading.Thread | None = None

    @property
    def state(self) -> CameraState:
        return self._state

    @property
    def is_enabled(self) -> bool:
        return self._enabled.is_set()

    def enable(self) -> None:
        self._enabled.set()

    def disable(self) -> None:
        self._enabled.clear()

    def start(self) -> None:
        self._thread = threading.Thread(
            target=self._loop, daemon=True,
            name=f"motion-{self.camera_name}",
        )
        self._thread.start()

    def _loop(self) -> None:
        while not self._stop_event.is_set():
            # Wait until enabled
            while not self._enabled.is_set() and not self._stop_event.is_set():
                self._stop_event.wait(timeout=1.0)
            if self._stop_event.is_set():
                break

            # Wait for the shared stream to deliver its first frame
            if not self._stream.wait_for_frame(timeout=5.0):
                continue

            logger.info("motion/%s: stream ready, starting detection", self.camera_name)

            bg_sub = cv2.createBackgroundSubtractorMOG2(
                history=self.config.history,
                varThreshold=self.config.var_threshold,
                detectShadows=True,
            )
            frame_count = 0
            frame_interval = 1.0 / self.config.fps

            while not self._stop_event.is_set() and self._enabled.is_set():
                frame = self._stream.get_frame(timeout=frame_interval)
                if frame is None:
                    continue

                frame_count += 1
                w, h = self.config.resolution
                small = cv2.resize(frame, (w, h))

                fg_mask = bg_sub.apply(small)

                if frame_count < self.config.warmup_frames:
                    time.sleep(frame_interval)
                    continue

                # Remove shadows and noise
                _, thresh = cv2.threshold(fg_mask, 200, 255, cv2.THRESH_BINARY)
                kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (3, 3))
                thresh = cv2.morphologyEx(thresh, cv2.MORPH_OPEN, kernel)

                contours, _ = cv2.findContours(
                    thresh, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE
                )
                significant = [
                    c for c in contours
                    if cv2.contourArea(c) >= self.config.min_contour_area
                ]
                motion_area = sum(cv2.contourArea(c) for c in significant)
                total_area = w * h
                motion_pct = (motion_area / total_area) * 100

                if motion_pct >= self.config.threshold:
                    self._handle_motion(
                        frame, motion_pct, len(significant),
                    )

                time.sleep(frame_interval)

    def _handle_motion(
        self, frame, motion_pct: float, contour_count: int,
    ) -> None:
        now = time.time()

        # Run person detection if enabled
        person_count = 0
        if self.config.person_detection:
            person_count = self._count_people(frame)

        prev_count = self._state.person_count
        person_changed = person_count != prev_count
        motion_trigger = motion_pct >= self.config.threshold

        # Determine trigger type
        if person_changed and motion_trigger:
            trigger = "both"
        elif person_changed:
            trigger = "person_change"
        else:
            trigger = "motion"

        # Check cooldown (person changes always bypass cooldown)
        if not person_changed and (now - self._last_event_time < self.config.cooldown):
            return

        self._last_event_time = now
        ts = datetime.now(timezone.utc)
        self._state.last_motion_at = ts

        if person_changed:
            self._state.person_count = person_count
            self._state.last_person_change_at = ts

        # Encode snapshot
        _, jpeg = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, 85])

        event = MotionEvent(
            camera_name=self.camera_name,
            timestamp=ts,
            motion_percentage=round(motion_pct, 1),
            contour_count=contour_count,
            person_count=person_count,
            previous_person_count=prev_count,
            snapshot=jpeg.tobytes(),
            trigger=trigger,
        )
        self._event_queue.put(event)
        logger.info(
            "motion/%s: %s — motion=%.1f%%, people=%d (was %d)",
            self.camera_name, trigger, motion_pct, person_count, prev_count,
        )

    def _count_people(self, frame) -> int:
        model = self._yolo_loader()
        if model is None:
            return 0
        results = model(
            frame,
            conf=self.config.person_confidence,
            classes=[0],  # COCO class 0 = person
            verbose=False,
        )
        if results and results[0].boxes is not None:
            return len(results[0].boxes)
        return 0

class MotionDetectorManager:
    """Orchestrates motion detection across all cameras."""

    def __init__(
        self,
        cameras: dict[str, dict],
        global_config: MotionConfig,
        per_camera_configs: dict[str, MotionConfig] | None = None,
        streams: dict[str, StreamCapture] | None = None,
    ):
        """
        Args:
            cameras: dict of camera_name -> {"rtsp_url": str, "display_name": str, ...}
            global_config: default motion config
            per_camera_configs: optional per-camera overrides
            streams: shared StreamCapture instances keyed by camera name
        """
        self.event_queue: queue.Queue[MotionEvent] = queue.Queue()
        self._stop_event = threading.Event()
        self._detectors: dict[str, CameraMotionDetector] = {}
        self._yolo_model = None
        self._yolo_lock = threading.Lock()
        self._yolo_model_name = global_config.yolo_model

        per_camera_configs = per_camera_configs or {}
        streams = streams or {}

        for name, cam_info in cameras.items():
            stream = streams.get(name)
            if stream is None:
                logger.warning("motion/%s: no shared stream available, skipping", name)
                continue
            config = per_camera_configs.get(name, global_config)
            detector = CameraMotionDetector(
                camera_name=name,
                stream=stream,
                config=config,
                event_queue=self.event_queue,
                stop_event=self._stop_event,
                yolo_loader=self._get_yolo_model,
            )
            self._detectors[name] = detector

    def _get_yolo_model(self):
        """Lazy-load YOLO model (thread-safe, shared across detectors)."""
        if self._yolo_model is not None:
            return self._yolo_model
        with self._yolo_lock:
            if self._yolo_model is not None:
                return self._yolo_model
            try:
                from ultralytics import YOLO
                logger.info("Loading YOLO model: %s", self._yolo_model_name)
                self._yolo_model = YOLO(f"{self._yolo_model_name}.pt")
                return self._yolo_model
            except Exception as e:
                logger.error("Failed to load YOLO model: %s", e)
                return None

    def start(self) -> None:
        for detector in self._detectors.values():
            detector.start()

    def stop(self) -> None:
        self._stop_event.set()

    def enable_camera(self, camera_name: str) -> bool:
        if camera_name in self._detectors:
            self._detectors[camera_name].enable()
            return True
        return False

    def disable_camera(self, camera_name: str) -> bool:
        if camera_name in self._detectors:
            self._detectors[camera_name].disable()
            return True
        return False

    def get_pending_events(self) -> list[MotionEvent]:
        events = []
        while True:
            try:
                events.append(self.event_queue.get_nowait())
            except queue.Empty:
                break
        return events

    def get_scene_state(self, camera_name: str | None = None) -> dict[str, dict]:
        result = {}
        detectors = (
            {camera_name: self._detectors[camera_name]}
            if camera_name and camera_name in self._detectors
            else self._detectors
        )
        for name, detector in detectors.items():
            state = detector.state
            result[name] = {
                "person_count": state.person_count,
                "last_person_change_at": (
                    state.last_person_change_at.isoformat()
                    if state.last_person_change_at else None
                ),
                "last_motion_at": (
                    state.last_motion_at.isoformat()
                    if state.last_motion_at else None
                ),
                "enabled": detector.is_enabled,
            }
        return result

    def status(self) -> dict[str, dict]:
        result = {}
        for name, detector in self._detectors.items():
            state = detector.state
            result[name] = {
                "enabled": detector.is_enabled,
                "person_count": state.person_count,
                "last_motion_at": (
                    state.last_motion_at.isoformat()
                    if state.last_motion_at else None
                ),
            }
        return result
