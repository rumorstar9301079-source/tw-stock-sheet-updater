import os
import json
import time
import datetime as dt

import pandas as pd
import yfinance as yf
import gspread
from google.oauth2.service_account import Credentials


WS_SYMBOLS = "SYMBOLS"
WS_PRICES = "PRICES"

LOOKBACK_CAL_DAYS = 30
SLEEP_SEC = 0.2


# ---------- Google Sheets ----------
def get_client():
    info = json.loads(os.environ["GOOGLE_SERVICE_ACCOUNT_JSON"])
    scopes = [
        "https://www.googleapis.com/auth/spreadsheets",
        "https://www.googleapis.com/auth/drive",
    ]
    creds = Credentials.from_service_account_info(info, scopes=scopes)
    return gspread.authorize(creds)


# ---------- Yahoo Finance ----------
def fetch_latest_yf(symbol: str, market: str):
    """
    回傳 (date_str, close, volume_in_lots)
    volume 一律回傳「張」
    """
    suffix = ".TW" if market == "tse" else ".TWO"
    ticker = f"{symbol}{suffix}"

    def normalize(df):
        if df is None or df.empty:
            return None

        # 壓扁 MultiIndex 欄位
        if isinstance(df.columns, pd.MultiIndex):
            df.columns = [c[0] for c in df.columns]

        if "Close" not in df.columns or "Volume" not in df.columns:
            return None

        last = df.tail(1)
        d = last.index[0].strftime("%Y-%m-%d")

        close = float(last["Close"].values[0])
        vol_shares = float(last["Volume"].values[0])

        # ✅ 一律轉成「張」
        vol_lots = int(round(vol_shares / 1000.0))

        return d, close, vol_lots

    end = dt.datetime.utcnow()
    start = end - dt.timedelta(days=LOOKBACK_CAL_DAYS)

    # 1️⃣ download（快）
    df1 = yf.download(
        ticker,
        start=start.strftime("%Y-%m-%d"),
        end=(end + dt.timedelta(days=1)).strftime("%Y-%m-%d"),
        interval="1d",
        progress=False,
        auto_adjust=False,
        threads=False,
        group_by="column",
    )
    res = normalize(df1)
    if res:
        return res

    # 2️⃣ 備援：Ticker().history（OTC 常比較穩）
    df2 = yf.Ticker(ticker).history(period="1mo", interval="1d", auto_adjust=False)
    res = normalize(df2)
    if res:
        return res

    return None


# ---------- Main ----------
def main():
    sheet_url = os.environ["SHEET_URL"]
    gc = get_client()
    sh = gc.open_by_url(sheet_url)

    # 讀 SYMBOLS
    ws_sym = sh.worksheet(WS_SYMBOLS)
    values = ws_sym.get_all_values()
    if len(values) < 2:
        raise RuntimeError("SYMBOLS 沒有資料")

    header = [h.strip().lower() for h in values[0]]
    df = pd.DataFrame(values[1:], columns=header)

    if "symbol" not in df.columns or "market" not in df.columns:
        raise RuntimeError("SYMBOLS 必須包含 symbol, market 欄位")

    if "active" in df.columns:
        df = df[df["active"].astype(str).str.strip() != "0"]

    df["symbol"] = df["symbol"].astype(str).str.strip()
    df["market"] = df["market"].astype(str).str.lower().str.strip()
    df = df[df["market"].isin(["tse", "otc"])]

    if df.empty:
        raise RuntimeError("SYMBOLS 沒有有效股票")

    # 讀 PRICES（避免重複）
    ws_p = sh.worksheet(WS_PRICES)
    pvals = ws_p.get_all_values()

    exist = set()
    if len(pvals) > 1:
        ph = [h.strip().lower() for h in pvals[0]]
        dfp = pd.DataFrame(pvals[1:], columns=ph)
        if "date" in dfp.columns and "symbol" in dfp.columns:
            exist = set(zip(dfp["date"].astype(str), dfp["symbol"].astype(str)))

    rows = []
    for _, r in df.iterrows():
        sym = r["symbol"]
        market = r["market"]

        res = fetch_latest_yf(sym, market)
        if not res:
            print(f"[WARN] {sym}({market}) no data from Yahoo ({sym}{'.TW' if market=='tse' else '.TWO'})")
            time.sleep(SLEEP_SEC)
            continue

        d, close, vol = res
        if (d, sym) in exist:
            print(f"[SKIP] {sym} {d} exists")
            time.sleep(SLEEP_SEC)
            continue

        rows.append([d, sym, close, vol])
        print(f"[OK] {sym}({market}) {d} close={close} vol(張)={vol}")
        time.sleep(SLEEP_SEC)

    if rows:
        ws_p.append_rows(rows, value_input_option="USER_ENTERED")
        print(f"[DONE] appended {len(rows)} rows")
    else:
        print("[DONE] nothing to append")


if __name__ == "__main__":
    main()
