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


# =========================
# ✅ WATCHLIST 讀取：支援
# - 第 1 行放說明（表頭不一定在第 1 行）
# - Symbol 欄：Symbol / 股票代碼 / 股票代號 / 代號 / 證券代號...
# - Market 欄：Market / tse上市otc上櫃 / 上市otc上櫃 / 市場...
# =========================
def _norm(s: str) -> str:
    return (s or "").replace("\u00a0", " ").strip().lower()


def _col_index(header: List[str], key: str) -> int:
    """
    key: "symbol" / "market"
    """
    alias = {
        "symbol": [
            "symbol", "ticker", "stockno", "stock_no",
            "代號", "股號", "股票代號", "股票代碼",
            "證券代號", "證券代碼", "公司代號",
        ],
        "market": [
            "market", "市場",
            "上市/上櫃", "上市／上櫃", "上市上櫃",
            "tse上市otc上櫃", "上市otc上櫃",
            "tse上市 otc上櫃",
        ],
    }
    candidates = [_norm(x) for x in alias.get(key, [key])]

    for i, h in enumerate(header):
        hh = _norm(h)
        if not hh:
            continue
        if hh in candidates:
            return i
        if any(c in hh or hh in c for c in candidates):
            return i
    return -1


def _find_header_row(vals: List[List[str]], max_scan: int = 10) -> int:
    """
    掃描前 max_scan 列，找出最像表頭的那一列
    規則：該列含 symbol/股票代碼/代號 任一字樣
    """
    keys = ["symbol", "股票代碼", "股票代號", "代號", "證券代號", "ticker", "stockno"]
    scan = vals[: min(max_scan, len(vals))]
    for i, row in enumerate(scan):
        text = "|".join(_norm(x) for x in row)
        if any(_norm(k) in text for k in keys):
            return i
    return 0


def _normalize_market(mkt: str) -> str:
    m = _norm(mkt)
    if m in ("otc", "tpex", "two"):
        return "otc"
    if m in ("tse", "twse", "tw", "上市"):
        return "tse"
    if "otc" in m or "tpex" in m or "上櫃" in m:
        return "otc"
    if "tse" in m or "twse" in m or "上市" in m:
        return "tse"
    return "tse"


def read_watchlist(ws_watch) -> List[Tuple[str, str]]:
    """
    從 WATCHLIST 讀 Symbol/Market，回傳 [(symbol, market), ...]
    - 支援第 1 行放說明：自動偵測表頭列
    - Symbol 欄可為：Symbol/股票代碼/代號...
    - Market 欄可為：Market/tse上市otc上櫃/市場...
    """
    vals = ws_watch.get_all_values()
    if len(vals) < 2:
        raise RuntimeError("WATCHLIST 沒有資料")

    hrow = _find_header_row(vals, max_scan=10)
    header_raw = vals[hrow]
    rows = vals[hrow + 1:]

    idx_symbol = _col_index(header_raw, "symbol")
    if idx_symbol < 0:
        raise RuntimeError("WATCHLIST 找不到 Symbol/股票代碼/代號 欄位（表頭可能不在前 10 列）")

    idx_market = _col_index(header_raw, "market")  # may be -1

    out: List[Tuple[str, str]] = []
    for r in rows:
        if len(r) <= idx_symbol:
            continue
        sym = str(r[idx_symbol]).strip()
        if not sym:
            continue

        mkt = ""
        if idx_market >= 0 and len(r) > idx_market:
            mkt = str(r[idx_market]).strip()
        mkt = _normalize_market(mkt)
        out.append((sym, mkt))

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

    index: Dict[Tuple[str, str], int] = {}
    for row_no, r in enumerate(vals[1:], start=2):
        if idx_date is None or idx_symbol is None:
            continue
        if len(r) <= max(idx_date, idx_symbol):
            continue
        d = str(r[idx_date]).strip()
        s = str(r[idx_symbol]).strip()
        if not d or not s:
            continue
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

    # 若缺 Close/Volume 欄位，補齊到 D 欄（直接覆蓋成標準 4 欄表頭）
    if idx_close is None or idx_volume is None:
        ws_prices.update("A1:D1", [PRICES_HEADERS])
        header = PRICES_HEADERS
        hmap = {h.lower(): i for i, h in enumerate(header)}
        idx_date, idx_symbol, idx_close, idx_volume = 0, 1, 2, 3

    items = read_watchlist(ws_watch)
    print("[DBG] WATCHLIST items:", items)

    price_index, _last_row = build_prices_index(ws_prices, hmap)

    updates = []   # (rangeA1, [[...]])
    new_rows = []  # [[date, symbol, close, vol_lots]]

    updated = 0
    appended = 0

    # ✅ 台灣日期/時間（用 UTC+8 推算，不依賴系統時區）
    now_tpe = dt.datetime.utcnow() + dt.timedelta(hours=8)
    today_tpe = now_tpe.date()

    for sym, mkt in items:
        df = fetch_history_yf(sym, mkt)
        if df.empty:
            print(f"[WARN] {sym}({mkt}) no data from Yahoo")
            continue

        # ✅ 檢查是否已包含「台灣今天」的日線資料（避免 Yahoo 還沒更新）
        last_dt = pd.to_datetime(df.index.max())
        last_date = last_dt.date()

        if last_date < today_tpe and now_tpe.hour >= 20:
            print(f"[WARN] {sym}({mkt}) Yahoo 尚未更新到今天：last_date={last_date}, today={today_tpe}")
            # 若你想「沒更新到今天就不要寫入」，打開下一行：
            # continue

        df = df.tail(90)

        for dtt, row in df.iterrows():
            d_str = pd.to_datetime(dtt).strftime("%Y-%m-%d")
            close = float(row["Close"])
            vol_shares = float(row["Volume"]) if pd.notna(row["Volume"]) else 0.0
            vol_lots = int(round(vol_shares / 1000.0))

            key = (d_str, sym)
            if key in price_index:
                row_no = price_index[key]
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

    if updates:
        body = [{"range": rng, "values": vals} for rng, vals in updates]
        ws_prices.batch_update(body, value_input_option="USER_ENTERED")

    if new_rows:
        new_rows.sort(key=lambda x: (x[0], x[1]))
        ws_prices.append_rows(new_rows, value_input_option="USER_ENTERED")

    print(f"[DONE] updated={updated}, appended={appended}")


if __name__ == "__main__":
    main()


