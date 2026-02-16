import asyncio
import os

from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, filters


class TelegramBot:
    """Telegram frontend for the security camera agent."""

    def __init__(self, agent):
        self.agent = agent
        self._loop: asyncio.AbstractEventLoop | None = None
        token = os.environ["TELEGRAM_BOT_TOKEN"]

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

    async def _cmd_start(self, update: Update, context) -> None:
        await update.message.reply_text(
            "Security camera monitor ready. Ask me anything about your cameras."
        )

    async def _handle_message(self, update: Update, context) -> None:
        text = update.message.text
        if not text:
            return

        loop = asyncio.get_event_loop()
        response = await loop.run_in_executor(None, self.agent.chat, text)
        await update.message.reply_text(response)

    async def _send_message(self, chat_id: int | str, message: str) -> None:
        await self.app.bot.send_message(chat_id=chat_id, text=message)

    def send_alert_sync(self, chat_id: int | str, message: str) -> None:
        """Thread-safe alert sender for use from the watcher thread."""
        if self._loop:
            asyncio.run_coroutine_threadsafe(
                self._send_message(chat_id, message), self._loop
            )

    def run(self) -> None:
        self.app.run_polling(allowed_updates=Update.ALL_TYPES)
