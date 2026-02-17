import argparse
import itertools
import logging
import os
import sys
import tempfile
import threading

from dotenv import load_dotenv

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(name)s %(levelname)s: %(message)s",
    datefmt="%H:%M:%S",
)
logging.getLogger("httpx").setLevel(logging.WARNING)

from cambot.camera import CameraManager
from cambot.agent import SecurityAgent
from cambot.config import load_cameras_config


class Spinner:
    """Simple CLI spinner shown while the agent is thinking."""

    _FRAMES = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"

    def __init__(self, message: str = "Thinking"):
        self._message = message
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def __enter__(self):
        self._stop.clear()
        self._thread = threading.Thread(target=self._spin, daemon=True)
        self._thread.start()
        return self

    def __exit__(self, *_):
        self._stop.set()
        if self._thread:
            self._thread.join()
        sys.stderr.write("\r\033[K")
        sys.stderr.flush()

    def _spin(self):
        for frame in itertools.cycle(self._FRAMES):
            if self._stop.is_set():
                break
            sys.stderr.write(f"\r{frame} {self._message}...")
            sys.stderr.flush()
            self._stop.wait(0.1)


def _init_streams(config: dict) -> dict:
    """Create and start StreamCapture instances for motion-enabled cameras."""
    from cambot.capture import StreamCapture

    motion_settings = config.get("settings", {}).get("motion", {})
    fps = motion_settings.get("fps", 2)

    streams = {}
    for cam_cfg in config.get("cameras", []):
        if not cam_cfg.get("enabled", True):
            continue
        if not cam_cfg.get("motion_detection", False):
            continue

        name = cam_cfg["name"]
        sdp_file = cam_cfg.get("sdp_file")
        rtsp_url = cam_cfg.get("rtsp_url")
        if not sdp_file and not rtsp_url:
            continue

        stream = StreamCapture(
            camera_name=name,
            sdp_file=sdp_file,
            rtsp_url=rtsp_url,
            fps=fps,
        )
        stream.start()
        streams[name] = stream

    return streams


def _init_motion(config: dict, camera_manager, streams: dict | None = None):
    """Build a MotionDetectorManager from cameras.yaml config, or return None."""
    from cambot.motion import MotionConfig, MotionDetectorManager

    motion_settings = config.get("settings", {}).get("motion", {})
    if not motion_settings.get("enabled", False):
        return None

    global_config = MotionConfig(
        enabled=True,
        threshold=motion_settings.get("threshold", 1.0),
        cooldown=motion_settings.get("cooldown", 60),
        fps=motion_settings.get("fps", 2),
        resolution=tuple(motion_settings.get("resolution", [320, 240])),
        min_contour_area=motion_settings.get("min_contour_area", 500),
        warmup_frames=motion_settings.get("warmup_frames", 30),
        history=motion_settings.get("history", 500),
        var_threshold=motion_settings.get("var_threshold", 16),
        reconnect_delay=motion_settings.get("reconnect_delay", 5),
        max_reconnect_delay=motion_settings.get("max_reconnect_delay", 60),
        person_detection=motion_settings.get("person_detection", True),
        person_confidence=motion_settings.get("person_confidence", 0.4),
        yolo_model=motion_settings.get("yolo_model", "yolov8n"),
    )

    # Build per-camera configs and camera info dict
    per_camera_configs: dict[str, MotionConfig] = {}
    cameras_info: dict[str, dict] = {}

    for cam_cfg in config.get("cameras", []):
        if not cam_cfg.get("enabled", True):
            continue
        if not cam_cfg.get("motion_detection", False):
            continue

        name = cam_cfg["name"]
        cameras_info[name] = {
            "rtsp_url": cam_cfg.get("rtsp_url"),
            "sdp_file": cam_cfg.get("sdp_file"),
            "display_name": cam_cfg.get("display_name", name),
        }

        cam_motion = cam_cfg.get("motion_config", {})
        if cam_motion:
            per_camera_configs[name] = MotionConfig(
                enabled=True,
                threshold=cam_motion.get("threshold", global_config.threshold),
                cooldown=cam_motion.get("cooldown", global_config.cooldown),
                fps=cam_motion.get("fps", global_config.fps),
                resolution=global_config.resolution,
                min_contour_area=global_config.min_contour_area,
                warmup_frames=global_config.warmup_frames,
                history=global_config.history,
                var_threshold=global_config.var_threshold,
                reconnect_delay=global_config.reconnect_delay,
                max_reconnect_delay=global_config.max_reconnect_delay,
                person_detection=global_config.person_detection,
                person_confidence=global_config.person_confidence,
                yolo_model=global_config.yolo_model,
            )

    if not cameras_info:
        return None

    return MotionDetectorManager(
        cameras_info, global_config, per_camera_configs, streams=streams,
    )


def _save_photos(photos: list[tuple[bytes, str]]) -> None:
    """Save photos to temp dir and print paths."""
    if not photos:
        return
    tmp_dir = tempfile.mkdtemp(prefix="cambot_")
    for i, (jpeg_data, caption) in enumerate(photos):
        path = os.path.join(tmp_dir, f"photo_{i}.jpg")
        with open(path, "wb") as f:
            f.write(jpeg_data)
        print(f"  Photo saved: {path} — {caption}")


def main():
    parser = argparse.ArgumentParser(description="Security camera monitoring agent")
    parser.add_argument("--model", type=str, default=None, help="Claude model to use")
    parser.add_argument("--interval", type=int, default=5, help="Default minutes between watch checks (default: 5)")
    parser.add_argument("--telegram", action="store_true", help="Run as a Telegram bot instead of CLI")
    parser.add_argument("--config", type=str, default=None, help="Path to cameras YAML config file")
    parser.add_argument("--language", type=str, default=None, help="Language for responses (e.g. en, es, pt-BR)")
    parser.add_argument("--locale", type=str, default=None, help="Locale for date/time formatting (e.g. en_US, pt_BR)")
    parser.add_argument("--no-motion", action="store_true", help="Disable motion detection even if configured")
    args = parser.parse_args()

    from pathlib import Path
    config_path = Path(args.config) if args.config else None
    try:
        config = load_cameras_config(config_path)
    except (FileNotFoundError, ValueError) as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)
    camera_manager = CameraManager(config_path)

    # Create shared stream captures for motion-enabled cameras
    streams = {}
    if not args.no_motion:
        streams = _init_streams(config)
        camera_manager.set_streams(streams)

    model = args.model or config.get("settings", {}).get("model", "claude-sonnet-4-5-20250929")
    agent = SecurityAgent(
        camera_manager, config, model=model,
        language=args.language, locale=args.locale,
    )

    # Motion detection setup
    motion_detector = None
    if not args.no_motion:
        motion_detector = _init_motion(config, camera_manager, streams)

    if args.telegram:
        from cambot.telegram import TelegramBot
        from cambot.watcher import Watcher

        bot = TelegramBot(agent)

        chat_id = bot.chat_id
        if chat_id:
            on_alert = lambda text, photos=None: bot.send_alert_sync(chat_id, text, photos)
            on_activity = lambda: bot.send_typing_sync(chat_id)
            watcher = Watcher(
                agent, default_interval=args.interval * 60,
                on_alert=on_alert, on_activity=on_activity,
                motion_detector=motion_detector,
            )
        else:
            watcher = Watcher(agent, default_interval=args.interval * 60,
                              motion_detector=motion_detector)
        agent.watcher = watcher
        agent.motion_detector = motion_detector
        watcher.start()
        if motion_detector:
            motion_detector.start()
            count = sum(1 for d in motion_detector._detectors.values() if d.is_enabled)
            print(f"Motion detection active on {count} camera(s).")
        print(f"Watcher enabled — checking every {args.interval} min (agent can adjust).")

        if agent.memory_store.read():
            chat_id = bot.chat_id
            if chat_id:
                try:
                    bot.send_typing_sync(chat_id)
                    summary = agent.chat(
                        "You just started up. Briefly summarize what you remember "
                        "from your memory — key facts, recent observations, and "
                        "anything the user should know. Be concise."
                    )
                    bot.send_alert_sync(chat_id, f"Startup memory summary:\n{summary}")
                except Exception as e:
                    print(f"(Could not send memory summary: {e})", file=sys.stderr)

        print("Telegram bot starting...")
        bot.run()
    else:
        from cambot.watcher import Watcher
        watcher = Watcher(agent, default_interval=args.interval * 60,
                          motion_detector=motion_detector)
        agent.watcher = watcher
        agent.motion_detector = motion_detector
        watcher.start()
        if motion_detector:
            motion_detector.start()
            count = sum(1 for d in motion_detector._detectors.values() if d.is_enabled)
            print(f"Motion detection active on {count} camera(s).")
        print(f"Watcher enabled — checking every {args.interval} min (agent can adjust).")

        print("Security camera monitor ready. Ask me anything about your cameras.")
        print("Type 'quit' or 'exit' to stop.\n")

        if agent.memory_store.read():
            try:
                with Spinner("Loading memory"):
                    summary = agent.chat(
                        "You just started up. Briefly summarize what you remember "
                        "from your memory — key facts, recent observations, and "
                        "anything the user should know. Be concise."
                    )
                print(f"Agent: {summary}\n")
            except Exception as e:
                print(f"(Could not load memory summary: {e})\n", file=sys.stderr)

        while True:
            try:
                user_input = input("You: ").strip()
            except (KeyboardInterrupt, EOFError):
                print("\nGoodbye!")
                break

            if not user_input:
                continue
            if user_input.lower() in ("quit", "exit"):
                print("Goodbye!")
                break

            try:
                with Spinner():
                    response = agent.chat(user_input)
                    photos = agent.pop_pending_photos()
                print(f"\nAgent: {response}\n")
                _save_photos(photos)
            except Exception as e:
                print(f"\nError: {e}\n", file=sys.stderr)
