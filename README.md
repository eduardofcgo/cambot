# cambot

An autonomous and privacy conscious AI that watches your security cameras and tells you what's going on. You talk to it in plain language — ask what's happening, teach it who belongs where, tell it what should and shouldn't be happening.

Unlike having a person watch your feeds, cambot respects privacy by default — it only reports what's security-relevant and ignores personal details. No one is watching you live; it just checks in, looks for what matters, and stays quiet unless something needs your attention.

## Setup

```bash
# Clone and install
git clone <repo-url> && cd cambot
pip install -e .

# Add your Anthropic API key
echo "ANTHROPIC_API_KEY=sk-..." > .env

# Configure your cameras (copy the example and fill in your RTSP URLs)
cp config/cameras.sdp.yaml config/cameras.yaml
```

## Run

```bash
# Start autonomous monitoring CLI
cambot --watch

# Run as a Telegram bot
export TELEGRAM_BOT_TOKEN=your-token
export TELEGRAM_CHAT_ID=your-chat-id
cambot --telegram --watch
```
