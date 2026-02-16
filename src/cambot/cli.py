import argparse
import os
import sys

from dotenv import load_dotenv

load_dotenv()

from cambot.camera import CameraManager
from cambot.agent import SecurityAgent
from cambot.config import load_cameras_config


def main():
    parser = argparse.ArgumentParser(description="Security camera monitoring agent")
    parser.add_argument("--model", type=str, default=None, help="Claude model to use")
    parser.add_argument("--mock", action="store_true", help="Use mock cameras with sample images (no real cameras needed)")
    parser.add_argument("--watch", action="store_true", help="Enable autonomous periodic camera checks")
    parser.add_argument("--interval", type=int, default=5, help="Default minutes between watch checks (default: 5)")
    parser.add_argument("--telegram", action="store_true", help="Run as a Telegram bot instead of CLI")
    parser.add_argument("--config", type=str, default=None, help="Path to cameras YAML config file")
    args = parser.parse_args()

    if args.mock:
        from cambot.mock import MockCameraManager, load_mock_config
        camera_manager = MockCameraManager()
        config = load_mock_config()
        print("Running in mock mode with sample camera images.\n")
    else:
        from pathlib import Path
        config_path = Path(args.config) if args.config else None
        try:
            config = load_cameras_config(config_path)
        except (FileNotFoundError, ValueError) as e:
            print(f"Error: {e}", file=sys.stderr)
            sys.exit(1)
        camera_manager = CameraManager(config_path)

    model = args.model or config.get("settings", {}).get("model", "claude-sonnet-4-5-20250929")
    agent = SecurityAgent(camera_manager, config, model=model)

    if args.telegram:
        from cambot.telegram import TelegramBot

        bot = TelegramBot(agent)

        if args.watch:
            from cambot.watcher import Watcher
            chat_id = os.environ.get("TELEGRAM_CHAT_ID")
            if chat_id:
                on_alert = lambda text: bot.send_alert_sync(chat_id, text)
                watcher = Watcher(agent, default_interval=args.interval * 60, on_alert=on_alert)
            else:
                print("Warning: TELEGRAM_CHAT_ID not set, watcher alerts will print to stdout.")
                watcher = Watcher(agent, default_interval=args.interval * 60)
            agent.watcher = watcher
            watcher.start()
            print(f"Watcher enabled — checking every {args.interval} min (agent can adjust).")

        if agent.memory_store.read():
            chat_id = os.environ.get("TELEGRAM_CHAT_ID")
            if chat_id:
                try:
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
        if args.watch:
            from cambot.watcher import Watcher
            watcher = Watcher(agent, default_interval=args.interval * 60)
            agent.watcher = watcher
            watcher.start()
            print(f"Watcher enabled — checking every {args.interval} min (agent can adjust).")

        print("Security camera monitor ready. Ask me anything about your cameras.")
        print("Type 'quit' or 'exit' to stop.\n")

        if agent.memory_store.read():
            try:
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
                response = agent.chat(user_input)
                print(f"\nAgent: {response}\n")
            except Exception as e:
                print(f"\nError: {e}\n", file=sys.stderr)
