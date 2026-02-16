import asyncio
import os
import sys

from telegram import Update
from telegram.constants import ChatAction
from telegram.ext import Application, CommandHandler, MessageHandler, filters


class TelegramBot:
    """Telegram frontend for the security camera agent."""

    def __init__(self, agent):
        self.agent = agent

        token = os.environ.get("TELEGRAM_BOT_TOKEN")
        if not token:
            print(
                "Error: TELEGRAM_BOT_TOKEN not set. "
                "Add it to your .env file or export it as an environment variable.",
                file=sys.stderr,
            )
            sys.exit(1)

        self.chat_id = os.environ.get("TELEGRAM_CHAT_ID")
        if not self.chat_id:
            print(
                "Warning: TELEGRAM_CHAT_ID not set. "
                "Watcher alerts and startup summary will print to stdout only.",
                file=sys.stderr,
            )

        self._loop: asyncio.AbstractEventLoop | None = None

        async def _capture_loop(app):
            self._loop = asyncio.get_event_loop()

        self.app = (
            Application.builder()
            .token(token)
            .post_init(_capture_loop)
            .build()
        )
        self.app.add_handler(CommandHandler("start", self._cmd_start))
        self.app.add_handler(
            MessageHandler(filters.TEXT & ~filters.COMMAND, self._handle_message)
        )

    async def _typing_loop(self, chat_id):
        """Send typing action every 5 seconds until cancelled."""
        try:
            while True:
                await self.app.bot.send_chat_action(
                    chat_id=chat_id, action=ChatAction.TYPING
                )
                await asyncio.sleep(5)
        except asyncio.CancelledError:
            pass

    async def _cmd_start(self, update: Update, context) -> None:
        await update.message.reply_text(
            "Security camera monitor ready. Ask me anything about your cameras."
        )

    async def _handle_message(self, update: Update, context) -> None:
        text = update.message.text
        if not text:
            return

        chat_id = update.effective_chat.id
        typing_task = asyncio.create_task(self._typing_loop(chat_id))

        try:
            loop = asyncio.get_event_loop()
            response = await loop.run_in_executor(None, self.agent.chat, text)
            photos = self.agent.pop_pending_photos()
        finally:
            typing_task.cancel()
            await typing_task

        await update.message.reply_text(response)
        for jpeg_data, caption in photos:
            await self.app.bot.send_photo(
                chat_id=chat_id, photo=jpeg_data, caption=caption
            )

    async def _send_message(self, chat_id: int | str, message: str) -> None:
        await self.app.bot.send_message(chat_id=chat_id, text=message)

    async def _send_photos(
        self, chat_id: int | str, photos: list[tuple[bytes, str]]
    ) -> None:
        for jpeg_data, caption in photos:
            await self.app.bot.send_photo(
                chat_id=chat_id, photo=jpeg_data, caption=caption
            )

    async def _send_typing(self, chat_id: int | str) -> None:
        await self.app.bot.send_chat_action(
            chat_id=chat_id, action=ChatAction.TYPING
        )

    def send_alert_sync(
        self,
        chat_id: int | str,
        message: str,
        photos: list[tuple[bytes, str]] | None = None,
    ) -> None:
        """Thread-safe alert sender for use from the watcher thread."""
        if self._loop:
            asyncio.run_coroutine_threadsafe(
                self._send_message(chat_id, message), self._loop
            )
            if photos:
                asyncio.run_coroutine_threadsafe(
                    self._send_photos(chat_id, photos), self._loop
                )

    def send_typing_sync(self, chat_id: int | str) -> None:
        """Thread-safe typing indicator for use from the watcher thread."""
        if self._loop:
            asyncio.run_coroutine_threadsafe(
                self._send_typing(chat_id), self._loop
            )

    def run(self) -> None:
        self.app.run_polling(allowed_updates=Update.ALL_TYPES)
