# 🤖 MemeBot – Meme-Coin-Scanner mit Telegram-Alarm

Läuft rund um die Uhr und

1. **findet neue Meme Coins** auf Solana, Ethereum, Base, BNB Chain und Arbitrum (DexScreener, GeckoTerminal),
2. **prüft Honeypot & Rug Pull** (GoPlus, Honeypot.is, RugCheck: Steuern, Mint/Freeze, LP-Lock, Wale, Insider/Bundles),
3. **prüft den Markt** (Liquidität, MC, Alter, Volumen, Kauf/Verkauf, Wash-Trading, 5-Min-Spikes),
4. **misst den Hype**: X/Twitter (twitterapi.io), Telegram-Kanäle (Zweit-Account), US-News & Promi-Posts (Trump, Elon, CZ …),
5. **lässt Claude recherchieren**, was hinter dem Coin steckt und woher der Hype kommt,
6. **beobachtet Wallets**, verfolgt jeden Call (MC beim Call, Hoch, x-Faktor) und rechnet die Bilanz,
7. **meldet nur Coins, die nach 15 Minuten Bestätigung immer noch alles erfüllen** – oder sofort bei sehr starkem Hype (⚡ HYPE-CALL).

> ⚠️ Keine Anlageberatung. Die meisten Meme Coins verlieren fast ihren ganzen Wert.

## Installation (Hetzner, Ubuntu)

Beim Server-Erstellen unter **Cloud config** einfügen (Token eintragen):

```yaml
#cloud-config
write_files:
  - path: /root/memebot.env
    permissions: '0600'
    content: |
      TELEGRAM_BOT_TOKEN=DEIN_TOKEN
      ALERT_CHAT_ID=
runcmd:
  - apt-get update
  - apt-get install -y git
  - git clone https://github.com/<DEIN-NAME>/memebot /opt/memebot
  - mv /root/memebot.env /opt/memebot/.env
  - bash /opt/memebot/deploy/install.sh
```

Danach dem Bot in Telegram `/start` schreiben. Alle weiteren Schlüssel setzt du im Chat:
`/ki_key`, `/x_key`, `/tg_api` + `/tg_login`. Updates: `/update`. Einstellungen: `/get`, `/set`.

## Test ohne Internet

```
python3 tests/test_offline.py
```
