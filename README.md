# flight-watch

Finds the soonest affordable flight to Eindhoven (from Vienna or Bratislava) and posts it to Telegram.
Runs every ~5 min on GitHub Actions. Settings in `config.json`.

- Telegram commands: `/now`, `/budget 80`, `/help`.
- Alerts: earlier affordable flight, best flight price change, new cheapest fares. Silent 23:00-07:00, digest at 08:00.
- Secrets (repo settings): `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`.
