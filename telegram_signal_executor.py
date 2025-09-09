import os
import re
import csv
from typing import Optional, List
from dataclasses import dataclass
from datetime import datetime

from telethon import TelegramClient, events
import oandapyV20
import oandapyV20.endpoints.orders as orders
import oandapyV20.endpoints.accounts as accounts

# -------- CONFIG --------
API_ID = int(os.getenv("TELEGRAM_API_ID") or "0")
API_HASH = os.getenv("TELEGRAM_API_HASH")
SESSION_NAME = "tg_signals_session"
OANDA_TOKEN = os.getenv("OANDA_API_TOKEN")
OANDA_ACCOUNT_ID = os.getenv("OANDA_ACCOUNT_ID")

DRY_RUN = False   # 🚨 LIVE MODE

# Risk sizing
RISK_PER_TRADE = 0.01
DEFAULT_UNITS = 100

# Log file
LOG_FILE = "trades_log.csv"

# Allowed Telegram channels
ALLOWED_CHATS = {
    -1001923416377: "TheForexSupport | Free Forex Signals",
    -1001949522847: "EU Forex & Gold (XAUUSD) Free",
    -1001285105872: "EU Forex Signals Free",
    -1002680963182: "gold cast (1)",
    -1001289155011: "gold cast (2)",
    -1001953985322: "SNIPER FREE GOLD (XAUUSD) & FOREX",
    -1001765226347: "Ben, Gold Trader",
    -1002011420218: "ALL STAR FOREX"
}

# -------- Instrument map (abridged for brevity) --------
INSTRUMENT_MAP = {
    "eurusd": "EUR_USD", "usdjpy": "USD_JPY", "gbpusd": "GBP_USD",
    "usdchf": "USD_CHF", "xauusd": "XAU_USD", "gold": "XAU_USD",
    "silver": "XAG_USD", "xagusd": "XAG_USD", "nas100": "NAS100_USD",
    "sp500": "SPX500_USD", "dow": "US30_USD"
}

# -------- Regex patterns --------
SIDE_RE = re.compile(r"\b(buy|long|sell|short)\b", re.I)
PRICE_RANGE_RE = re.compile(r"@?\s*([\d.,]+)\s*(?:-|to|/)\s*([\d.,]+)")
SL_RE = re.compile(r"(?:sl|stoploss|stop loss)[:=\s]*([\d.,]+)", re.I)

@dataclass
class TradeSignal:
    side: str
    instrument: str
    entry_min: Optional[float] = None
    entry_max: Optional[float] = None
    entry: Optional[float] = None
    sl: Optional[float] = None
    tps: List[float] = None

# -------- Utilities --------
def parse_number(s: str) -> float:
    s = s.replace(",", "").strip()
    return float(s)

def normalize_instrument(token: str) -> Optional[str]:
    t = token.lower().replace(" ", "").replace("-", "").replace("_", "")
    return INSTRUMENT_MAP.get(t)

# -------- Multi-signal Parser --------
def parse_signals(text: str) -> List[TradeSignal]:
    txt = text.replace("\n", " ").strip()
    txt_low = txt.lower()
    signals = []

    # Identify instrument tokens
    instrument_candidates = []
    for key in INSTRUMENT_MAP.keys():
        if key in txt_low:
            instrument_candidates.append((key, INSTRUMENT_MAP[key]))

    for m in re.finditer(r"#?([A-Za-z]{3,6})([A-Za-z]{3,6})?", text):
        token = (m.group(1) + (m.group(2) or "")).lower()
        inst = normalize_instrument(token)
        if inst and (token, inst) not in instrument_candidates:
            instrument_candidates.append((token, inst))

    for token, inst in instrument_candidates:
        pattern = re.compile(rf"({token})[^A-Za-z0-9]*([^#]+)", re.I)
        match = pattern.search(txt)
        if not match:
            continue
        chunk = match.group(0)

        m_side = SIDE_RE.search(chunk)
        if not m_side:
            continue
        side_raw = m_side.group(1).lower()
        side = "buy" if side_raw in ("buy", "long") else "sell"

        entry_min = entry_max = None
        m_range = PRICE_RANGE_RE.search(chunk)
        if m_range:
            entry_min = parse_number(m_range.group(1))
            entry_max = parse_number(m_range.group(2))
        else:
            m_single = re.search(r"@?\s*([\d.,]+)", chunk)
            if m_single:
                entry_min = entry_max = parse_number(m_single.group(1))

        m_sl = SL_RE.search(chunk)
        sl = parse_number(m_sl.group(1)) if m_sl else None

        tps = []
        for m in re.finditer(r"(tp\d*|target)[:=\s]*([\d.,]+)", chunk, re.I):
            try:
                tps.append(parse_number(m.group(2)))
            except:
                pass

        if not tps:
            for m in re.finditer(r"(tp\d*|target)[:=\s]*([\d.,]+)", txt, re.I):
                try:
                    tps.append(parse_number(m.group(2)))
                except:
                    pass

        sig = TradeSignal(side=side, instrument=inst,
                          entry_min=entry_min, entry_max=entry_max,
                          sl=sl, tps=tps or [])
        signals.append(sig)

    return signals

# -------- Risk sizing --------
def calc_units(balance: float, signal: TradeSignal) -> int:
    if not signal.sl or not signal.entry_min:
        return DEFAULT_UNITS

    risk_amount = balance * RISK_PER_TRADE
    entry_price = signal.entry_min
    stop_loss = signal.sl
    pip_risk = abs(entry_price - stop_loss)

    if pip_risk == 0:
        return DEFAULT_UNITS

    units = risk_amount / pip_risk
    return int(units if signal.side == "buy" else -units)

# -------- Trade logging --------
def log_trade(signal: TradeSignal, source: str, units: int, mode: str):
    file_exists = os.path.isfile(LOG_FILE)

    with open(LOG_FILE, mode="a", newline="") as f:
        writer = csv.writer(f)
        if not file_exists:
            writer.writerow([
                "timestamp", "source", "instrument", "side", "units",
                "entry_min", "entry_max", "stop_loss", "take_profits", "mode"
            ])

        writer.writerow([
            datetime.utcnow().isoformat(),
            source,
            signal.instrument,
            signal.side,
            units,
            signal.entry_min,
            signal.entry_max,
            signal.sl,
            "|".join(map(str, signal.tps)) if signal.tps else "",
            mode
        ])

# -------- Order execution --------
async def execute_order(signal: TradeSignal, source: str):
    client = oandapyV20.API(access_token=OANDA_TOKEN)

    # fetch account balance
    r_acc = accounts.AccountDetails(OANDA_ACCOUNT_ID)
    client.request(r_acc)
    balance = float(r_acc.response["account"]["balance"])

    units = calc_units(balance, signal)

    order_data = {
        "order": {
            "instrument": signal.instrument,
            "units": str(units),
            "type": "MARKET",
            "positionFill": "DEFAULT"
        }
    }

    if signal.sl:
        order_data["order"]["stopLossOnFill"] = {"price": str(signal.sl)}

    if signal.tps:
        order_data["order"]["takeProfitOnFill"] = {"price": str(signal.tps[0])}

    if DRY_RUN:
        print(f"[DRY RUN] {source} → {signal.side.upper()} {signal.instrument} "
              f"units={units} SL={signal.sl} TP={signal.tps}")
        log_trade(signal, source, units, "DRY_RUN")
        return

    r = orders.OrderCreate(OANDA_ACCOUNT_ID, data=order_data)
    client.request(r)
    print("Order executed:", r.response)

    log_trade(signal, source, units, "LIVE")

# -------- Telegram client --------
async def main_loop():
    tg_id = int(os.getenv("TELEGRAM_API_ID") or API_ID)
    tg_hash = os.getenv("TELEGRAM_API_HASH") or API_HASH
    if tg_id == 0 or not tg_hash:
        raise RuntimeError("Set TELEGRAM_API_ID and TELEGRAM_API_HASH environment variables.")

    client = TelegramClient(SESSION_NAME, tg_id, tg_hash)
    await client.start()
    print("Telegram client started. Listening for messages...")

    @client.on(events.NewMessage(incoming=True))
    async def handler(event):
        # 🚫 skip forwarded messages
        if event.message.fwd_from:
            return

        text = event.message.message or ""
        chat = await event.get_chat()
        sender = getattr(chat, "title", None) or getattr(chat, "username", None) or str(event.chat_id)

        if event.chat_id not in ALLOWED_CHATS:
            return

        print(f"\nNew message from [{sender}]: {text}")

        sigs = parse_signals(text)
        if not sigs:
            print("No actionable trade signals detected.")
            return

        for sig in sigs:
            print("Parsed signal:", sig)
            await execute_order(sig, sender)

    await client.run_until_disconnected()

if __name__ == "__main__":
    import asyncio
    print("Starting Telegram -> OANDA signal executor")
    print("DRY_RUN =", DRY_RUN)
    try:
        asyncio.run(main_loop())
    except KeyboardInterrupt:
        print("Stopping...")
