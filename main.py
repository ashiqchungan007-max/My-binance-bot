import hashlib
import hmac
import math
import os
import time
import urllib.parse
from datetime import datetime, timezone, timedelta
import requests

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

TIME_OFFSET = 0

# ===================================================
# TIME SYNC WITH BINANCE SERVER
# ===================================================
def sync_time_with_binance():
    global TIME_OFFSET
    try:
        url = f"{BASE_URL}/fapi/v1/time"
        res = requests.get(url, timeout=5)
        if res.status_code == 200:
            server_time = res.json().get("serverTime", 0)
            local_time = int(time.time() * 1000)
            TIME_OFFSET = server_time - local_time
            print(f"🕒 Time synchronized with Binance. Offset: {TIME_OFFSET} ms")
    except Exception as e:
        print(f"⚠️ Time Sync Error: {e}")

def get_corrected_timestamp():
    return int(time.time() * 1000) + TIME_OFFSET

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
        timestamp = get_corrected_timestamp()
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
            # If timestamp error occurs, re-sync time once
            if "timestamp" in res.text.lower() or "-1021" in res.text:
                sync_time_with_binance()
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

# ===================================================
# ORDERS & EMERGENCY SYSTEM
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

def calculate_ema_series(prices, period):
    if len(prices) < period: return [0.0] * len(prices)
    k = 2 / (period + 1)
    ema_list = [sum(prices[:period]) / period]
    for price in prices[period:]:
        ema_list.append((price * k) + (ema_list[-1] * (1 - k)))
    return [0.0] * (period - 1) + ema_list

def _wilders_rma(values, period):
    if len(values) < period: return [0.0] * len(values)
    rma = [sum(values[:period]) / period]
    for val in values[period:]:
        rma.append((rma[-1] * (period - 1) + val) / period)
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
        pdm_list.append(up_move if (up_move > down_move and up_move > 0) else 0.0)
        mdm_list.append(down_move if (down_move > up_move and down_move > 0) else 0.0)

    tr_rma = _wilders_rma(tr_list, period)
    pdm_rma = _wilders_rma(pdm_list, period)
    mdm_rma = _wilders_rma(mdm_list, period)

    dx_list = []
    for i in range(len(tr_rma)):
        if tr_rma[i] == 0: continue
        pdi = (pdm_rma[i] / tr_rma[i]) * 100
        mdi = (mdm_rma[i] / tr_rma[i]) * 100
        di_sum = pdi + mdi
        dx_list.append((abs(pdi - mdi) / di_sum * 100) if di_sum != 0 else 0.0)

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
    global TICK_SIZE, STEP_SIZE, IS_BE_ACTIVATED
    
    # Sync time on startup
    sync_time_with_binance()
    
    notify("SYSTEM STARTUP", f"Bot Initialized for {SYMBOL} on Oracle Cloud VPS.")
    
    ensure_one_way_mode()
    set_leverage(SYMBOL, LEVERAGE)
    TICK_SIZE, STEP_SIZE = get_symbol_filters(SYMBOL)

    was_in_position = False
    last_traded_candle_time = None
    loop_counter = 0

    while True:
        try:
            # Re-sync time every ~1 hour (240 loops * 15s) to prevent drift
            loop_counter += 1
            if loop_counter >= 240:
                sync_time_with_binance()
                loop_counter = 0

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

                ema_9_series = calculate_ema_series(closes[:-1], EMA_FAST)
                ema_21_series = calculate_ema_series(closes[:-1], EMA_SLOW)
                ema_200_15m_series = calculate_ema_series(closes[:-1], EMA_TREND)
                
                ema_200_1h_series = calculate_ema_series(macro_closes_1h[:-1], EMA_TREND)
                ema_200_4h_series = calculate_ema_series(macro_closes_4h[:-1], EMA_TREND)

                ema_9_last = ema_9_series[-1]
                ema_21_last = ema_21_series[-1]
                ema_200_15m_last = ema_200_15m_series[-1]
                ema_200_1h_last = ema_200_1h_series[-1]
                ema_200_4h_last = ema_200_4h_series[-1]

                low_last = lows[-2]
                high_last = highs[-2]

                adx_val = calculate_adx(highs[:-1], lows[:-1], closes[:-1], ATR_PERIOD)
                stoch_rsi = calculate_stoch_rsi(closes[:-1])
                atr_val = calculate_atr(highs[:-1], lows[:-1], closes[:-1], ATR_PERIOD)

                last_vol = volumes[-2]
                vol_ma = sum(volumes[-21:-1]) / 20

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
