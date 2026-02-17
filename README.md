# cambot

An autonomous and privacy conscious AI that watches your security cameras and tells you what's going on. You talk to it in plain language — ask what's happening, teach it who belongs where, tell it what should and shouldn't be happening.

Unlike having a person watch your feeds, cambot respects privacy by default — it only reports what's security-relevant and ignores personal details. No one is watching you live; it just checks in, looks for what matters, and stays quiet unless something needs your attention.

## Setup

```bash
# Clone and install
git clone <repo-url> && cd cambot
pip install -e .

# Copy the example env and fill in your keys
cp .env.example .env

# Configure your cameras (copy the example and fill in your RTSP URLs)
cp config/cameras.sdp.yaml config/cameras.yaml
```

### Environment variables

| Variable | Required | Description |
|---|---|---|
| `ANTHROPIC_API_KEY` | Yes | Anthropic API key for Claude |
| `TELEGRAM_BOT_TOKEN` | For `--telegram` | Bot token from @BotFather |
| `TELEGRAM_CHAT_ID` | For alerts | Chat ID for watcher alerts and startup summaries |

## Run

```bash
# Interactive CLI with autonomous monitoring
cambot

# Specify language for responses
cambot --language pt-PT

# Run as a Telegram bot
cambot --telegram
```

### CLI options

| Flag | Description |
|---|---|
| `--telegram` | Run as a Telegram bot instead of interactive CLI |
| `--language LANG` | Language for responses (e.g. `en`, `es`, `pt-BR`) |
| `--locale LOCALE` | Locale for date/time formatting (e.g. `en_US`, `pt_BR`) |
| `--model MODEL` | Claude model to use (default: from config or `claude-sonnet-4-5-20250929`) |
| `--interval MIN` | Minutes between watch checks (default: 5, agent can adjust dynamically) |
| `--config PATH` | Path to cameras YAML config file |
