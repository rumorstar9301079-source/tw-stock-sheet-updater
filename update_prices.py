import os
import json
import time
import datetime as dt

import pandas as pd
import yfinance as yf
import gspread
from google.oauth2.service_account import Credentials


WS_WATCHLIST = "WATCHLIST"
WS_PRICES = "PRICES"

LOOKBACK_CAL_DAYS = 30
SLEEP_SEC = 0.2


# -----------------------------
# Google Sheet client
# -----------------------------
def get_client():
    info = json.loads(os.environ["GOOGLE_SERVICE_ACCOUNT_JSON"])
    scopes = [
        "https://www.googleapis.com/auth/spreadsheets",
        "https://www.googleapis.com/auth/drive",
    ]
    creds = Credentials.from_service_account_info(info, scopes=scopes)
    return gspread.authorize(creds)


# -----------------------------
# Market normalizer
# -----------------------------
def norm_market(v: str) -> str:
    """
    Normalize market to:
      - 'tse'  (上市)
      - 'otc'  (上櫃)
    """
    s = str(v or "").strip().lower()
    if not s:
        return "tse"

    # common aliases
    if s in ("tse", "twse", "tw", "上市", "市"):
        return "tse"
    if s in ("otc", "tpex", "two", "上櫃", "櫃買", "櫃", "興櫃"):
        # 興櫃其實不是 two，但你表內若混用，先當作 otc 走 yahoo .TWO
        return "otc"

    # contains
    if "otc" in s or "tpex" in s or "two" in s or "上櫃" in s or "櫃買" in s:
        return "otc"
    return "tse"


# -----------------------------
# Yahoo fetch
# -----------------------------
def fetch_latest_yf(symbol: str, market: str):
    """
    return (date_str, close, vol_lots)
    vol_lots 一律「張」
    date_str 用台灣時間 YYYY-MM-DD（用交易日 index）
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
        vol_lots = int(round(vol_shares / 1000.0))  # ✅ 張
        return d, close, vol_lots

    end = dt.datetime.utcnow()
    start = end - dt.timedelta(days=LOOKBACK_CAL_DAYS)

    # try download
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

    # fallback history
    df2 = yf.Ticker(ticker).history(period="1mo", interval="1d", auto_adjust=False)
    res = normalize(df2)
    if res:
        return res

    return None


# -----------------------------
# PRICES helpers
# -----------------------------
def build_prices_symbol_index(ws_prices):
    """
    Build symbol -> row_number map for PRICES sheet.
    row_number is 1-based row index in Google Sheets.
    """
    vals = ws_prices.get_all_values()
    if not vals:
        return {}, []

    header = [h.strip().lower() for h in vals[0]]
    rows = vals[1:]

    if "symbol" not in header:
        raise RuntimeError("PRICES 必須有 Symbol 欄位")

    idx_symbol = header.index("symbol")
    sym_index = {}

    for row_no, r in enumerate(rows, start=2):
        if len(r) <= idx_symbol:
            continue
        sym = str(r[idx_symbol]).strip()
        if sym:
            # 同 symbol 以最後一列為準
            sym_index[sym] = row_no

    return sym_index, header


def col_1based(header, name: str):
    name = name.strip().lower()
    if name not in header:
        return None
    return header.index(name) + 1


# -----------------------------
# WATCHLIST reader
# -----------------------------
def read_watchlist_symbols(ws_watch):
    vals = ws_watch.get_all_values()
    if len(vals) < 2:
        raise RuntimeError("WATCHLIST 沒有資料")

    header = [h.strip().lower() for h in vals[0]]
    rows = vals[1:]

    if "symbol" not in header:
        raise RuntimeError("WATCHLIST 必須有 Symbol 欄位")

    idx_symbol = header.index("symbol")
    idx_market = header.index("market") if "market" in header else None

    items = []
    for r in rows:
        if len(r) <= idx_symbol:
            continue
        sym = str(r[idx_symbol]).strip()
        if not sym:
            continue
        mkt = "tse"
        if idx_market is not None and len(r) > idx_market:
            mkt = norm_market(r[idx_market])
        items.append((sym, mkt))

    # 去重：同 symbol 以最後出現為準
    seen = {}
    for sym, mkt in items:
        seen[sym] = mkt
    return [(s, seen[s]) for s in seen.keys()]


# -----------------------------
# main
# -----------------------------
def main():
    sheet_url = os.environ["SHEET_URL"]

    gc = get_client()
    sh = gc.open_by_url(sheet_url)

    ws_watch = sh.worksheet(WS_WATCHLIST)
    ws_prices = sh.worksheet(WS_PRICES)

    # 1) symbols from WATCHLIST
    symbols_to_update = read_watchlist_symbols(ws_watch)
    if not symbols_to_update:
        raise RuntimeError("WATCHLIST 沒有有效 symbol")

    # 2) build PRICES index
    sym_index, header = build_prices_symbol_index(ws_prices)

    c_date = col_1based(header, "date")
    c_symbol = col_1based(header, "symbol")
    c_close = col_1based(header, "close")
    c_vol = col_1based(header, "volume")

    if None in (c_date, c_symbol, c_close, c_vol):
        raise RuntimeError("PRICES 必須有 Date, Symbol, Close, Volume 欄位（大小寫不拘）")

    updated = 0
    appended = 0

    # ✅ collect batch updates
    cell_updates = []
    append_rows = []

    for sym, market in symbols_to_update:
        res = fetch_latest_yf(sym, market)
        if not res:
            print(f"[WARN] {sym}({market}) no data from Yahoo ({sym}{'.TW' if market=='tse' else '.TWO'})")
            time.sleep(SLEEP_SEC)
            continue

        d, close, vol = res

        if sym in sym_index:
            row_no = sym_index[sym]
            # 批次更新：同 symbol 更新同一列
            cell_updates.append(gspread.Cell(row_no, c_date, d))
            cell_updates.append(gspread.Cell(row_no, c_close, close))
            cell_updates.append(gspread.Cell(row_no, c_vol, vol))
            updated += 1
            print(f"[UPD] {sym} row={row_no} date={d} close={close} vol(張)={vol}")
        else:
            append_rows.append([d, sym, close, vol])
            appended += 1
            print(f"[APP] {sym} date={d} close={close} vol(張)={vol}")

        time.sleep(SLEEP_SEC)

    # ✅ batch write (faster + stable)
    if cell_updates:
        ws_prices.update_cells(cell_updates, value_input_option="USER_ENTERED")

    for r in append_rows:
        ws_prices.append_row(r, value_input_option="USER_ENTERED")

    print(f"[DONE] updated={updated}, appended={appended}")


if __name__ == "__main__":
    main()


if __name__ == "__main__":
    main()

