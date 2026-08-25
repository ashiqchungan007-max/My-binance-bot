import hashlib
import hmac
import math
import os
import threading
import time
import urllib.parse
from datetime import datetime, timezone
from flask import Flask
import requests

# ===================================================
# DUMMY WEB SERVER (FOR RENDER / REPLIT)
# ===================================================
app = Flask(__name__)

@app.route('/')
def home():
    return "XAUUSDT High-Volume Session Bot with Telegram Notifications is Running!"

def run_web_server():
    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port)

# Self-Ping to prevent Render from sleeping
def keep_alive():
    render_url = os.environ.get("RENDER_EXTERNAL_URL")
    if render_url:
        while True:
            try:
                requests.get(render_url, timeout=10)
                print("\n Keep-alive ping sent to server.")
            except Exception as e:
                print(f"\nPing Error: {e}")
            time.sleep(600)  # Every 10 minutes

# ===================================================
# API KEYS & CONFIGURATION (REAL ACCOUNT SETTINGS)
# ===================================================
API_KEY = os.environ.get("BINANCE_API_KEY", "")
API_SECRET = os.environ.get("BINANCE_SECRET_KEY", "")

# TELEGRAM CONFIGURATION
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")

# Real Account Configuration
USE_TESTNET = False  

SYMBOL = "XAUUSDT"
BASE_URL = "https://fapi.binance.com"  # Real Account Futures Endpoint

# MODIFIED: Reduced leverage to 5x for safe account management
LEVERAGE = 20
QUANTITY = 0.005  # Minimum safe quantity for XAUUSDT

CANDLE_TIMEFRAME = "15m"
MACRO_TIMEFRAME = "1h"
POLL_INTERVAL = 10 

# RISK & STRATEGY SETTINGS
ATR_PERIOD = 14
ATR_SL_MULTIPLIER = 1.5
ATR_TP_MULTIPLIER = 3.0
BE_ATR_MULTIPLIER = 2.0  
BREAKOUT_BUFFER_PERCENT = 0.001  
ADX_THRESHOLD = 25
DONCHIAN_PERIOD = 20

# SESSION TIME FILTER (UTC) - London & New York Active Trading Hours
SESSION_START_HOUR_UTC = 7   # 07:00 UTC (12:30 PM IST)
SESSION_END_HOUR_UTC = 21    # 21:00 UTC (02:30 AM IST)

def notify(title, message):
    current_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    formatted_msg = f"[{current_time}]\n🔔 *{title}*\n{message}"
    
    # 1. Console Log
    print(f"\n{formatted_msg}")
    
    # 2. Telegram Send
    if TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID:
        try:
            url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
            payload = {
                "chat_id": TELEGRAM_CHAT_ID,
                "text": formatted_msg,
                "parse_mode": "Markdown"
            }
            requests.post(url, json=payload, timeout=5)
        except Exception as e:
            print(f"Telegram Send Error: {e}")

def is_high_volume_session():
    """Checks if current UTC time falls within London / New York high-volume hours on weekdays."""
    now_utc = datetime.now(timezone.utc)
    if now_utc.weekday() in [5, 6]:
        return False
    return SESSION_START_HOUR_UTC <= now_utc.hour < SESSION_END_HOUR_UTC

def send_signed_request(http_method, url_path, payload=None):
    if not API_KEY or not API_SECRET:
        print("\n❌ ERROR: BINANCE_API_KEY or BINANCE_SECRET_KEY is missing!")
        return None

    if payload is None:
        payload = {}
    try:
        query_string = urllib.parse.urlencode(payload)
        timestamp = int(time.time() * 1000)
        query_string += f"&timestamp={timestamp}" if query_string else f"timestamp={timestamp}"

        signature = hmac.new(
            API_SECRET.encode('utf-8'),
            query_string.encode('utf-8'),
            hashlib.sha256
        ).hexdigest()

        full_url = f"{BASE_URL}{url_path}?{query_string}&signature={signature}"
        headers = {"X-MBX-APIKEY": API_KEY}

        if http_method == "POST":
            res = requests.post(full_url, headers=headers, timeout=10)
        elif http_method == "DELETE":
            res = requests.delete(full_url, headers=headers, timeout=10)
        else:
            res = requests.get(full_url, headers=headers, timeout=10)

        if res.status_code == 200:
            return res.json()
        elif res.status_code in [429, 418]:
            print(f"\n⚠️ Rate Limit Warning ({res.status_code}). Pausing 30s...")
            time.sleep(30)
            return None
        else:
            print(f"\n⚠️ Binance API Error ({res.status_code}): {res.text}")
            return None
    except Exception as e:
        print(f"\n⚠️ Network Exception: {e}")
        return None

# ===================================================
# EXCHANGE FILTERS & PRECISION
# ===================================================
def get_symbol_filters(symbol):
    try:
        url = f"{BASE_URL}/fapi/v1/exchangeInfo"
        res = requests.get(url, timeout=5).json()
        if "symbols" in res:
            for sym in res['symbols']:
                if sym['symbol'] == symbol:
                    tick_size, step_size = 0.01, 0.001
                    for f in sym['filters']:
                        if f['filterType'] == 'PRICE_FILTER':
                            tick_size = float(f['tickSize'])
                        elif f['filterType'] == 'LOT_SIZE':
                            step_size = float(f['stepSize'])
                    return tick_size, step_size
    except Exception as e:
        print(f"Filter Fetch Error: {e}")
    return 0.01, 0.001

TICK_SIZE, STEP_SIZE = get_symbol_filters(SYMBOL)

def round_step(value, step):
    if step <= 0: return float(value)
    precision = int(round(-math.log10(step)))
    factor = 10 ** precision
    return round(math.floor(float(value) * factor) / factor, precision)

# ===================================================
# ACCOUNT & POSITION MANAGEMENT
# ===================================================
def ensure_one_way_mode():
    path = "/fapi/v1/positionSide/dual"
    res = send_signed_request("GET", path)
    if res and res.get("dualSidePosition") == True:
        send_signed_request("POST", path, {"dualSidePosition": "false"})

def set_leverage(symbol, leverage):
    path = "/fapi/v1/leverage"
    payload = {"symbol": symbol, "leverage": leverage}
    return send_signed_request("POST", path, payload)

def get_position_info(symbol):
    path = "/fapi/v2/positionRisk"
    res = send_signed_request("GET", path, {"symbol": symbol})
    if res and isinstance(res, list):
        for pos in res:
            if pos['symbol'] == symbol:
                return float(pos['positionAmt']), float(pos['entryPrice'])
    return None, 0.0

def check_if_sl_is_at_breakeven(symbol, entry_price):
    path = "/fapi/v1/openOrders"
    res = send_signed_request("GET", path, {"symbol": symbol})
    if res and isinstance(res, list):
        for order in res:
            if order.get("type") == "STOP_MARKET":
                stop_price = float(order.get("stopPrice", 0.0))
                if abs(stop_price - entry_price) < (TICK_SIZE * 5):
                    return True
    return False

# ===================================================
# ORDERS & BREAK-EVEN SYSTEM
# ===================================================
def cancel_all_orders(symbol):
    path = "/fapi/v1/allOpenOrders"
    payload = {"symbol": symbol}
    return send_signed_request("DELETE", path, payload)

def emergency_close_position(symbol, current_position_side, quantity):
    path = "/fapi/v1/order"
    close_side = "SELL" if current_position_side.upper() in ["BUY", "LONG"] else "BUY"
    payload = {
        "symbol": symbol,
        "side": close_side,
        "type": "MARKET",
        "quantity": f"{round_step(quantity, STEP_SIZE)}",
        "reduceOnly": "true"
    }
    notify("EMERGENCY CLOSE", f"Closing {current_position_side} position immediately with {close_side} order!")
    return send_signed_request("POST", path, payload)

def place_futures_order(symbol, side, quantity):
    path = "/fapi/v1/order"
    qty_str = f"{round_step(quantity, STEP_SIZE)}"
    payload = {
        "symbol": symbol,
        "side": side,
        "type": "MARKET",
        "quantity": qty_str
    }
    res = send_signed_request("POST", path, payload)
    if res and 'orderId' in res:
        avg_price = float(res.get('avgPrice', 0.0))
        return res, avg_price
    return None, 0.0

def place_stop_loss_and_take_profit(symbol, exit_side, sl_price, tp_price, qty):
    cancel_all_orders(symbol)
    path = "/fapi/v1/order"
    sl_str = f"{round_step(sl_price, TICK_SIZE)}"
    tp_str = f"{round_step(tp_price, TICK_SIZE)}"

    sl_payload = {
        "symbol": symbol,
        "side": exit_side,
        "type": "STOP_MARKET",
        "stopPrice": sl_str,
        "closePosition": "true"
    }
    sl_res = send_signed_request("POST", path, sl_payload)

    tp_payload = {
        "symbol": symbol,
        "side": exit_side,
        "type": "TAKE_PROFIT_MARKET",
        "stopPrice": tp_str,
        "closePosition": "true"
    }
    tp_res = send_signed_request("POST", path, tp_payload)

    sl_ok = sl_res and 'orderId' in sl_res
    tp_ok = tp_res and 'orderId' in tp_res

    if not (sl_ok and tp_ok):
        notify("CRITICAL ERROR", "SL or TP placement failed! Executing emergency close for safety.")
        pos_side = "LONG" if exit_side == "SELL" else "SHORT"
        emergency_close_position(symbol, pos_side, qty)
        return False
    return True

def update_break_even_sl(symbol, exit_side, entry_price, tp_price, qty, current_price):
    min_dist = TICK_SIZE * 10
    if exit_side == "SELL" and (current_price - entry_price) < min_dist:
        notify("BE WARNING", "Price too close to Entry. Skipping Break-Even update.")
        return False
    elif exit_side == "BUY" and (entry_price - current_price) < min_dist:
        notify("BE WARNING", "Price too close to Entry. Skipping Break-Even update.")
        return False

    return place_stop_loss_and_take_profit(symbol, exit_side, entry_price, tp_price, qty)

# ===================================================
# KLINES & INDICATORS
# ===================================================
def get_klines_data(symbol, interval, limit=250):
    try:
        url = f"{BASE_URL}/fapi/v1/klines?symbol={symbol}&interval={interval}&limit={limit}"
        res = requests.get(url, timeout=5)
        if res.status_code == 200:
            data = res.json()
            timestamps = [int(candle[0]) for candle in data]
            closes = [float(candle[4]) for candle in data]
            highs = [float(candle[2]) for candle in data]
            lows = [float(candle[3]) for candle in data]
            volumes = [float(candle[5]) for candle in data]
            return timestamps, closes, highs, lows, volumes
    except Exception as e:
        print(f"Candle Fetch Error ({interval}): {e}")
    return None, None, None, None, None

def calculate_ema(prices, period):
    if len(prices) < period: return 0.0
    k = 2 / (period + 1)
    ema = sum(prices[:period]) / period
    for price in prices[period:]:
        ema = (price * k) + (ema * (1 - k))
    return ema

def _wilders_rma(values, period):
    if len(values) < period: return [0.0] * len(values)
    rma = [sum(values[:period]) / period]
    for val in values[period:]:
        new_rma = (rma[-1] * (period - 1) + val) / period
        rma.append(new_rma)
    return rma

def calculate_atr(highs, lows, closes, period=14):
    if len(closes) <= period: return 2.0
    tr_list = []
    for i in range(1, len(closes)):
        tr = max(highs[i] - lows[i], abs(highs[i] - closes[i-1]), abs(lows[i] - closes[i-1]))
        tr_list.append(tr)
    atr_series = _wilders_rma(tr_list, period)
    return atr_series[-1]

def calculate_adx(highs, lows, closes, period=14):
    if len(closes) <= (period * 2): return 0.0
    tr_list, pdm_list, mdm_list = [], [], []
    for i in range(1, len(closes)):
        tr = max(highs[i] - lows[i], abs(highs[i] - closes[i-1]), abs(lows[i] - closes[i-1]))
        tr_list.append(tr)
        up_move = highs[i] - highs[i-1]
        down_move = lows[i-1] - lows[i]
        pdm = up_move if (up_move > down_move and up_move > 0) else 0.0
        mdm = down_move if (down_move > up_move and down_move > 0) else 0.0
        pdm_list.append(pdm)
        mdm_list.append(mdm)

    tr_rma = _wilders_rma(tr_list, period)
    pdm_rma = _wilders_rma(pdm_list, period)
    mdm_rma = _wilders_rma(mdm_list, period)

    dx_list = []
    for i in range(len(tr_rma)):
        if tr_rma[i] == 0: continue
        pdi = (pdm_rma[i] / tr_rma[i]) * 100
        mdi = (mdm_rma[i] / tr_rma[i]) * 100
        di_sum = pdi + mdi
        dx = (abs(pdi - mdi) / di_sum * 100) if di_sum != 0 else 0.0
        dx_list.append(dx)

    if len(dx_list) < period: return 0.0
    adx_rma = _wilders_rma(dx_list, period)
    return adx_rma[-1]

def calculate_stoch_rsi(closes, period=14, stoch_period=14):
    if len(closes) < (period + stoch_period + 5): return 50.0
    gains, losses = [], []
    for i in range(1, len(closes)):
        change = closes[i] - closes[i-1]
        gains.append(max(change, 0.0))
        losses.append(abs(min(change, 0.0)))

    avg_gains = _wilders_rma(gains, period)
    avg_losses = _wilders_rma(losses, period)

    rsi_list = []
    for i in range(len(avg_gains)):
        if avg_losses[i] == 0:
            rsi_list.append(100.0)
        else:
            rs = avg_gains[i] / avg_losses[i]
            rsi_list.append(100.0 - (100.0 / (1.0 + rs)))

    if len(rsi_list) < stoch_period: return 50.0
    recent_rsi = rsi_list[-stoch_period:]
    min_rsi, max_rsi = min(recent_rsi), max(recent_rsi)
    if max_rsi == min_rsi: return 50.0

    return ((rsi_list[-1] - min_rsi) / (max_rsi - min_rsi)) * 100.0

# ===================================================
# BOT MAIN LOOP
# ===================================================
def bot_loop():
    global TICK_SIZE, STEP_SIZE
    notify("SYSTEM STARTUP", f"Bot Initialized for {SYMBOL} ({CANDLE_TIMEFRAME}) | Leverage: {LEVERAGE}x | Qty: {QUANTITY}")
    
    ensure_one_way_mode()
    set_leverage(SYMBOL, LEVERAGE)
    TICK_SIZE, STEP_SIZE = get_symbol_filters(SYMBOL)

    was_in_position = False
    last_traded_candle_time = None

    while True:
        try:
            actual_pos, current_entry_price = get_position_info(SYMBOL)

            if actual_pos is None:
                time.sleep(POLL_INTERVAL)
                continue

            if actual_pos == 0 and was_in_position:
                cancel_all_orders(SYMBOL)
                notify("POSITION CLOSED", "Position closed via TP/SL. Orders cleaned up safely.")
                was_in_position = False

            if actual_pos != 0:
                was_in_position = True

            timestamps, closes, highs, lows, volumes = get_klines_data(SYMBOL, CANDLE_TIMEFRAME, limit=250)
            _, macro_closes, _, _, _ = get_klines_data(SYMBOL, MACRO_TIMEFRAME, limit=250)

            if (closes is not None and macro_closes is not None and 
                len(closes) >= 200 and len(macro_closes) >= 200):
                
                current_candle_time = timestamps[-2]
                last_closed_price = closes[-2]
                current_price = closes[-1]

                # Indicators
                ema_200_15m = calculate_ema(closes[:-1], 200)
                ema_200_1h = calculate_ema(macro_closes[:-1], 200)
                
                adx_val = calculate_adx(highs[:-1], lows[:-1], closes[:-1], ATR_PERIOD)
                stoch_rsi = calculate_stoch_rsi(closes[:-1])
                atr_val = calculate_atr(highs[:-1], lows[:-1], closes[:-1], ATR_PERIOD)

                last_vol = volumes[-2]
                vol_ma = sum(volumes[-21:-1]) / 20

                # Donchian Level System
                donchian_high = max(highs[-(DONCHIAN_PERIOD + 1): -1])
                donchian_low = min(lows[-(DONCHIAN_PERIOD + 1): -1])

                donchian_high_buffered = donchian_high * (1 + BREAKOUT_BUFFER_PERCENT)
                donchian_low_buffered = donchian_low * (1 - BREAKOUT_BUFFER_PERCENT)

                session_active = is_high_volume_session()
                print(f"Price: ${current_price:,.2f} | ADX: {adx_val:.1f} | Active Session: {session_active} | Active Qty: {actual_pos}", end="\r")

                # BREAK-EVEN STOP LOSS MANAGEMENT
                if actual_pos != 0 and current_entry_price > 0:
                    is_sl_moved_to_be = check_if_sl_is_at_breakeven(SYMBOL, current_entry_price)
                    
                    if not is_sl_moved_to_be:
                        profit_distance = (current_price - current_entry_price) if actual_pos > 0 else (current_entry_price - current_price)
                        if profit_distance >= (atr_val * BE_ATR_MULTIPLIER):
                            close_side = "SELL" if actual_pos > 0 else "BUY"
                            new_tp = current_entry_price + (atr_val * ATR_TP_MULTIPLIER) if actual_pos > 0 else current_entry_price - (atr_val * ATR_TP_MULTIPLIER)
                            
                            success = update_break_even_sl(
                                SYMBOL, 
                                close_side, 
                                current_entry_price, 
                                new_tp, 
                                abs(actual_pos),
                                current_price
                            )
                            if success:
                                notify("RISK UPDATE", f"Profit target reached! SL moved to Break-Even (${current_entry_price:,.2f}) safely.")

                # ENTRY CONDITIONS WITH SESSION FILTER
                if actual_pos == 0 and adx_val >= ADX_THRESHOLD and last_traded_candle_time != current_candle_time:
                    if session_active and last_vol > vol_ma:
                        sl_distance = atr_val * ATR_SL_MULTIPLIER
                        tp_distance = atr_val * ATR_TP_MULTIPLIER

                        # LONG ENTRY
                        if last_closed_price > donchian_high_buffered and last_closed_price > ema_200_15m and last_closed_price > ema_200_1h and stoch_rsi < 80:
                            order, avg_price = place_futures_order(SYMBOL, "BUY", QUANTITY)

                            if order and 'orderId' in order:
                                last_traded_candle_time = current_candle_time
                                time.sleep(2.5)  
                                _, real_entry = get_position_info(SYMBOL)
                                entry = real_entry if real_entry > 0 else (avg_price if avg_price > 0 else current_price)
                                
                                sl_price = entry - sl_distance
                                tp_price = entry + tp_distance

                                place_stop_loss_and_take_profit(SYMBOL, "SELL", sl_price, tp_price, QUANTITY)
                                notify("LONG EXECUTED", f"Entry: ${entry:,.2f}\nSL: ${sl_price:,.2f}\nTP: ${tp_price:,.2f}")

                        # SHORT ENTRY
                        elif last_closed_price < donchian_low_buffered and last_closed_price < ema_200_15m and last_closed_price < ema_200_1h and stoch_rsi > 20:
                            order, avg_price = place_futures_order(SYMBOL, "SELL", QUANTITY)

                            if order and 'orderId' in order:
                                last_traded_candle_time = current_candle_time
                                time.sleep(2.5)  
                                _, real_entry = get_position_info(SYMBOL)
                                entry = real_entry if real_entry > 0 else (avg_price if avg_price > 0 else current_price)

                                sl_price = entry + sl_distance
                                tp_price = entry - tp_distance

                                place_stop_loss_and_take_profit(SYMBOL, "BUY", sl_price, tp_price, QUANTITY)
                                notify("SHORT EXECUTED", f"Entry: ${entry:,.2f}\nSL: ${sl_price:,.2f}\nTP: ${tp_price:,.2f}")

        except Exception as e:
            print(f"\nLoop Error: {e}")

        time.sleep(POLL_INTERVAL)

if __name__ == "__main__":
    t = threading.Thread(target=bot_loop)
    t.daemon = True
    t.start()
    
    t_ping = threading.Thread(target=keep_alive)
    t_ping.daemon = True
    t_ping.start()
    
    run_web_server()
