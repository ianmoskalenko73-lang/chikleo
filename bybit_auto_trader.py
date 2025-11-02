#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Bybit Auto-Trader (Template, v5 API)
------------------------------------
⚠️ Ни одна стратегия не гарантирует прибыль. Этот шаблон в DRY_RUN и TESTNET по умолчанию.
"""
import os, hmac, time, json, math, hashlib, signal
from dataclasses import dataclass
from datetime import datetime, timezone
import requests

BASE_URL_MAIN = "https://api.bybit.com"
BASE_URL_TEST = "https://api-testnet.bybit.com"

API_KEY = os.getenv("BYBIT_API_KEY", "")
API_SECRET = os.getenv("BYBIT_API_SECRET", "")
ENV = os.getenv("BYBIT_ENV", "testnet").lower()
DRY_RUN = os.getenv("DRY_RUN", "true").lower() == "true"
BASE_URL = BASE_URL_MAIN if ENV == "main" else BASE_URL_TEST

DEFAULT_SYMBOL = "ETHUSDT"
CATEGORY = "linear"

SMA_FAST = 20
SMA_SLOW = 50
EMA_TREND = 200
RSI_LEN = 14
RSI_LONG = 55.0
RSI_SHORT = 45.0
ATR_LEN = 14
ATR_SL_MULT = 1.5
ATR_TP_MULT = 2.0

RISK_PCT = 0.003
MAX_LEVERAGE = 5
MAX_POS_EQUITY_FRAC = 0.2
DAILY_LOSS_LIMIT = 0.02
GLOBAL_DD_LIMIT = 0.05
POLL_SEC = 15
CANDLE_INTERVAL = "5"

def ts_ms():
    return str(int(time.time()*1000))

def hmac_sha256(secret, msg):
    return hmac.new(secret.encode(), msg.encode(), hashlib.sha256).hexdigest()

def private_headers(payload):
    body = json.dumps(payload) if payload else ""
    t = ts_ms(); recv = "5000"
    sign_str = t + API_KEY + recv + body
    sign = hmac_sha256(API_SECRET, sign_str)
    return {
        "Content-Type": "application/json",
        "X-BAPI-API-KEY": API_KEY,
        "X-BAPI-TIMESTAMP": t,
        "X-BAPI-RECV-WINDOW": recv,
        "X-BAPI-SIGN": sign,
    }

def http_get(path, params=None, auth=False):
    url = BASE_URL + path
    if not auth:
        r = requests.get(url, params=params or {}, timeout=10)
    else:
        headers = private_headers({})
        r = requests.get(url, params=params or {}, headers=headers, timeout=10)
    r.raise_for_status()
    j = r.json()
    if j.get("retCode") != 0:
        raise RuntimeError(f"Bybit error {j.get('retCode')}: {j.get('retMsg')}")
    return j["result"]

def http_post(path, payload):
    url = BASE_URL + path
    headers = private_headers(payload)
    r = requests.post(url, data=json.dumps(payload), headers=headers, timeout=10)
    r.raise_for_status()
    j = r.json()
    if j.get("retCode") != 0:
        raise RuntimeError(f"Bybit error {j.get('retCode')}: {j.get('retMsg')} ({j})")
    return j["result"]

def round_step(value, step):
    import math
    return math.floor(value/step)*step

def sma(values, n):
    if len(values) < n or n <= 0: return None
    return sum(values[-n:])/n

def ema(values, n):
    if len(values) < n or n <= 0: return None
    k = 2/(n+1)
    e = sum(values[:n])/n
    for v in values[n:]: e = v*k + e*(1-k)
    return e

def rsi(values, n):
    if len(values) < n+1: return None
    gains=0.0; losses=0.0
    for i in range(-n,0):
        d = values[i]-values[i-1]
        if d>=0: gains+=d
        else: losses-=d
    if losses==0: return 100.0
    rs = gains/losses
    return 100.0 - (100.0/(1.0+rs))

def atr(high, low, close, n):
    if len(close) < n+1: return None
    trs=[]
    for i in range(1,len(close)):
        tr = max(high[i]-low[i], abs(high[i]-close[i-1]), abs(low[i]-close[i-1]))
        trs.append(tr)
    if len(trs) < n: return None
    a = sum(trs[:n])/n
    for x in trs[n:]:
        a = (a*(n-1)+x)/n
    return a

def get_klines(symbol, limit=200):
    params = {"category": CATEGORY, "symbol": symbol, "interval": CANDLE_INTERVAL, "limit": str(limit)}
    res = http_get("/v5/market/kline", params)
    data = sorted(res["list"], key=lambda x: int(x[0]))
    o,h,l,c=[],[],[],[]
    for row in data:
        o.append(float(row[1])); h.append(float(row[2])); l.append(float(row[3])); c.append(float(row[4]))
    return {"open":o,"high":h,"low":l,"close":c}

def get_instrument(symbol):
    res = http_get("/v5/market/instruments-info", {"category": CATEGORY, "symbol": symbol})
    item = res["list"][0]; lot=item["lotSizeFilter"]; pricef=item["priceFilter"]; lev=item.get("leverageFilter",{})
    return {"qty_step": float(lot["qtyStep"]), "min_qty": float(lot["minOrderQty"]),
            "tick_size": float(pricef["tickSize"]), "max_leverage": float(lev.get("maxLeverage","10"))}

def get_wallet_equity():
    res = http_get("/v5/account/wallet-balance", {"accountType":"UNIFIED"}, auth=True)
    lst = res.get("list", [])
    if not lst: return 0.0
    return float(lst[0]["totalEquity"])

def set_leverage(symbol, lev):
    payload = {"category": CATEGORY, "symbol": symbol, "buyLeverage": str(lev), "sellLeverage": str(lev)}
    if DRY_RUN: print("[DRY_RUN] set leverage", payload); return
    http_post("/v5/position/set-leverage", payload)

def place_market_order(symbol, side, qty, tp, sl):
    payload = {"category": CATEGORY, "symbol": symbol, "side": "Buy" if side=='long' else "Sell",
               "orderType":"Market", "qty": f"{qty}", "timeInForce":"IOC", "reduceOnly": False, "tpslMode":"Full"}
    if tp: payload["takeProfit"]=f"{tp}"
    if sl: payload["stopLoss"]=f"{sl}"
    if DRY_RUN: print("[DRY_RUN] order", payload); return {"orderId":"DRYRUN"}
    return http_post("/v5/order/create", payload)

@dataclass
class Position:
    side:str=""
    entry:float=0.0
    qty:float=0.0
    sl:float=0.0
    tp:float=0.0

class Bot:
    def __init__(self, symbol):
        self.symbol=symbol
        self.instrument=get_instrument(symbol)
        self.start_equity=get_wallet_equity()
        self.day_start_equity=self.start_equity
        self.position=Position()
        lev=min(5,int(self.instrument["max_leverage"]))
        try: set_leverage(symbol, lev)
        except Exception as e: print("[WARN] set_leverage failed:",e)

    def compute_indicators(self, k):
        c=k["close"]; h=k["high"]; l=k["low"]
        return {"ema200": ema(c, EMA_TREND), "sfast": sma(c, SMA_FAST), "sslow": sma(c, SMA_SLOW),
                "rsi": rsi(c, RSI_LEN), "atr": atr(h,l,c,ATR_LEN), "close": c[-1]}

    def decide(self, x):
        price=x["close"]
        long_sig  = (x["sfast"] and x["sslow"] and x["sfast"]>x["sslow"]) and (x["ema200"] and price>x["ema200"]) and (x["rsi"] and x["rsi"]>RSI_LONG)
        short_sig = (x["sfast"] and x["sslow"] and x["sfast"]<x["sslow"]) and (x["ema200"] and price<x["ema200"]) and (x["rsi"] and x["rsi"]<RSI_SHORT)
        if not self.position.side:
            if long_sig: return "open_long"
            if short_sig: return "open_short"
            return "hold"
        if self.position.side=="long":
            if x["atr"]:
                new_sl=max(self.position.sl, price-ATR_SL_MULT*x["atr"])
                if new_sl>self.position.sl: self.position.sl=new_sl
            if short_sig: return "close_long"
        else:
            if x["atr"]:
                new_sl=min(self.position.sl, price+ATR_SL_MULT*x["atr"])
                if new_sl<self.position.sl: self.position.sl=new_sl
            if long_sig: return "close_short"
        return "manage"

    def position_sizing(self, price, atr_val):
        equity=get_wallet_equity()
        risk_dollars=equity*RISK_PCT
        stop_dist=max(atr_val*ATR_SL_MULT, price*0.002)
        qty=risk_dollars/stop_dist
        max_notional=equity*MAX_POS_EQUITY_FRAC*MAX_LEVERAGE
        qty=min(qty, max_notional/price)
        qty_step=self.instrument["qty_step"]
        qty=max(round_step(qty, qty_step), self.instrument["min_qty"])
        return float(f"{qty:.6f}")

    def bracket_prices(self, side, price, atr_val):
        tick=self.instrument["tick_size"]
        if side=="long":
            sl=round_step(price-ATR_SL_MULT*atr_val, tick)
            tp=round_step(price+ATR_TP_MULT*atr_val, tick)
        else:
            sl=round_step(price+ATR_SL_MULT*atr_val, tick)
            tp=round_step(price-ATR_TP_MULT*atr_val, tick)
        return tp, sl

    def circuit_breakers(self):
        equity=get_wallet_equity()
        if self.day_start_equity>0 and (equity-self.day_start_equity)/self.day_start_equity<=-DAILY_LOSS_LIMIT:
            print("⛔ Daily loss limit reached. Pausing."); return True
        if self.start_equity>0 and (equity-self.start_equity)/self.start_equity<=-GLOBAL_DD_LIMIT:
            print("⛔ Global drawdown limit reached. Stopping."); raise SystemExit(0)
        return False

    def loop(self):
        print(f"▶️ ENV={ENV} DRY_RUN={DRY_RUN} {self.symbol}")
        print(f"Equity: {get_wallet_equity():.2f} USDT")
        while True:
            try:
                if self.circuit_breakers():
                    time.sleep(60); continue
                k=get_klines(self.symbol)
                x=self.compute_indicators(k)
                price=x["close"]
                action=self.decide(x)
                now=datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
                print(f"[{now}] P={price:.2f} EMA200={x['ema200'] and round(x['ema200'],2)} "
                      f"SMA{SMA_FAST}/{SMA_SLOW}={x['sfast'] and round(x['sfast'],2)}/{x['sslow'] and round(x['sslow'],2)} "
                      f"RSI={x['rsi'] and round(x['rsi'],1)} ATR={x['atr'] and round(x['atr'],2)} -> {action}")
                if action=="open_long" and x["atr"]:
                    qty=self.position_sizing(price, x["atr"]); tp,sl=self.bracket_prices("long",price,x["atr"])
                    place_market_order(self.symbol,"long",qty,tp,sl); self.position=Position("long",price,qty,sl,tp)
                    print(f"✅ Open LONG qty={qty} @≈{price:.2f} TP={tp} SL={sl}")
                elif action=="open_short" and x["atr"]:
                    qty=self.position_sizing(price, x["atr"]); tp,sl=self.bracket_prices("short",price,x["atr"])
                    place_market_order(self.symbol,"short",qty,tp,sl); self.position=Position("short",price,qty,sl,tp)
                    print(f"✅ Open SHORT qty={qty} @≈{price:.2f} TP={tp} SL={sl}")
                elif action in ("close_long","close_short") and self.position.side:
                    side="Sell" if self.position.side=="long" else "Buy"
                    payload={"category":CATEGORY,"symbol":self.symbol,"side":side,"orderType":"Market","qty":f"{self.position.qty}","reduceOnly":True,"timeInForce":"IOC"}
                    if DRY_RUN: print("[DRY_RUN] close", payload)
                    else: http_post("/v5/order/create", payload)
                    print("❎ Close position"); self.position=Position()
                if self.position.side:
                    if self.position.side=="long" and price<=self.position.sl:
                        payload={"category":CATEGORY,"symbol":self.symbol,"side":"Sell","orderType":"Market",
                                 "qty":f\"{self.position.qty}\", "reduceOnly":True,"timeInForce":"IOC"}
                        if DRY_RUN: print("[DRY_RUN] SL"); else: http_post("/v5/order/create", payload)
                        print("🛑 Long SL"); self.position=Position()
                    elif self.position.side=="short" and price>=self.position.sl:
                        payload={"category":CATEGORY,"symbol":self.symbol,"side":"Buy","orderType":"Market",
                                 "qty":f\"{self.position.qty}\", "reduceOnly":True,"timeInForce":"IOC"}
                        if DRY_RUN: print("[DRY_RUN] SL"); else: http_post("/v5/order/create", payload)
                        print("🛑 Short SL"); self.position=Position()
                time.sleep(POLL_SEC)
            except KeyboardInterrupt:
                print("Stopping..."); break
            except Exception as e:
                print("[WARN]", e); time.sleep(POLL_SEC)

def main():
    import argparse
    parser=argparse.ArgumentParser()
    parser.add_argument("--symbol", default=DEFAULT_SYMBOL)
    args=parser.parse_args()
    if not API_KEY or not API_SECRET:
        print("⚠️ BYBIT_API_KEY/SECRET не заданы — DRY_RUN.")
    Bot(args.symbol.upper()).loop()

if __name__=="__main__":
    signal.signal(signal.SIGTERM, lambda *_: exit(0))
    main()
