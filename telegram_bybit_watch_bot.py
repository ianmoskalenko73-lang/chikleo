#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Telegram Bybit Watch Bot
------------------------
- Следит за торговой парой (Bybit public API, без ключей), присылает алерты.
- Настройки на чат: symbol/category/interval/above/below/pct_move.
- Команды (в чате с ботом):
  /start - справка
  /price <SYMBOL> [category] - показать текущую цену (category: spot|linear, по умолчанию текущее)
  /watch <SYMBOL> [interval] - запустить наблюдение (секунды)
  /stop - остановить наблюдение
  /above <price> - алерт при пересечении ВЫШЕ
  /below <price> - алерт при пересечении НИЖЕ
  /pct <X> - алерт при движении ±X% от базовой цены (база сбрасывается после алерта)
  /interval <sec> - изменить период опроса
  /category <spot|linear> - категория (spot или фьючерсы linear)
  /status - текущие настройки
"""
import asyncio
import os
from dataclasses import dataclass
from typing import Optional, Dict

import requests
from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes

BYBIT_TICKER_URL = "https://api.bybit.com/v5/market/tickers"

@dataclass
class WatchState:
    symbol: str = "ETHUSDT"
    category: str = "spot"        # 'spot' | 'linear' | 'inverse' | 'option'
    interval: float = 5.0          # seconds
    above: Optional[float] = None
    below: Optional[float] = None
    pct_move: Optional[float] = None
    baseline: Optional[float] = None
    task: Optional[asyncio.Task] = None
    last_above_triggered: bool = False
    last_below_triggered: bool = False
    running: bool = False

class BotState:
    def __init__(self):
        self.watches: Dict[int, WatchState] = {}

STATE = BotState()

def get_price(category: str, symbol: str) -> dict:
    params = {"category": category, "symbol": symbol.upper()}
    r = requests.get(BYBIT_TICKER_URL, params=params, timeout=10)
    r.raise_for_status()
    j = r.json()
    if j.get("retCode") != 0:
        raise RuntimeError(f"Bybit error: {j.get('retMsg')}")
    lst = j.get("result", {}).get("list", [])
    if not lst:
        raise RuntimeError("Empty result; check symbol/category")
    it = lst[0]
    price = float(it["lastPrice"])
    chg24 = float(it.get("price24hPcnt", 0.0)) * 100.0 if it.get("price24hPcnt") is not None else None
    return {"price": price, "chg24": chg24}

async def watcher(chat_id: int, ctx: ContextTypes.DEFAULT_TYPE):
    st = STATE.watches[chat_id]
    st.running = True
    await ctx.bot.send_message(chat_id, f"▶️ Старт наблюдения: {st.symbol} ({st.category}), интервал {st.interval}s")
    while st.running:
        try:
            tick = get_price(st.category, st.symbol)
            p = tick["price"]
            chg = tick["chg24"]
            if st.pct_move is not None and st.baseline is None:
                st.baseline = p
            text = f"{st.symbol} = {p:.4f}"
            if chg is not None:
                text += f"  (24h {chg:+.2f}%)"
            alerts = []
            if st.above is not None:
                if p >= st.above and not st.last_above_triggered:
                    alerts.append(f"📈 Выше {st.above:.4f} → {p:.4f}")
                    st.last_above_triggered = True
                elif p < st.above:
                    st.last_above_triggered = False
            if st.below is not None:
                if p <= st.below and not st.last_below_triggered:
                    alerts.append(f"📉 Ниже {st.below:.4f} → {p:.4f}")
                    st.last_below_triggered = True
                elif p > st.below:
                    st.last_below_triggered = False
            if st.pct_move is not None and st.baseline is not None:
                delta = abs(p - st.baseline) / st.baseline * 100.0
                if delta >= st.pct_move:
                    direction = "вверх" if p > st.baseline else "вниз"
                    alerts.append(f"⚠️ Движение {delta:.2f}% {direction} от базы {st.baseline:.4f} → {p:.4f}")
                    st.baseline = p
            if alerts:
                text += "\n" + "\n".join(alerts)
            await ctx.bot.send_message(chat_id, text)
        except Exception as e:
            await ctx.bot.send_message(chat_id, f"[WARN] {e}")
        await asyncio.sleep(max(1.0, st.interval))
    await ctx.bot.send_message(chat_id, "⏹ Наблюдение остановлено.")

def ensure_state(chat_id: int) -> WatchState:
    if chat_id not in STATE.watches:
        STATE.watches[chat_id] = WatchState()
    return STATE.watches[chat_id]

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    st = ensure_state(update.effective_chat.id)
    msg = (
        "Привет! Я бот-наблюдатель за парами Bybit.\n\n"
        "Команды:\n"
        "/price <SYMBOL> [category] — текущая цена\n"
        "/watch <SYMBOL> [interval] — запустить наблюдение\n"
        "/stop — остановить наблюдение\n"
        "/above <price> — алерт при пробое вверх\n"
        "/below <price> — алерт при пробое вниз\n"
        "/pct <X> — алерт при движении ±X% от базовой\n"
        "/interval <sec> — период опроса\n"
        "/category <spot|linear> — категория рынка\n"
        "/status — показать настройки\n\n"
        f"Текущие: {st.symbol} ({st.category}), interval={st.interval}s"
    )
    await update.message.reply_text(msg)

async def cmd_price(update: Update, context: ContextTypes.DEFAULT_TYPE):
    st = ensure_state(update.effective_chat.id)
    try:
        if context.args:
            st.symbol = context.args[0].upper()
        if len(context.args) >= 2:
            st.category = context.args[1].lower()
        t = get_price(st.category, st.symbol)
        await update.message.reply_text(f"{st.symbol} ({st.category}) = {t['price']:.6f}  24h {t['chg24']:+.2f}%")
    except Exception as e:
        await update.message.reply_text(f"Ошибка: {e}")

async def cmd_watch(update: Update, context: ContextTypes.DEFAULT_TYPE):
    st = ensure_state(update.effective_chat.id)
    if context.args:
        st.symbol = context.args[0].upper()
    if len(context.args) >= 2:
        try:
            st.interval = float(context.args[1])
        except:
            pass
    if st.task and not st.task.done():
        await update.message.reply_text("Уже запущено.")
        return
    st.running = True
    st.task = asyncio.create_task(watcher(update.effective_chat.id, context))
    await update.message.reply_text(f"Запускаю наблюдение за {st.symbol} ({st.category})…")

async def cmd_stop(update: Update, context: ContextTypes.DEFAULT_TYPE):
    st = ensure_state(update.effective_chat.id)
    st.running = False
    if st.task:
        try:
            st.task.cancel()
        except:
            pass
    await update.message.reply_text("Останавливаю…")

async def cmd_above(update: Update, context: ContextTypes.DEFAULT_TYPE):
    st = ensure_state(update.effective_chat.id)
    if not context.args:
        await update.message.reply_text("Использование: /above <price>")
        return
    st.above = float(context.args[0])
    st.last_above_triggered = False
    await update.message.reply_text(f"Порог ВЫШЕ установлен: {st.above}")

async def cmd_below(update: Update, context: ContextTypes.DEFAULT_TYPE):
    st = ensure_state(update.effective_chat.id)
    if not context.args:
        await update.message.reply_text("Использование: /below <price>")
        return
    st.below = float(context.args[0])
    st.last_below_triggered = False
    await update.message.reply_text(f"Порог НИЖЕ установлен: {st.below}")

async def cmd_pct(update: Update, context: ContextTypes.DEFAULT_TYPE):
    st = ensure_state(update.effective_chat.id)
    if not context.args:
        await update.message.reply_text("Использование: /pct <процентов>")
        return
    st.pct_move = float(context.args[0])
    st.baseline = None
    await update.message.reply_text(f"Порог движения ±{st.pct_move}% установлен.")

async def cmd_interval(update: Update, context: ContextTypes.DEFAULT_TYPE):
    st = ensure_state(update.effective_chat.id)
    if not context.args:
        await update.message.reply_text(f"Текущий интервал: {st.interval}s")
        return
    st.interval = max(1.0, float(context.args[0]))
    await update.message.reply_text(f"Интервал обновлён: {st.interval}s")

async def cmd_category(update: Update, context: ContextTypes.DEFAULT_TYPE):
    st = ensure_state(update.effective_chat.id)
    if not context.args:
        await update.message.reply_text(f"Текущая категория: {st.category}")
        return
    c = context.args[0].lower()
    if c not in ("spot", "linear", "inverse", "option"):
        await update.message.reply_text("Категория должна быть: spot | linear | inverse | option")
        return
    st.category = c
    await update.message.reply_text(f"Категория установлена: {st.category}")

async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    st = ensure_state(update.effective_chat.id)
    msg = (
        f"Символ: {st.symbol}\n"
        f"Категория: {st.category}\n"
        f"Интервал: {st.interval}s\n"
        f"Порог выше: {st.above}\n"
        f"Порог ниже: {st.below}\n"
        f"Порог ±%: {st.pct_move}\n"
        f"Статус: {'идёт наблюдение' if st.running else 'остановлен'}"
    )
    await update.message.reply_text(msg)

async def main():
    token = os.getenv("TELEGRAM_BOT_TOKEN")
    if not token:
        raise SystemExit("TELEGRAM_BOT_TOKEN не задан в окружении.")
    app = ApplicationBuilder().token(token).build()
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("price", cmd_price))
    app.add_handler(CommandHandler("watch", cmd_watch))
    app.add_handler(CommandHandler("stop", cmd_stop))
    app.add_handler(CommandHandler("above", cmd_above))
    app.add_handler(CommandHandler("below", cmd_below))
    app.add_handler(CommandHandler("pct", cmd_pct))
    app.add_handler(CommandHandler("interval", cmd_interval))
    app.add_handler(CommandHandler("category", cmd_category))
    app.add_handler(CommandHandler("status", cmd_status))
    await app.start()
    await app.updater.start_polling()
    try:
        while True:
            await asyncio.sleep(1)
    except (KeyboardInterrupt, SystemExit):
        pass
    finally:
        await app.updater.stop()
        await app.stop()

if __name__ == "__main__":
    asyncio.run(main())
