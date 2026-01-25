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
    s = str(v or "").strip().lower()
    if not s:
        return "tse"
    if s in ("tse", "twse", "tw", "上市", "市", "listed"):
        return "tse"
    if s in ("otc", "tpex", "two", "上櫃", "櫃買", "櫃", "unlisted", "tpEx".lower()):
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
    - vol_lots: 張
    - market_used: tse/otc (suffix that worked)
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
        vol_lots = int(round(vol_shares / 1000.0))
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
    for i, r in enumerate(rows, start=2):  # sheet row
        if len(r) <= idx_symbol:
            continue
        sym = str(r[idx_symbol]).strip()
        if not sym:
            continue
        mkt = "tse"
        if idx_market is not None and len(r) > idx_market:
            mkt = norm_market(r[idx_market])
        items.append((i, sym, mkt))

    # de-dup symbol (keep last)
    last = {}
    for row_no, sym, mkt in items:
        last[sym] = (row_no, mkt)
    out = [(last[sym][0], sym, last[sym][1]) for sym in last.keys()]
    return out, idx_market


def batch_writeback_market(ws_watch, idx_market_0based, updates):
    if idx_market_0based is None:
        return
    col_market = idx_market_0based + 1
    cells = [gspread.Cell(r, col_market, m) for r, m in updates]
    if cells:
        ws_watch.update_cells(cells, value_input_option="USER_ENTERED")


# -----------------------------
# robust append (NO append_row)
# -----------------------------
def append_rows_by_update(ws, rows):
    """
    ✅ Avoid ws.append_row() because it may end up outside a Google Sheets 'Table' view.
    This writes rows to the first empty row after existing sheet content.
    """
    if not rows:
        return

    # last_row includes header + data rows; stable for "data area"
    start_row = ws.get_all_values().__len__() + 1  # next empty row
    start_col = 1
    num_rows = len(rows)
    num_cols = len(rows[0])

    # Write as a block (USER_ENTERED to keep number formats)
    ws.update(
        f"A{start_row}:{gspread.utils.rowcol_to_a1(start_row + num_rows - 1, start_col + num_cols - 1)}",
        rows,
        value_input_option="USER_ENTERED",
    )


# -----------------------------
# main
# -----------------------------
def main():
    sheet_url = os.environ["SHEET_URL"]
    gc = get_client()
    sh = gc.open_by_url(sheet_url)

    ws_watch = sh.worksheet(WS_WATCHLIST)
    ws_prices = sh.worksheet(WS_PRICES)

    items, idx_market = read_watchlist(ws_watch)
    if not items:
        raise RuntimeError("WATCHLIST 沒有有效 symbol")

    print("[DBG] WATCHLIST items:", [(sym, mkt) for _, sym, mkt in items])

    sym_index, header = build_prices_symbol_index(ws_prices)

    c_date = col_1based(header, "date")
    c_symbol = col_1based(header, "symbol")
    c_close = col_1based(header, "close")
    c_vol = col_1based(header, "volume")
    if None in (c_date, c_symbol, c_close, c_vol):
        raise RuntimeError("PRICES 必須有 Date, Symbol, Close, Volume 欄位（大小寫不拘）")

    updated = 0
    appended = 0

    price_cells = []
    append_rows = []
    market_writebacks = []

    for row_no_watch, sym, market_hint in items:
        res = fetch_latest_yf_try_both(sym, market_hint)
        if not res:
            print(f"[WARN] {sym} no data from Yahoo with .TW/.TWO")
            time.sleep(SLEEP_SEC)
            continue

        d, close, vol, market_used = res

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
            # ✅ IMPORTANT: match PRICES header order: Date, Symbol, Close, Volume
            append_rows.append([d, sym, close, vol])
            appended += 1
            print(f"[APP] {sym} date={d} close={close} vol(張)={vol} marketUsed={market_used}")

        time.sleep(SLEEP_SEC)

    # commit updates
    if price_cells:
        ws_prices.update_cells(price_cells, value_input_option="USER_ENTERED")

    # ✅ commit appends using update() block, not append_row()
    if append_rows:
        append_rows_by_update(ws_prices, append_rows)

    # write back market if needed
    batch_writeback_market(ws_watch, idx_market, market_writebacks)

    print(f"[DONE] updated={updated}, appended={appended}")


if __name__ == "__main__":
    main()
