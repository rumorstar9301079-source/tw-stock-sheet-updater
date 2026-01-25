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
    return (date_str, close, vol_lots)
    vol_lots 一律「張」
    """
    suffix = ".TW" if market == "tse" else ".TWO"
    ticker = f"{symbol}{suffix}"

    def normalize(df):
        if df is None or df.empty:
            return None

        if isinstance(df.columns, pd.MultiIndex):
            df.columns = [c[0] for c in df.columns]

        if "Close" not in df.columns or "Volume" not in df.columns:
            return None

        last = df.tail(1)
        d = last.index[0].strftime("%Y-%m-%d")
        close = float(last["Close"].values[0])
        vol_shares = float(last["Volume"].values[0])

        # ✅ 一律轉張
        vol_lots = int(round(vol_shares / 1000.0))
        return d, close, vol_lots

    end = dt.datetime.utcnow()
    start = end - dt.timedelta(days=LOOKBACK_CAL_DAYS)

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

    df2 = yf.Ticker(ticker).history(period="1mo", interval="1d", auto_adjust=False)
    res = normalize(df2)
    if res:
        return res

    return None


# ---------- PRICES: build index ----------
def build_prices_index(ws):
    """
    建立 (date, symbol) -> row_number 的索引
    row_number 是 Google Sheet 的實際列號（從 1 開始，含 header）
    """
    vals = ws.get_all_values()
    if not vals:
        return {}, []

    header = [h.strip().lower() for h in vals[0]]
    rows = vals[1:]

    idx_date = header.index("date") if "date" in header else None
    idx_symbol = header.index("symbol") if "symbol" in header else None

    if idx_date is None or idx_symbol is None:
        raise RuntimeError("PRICES 需要欄位：Date, Symbol（大小寫不拘）")

    index = {}
    # Google Sheet：第1列是 header，所以 data 從第2列開始
    for i, r in enumerate(rows, start=2):
        if len(r) <= max(idx_date, idx_symbol):
            continue
        d = str(r[idx_date]).strip()
        s = str(r[idx_symbol]).strip()
        if d and s:
            index[(d, s)] = i

    return index, header


def main():
    sheet_url = os.environ["SHEET_URL"]
    gc = get_client()
    sh = gc.open_by_url(sheet_url)

    ws_sym = sh.worksheet(WS_SYMBOLS)
    sym_vals = ws_sym.get_all_values()
    if len(sym_vals) < 2:
        raise RuntimeError("SYMBOLS 沒有資料")

    sym_header = [h.strip().lower() for h in sym_vals[0]]
    df = pd.DataFrame(sym_vals[1:], columns=sym_header)

    if "symbol" not in df.columns or "market" not in df.columns:
        raise RuntimeError("SYMBOLS 必須包含 symbol, market 欄位（market=tse/otc）")

    if "active" in df.columns:
        df = df[df["active"].astype(str).str.strip() != "0"]

    df["symbol"] = df["symbol"].astype(str).str.strip()
    df["market"] = df["market"].astype(str).str.lower().str.strip()
    df = df[df["market"].isin(["tse", "otc"])]

    if df.empty:
        raise RuntimeError("SYMBOLS 沒有有效股票（market 必須是 tse 或 otc）")

    ws_p = sh.worksheet(WS_PRICES)
    prices_index, prices_header = build_prices_index(ws_p)

    # 找出 Close/Volume 欄位在哪
    col_close = prices_header.index("close") + 1 if "close" in prices_header else None
    col_vol = prices_header.index("volume") + 1 if "volume" in prices_header else None

    if col_close is None or col_vol is None:
        raise RuntimeError("PRICES 需要欄位：Close, Volume（大小寫不拘）")

    appended = 0
    updated = 0

    for _, r in df.iterrows():
        sym = r["symbol"]
        market = r["market"]

        res = fetch_latest_yf(sym, market)
        if not res:
            print(f"[WARN] {sym}({market}) no data from Yahoo ({sym}{'.TW' if market=='tse' else '.TWO'})")
            time.sleep(SLEEP_SEC)
            continue

        d, close, vol = res
        key = (d, sym)

        if key in prices_index:
            row_no = prices_index[key]
            # 覆蓋 Close / Volume
            ws_p.update_cell(row_no, col_close, close)
            ws_p.update_cell(row_no, col_vol, vol)
            updated += 1
            print(f"[UPD] {sym} {d} close={close} vol(張)={vol} (row={row_no})")
        else:
            ws_p.append_row([d, sym, close, vol], value_input_option="USER_ENTERED")
            appended += 1
            # 新增後更新索引（避免同一輪重複）
            prices_index[key] = len(ws_p.get_all_values())
            print(f"[APP] {sym} {d} close={close} vol(張)={vol}")

        time.sleep(SLEEP_SEC)

    print(f"[DONE] updated={updated}, appended={appended}")


if __name__ == "__main__":
    main()

