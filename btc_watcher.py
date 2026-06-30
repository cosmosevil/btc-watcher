#!/usr/bin/env python3
"""
BTC Wallet Watcher -> Telegram Notifier

Опрашивает mempool.space API по заданному BTC-адресу и присылает
сообщение в Telegram, когда появляется новая входящая транзакция
и когда она набирает нужное число подтверждений.

Настройка:
  1. Создайте бота у @BotFather, получите TELEGRAM_BOT_TOKEN.
  2. Узнайте свой chat_id (например, через @userinfobot) -> TELEGRAM_CHAT_ID.
  3. Укажите BTC_ADDRESS — адрес кошелька, который отслеживаем.
  4. Запустите: python3 btc_watcher.py

Зависимости: requests, python-telegram-bot
  pip install requests python-telegram-bot --break-system-packages
"""

import asyncio
import json
import logging
import os
from pathlib import Path

import requests
from telegram import Bot
from telegram.constants import ParseMode

# ---------------------- НАСТРОЙКИ ----------------------

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "ВАШ_ТОКЕН_БОТА")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "ВАШ_CHAT_ID")
BTC_ADDRESS = os.environ.get("BTC_ADDRESS", "ВАШ_BTC_АДРЕС")

# Сколько подтверждений считать "подтверждённой" транзакцией
REQUIRED_CONFIRMATIONS = 1

# Как часто опрашивать API (в секундах)
POLL_INTERVAL = 90

# Файл, где храним состояние уже обработанных tx, чтобы не дублировать
# уведомления и переживать перезапуск скрипта.
# На Fly.io указываем STATE_FILE_DIR=/data и монтируем туда volume,
# чтобы состояние переживало рестарты контейнера.
STATE_DIR = Path(os.environ.get("STATE_FILE_DIR", "."))
STATE_DIR.mkdir(parents=True, exist_ok=True)
STATE_FILE = STATE_DIR / "btc_watcher_state.json"

# mempool.space — бесплатный публичный API, без ключа
API_BASE = "https://mempool.space/api"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger("btc_watcher")

# ---------------------------------------------------------


def load_state() -> dict:
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text())
    return {"notified_seen": [], "notified_confirmed": []}


def save_state(state: dict) -> None:
    STATE_FILE.write_text(json.dumps(state, indent=2))


def get_current_block_height() -> int:
    r = requests.get(f"{API_BASE}/blocks/tip/height", timeout=15)
    r.raise_for_status()
    return int(r.text)


def get_address_txs(address: str) -> list[dict]:
    """Возвращает список транзакций (включая mempool) по адресу."""
    r = requests.get(f"{API_BASE}/address/{address}/txs", timeout=15)
    r.raise_for_status()
    return r.json()


def tx_is_incoming(tx: dict, address: str) -> tuple[bool, int]:
    """Проверяет, является ли tx входящей на наш адрес, и считает сумму в сатоши."""
    received = 0
    for vout in tx.get("vout", []):
        if vout.get("scriptpubkey_address") == address:
            received += vout.get("value", 0)
    return received > 0, received


async def send_telegram_message(bot: Bot, text: str) -> None:
    await bot.send_message(
        chat_id=TELEGRAM_CHAT_ID,
        text=text,
        parse_mode=ParseMode.HTML,
        disable_web_page_preview=True,
    )


async def check_once(bot: Bot, state: dict) -> None:
    try:
        height = get_current_block_height()
        txs = get_address_txs(BTC_ADDRESS)
    except requests.RequestException as e:
        log.warning("Ошибка запроса к API: %s", e)
        return

    for tx in txs:
        txid = tx["txid"]
        incoming, sats = tx_is_incoming(tx, BTC_ADDRESS)
        if not incoming:
            continue

        status = tx.get("status", {})
        confirmed = status.get("confirmed", False)
        block_height = status.get("block_height")
        confirmations = (height - block_height + 1) if confirmed and block_height else 0

        btc_amount = sats / 1e8

        # Уведомление о новой (ещё неподтверждённой) транзакции
        if txid not in state["notified_seen"]:
            state["notified_seen"].append(txid)
            await send_telegram_message(
                bot,
                f"🟡 <b>Новая входящая транзакция замечена</b>\n"
                f"Сумма: <b>{btc_amount:.8f} BTC</b>\n"
                f"TxID: <code>{txid}</code>\n"
                f"Статус: в мемпуле, ждём подтверждения...",
            )
            log.info("Новая входящая tx %s на %.8f BTC", txid, btc_amount)

        # Уведомление о подтверждении
        if (
            confirmed
            and confirmations >= REQUIRED_CONFIRMATIONS
            and txid not in state["notified_confirmed"]
        ):
            state["notified_confirmed"].append(txid)
            await send_telegram_message(
                bot,
                f"✅ <b>Транзакция подтверждена!</b>\n"
                f"Сумма: <b>{btc_amount:.8f} BTC</b>\n"
                f"Подтверждений: {confirmations}\n"
                f"TxID: <code>{txid}</code>",
            )
            log.info("Tx %s подтверждена (%d confirmations)", txid, confirmations)

    save_state(state)


async def main() -> None:
    if "ВАШ_" in TELEGRAM_BOT_TOKEN or "ВАШ_" in TELEGRAM_CHAT_ID or "ВАШ_" in BTC_ADDRESS:
        log.error(
            "Заполните TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID и BTC_ADDRESS "
            "(через переменные окружения или прямо в коде)."
        )
        return

    bot = Bot(token=TELEGRAM_BOT_TOKEN)
    state = load_state()

    log.info("Запуск мониторинга адреса %s, опрос каждые %d сек.", BTC_ADDRESS, POLL_INTERVAL)

    while True:
        await check_once(bot, state)
        await asyncio.sleep(POLL_INTERVAL)


if __name__ == "__main__":
    asyncio.run(main())
