import argparse
import itertools
import os
import sys
import tempfile
import threading

from dotenv import load_dotenv

load_dotenv()

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
    args = parser.parse_args()

    from pathlib import Path
    config_path = Path(args.config) if args.config else None
    try:
        config = load_cameras_config(config_path)
    except (FileNotFoundError, ValueError) as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)
    camera_manager = CameraManager(config_path)

    model = args.model or config.get("settings", {}).get("model", "claude-sonnet-4-5-20250929")
    agent = SecurityAgent(
        camera_manager, config, model=model,
        language=args.language, locale=args.locale,
    )

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
            )
        else:
            watcher = Watcher(agent, default_interval=args.interval * 60)
        agent.watcher = watcher
        watcher.start()
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
        watcher = Watcher(agent, default_interval=args.interval * 60)
        agent.watcher = watcher
        watcher.start()
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
