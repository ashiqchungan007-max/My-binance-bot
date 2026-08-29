import hashlib
import hmac
import math
import os
import time
import urllib.parse
from datetime import datetime, timezone, timedelta
import requests
import pandas as pd
import pandas_ta as ta

# ===================================================
# API KEYS & CONFIGURATION
# ===================================================
API_KEY = os.environ.get("BINANCE_API_KEY", "")
API_SECRET = os.environ.get("BINANCE_SECRET_KEY", "")

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")

USE_TESTNET = False  
SYMBOL = "XAUUSDT"
BASE_URL = "https://fapi.binance.com"

LEVERAGE = 20  
QUANTITY = 0.003  

CANDLE_TIMEFRAME = "15m"
MACRO_TIMEFRAME_1H = "1h"
MACRO_TIMEFRAME_4H = "4h"
POLL_INTERVAL = 15  

# RISK & EMA STRATEGY SETTINGS
EMA_FAST = 9
EMA_SLOW = 21
EMA_TREND = 200

ATR_PERIOD = 14
ATR_SL_MULTIPLIER = 1.5
ATR_TP_MULTIPLIER = 3.0
BE_ATR_MULTIPLIER = 2.0  

# ANTI-RANGE FILTERS
ADX_THRESHOLD = 22  
EMA_SPREAD_MIN_PERCENT = 0.0012  

BOT_PAUSED = False
LAST_UPDATE_ID = 0
IS_BE_ACTIVATED = False

# ===================================================
# TELEGRAM FUNCTIONS & COMMAND CONTROL
# ===================================================
def notify(title, message):
    current_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    formatted_msg = f"[{current_time}]\n🔔 *{title}*\n{message}"
    
    print(f"\n{formatted_msg}")
    
    if TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID:
        try:
            url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
            payload = {
                "chat_id": TELEGRAM_CHAT_ID,
                "text": formatted_msg,
                "parse_mode": "Markdown"
            }
            requests.post(url, json=payload, timeout=3)
        except Exception as e:
            print(f"Telegram Send Error: {e}")

def check_telegram_commands():
    global BOT_PAUSED, LAST_UPDATE_ID
    if not TELEGRAM_BOT_TOKEN:
        return

    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/getUpdates"
        params = {"offset": LAST_UPDATE_ID + 1, "timeout": 1}
        res = requests.get(url, params=params, timeout=2).json()

        if res.get("ok") and res.get("result"):
            for update in res["result"]:
                LAST_UPDATE_ID = update["update_id"]
                message = update.get("message", {})
                text = message.get("text", "").strip().lower()
                chat_id = str(message.get("chat", {}).get("id", ""))

                if chat_id == str(TELEGRAM_CHAT_ID):
                    if text == "/pause":
                        BOT_PAUSED = True
                        notify("PAUSED ⏸️", "Bot trading has been *PAUSED*.")
                    elif text in ["/resume", "/start"]:
                        BOT_PAUSED = False
                        notify("RESUMED ▶️", "Bot trading has been *RESUMED*.")
                    elif text == "/status":
                        status_str = "PAUSED ⏸️" if BOT_PAUSED else "RUNNING 🟢"
                        notify("STATUS 📊", f"Current Bot Status: *{status_str}*")
    except Exception:
        pass  

# ===================================================
# BINANCE UTILITIES & DYNAMIC SESSION FILTER
# ===================================================
def is_dst(dt):
    year = dt.year
    dst_start = datetime(year, 3, 31) - timedelta(days=(datetime(year, 3, 31).weekday() + 1) % 7)
    dst_end = datetime(year, 10, 31) - timedelta(days=(datetime(year, 10, 31).weekday() + 1) % 7)
    return dst_start.replace(tzinfo=timezone.utc) <= dt < dst_end.replace(tzinfo=timezone.utc)

def is_high_volume_session():
    now_utc = datetime.now(timezone.utc)
    if now_utc.weekday() in [5, 6]:
        return False
    
    if is_dst(now_utc):
        session_start, session_end = 6, 20
    else:
        session_start, session_end = 7, 21

    return session_start <= now_utc.hour < session_end

def send_signed_request(http_method, url_path, payload=None):
    if not API_KEY or not API_SECRET:
        print("\n❌ ERROR: BINANCE_API_KEY or BINANCE_SECRET_KEY is missing!")
        return None

    if payload is None: payload = {}
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
            res = requests.post(full_url, headers=headers, timeout=5)
        elif http_method == "DELETE":
            res = requests.delete(full_url, headers=headers, timeout=5)
        else:
            res = requests.get(full_url, headers=headers, timeout=5)

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

def get_position_info(symbol, retries=3):
    path = "/fapi/v2/positionRisk"
    for attempt in range(retries):
        res = send_signed_request("GET", path, {"symbol": symbol})
        if res and isinstance(res, list):
            for pos in res:
                if pos['symbol'] == symbol:
                    return float(pos['positionAmt']), float(pos['entryPrice'])
        time.sleep(0.5)
    return None, 0.0

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
    notify("EMERGENCY CLOSE", f"Closing {current_position_side} position immediately!")
    
    for i in range(5):
        res = send_signed_request("POST", path, payload)
        if res and 'orderId' in res:
            return True
        time.sleep(1)
    
    notify("CRITICAL ALERT 🚨", "EMERGENCY MARKET CLOSE FAILED MULTIPLE TIMES!")
    return False

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

    if not (sl_res and 'orderId' in sl_res and tp_res and 'orderId' in tp_res):
        pos_side = "LONG" if exit_side == "SELL" else "SHORT"
        emergency_close_position(symbol, pos_side, qty)
        return False
    return True

def update_break_even_sl(symbol, exit_side, entry_price, tp_price, qty, current_price):
    min_dist = TICK_SIZE * 10
    if exit_side == "SELL" and (current_price - entry_price) < min_dist:
        return False
    elif exit_side == "BUY" and (entry_price - current_price) < min_dist:
        return False

    return place_stop_loss_and_take_profit(symbol, exit_side, entry_price, tp_price, qty)

# ===================================================
# KLINES & PANDAS-TA INDICATORS
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

def get_df_with_indicators(highs, lows, closes, volumes):
    df = pd.DataFrame({
        'high': highs,
        'low': lows,
        'close': closes,
        'volume': volumes
    })

    df['ema_9'] = ta.ema(df['close'], length=EMA_FAST)
    df['ema_21'] = ta.ema(df['close'], length=EMA_SLOW)
    df['ema_200'] = ta.ema(df['close'], length=EMA_TREND)
    
    df['atr'] = ta.atr(df['high'], df['low'], df['close'], length=ATR_PERIOD)
    
    adx_df = ta.adx(df['high'], df['low'], df['close'], length=ATR_PERIOD)
    df['adx'] = adx_df[f'ADX_{ATR_PERIOD}']
    
    stoch_rsi_df = ta.stochrsi(df['close'], length=14, rsi_length=14, k=3, d=3)
    df['stoch_rsi'] = stoch_rsi_df['STOCHRSIk_14_14_3_3']
    
    df['vol_ma'] = df['volume'].rolling(window=20).mean()

    return df

# ===================================================
# BOT MAIN LOOP
# ===================================================
def bot_loop():
    global TICK_SIZE, STEP_SIZE, IS_BE_ACTIVATED
    notify("SYSTEM STARTUP", f"Bot Initialized for {SYMBOL} on Oracle Cloud VPS (Pandas Powered).")
    
    ensure_one_way_mode()
    set_leverage(SYMBOL, LEVERAGE)
    TICK_SIZE, STEP_SIZE = get_symbol_filters(SYMBOL)

    was_in_position = False
    last_traded_candle_time = None

    while True:
        try:
            check_telegram_commands()

            if BOT_PAUSED:
                print("Bot status: PAUSED (Waiting for /resume command...)", end="\r")
                time.sleep(POLL_INTERVAL)
                continue

            actual_pos, current_entry_price = get_position_info(SYMBOL)

            if actual_pos is None:
                time.sleep(POLL_INTERVAL)
                continue

            if actual_pos == 0 and was_in_position:
                cancel_all_orders(SYMBOL)
                notify("POSITION CLOSED", "Position closed via TP/SL. Resetting Risk States.")
                was_in_position = False
                IS_BE_ACTIVATED = False

            if actual_pos != 0:
                was_in_position = True

            timestamps, closes, highs, lows, volumes = get_klines_data(SYMBOL, CANDLE_TIMEFRAME, limit=250)
            _, macro_closes_1h, _, _, _ = get_klines_data(SYMBOL, MACRO_TIMEFRAME_1H, limit=250)
            _, macro_closes_4h, _, _, _ = get_klines_data(SYMBOL, MACRO_TIMEFRAME_4H, limit=250)

            if (closes is not None and macro_closes_1h is not None and macro_closes_4h is not None and 
                len(closes) >= 200 and len(macro_closes_1h) >= 200 and len(macro_closes_4h) >= 200):
                
                current_candle_time = timestamps[-2]
                last_closed_price = closes[-2]
                current_price = closes[-1]

                # Pandas Indicators calculation
                df_15m = get_df_with_indicators(highs[:-1], lows[:-1], closes[:-1], volumes[:-1])
                
                ema_200_1h_last = ta.ema(pd.Series(macro_closes_1h[:-1]), length=EMA_TREND).iloc[-1]
                ema_200_4h_last = ta.ema(pd.Series(macro_closes_4h[:-1]), length=EMA_TREND).iloc[-1]

                last_row = df_15m.iloc[-1]

                ema_9_last = last_row['ema_9']
                ema_21_last = last_row['ema_21']
                ema_200_15m_last = last_row['ema_200']
                
                adx_val = last_row['adx']
                atr_val = last_row['atr']
                stoch_rsi = last_row['stoch_rsi']
                
                last_vol = last_row['volume']
                vol_ma = last_row['vol_ma']

                low_last = lows[-2]
                high_last = highs[-2]

                session_active = is_high_volume_session()

                ema_spread = abs(ema_9_last - ema_21_last) / last_closed_price
                is_market_trending = ema_spread >= EMA_SPREAD_MIN_PERCENT

                print(f"Price: ${current_price:,.2f} | ADX: {adx_val:.1f} | EMA Spread: {ema_spread*100:.3f}% | Position: {actual_pos}", end="\r")

                # BREAK-EVEN STOP LOSS MANAGEMENT
                if actual_pos != 0 and current_entry_price > 0 and not IS_BE_ACTIVATED:
                    profit_distance = (current_price - current_entry_price) if actual_pos > 0 else (current_entry_price - current_price)
                    if profit_distance >= (atr_val * BE_ATR_MULTIPLIER):
                        close_side = "SELL" if actual_pos > 0 else "BUY"
                        new_tp = current_entry_price + (atr_val * ATR_TP_MULTIPLIER) if actual_pos > 0 else current_entry_price - (atr_val * ATR_TP_MULTIPLIER)
                        
                        success = update_break_even_sl(
                            SYMBOL, close_side, current_entry_price, new_tp, abs(actual_pos), current_price
                        )
                        if success:
                            IS_BE_ACTIVATED = True
                            notify("RISK UPDATE", f"Profit target reached! SL moved to Break-Even (${current_entry_price:,.2f}).")

                # ENTRY CONDITIONS
                if (actual_pos == 0 and adx_val >= ADX_THRESHOLD and 
                    is_market_trending and last_traded_candle_time != current_candle_time):
                    
                    if session_active and last_vol > vol_ma:
                        sl_distance = atr_val * ATR_SL_MULTIPLIER
                        tp_distance = atr_val * ATR_TP_MULTIPLIER

                        # 1. LONG ENTRY LOGIC
                        macro_uptrend = (last_closed_price > ema_200_4h_last) and (last_closed_price > ema_200_1h_last) and (last_closed_price > ema_200_15m_last)
                        ema_aligned_long = ema_9_last > ema_21_last
                        pulled_back_long = low_last <= max(ema_9_last, ema_21_last) and last_closed_price > ema_21_last

                        if macro_uptrend and ema_aligned_long and pulled_back_long and stoch_rsi < 70:
                            order, avg_price = place_futures_order(SYMBOL, "BUY", QUANTITY)

                            if order and 'orderId' in order:
                                last_traded_candle_time = current_candle_time
                                _, real_entry = get_position_info(SYMBOL, retries=3)
                                entry = real_entry if real_entry > 0 else (avg_price if avg_price > 0 else current_price)
                                
                                sl_price = entry - sl_distance
                                tp_price = entry + tp_distance

                                place_stop_loss_and_take_profit(SYMBOL, "SELL", sl_price, tp_price, QUANTITY)
                                notify("EMA PULLBACK LONG", f"Entry: ${entry:,.2f}\nSL: ${sl_price:,.2f}\nTP: ${tp_price:,.2f}")

                        # 2. SHORT ENTRY LOGIC
                        macro_downtrend = (last_closed_price < ema_200_4h_last) and (last_closed_price < ema_200_1h_last) and (last_closed_price < ema_200_15m_last)
                        ema_aligned_short = ema_9_last < ema_21_last
                        pulled_back_short = high_last >= min(ema_9_last, ema_21_last) and last_closed_price < ema_21_last

                        if macro_downtrend and ema_aligned_short and pulled_back_short and stoch_rsi > 30:
                            order, avg_price = place_futures_order(SYMBOL, "SELL", QUANTITY)

                            if order and 'orderId' in order:
                                last_traded_candle_time = current_candle_time
                                _, real_entry = get_position_info(SYMBOL, retries=3)
                                entry = real_entry if real_entry > 0 else (avg_price if avg_price > 0 else current_price)

                                sl_price = entry + sl_distance
                                tp_price = entry - tp_distance

                                place_stop_loss_and_take_profit(SYMBOL, "BUY", sl_price, tp_price, QUANTITY)
                                notify("EMA PULLBACK SHORT", f"Entry: ${entry:,.2f}\nSL: ${sl_price:,.2f}\nTP: ${tp_price:,.2f}")

        except Exception as e:
            print(f"\nLoop Error: {e}")

        time.sleep(POLL_INTERVAL)

if __name__ == "__main__":
    bot_loop()
