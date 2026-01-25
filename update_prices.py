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
      - 'tse'  (上市) -> suffix .TW
      - 'otc'  (上櫃) -> suffix .TWO
    Accepts many aliases.
    """
    s = str(v or "").strip().lower()
    if not s:
        return "tse"

    if s in ("tse", "twse", "tw", "上市", "市", "listed"):
        return "tse"
    if s in ("otc", "tpex", "two", "上櫃", "櫃買", "櫃", "unlisted"):
        return "otc"

    if any(k in s for k in ("otc", "tpex", "two", "上櫃", "櫃買")):
        return "otc"
    return "tse"


# -----------------------------
# Yahoo fetch (TRY BOTH .TW/.TWO)
# -----------------------------
def fetch_latest_yf_try_both(symbol: str, market_hint: str):
    """
    return (date_str, close, vol_lots, market_used)
    - vol_lots 一律「張」
    - date_str 用交易日 YYYY-MM-DD
    - market_used: 'tse' or 'otc' (based on suffix that worked)
    """
    symbol = str(symbol).strip()

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

    def try_ticker(ticker: str):
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

    hint = norm_market(market_hint)
    order = [hint, ("otc" if hint == "tse" else "tse")]

    for mkt in order:
        suffix = ".TW" if mkt == "tse" else ".TWO"
        ticker = f"{symbol}{suffix}"
        res = try_ticker(ticker)
        if res:
            d, close, vol = res
            return d, close, vol, mkt

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
# WATCHLIST reader + optional market writeback
# -----------------------------
def read_watchlist(ws_watch):
    """
    return: (items, header, idx_symbol, idx_market)
    items: list of (row_no_1based, symbol, market_hint)
    """
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
    for i, r in enumerate(rows, start=2):  # sheet row number
        if len(r) <= idx_symbol:
            continue
        sym = str(r[idx_symbol]).strip()
        if not sym:
            continue
        mkt = "tse"
        if idx_market is not None and len(r) > idx_market:
            mkt = norm_market(r[idx_market])
        items.append((i, sym, mkt))

    # 去重：同 symbol 以最後出現為準（避免重複更新）
    last = {}
    for row_no, sym, mkt in items:
        last[sym] = (row_no, mkt)

    out = [(last[sym][0], sym, last[sym][1]) for sym in last.keys()]
    return out, header, idx_symbol, idx_market


def batch_writeback_market(ws_watch, idx_market_0based, updates):
    """
    updates: list[(row_no_1based, market_used)]
    Only writes when market column exists and differs.
    """
    if idx_market_0based is None:
        return

    col_market_1based = idx_market_0based + 1
    cells = []
    for row_no, mkt in updates:
        cells.append(gspread.Cell(row_no, col_market_1based, mkt))
    if cells:
        ws_watch.update_cells(cells, value_input_option="USER_ENTERED")


# -----------------------------
# main
# -----------------------------
def main():
    sheet_url = os.environ["SHEET_URL"]
    gc = get_client()
    sh = gc.open_by_url(sheet_url)

    ws_watch = sh.worksheet(WS_WATCHLIST)
    ws_prices = sh.worksheet(WS_PRICES)

    # 1) watchlist items
    items, watch_header, idx_symbol, idx_market = read_watchlist(ws_watch)
    if not items:
        raise RuntimeError("WATCHLIST 沒有有效 symbol")

    # Debug: confirm watchlist contains your OTC symbols
    print("[DBG] WATCHLIST items:", [(sym, mkt) for _, sym, mkt in items])

    # 2) PRICES index + columns
    sym_index, prices_header = build_prices_symbol_index(ws_prices)

    c_date = col_1based(prices_header, "date")
    c_symbol = col_1based(prices_header, "symbol")
    c_close = col_1based(prices_header, "close")
    c_vol = col_1based(prices_header, "volume")

    if None in (c_date, c_symbol, c_close, c_vol):
        raise RuntimeError("PRICES 必須有 Date, Symbol, Close, Volume 欄位（大小寫不拘）")

    updated = 0
    appended = 0

    # Batch updates to PRICES
    price_cells = []
    append_rows = []

    # Batch market writeback to WATCHLIST
    market_writebacks = []

    for row_no_watch, sym, market_hint in items:
        res = fetch_latest_yf_try_both(sym, market_hint)
        if not res:
            print(f"[WARN] {sym} no data from Yahoo with .TW/.TWO")
            time.sleep(SLEEP_SEC)
            continue

        d, close, vol, market_used = res

        # write back correct market to WATCHLIST (if differs)
        if norm_market(market_hint) != market_used:
            market_writebacks.append((row_no_watch, market_used))

        if sym in sym_index:
            row_no = sym_index[sym]
            price_cells.append(gspread.Cell(row_no, c_date, d))
            price_cells.append(gspread.Cell(row_no, c_close, close))
            price_cells.append(gspread.Cell(row_no, c_vol, vol))
            updated += 1
            print(f"[UPD] {sym} row={row_no} date={d} close={close} vol(張)={vol} marketUsed={market_used}")
        else:
            append_rows.append([d, sym, close, vol])
            appended += 1
            print(f"[APP] {sym} date={d} close={close} vol(張)={vol} marketUsed={market_used}")

        time.sleep(SLEEP_SEC)

    # 3) commit PRICES updates
    if price_cells:
        ws_prices.update_cells(price_cells, value_input_option="USER_ENTERED")

    for r in append_rows:
        ws_prices.append_row(r, value_input_option="USER_ENTERED")

    # 4) commit WATCHLIST market writebacks (optional)
    batch_writeback_market(ws_watch, idx_market, market_writebacks)

    print(f"[DONE] updated={updated}, appended={appended}")


if __name__ == "__main__":
    main()
