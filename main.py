import hashlib
import hmac
import math
import os
import threading
import time
import urllib.parse
from datetime import datetime, timezone
from flask import Flask
import pandas as pd
import pandas_ta as ta
import requests

# ===================================================
# WEB SERVER & KEEP-ALIVE (FOR RENDER 24/7 LIVE)
# ===================================================
app = Flask(__name__)

@app.route('/')
def home():
    return "XAUUSDT High-Volume Session Bot is Running Safely on REAL ACCOUNT!", 200

def run_web_server():
    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port)

def keep_alive():
    """Self-Ping mechanism to prevent Render free instances from sleeping."""
    render_url = os.environ.get("RENDER_EXTERNAL_URL")
    if render_url:
        print(f"🔄 Keep-Alive service started for: {render_url}")
        while True:
            time.sleep(600)  # Ping every 10 minutes
            try:
                res = requests.get(render_url, timeout=10)
                print(f"\n🟢 Keep-alive ping sent to server. Status Code: {res.status_code}")
            except Exception as e:
                print(f"\n🔴 Keep-alive Ping Error: {e}")
    else:
        print("⚠️ Warning: RENDER_EXTERNAL_URL is not set in Environment Variables.")

# ===================================================
# API KEYS & CONFIGURATION (REAL ACCOUNT SETTINGS)
# ===================================================
API_KEY = os.environ.get("BINANCE_API_KEY", "")
API_SECRET = os.environ.get("BINANCE_SECRET_KEY", "")

# Real Account Configuration
USE_TESTNET = False  # Set to False for Real Live Account

SYMBOL = "XAUUSDT"
BASE_URL = "https://fapi.binance.com"  # Real Account Futures Endpoint

LEVERAGE = 40
QUANTITY = 0.005  # Safe minimum lot size for XAUUSDT

CANDLE_TIMEFRAME = "15m"
MACRO_TIMEFRAME = "1h"
POLL_INTERVAL = 10 

# RISK & STRATEGY SETTINGS
ATR_PERIOD = 14
ATR_SL_MULTIPLIER = 1.5
ATR_TP_MULTIPLIER = 3.0
BE_ATR_MULTIPLIER = 2.0  # Break-Even trigger set to 2.0x ATR
BREAKOUT_BUFFER_PERCENT = 0.001  # 0.1% Price Buffer
ADX_THRESHOLD = 25
DONCHIAN_PERIOD = 20

# SESSION TIME FILTER (UTC) - London & New York Active Trading Hours
SESSION_START_HOUR_UTC = 7   # 07:00 UTC
SESSION_END_HOUR_UTC = 21    # 21:00 UTC

def notify(title, message):
    current_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"\n[{current_time}] 🔔 {title}: {message}")

def is_high_volume_session():
    """Checks if current UTC time falls within London / New York high-volume hours."""
    now_utc = datetime.now(timezone.utc)
    return SESSION_START_HOUR_UTC <= now_utc.hour < SESSION_END_HOUR_UTC

def send_signed_request(http_method, url_path, payload=None):
    if not API_KEY or not API_SECRET:
        print("\n❌ ERROR: BINANCE_API_KEY or BINANCE_SECRET_KEY is missing in Environment Variables!")
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
    notify("EMERGENCY", f"Closing {current_position_side} position immediately with {close_side} order!")
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
        notify("BE WARNING", "Price too close to Entry. Skipping Break-Even update for safety.")
        return False
    elif exit_side == "BUY" and (entry_price - current_price) < min_dist:
        notify("BE WARNING", "Price too close to Entry. Skipping Break-Even update for safety.")
        return False

    return place_stop_loss_and_take_profit(symbol, exit_side, entry_price, tp_price, qty)

# ===================================================
# KLINES & PANDAS DATAFRAME
# ===================================================
def get_klines_df(symbol, interval, limit=250):
    try:
        url = f"{BASE_URL}/fapi/v1/klines?symbol={symbol}&interval={interval}&limit={limit}"
        res = requests.get(url, timeout=5)
        if res.status_code == 200:
            data = res.json()
            cols = ['timestamp', 'open', 'high', 'low', 'close', 'volume', 
                    'close_time', 'qav', 'num_trades', 'taker_base_vol', 'taker_quote_vol', 'ignore']
            df = pd.DataFrame(data, columns=cols)
            
            # Convert string values to float
            df['high'] = df['high'].astype(float)
            df['low'] = df['low'].astype(float)
            df['close'] = df['close'].astype(float)
            df['volume'] = df['volume'].astype(float)
            return df
    except Exception as e:
        print(f"Candle Fetch Error ({interval}): {e}")
    return None

# ===================================================
# BOT MAIN LOOP
# ===================================================
def bot_loop():
    global TICK_SIZE, STEP_SIZE
    notify("System Startup", f"Real Account Bot initialized for {SYMBOL} ({CANDLE_TIMEFRAME}) | Fixed Qty: {QUANTITY}")
    
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
                notify("Position Cleanup", "Position closed. Cleaned up remaining SL/TP orders.")
                was_in_position = False

            if actual_pos != 0:
                was_in_position = True

            # Fetch DataFrame using Pandas
            df_15m = get_klines_df(SYMBOL, CANDLE_TIMEFRAME, limit=250)
            df_1h = get_klines_df(SYMBOL, MACRO_TIMEFRAME, limit=250)

            if df_15m is not None and len(df_15m) >= 200 and df_1h is not None and len(df_1h) >= 200:
                # 1. EMAs
                df_15m['ema_200'] = ta.ema(df_15m['close'], length=200)
                df_1h['ema_200'] = ta.ema(df_1h['close'], length=200)

                # 2. ATR
                df_15m['atr'] = ta.atr(df_15m['high'], df_15m['low'], df_15m['close'], length=ATR_PERIOD)

                # 3. ADX
                adx_df = ta.adx(df_15m['high'], df_15m['low'], df_15m['close'], length=ATR_PERIOD)
                df_15m['adx'] = adx_df[f'ADX_{ATR_PERIOD}']

                # 4. Stoch RSI
                stoch_rsi_df = ta.stochrsi(df_15m['close'], length=14, rsi_length=14, k=3, d=3)
                df_15m['stoch_rsi'] = stoch_rsi_df['STOCHRSIk_14_14_3_3']

                # 5. Volume MA
                df_15m['vol_ma'] = ta.sma(df_15m['volume'], length=20)

                # 6. Donchian Channel
                donchian_df = ta.donchian(df_15m['high'], df_15m['low'], lower_length=DONCHIAN_PERIOD, upper_length=DONCHIAN_PERIOD)
                df_15m['donchian_high'] = donchian_df[f'DCU_{DONCHIAN_PERIOD}_{DONCHIAN_PERIOD}']
                df_15m['donchian_low'] = donchian_df[f'DCL_{DONCHIAN_PERIOD}_{DONCHIAN_PERIOD}']

                # Values for closed candle (iloc[-2]) and active candle (iloc[-1])
                current_candle_time = df_15m['timestamp'].iloc[-2]
                last_closed_price = df_15m['close'].iloc[-2]
                current_price = df_15m['close'].iloc[-1]

                ema_200_15m = df_15m['ema_200'].iloc[-2]
                ema_200_1h = df_1h['ema_200'].iloc[-2]
                adx_val = df_15m['adx'].iloc[-2]
                stoch_rsi = df_15m['stoch_rsi'].iloc[-2]
                atr_val = df_15m['atr'].iloc[-2]

                last_vol = df_15m['volume'].iloc[-2]
                vol_ma = df_15m['vol_ma'].iloc[-2]

                donchian_high = df_15m['donchian_high'].iloc[-2]
                donchian_low = df_15m['donchian_low'].iloc[-2]

                # Price Buffer for Donchian Levels
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
                                notify("Risk Update", f"Profit target {BE_ATR_MULTIPLIER}x ATR reached! SL moved to Break-Even safely.")

                # ENTRY CONDITIONS WITH SESSION FILTER
                if actual_pos == 0 and adx_val >= ADX_THRESHOLD and last_traded_candle_time != current_candle_time:
                    if session_active and last_vol > vol_ma:
                        sl_distance = atr_val * ATR_SL_MULTIPLIER
                        tp_distance = atr_val * ATR_TP_MULTIPLIER

                        # LONG ENTRY
                        if last_closed_price > donchian_high_buffered and last_closed_price > ema_200_15m and last_closed_price > ema_200_1h and stoch_rsi < 80:
                            notify("Trade Signal", f"Active Session Bullish Breakout! Opening LONG Qty: {QUANTITY}...")
                            order, avg_price = place_futures_order(SYMBOL, "BUY", QUANTITY)

                            if order and 'orderId' in order:
                                last_traded_candle_time = current_candle_time
                                time.sleep(1.5)
                                _, real_entry = get_position_info(SYMBOL)
                                entry = real_entry if real_entry > 0 else (avg_price if avg_price > 0 else current_price)
                                
                                sl_price = entry - sl_distance
                                tp_price = entry + tp_distance

                                place_stop_loss_and_take_profit(SYMBOL, "SELL", sl_price, tp_price, QUANTITY)
                                notify("LONG Executed", f"Entry: ${entry:,.2f} | SL: ${sl_price:,.2f} | TP: ${tp_price:,.2f}")

                        # SHORT ENTRY
                        elif last_closed_price < donchian_low_buffered and last_closed_price < ema_200_15m and last_closed_price < ema_200_1h and stoch_rsi > 20:
                            notify("Trade Signal", f"Active Session Bearish Breakdown! Opening SHORT Qty: {QUANTITY}...")
                            order, avg_price = place_futures_order(SYMBOL, "SELL", QUANTITY)

                            if order and 'orderId' in order:
                                last_traded_candle_time = current_candle_time
                                time.sleep(1.5)
                                _, real_entry = get_position_info(SYMBOL)
                                entry = real_entry if real_entry > 0 else (avg_price if avg_price > 0 else current_price)

                                sl_price = entry + sl_distance
                                tp_price = entry - tp_distance

                                place_stop_loss_and_take_profit(SYMBOL, "BUY", sl_price, tp_price, QUANTITY)
                                notify("SHORT Executed", f"Entry: ${entry:,.2f} | SL: ${sl_price:,.2f} | TP: ${tp_price:,.2f}")

        except Exception as e:
            print(f"\nLoop Error: {e}")

        time.sleep(POLL_INTERVAL)

# ===================================================
# MAIN EXECUTION THREADS
# ===================================================
if __name__ == "__main__":
    # 1. Trading Bot Loop Thread
    t_bot = threading.Thread(target=bot_loop, daemon=True)
    t_bot.start()
    
    # 2. Keep-Alive Self-Ping Thread
    t_ping = threading.Thread(target=keep_alive, daemon=True)
    t_ping.start()
    
    # 3. Flask Web Server (Primary Thread)
    run_web_server()
