import os
import json
import datetime as dt
from typing import Dict, Tuple, List

import pandas as pd
import yfinance as yf
import gspread
from google.oauth2.service_account import Credentials


SHEET_WATCHLIST = os.getenv("WS_WATCHLIST", "WATCHLIST")
SHEET_PRICES = os.getenv("WS_PRICES", "PRICES")

# 回補「交易日」約 90 天：用日曆天抓 120 天較穩
LOOKBACK_CAL_DAYS = int(os.getenv("LOOKBACK_CAL_DAYS", "120"))

# PRICES 欄位（大小寫不拘，但會用這些名稱建立/比對）
PRICES_HEADERS = ["Date", "Symbol", "Close", "Volume"]


def get_client():
    info = json.loads(os.environ["GOOGLE_SERVICE_ACCOUNT_JSON"])
    scopes = [
        "https://www.googleapis.com/auth/spreadsheets",
        "https://www.googleapis.com/auth/drive",
    ]
    creds = Credentials.from_service_account_info(info, scopes=scopes)
    return gspread.authorize(creds)


def _normalize_cols(df: pd.DataFrame) -> pd.DataFrame:
    if df is None or df.empty:
        return df
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = [c[0] for c in df.columns]
    return df


def fetch_history_yf(symbol: str, market: str) -> pd.DataFrame:
    """
    取近 LOOKBACK_CAL_DAYS 日曆天的日線資料，回傳 DataFrame:
    index = datetime (交易日)
    columns = Close, Volume (Volume = shares)
    """
    market = (market or "").strip().lower()
    suffix = ".TWO" if market == "otc" else ".TW"  # 預設 tse
    ticker = f"{symbol}{suffix}"

    end = dt.datetime.utcnow()
    start = end - dt.timedelta(days=LOOKBACK_CAL_DAYS)

    df = yf.download(
        ticker,
        start=start.strftime("%Y-%m-%d"),
        end=(end + dt.timedelta(days=1)).strftime("%Y-%m-%d"),
        interval="1d",
        progress=False,
        auto_adjust=False,
        threads=False,
        group_by="column",
    )
    df = _normalize_cols(df)
    if df is None or df.empty:
        return pd.DataFrame()

    need = {"Close", "Volume"}
    if not need.issubset(set(df.columns)):
        return pd.DataFrame()

    df = df[["Close", "Volume"]].copy()
    df = df.dropna(subset=["Close"])
    return df


def ensure_prices_header(ws_prices) -> Tuple[List[str], Dict[str, int]]:
    """
    確保 PRICES 第一列有表頭。回傳 header list (原樣) 與 header->colIndex(0-based)
    """
    vals = ws_prices.get_all_values()
    if not vals:
        ws_prices.append_row(PRICES_HEADERS)
        header = PRICES_HEADERS
        return header, {h.lower(): i for i, h in enumerate(header)}

    header = [h.strip() for h in vals[0]]
    lower = [h.lower() for h in header]
    if "date" not in lower or "symbol" not in lower:
        # 表頭亂掉：直接覆蓋第一列
        ws_prices.update("A1:D1", [PRICES_HEADERS])
        header = PRICES_HEADERS
        return header, {h.lower(): i for i, h in enumerate(header)}

    return header, {h.lower(): i for i, h in enumerate(header)}


def read_watchlist(ws_watch) -> List[Tuple[str, str]]:
    """
    從 WATCHLIST 讀 Symbol/Market，回傳 [(symbol, market), ...]
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

    out = []
    for r in rows:
        if len(r) <= idx_symbol:
            continue
        sym = str(r[idx_symbol]).strip()
        if not sym:
            continue
        mkt = ""
        if idx_market is not None and len(r) > idx_market:
            mkt = str(r[idx_market]).strip().lower()
        # 預設 tse
        if mkt not in ("tse", "otc"):
            mkt = "tse"
        out.append((sym, mkt))

    # 去重（保留第一次出現）
    seen = set()
    uniq = []
    for sym, mkt in out:
        key = (sym, mkt)
        if key in seen:
            continue
        seen.add(key)
        uniq.append((sym, mkt))
    return uniq


def build_prices_index(ws_prices, header_map: Dict[str, int]) -> Tuple[Dict[Tuple[str, str], int], int]:
    """
    建立 (date_str, symbol)->row_no (Google sheet row number, 1-based)
    回傳 index dict 與 last_row
    """
    vals = ws_prices.get_all_values()
    if not vals:
        return {}, 1

    idx_date = header_map.get("date")
    idx_symbol = header_map.get("symbol")
    idx_close = header_map.get("close")
    idx_vol = header_map.get("volume")

    index: Dict[Tuple[str, str], int] = {}
    # data 從第2列開始
    for row_no, r in enumerate(vals[1:], start=2):
        if idx_date is None or idx_symbol is None:
            continue
        if len(r) <= max(idx_date, idx_symbol):
            continue
        d = str(r[idx_date]).strip()
        s = str(r[idx_symbol]).strip()
        if not d or not s:
            continue
        # 同 key 重複，以最後一筆為準
        index[(d, s)] = row_no

    return index, len(vals)


def main():
    sheet_url = os.environ["SHEET_URL"]

    gc = get_client()
    sh = gc.open_by_url(sheet_url)

    ws_watch = sh.worksheet(SHEET_WATCHLIST)
    ws_prices = sh.worksheet(SHEET_PRICES)

    # 確保 PRICES 表頭
    header, hmap = ensure_prices_header(ws_prices)
    idx_date = hmap["date"]
    idx_symbol = hmap["symbol"]
    idx_close = hmap.get("close")
    idx_volume = hmap.get("volume")

    # 若缺 Close/Volume 欄位，補齊到 D 欄
    #（最簡單方式：直接覆蓋成標準 4 欄表頭）
    if idx_close is None or idx_volume is None:
        ws_prices.update("A1:D1", [PRICES_HEADERS])
        header = PRICES_HEADERS
        hmap = {h.lower(): i for i, h in enumerate(header)}
        idx_date, idx_symbol, idx_close, idx_volume = 0, 1, 2, 3

    # 讀 watchlist
    items = read_watchlist(ws_watch)
    print("[DBG] WATCHLIST items:", items)

    # 讀 PRICES index
    price_index, last_row = build_prices_index(ws_prices, hmap)

    # 準備 batch 更新 / append
    updates = []  # (rangeA1, [[...]])
    new_rows = []  # [[date, symbol, close, vol_lots]]

    updated = 0
    appended = 0

    for sym, mkt in items:
        df = fetch_history_yf(sym, mkt)
        if df.empty:
            print(f"[WARN] {sym}({mkt}) no data from Yahoo")
            continue

        # 只保留最近約 90 個交易日（若你想更長就改這裡）
        df = df.tail(90)

        for dtt, row in df.iterrows():
            # yfinance index 通常是日期（交易日），直接 format
            d_str = pd.to_datetime(dtt).strftime("%Y-%m-%d")
            close = float(row["Close"])
            vol_shares = float(row["Volume"]) if pd.notna(row["Volume"]) else 0.0
            vol_lots = int(round(vol_shares / 1000.0))

            key = (d_str, sym)
            if key in price_index:
                row_no = price_index[key]
                # 只更新 Close/Volume（Date/Symbol 不動）
                # Close: C欄，Volume: D欄（以 header_map 為準）
                # 用 A1 range 更新同列兩格
                col_close = idx_close + 1
                col_vol = idx_volume + 1
                a1 = gspread.utils.rowcol_to_a1(row_no, col_close)
                b1 = gspread.utils.rowcol_to_a1(row_no, col_vol)
                rng = f"{a1}:{b1}"
                updates.append((rng, [[close, vol_lots]]))
                updated += 1
            else:
                new_rows.append([d_str, sym, close, vol_lots])
                appended += 1

    # 批次更新（一次丟多個 range）
    if updates:
        # gspread batch_update needs list of dicts
        body = [{"range": rng, "values": vals} for rng, vals in updates]
        ws_prices.batch_update(body, value_input_option="USER_ENTERED")

    # 批次 append（一次 append 多列）
    if new_rows:
        # 依 Date, Symbol 排序，表比較乾淨
        new_rows.sort(key=lambda x: (x[0], x[1]))
        ws_prices.append_rows(new_rows, value_input_option="USER_ENTERED")

    print(f"[DONE] updated={updated}, appended={appended}")


if __name__ == "__main__":
    main()
