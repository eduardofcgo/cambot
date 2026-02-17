import threading
from datetime import datetime, timedelta, timezone

from cambot.motion import MotionDetectorManager, MotionEvent

WATCH_OK = "WATCH_OK"


class Watcher:
    """Background watcher that periodically triggers the agent to check cameras."""

    def __init__(
        self, agent, default_interval: int = 300,
        on_alert=None, on_activity=None,
        motion_detector: MotionDetectorManager | None = None,
    ):
        self.agent = agent
        self.default_interval = default_interval  # seconds
        self._next_interval = default_interval
        self._on_alert = on_alert  # callback(alert_text: str, photos: list) or None
        self._on_activity = on_activity  # callback() fired when a check starts
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._motion_detector = motion_detector

        # Observable state
        self.running: bool = False
        self.last_check_at: datetime | None = None
        self.next_check_at: datetime | None = None
        self.last_report: str | None = None
        self.last_schedule_reason: str | None = None
        self._focus_cameras: list[str] | None = None

    def start(self) -> None:
        self.running = True
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop_event.set()
        self.running = False

    def _loop(self) -> None:
        while not self._stop_event.is_set():
            self.next_check_at = datetime.now(timezone.utc) + timedelta(seconds=self._next_interval)

            # Wait for timer, polling for motion events every second
            remaining = self._next_interval
            motion_events: list[MotionEvent] = []
            while remaining > 0 and not self._stop_event.is_set():
                wait_time = min(remaining, 1.0)
                if self._stop_event.wait(timeout=wait_time):
                    break
                remaining -= wait_time

                if self._motion_detector:
                    events = self._motion_detector.get_pending_events()
                    if events:
                        motion_events.extend(events)
                        break

            if self._stop_event.is_set():
                break

            try:
                if self._on_activity:
                    self._on_activity()

                if motion_events:
                    report, next_minutes, schedule_reason, focus_cameras = (
                        self._handle_motion_events(motion_events)
                    )
                else:
                    report, next_minutes, schedule_reason, focus_cameras = self.agent.watch(
                        focus_cameras=self._focus_cameras,
                    )

                photos = self.agent.pop_pending_photos()
                self.last_check_at = datetime.now(timezone.utc)
                self.last_report = report

                # Update interval and focus for next check
                if next_minutes and next_minutes > 0:
                    self._next_interval = next_minutes * 60
                    self.last_schedule_reason = schedule_reason
                    self._focus_cameras = focus_cameras  # may be None (= check all)
                else:
                    self._next_interval = self.default_interval
                    self.last_schedule_reason = None
                    self._focus_cameras = None

                # Only alert if the agent has something to say
                is_ok = report.strip().upper().replace(".", "") == WATCH_OK if report else True

                if not is_ok:
                    now = datetime.now(timezone.utc).strftime("%H:%M UTC")
                    next_min = self._next_interval // 60
                    alert_text = (
                        f"--- Watch alert at {now} ---\n"
                        f"{report}\n"
                        f"--- Next check in {next_min} min ---"
                    )
                    if self._on_alert:
                        self._on_alert(alert_text, photos)
                    else:
                        print(f"\n{alert_text}\n")

            except Exception as e:
                print(f"\nWatch error: {e}")
                self._next_interval = self.default_interval

    def _handle_motion_events(
        self, events: list[MotionEvent],
    ) -> tuple[str, int | None, str | None, list[str] | None]:
        """Deduplicate motion events and call agent with motion context."""
        # Keep highest motion per camera
        by_camera: dict[str, MotionEvent] = {}
        for event in events:
            existing = by_camera.get(event.camera_name)
            if existing is None or event.motion_percentage > existing.motion_percentage:
                by_camera[event.camera_name] = event

        camera_names = list(by_camera.keys())
        motion_context = []
        for cam_name, event in by_camera.items():
            line = (
                f"- {cam_name}: {event.motion_percentage}% motion, "
                f"{event.contour_count} regions, "
                f"{event.person_count} people detected "
                f"(was {event.previous_person_count}), "
                f"trigger={event.trigger}, "
                f"at {event.timestamp.strftime('%H:%M:%S UTC')}"
            )
            motion_context.append(line)

        motion_snapshots = {
            name: event.snapshot
            for name, event in by_camera.items()
            if event.snapshot
        }

        return self.agent.watch_motion(
            motion_cameras=camera_names,
            motion_context="\n".join(motion_context),
            motion_snapshots=motion_snapshots,
        )

    def status(self) -> dict:
        """Return the current watcher state as a dict."""
        result = {
            "running": self.running,
            "last_check_at": self.last_check_at.isoformat() if self.last_check_at else None,
            "next_check_at": self.next_check_at.isoformat() if self.next_check_at else None,
            "last_report": self.last_report,
            "last_schedule_reason": self.last_schedule_reason,
            "interval_seconds": self._next_interval,
            "focus_cameras": self._focus_cameras,
        }
        if self._motion_detector:
            result["motion_detection"] = self._motion_detector.status()
        return result
