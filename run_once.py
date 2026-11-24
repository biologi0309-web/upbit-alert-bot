from datetime import datetime, timedelta, timezone
import json
import os
import time
import requests

# ==========================================
# [나중에 연필 아이콘 눌러서 수정할 설정 구역]
TOPIC = "coin-alert-j0069006"
MIN_AMOUNT = 1_000_000_000  # 최소 24시간 거래대금 (10억 원)
FAST_MA = 5  # 단기 지수이동평균선 (EMA 5)
SLOW_MA = 25  # 장기 지수이동평균선 (EMA 25)
VOL_MULTIPLIER = 1.5  # 직전 평균 거래량 대비 최소 배수 (1.5배 이상)
VOL_AVG_PERIOD = 5  # 거래량 평균을 낼 직전 캔들 개수

TREND_MA = 200  # 일봉 장기 추세 필터 (200일선)
ALLOW_NEW_COINS = False  # 상장 200일 미만 코인 제외 여부

RSI_PERIOD = 14  # RSI 기간
RSI_MAX = 70.0  # RSI 상한선 (이 값 이상이면 과매수로 제외)
CHECK_5M = False  # GitHub는 5분 간격이므로 25분봉·일봉 위주 추천 (5분봉도 받으려면 True)
# ==========================================

HISTORY_FILE = "sent_history.json"
KST = timezone(timedelta(hours=9))


def load_history():
  if os.path.exists(HISTORY_FILE):
    try:
      with open(HISTORY_FILE, "r", encoding="utf-8") as f:
        data = json.load(f)
        # 최근 300개 기록만 유지해 파일이 무한정 커지지 않게 관리
        return data[-300:]
    except Exception:
      return []
  return []


def save_history(history_list):
  with open(HISTORY_FILE, "w", encoding="utf-8") as f:
    json.dump(history_list[-300:], f, ensure_ascii=False, indent=2)


def send_push(title, msg, click_url=None):
  headers = {
      "Title": title.encode("utf-8"),
      "Priority": "high",
  }
  if click_url:
    headers["Click"] = click_url

  requests.post(
      f"https://ntfy.sh/{TOPIC}",
      data=msg.encode("utf-8"),
      headers=headers,
      timeout=15,
  )


def get_upbit_chart_url(market):
  return f"https://upbit.com/exchange?code=CRIX.UPBIT.{market}"


def get_candles(market, count=200, candle_type="minutes/5"):
  url = f"https://api.upbit.com/v1/candles/{candle_type}"
  res = requests.get(url, params={"market": market, "count": count}, timeout=15)
  data = res.json()
  if not data or isinstance(data, dict):
    return [], [], []
  data.reverse()

  times = [x["candle_date_time_kst"] for x in data]
  closes = [float(x["trade_price"]) for x in data]
  volumes = [float(x["candle_acc_trade_volume"]) for x in data]
  return times, closes, volumes


def make_25m_candles(min5_times, min5_closes, min5_volumes):
  times_25m, closes_25m, volumes_25m = [], [], []
  for i in range(0, len(min5_closes) - 4, 5):
    times_25m.append(min5_times[i])
    closes_25m.append(min5_closes[i + 4])
    volumes_25m.append(sum(min5_volumes[i : i + 5]))
  return times_25m, closes_25m, volumes_25m


def calc_ema_series(closes, period):
  if len(closes) < period:
    return []
  k = 2 / (period + 1)
  ema = sum(closes[:period]) / period
  ema_list = [ema]
  for price in closes[period:]:
    ema = (price - ema) * k + ema
    ema_list.append(ema)
  return ema_list


def calc_latest_rsi(closes, period=RSI_PERIOD):
  if len(closes) < period + 1:
    return None
  deltas = [closes[i] - closes[i - 1] for i in range(1, len(closes))]
  gains = [max(d, 0.0) for d in deltas]
  losses = [max(-d, 0.0) for d in deltas]

  avg_gain = sum(gains[:period]) / period
  avg_loss = sum(losses[:period]) / period

  for i in range(period, len(deltas)):
    avg_gain = (avg_gain * (period - 1) + gains[i]) / period
    avg_loss = (avg_loss * (period - 1) + losses[i]) / period

  if avg_loss == 0:
    return 100.0
  rs = avg_gain / avg_loss
  return 100.0 - (100.0 / (1.0 + rs))


def is_uptrend_above_ma(day_closes, trend_period=TREND_MA):
  if len(day_closes) < trend_period:
    return ALLOW_NEW_COINS
  trend_emas = calc_ema_series(day_closes, trend_period)
  if not trend_emas:
    return ALLOW_NEW_COINS
  return day_closes[-1] > trend_emas[-1]


def check_signal(closes, volumes):
  min_len = max(SLOW_MA + 2, VOL_AVG_PERIOD + 2, RSI_PERIOD + 2)
  if len(closes) < min_len:
    return False, 0.0, 0.0

  fast_emas = calc_ema_series(closes, FAST_MA)
  slow_emas = calc_ema_series(closes, SLOW_MA)
  if len(fast_emas) < 2 or len(slow_emas) < 2:
    return False, 0.0, 0.0

  if not (
      (fast_emas[-2] <= slow_emas[-2]) and (fast_emas[-1] > slow_emas[-1])
  ):
    return False, 0.0, 0.0

  recent_vol = max(volumes[-1], volumes[-2])
  prev_avg_vol = sum(volumes[-(VOL_AVG_PERIOD + 2) : -2]) / VOL_AVG_PERIOD
  if prev_avg_vol <= 0:
    return False, 0.0, 0.0

  vol_ratio = recent_vol / prev_avg_vol
  if vol_ratio < VOL_MULTIPLIER:
    return False, vol_ratio, 0.0

  rsi_val = calc_latest_rsi(closes, RSI_PERIOD)
  if rsi_val is None or rsi_val >= RSI_MAX:
    return False, vol_ratio, (rsi_val or 0.0)

  return True, vol_ratio, rsi_val


def format_billion(amount):
  return f"{amount / 100_000_000:,.1f}억"


def format_price(price):
  if price >= 100:
    return f"{price:,.0f}"
  elif price >= 1:
    return f"{price:,.2f}"
  else:
    return f"{price:,.4f}"


def get_target_coins():
  markets = requests.get(
      "https://api.upbit.com/v1/market/all", timeout=15
  ).json()
  name_map = {
      m["market"]: m.get("korean_name", m["market"])
      for m in markets
      if m["market"].startswith("KRW-")
  }
  krw_codes = list(name_map.keys())

  targets = []
  for i in range(0, len(krw_codes), 100):
    chunk = krw_codes[i : i + 100]
    tickers = requests.get(
        "https://api.upbit.com/v1/ticker",
        params={"markets": ",".join(chunk)},
        timeout=15,
    ).json()
    for t in tickers:
      trade_24h = float(t.get("acc_trade_price_24h", 0))
      if trade_24h >= MIN_AMOUNT:
        code = t["market"]
        targets.append((code, name_map.get(code, code), trade_24h))
    time.sleep(0.1)

  targets.sort(key=lambda x: x[2], reverse=True)
  return targets


def main():
  now_kst = datetime.now(KST).strftime("%Y-%m-%d %H:%M:%S")
  print(f"[{now_kst} KST] 업비트 1회 스캔 시작...")

  history = load_history()
  sent_set = set(history)
  new_sent = False

  coins = get_target_coins()
  print(f"거래대금 {format_billion(MIN_AMOUNT)}원 이상 코인 {len(coins)}개 확인")

  for coin, kor_name, trade_24h in coins:
    td, cd, vd = get_candles(coin, count=200, candle_type="days")
    time.sleep(0.12)

    if not cd or not is_uptrend_above_ma(cd, TREND_MA):
      continue

    trade_str = format_billion(trade_24h)
    display_name = f"{kor_name}({coin})"
    chart_url = get_upbit_chart_url(coin)

    t5, c5, v5 = get_candles(coin, count=200, candle_type="minutes/5")
    t25, c25, v25 = make_25m_candles(t5, c5, v5)
    time.sleep(0.12)

    if CHECK_5M and c5:
      ok_5m, ratio_5m, rsi_5m = check_signal(c5, v5)
      if ok_5m:
        key = f"{coin}-5m-{t5[-1]}"
        if key not in sent_set:
          sent_set.add(key)
          history.append(key)
          new_sent = True
          send_push(
              f"5분봉 EMA 골든크로스 | {display_name}",
              (
                  f"현재가: {format_price(c5[-1])}원 | 거래량 {ratio_5m:.1f}배"
                  f" | RSI {rsi_5m:.1f} | 24h {trade_str}원"
              ),
              click_url=chart_url,
          )
          print(f"알림 발송: 5분봉 - {display_name}")

    if c25:
      ok_25m, ratio_25m, rsi_25m = check_signal(c25, v25)
      if ok_25m:
        key = f"{coin}-25m-{t25[-1]}"
        if key not in sent_set:
          sent_set.add(key)
          history.append(key)
          new_sent = True
          send_push(
              f"25분봉 EMA 골든크로스 | {display_name}",
              (
                  f"현재가: {format_price(c25[-1])}원 | 거래량"
                  f" {ratio_25m:.1f}배 | RSI {rsi_25m:.1f} | 24h"
                  f" {trade_str}원"
              ),
              click_url=chart_url,
          )
          print(f"알림 발송: 25분봉 - {display_name}")

    ok_1d, ratio_1d, rsi_1d = check_signal(cd, vd)
    if ok_1d:
      key = f"{coin}-1d-{td[-1]}"
      if key not in sent_set:
        sent_set.add(key)
        history.append(key)
        new_sent = True
        send_push(
            f"일봉 EMA 골든크로스 | {display_name}",
            (
                f"현재가: {format_price(cd[-1])}원 | 거래량 {ratio_1d:.1f}배 |"
                f" RSI {rsi_1d:.1f} | 24h {trade_str}원"
            ),
            click_url=chart_url,
        )
        print(f"알림 발송: 일봉 - {display_name}")

  if new_sent:
    save_history(history)

  print("스캔 완료!")


if __name__ == "__main__":
  main()
