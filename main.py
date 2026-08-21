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
import pandas as pd
import pandas_ta as ta

# ===================================================
# DUMMY WEB SERVER (FOR RENDER / REPLIT)
# ===================================================
app = Flask(__name__)

@app.route('/')
def home():
    return "XAUUSDT High-Volume Session Bot (Optimized v2) is Running Safely on REAL ACCOUNT!"

def run_web_server():
    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port)

# Self-Ping to prevent Render from sleeping (Pinged every 5 minutes)
def keep_alive():
    render_url = os.environ.get("RENDER_EXTERNAL_URL")
    if render_url:
        while True:
            try:
                requests.get(render_url, timeout=10)
                print("\n Keep-alive ping sent to server.")
            except Exception as e:
                print(f"\nPing Error: {e}")
            time.sleep(300)  # Every 5 minutes

# ===================================================
# API KEYS & CONFIGURATION (REAL ACCOUNT SETTINGS)
# ===================================================
API_KEY = os.environ.get("BINANCE_API_KEY", "")
API_SECRET = os.environ.get("BINANCE_SECRET_KEY", "")

# Real Account Configuration
USE_TESTNET = False  # Set to False for Real Live Account

SYMBOL = "XAUUSDT"
BASE_URL = "https://fapi.binance.com"  # Real Account Futures Endpoint

LEVERAGE = 5
QUANTITY = 0.01  # Safe minimum lot size for XAUUSDT

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
VOLUME_MULTIPLIER = 1.3  # Balanced volume spike filter to avoid fakeouts

# SESSION TIME FILTER (UTC) - London & New York Active Trading Hours
SESSION_START_HOUR_UTC = 7   # 07:00 UTC (12:30 PM IST)
SESSION_END_HOUR_UTC = 21    # 21:00 UTC (02:30 AM IST)

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

def get_open_stop_orders(symbol):
    path = "/fapi/v1/openOrders"
    res = send_signed_request("GET", path, {"symbol": symbol})
    open_sl_orders = []
    if res and isinstance(res, list):
        for order in res:
            if order.get("type") in ["STOP_MARKET", "STOP"]:
                open_sl_orders.append(order)
    return open_sl_orders

def check_if_sl_is_at_breakeven(symbol, entry_price):
    open_sl_orders = get_open_stop_orders(symbol)
    for order in open_sl_orders:
        stop_price = float(order.get("stopPrice", 0.0))
        if abs(stop_price - entry_price) < (TICK_SIZE * 5):
            return True
    return False

# ===================================================
# ORDERS & SAFE BREAK-EVEN SYSTEM
# ===================================================
def cancel_all_orders(symbol):
    path = "/fapi/v1/allOpenOrders"
    payload = {"symbol": symbol}
    return send_signed_request("DELETE", path, payload)

def cancel_single_order(symbol, order_id):
    path = "/fapi/v1/order"
    payload = {"symbol": symbol, "orderId": order_id}
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

def safe_update_break_even_sl(symbol, exit_side, entry_price, current_price):
    """
    Safely updates SL to Break-Even WITHOUT leaving the position unprotected.
    It places the new Break-Even SL FIRST, verifies success, and only then cancels the old SL.
    """
    min_dist = TICK_SIZE * 10
    if exit_side == "SELL" and (current_price - entry_price) < min_dist:
        notify("BE WARNING", "Price too close to Entry. Skipping Break-Even update for safety.")
        return False
    elif exit_side == "BUY" and (entry_price - current_price) < min_dist:
        notify("BE WARNING", "Price too close to Entry. Skipping Break-Even update for safety.")
        return False

    sl_str = f"{round_step(entry_price, TICK_SIZE)}"
    path = "/fapi/v1/order"
    
    new_sl_payload = {
        "symbol": symbol,
        "side": exit_side,
        "type": "STOP_MARKET",
        "stopPrice": sl_str,
        "closePosition": "true"
    }

    # Step 1: Get existing SL orders before placing new one
    old_sl_orders = get_open_stop_orders(symbol)

    # Step 2: Place NEW Break-Even SL order FIRST
    new_sl_res = send_signed_request("POST", path, new_sl_payload)

    if new_sl_res and 'orderId' in new_sl_res:
        # Step 3: Only after new SL is confirmed active, cancel old SL orders
        for old_order in old_sl_orders:
            cancel_single_order(symbol, old_order['orderId'])
        return True
    else:
        notify("BE ERROR", "Failed to place new Break-Even SL! Retaining old Stop Loss for safety.")
        return False

# ===================================================
# FAST INDICATORS (USING PANDAS-TA)
# ===================================================
def get_klines_df(symbol, interval, limit=250):
    try:
        url = f"{BASE_URL}/fapi/v1/klines?symbol={symbol}&interval={interval}&limit={limit}"
        res = requests.get(url, timeout=5)
        if res.status_code == 200:
            data = res.json()
            df = pd.DataFrame(data, columns=[
                'timestamp', 'open', 'high', 'low', 'close', 'volume',
                'close_time', 'quote_vol', 'trades', 'tb_base_vol', 'tb_quote_vol', 'ignore'
            ])
            df['timestamp'] = pd.to_numeric(df['timestamp'])
            df['close'] = pd.to_numeric(df['close'])
            df['high'] = pd.to_numeric(df['high'])
            df['low'] = pd.to_numeric(df['low'])
            df['volume'] = pd.to_numeric(df['volume'])
            return df
    except Exception as e:
        print(f"Candle Fetch Error ({interval}): {e}")
    return None

def calculate_fast_indicators(df_15m, df_1h):
    """Calculates EMA, ATR, ADX, Stoch RSI using pandas-ta for high-performance processing."""
    # 15m Indicators
    df_15m['ema200'] = ta.ema(df_15m['close'], length=200)
    df_15m['atr'] = ta.atr(df_15m['high'], df_15m['low'], df_15m['close'], length=ATR_PERIOD)
    
    adx_df = ta.adx(df_15m['high'], df_15m['low'], df_15m['close'], length=ATR_PERIOD)
    if adx_df is not None and f'ADX_{ATR_PERIOD}' in adx_df.columns:
        df_15m['adx'] = adx_df[f'ADX_{ATR_PERIOD}']
    else:
        df_15m['adx'] = 0.0

    stoch_rsi_df = ta.stochrsi(df_15m['close'], length=14, rsi_length=14, k=3, d=3)
    if stoch_rsi_df is not None and 'STOCHRSIk_14_14_3_3' in stoch_rsi_df.columns:
        df_15m['stoch_rsi'] = stoch_rsi_df['STOCHRSIk_14_14_3_3']
    else:
        df_15m['stoch_rsi'] = 50.0

    # 1h Macro EMA
    df_1h['ema200'] = ta.ema(df_1h['close'], length=200)

    return df_15m, df_1h

# ===================================================
# BOT MAIN LOOP
# ===================================================
def bot_loop():
    global TICK_SIZE, STEP_SIZE
    notify("System Startup", f"Real Account Bot (Optimized v2) initialized for {SYMBOL} ({CANDLE_TIMEFRAME}) | Qty: {QUANTITY}")
    
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

            df_15m = get_klines_df(SYMBOL, CANDLE_TIMEFRAME, limit=250)
            df_1h = get_klines_df(SYMBOL, MACRO_TIMEFRAME, limit=250)

            if df_15m is not None and len(df_15m) >= 200 and df_1h is not None and len(df_1h) >= 200:
                df_15m, df_1h = calculate_fast_indicators(df_15m, df_1h)

                # Fetch closed candle values (index -2) and current live price (index -1)
                closed_candle = df_15m.iloc[-2]
                live_candle = df_15m.iloc[-1]
                macro_closed_candle = df_1h.iloc[-2]

                current_candle_time = int(closed_candle['timestamp'])
                last_closed_price = float(closed_candle['close'])
                current_price = float(live_candle['close'])

                ema_200_15m = float(closed_candle['ema200']) if not pd.isna(closed_candle['ema200']) else 0.0
                ema_200_1h = float(macro_closed_candle['ema200']) if not pd.isna(macro_closed_candle['ema200']) else 0.0
                
                adx_val = float(closed_candle['adx']) if not pd.isna(closed_candle['adx']) else 0.0
                stoch_rsi = float(closed_candle['stoch_rsi']) if not pd.isna(closed_candle['stoch_rsi']) else 50.0
                atr_val = float(closed_candle['atr']) if not pd.isna(closed_candle['atr']) else 2.0

                last_vol = float(closed_candle['volume'])
                vol_ma = float(df_15m['volume'].iloc[-22:-1].mean())

                # Donchian calculation on completed closed candles
                donchian_high = float(df_15m['high'].iloc[-(DONCHIAN_PERIOD + 1): -1].max())
                donchian_low = float(df_15m['low'].iloc[-(DONCHIAN_PERIOD + 1): -1].min())

                # Price Buffer for Donchian Levels
                donchian_high_buffered = donchian_high * (1 + BREAKOUT_BUFFER_PERCENT)
                donchian_low_buffered = donchian_low * (1 - BREAKOUT_BUFFER_PERCENT)

                session_active = is_high_volume_session()
                print(f"Price: ${current_price:,.2f} | ADX: {adx_val:.1f} | Active Session: {session_active} | Active Qty: {actual_pos}", end="\r")

                # SAFE BREAK-EVEN STOP LOSS MANAGEMENT
                if actual_pos != 0 and current_entry_price > 0:
                    is_sl_moved_to_be = check_if_sl_is_at_breakeven(SYMBOL, current_entry_price)
                    
                    if not is_sl_moved_to_be:
                        profit_distance = (current_price - current_entry_price) if actual_pos > 0 else (current_entry_price - current_price)
                        if profit_distance >= (atr_val * BE_ATR_MULTIPLIER):
                            close_side = "SELL" if actual_pos > 0 else "BUY"
                            
                            success = safe_update_break_even_sl(
                                SYMBOL, 
                                close_side, 
                                current_entry_price, 
                                current_price
                            )
                            if success:
                                notify("Risk Update", f"Profit target {BE_ATR_MULTIPLIER}x ATR reached! SL moved to Break-Even safely (Zero Risk).")

                # ENTRY CONDITIONS WITH SESSION & VOLUME FILTER
                if actual_pos == 0 and adx_val >= ADX_THRESHOLD and last_traded_candle_time != current_candle_time:
                    # Optimized Volume Spike Filter (1.3x average volume)
                    if session_active and last_vol > (vol_ma * VOLUME_MULTIPLIER):
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

if __name__ == "__main__":
    t = threading.Thread(target=bot_loop)
    t.daemon = True
    t.start()
    
    t_ping = threading.Thread(target=keep_alive)
    t_ping.daemon = True
    t_ping.start()
    
    run_web_server()
