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

LOOKBACK_CAL_DAYS = int(os.getenv("LOOKBACK_CAL_DAYS", "120"))

# 你 PRICES 的表頭（有多一欄 #）
REQUIRED_HEADERS = ["Date", "Symbol", "Close", "Volume"]


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


def _norm(s: str) -> str:
    return (s or "").replace("\u00a0", " ").strip().lower()


def _norm_symbol(sym: str) -> str:
    s = str(sym or "").strip().upper()
    s = s.replace(".TW", "").replace(".TWO", "").replace(".TPE", "")
    if s.endswith(".0") and s.replace(".", "", 1).isdigit():
        s = s[:-2]
    return s


def _norm_date_str(x: str) -> str:
    """
    把 Google Sheet 可能出現的日期格式統一成 YYYY-MM-DD
    """
    s = str(x or "").strip()
    if not s:
        return ""
    try:
        d = pd.to_datetime(s, errors="coerce")
        if pd.isna(d):
            return s  # 真的解析不了就原樣
        return d.strftime("%Y-%m-%d")
    except Exception:
        return s


def fetch_history_yf(symbol: str, market: str) -> pd.DataFrame:
    """
    取近 LOOKBACK_CAL_DAYS 日曆天的日線資料
    - 永遠嘗試 .TW / .TWO
    """
    symbol = _norm_symbol(symbol)

    end = dt.datetime.utcnow()
    start = end - dt.timedelta(days=LOOKBACK_CAL_DAYS)

    m = _norm(market)
    prefer = [f"{symbol}.TWO", f"{symbol}.TW"] if m == "otc" else [f"{symbol}.TW", f"{symbol}.TWO"]

    for ticker in prefer:
        df = yf.download(
            ticker,
            start=start.strftime("%Y-%m-%d"),
            end=(end + dt.timedelta(days=1)).strftime("%Y-%m-%d"),
            interval="1d",
            progress=False,
            auto_adjust=False,   # ✅ 用原始 Close（不是調整後）
            threads=False,
            group_by="column",
        )
        df = _normalize_cols(df)
        if df is None or df.empty:
            continue

        need = {"Close", "Volume"}
        if not need.issubset(set(df.columns)):
            continue

        df = df[["Close", "Volume"]].copy()
        df = df.dropna(subset=["Close"])
        if not df.empty:
            return df

    return pd.DataFrame()


def get_prices_header(ws_prices) -> Tuple[List[str], Dict[str, int]]:
    """
    讀 PRICES 第 1 列當表頭，回傳 (header原樣, header_map小寫->index)
    ✅ 不覆蓋、不清表
    """
    vals = ws_prices.get_all_values()
    if not vals:
        raise RuntimeError("PRICES 沒有任何資料（至少要有表頭列）")

    header = [h.strip() for h in vals[0]]
    hmap = {h.strip().lower(): i for i, h in enumerate(header) if h.strip()}

    # 必須存在 Date/Symbol/Close/Volume
    missing = [h for h in REQUIRED_HEADERS if h.lower() not in hmap]
    if missing:
        raise RuntimeError(f"PRICES 表頭缺少必要欄位：{missing}（你目前表頭：{header}）")

    return header, hmap


def build_prices_index(ws_prices, hmap: Dict[str, int]) -> Dict[Tuple[str, str], int]:
    """
    index: (YYYY-MM-DD, SYMBOL) -> sheet_row_no(1-based)
    """
    vals = ws_prices.get_all_values()
    if len(vals) < 2:
        return {}

    i_date = hmap["date"]
    i_sym = hmap["symbol"]

    index: Dict[Tuple[str, str], int] = {}
    for row_no, r in enumerate(vals[1:], start=2):
        if len(r) <= max(i_date, i_sym):
            continue
        d = _norm_date_str(r[i_date])
        s = _norm_symbol(r[i_sym])
        if not d or not s:
            continue
        index[(d, s)] = row_no

    return index


# =========================
# WATCHLIST 讀取（沿用你原本的）
# =========================
def _col_index(header: List[str], key: str) -> int:
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
        sym = _norm_symbol(str(r[idx_symbol]).strip())
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


def main():
    sheet_url = os.environ["SHEET_URL"]
    gc = get_client()
    sh = gc.open_by_url(sheet_url)

    ws_watch = sh.worksheet(SHEET_WATCHLIST)
    ws_prices = sh.worksheet(SHEET_PRICES)

    # ✅ 讀 PRICES 表頭（不覆蓋、不清表）
    header, hmap = get_prices_header(ws_prices)
    i_date = hmap["date"]
    i_sym = hmap["symbol"]
    i_close = hmap["close"]
    i_vol = hmap["volume"]  # 你這欄要放「張數」

    # ✅ 建索引：避免重複 append
    price_index = build_prices_index(ws_prices, hmap)

    items = read_watchlist(ws_watch)

    updates = []   # [{"range": "C10", "values":[[...]]}, ...]
    appends = []   # [[...全列...], ...]

    # 台灣日期時間（UTC+8）
    now_tpe = dt.datetime.utcnow() + dt.timedelta(hours=8)
    today_tpe = now_tpe.date()

    for sym, mkt in items:
        df = fetch_history_yf(sym, mkt)
        if df.empty:
            print(f"[WARN] {sym}({mkt}) no data from Yahoo (.TW/.TWO tried)")
            continue

        # 只取近 90 交易日
        df = df.tail(90)

        # 如果你想避免「Yahoo 還沒更新今日收盤」造成亂寫，可保守不寫今日：
        # 若現在已晚上且 Yahoo 還沒更新，這段只警告不阻擋
        last_dt = pd.to_datetime(df.index.max())
        last_date = last_dt.date()
        if last_date < today_tpe and now_tpe.hour >= 20:
            print(f"[WARN] {sym} Yahoo 尚未更新到今天：last_date={last_date}, today={today_tpe}")

        sym_key = _norm_symbol(sym)

        for dtt, row in df.iterrows():
            d_str = pd.to_datetime(dtt).strftime("%Y-%m-%d")
            close = float(row["Close"])
            vol_shares = float(row["Volume"]) if pd.notna(row["Volume"]) else 0.0
            vol_lots = int(round(vol_shares / 1000.0))  # ✅ 張數

            key = (d_str, sym_key)

            if key in price_index:
                row_no = price_index[key]
                # ✅ 只更新 Close & Volume 兩格（不動其他欄位，如 #）
                a1_close = gspread.utils.rowcol_to_a1(row_no, i_close + 1)
                a1_vol = gspread.utils.rowcol_to_a1(row_no, i_vol + 1)
                updates.append({"range": a1_close, "values": [[close]]})
                updates.append({"range": a1_vol, "values": [[vol_lots]]})
            else:
                # ✅ append 一整列（依 PRICES 表頭長度對齊）
                new_row = [""] * len(header)
                new_row[i_date] = d_str
                new_row[i_sym] = sym_key
                new_row[i_close] = close
                new_row[i_vol] = vol_lots
                appends.append(new_row)

    if updates:
        ws_prices.batch_update(updates, value_input_option="USER_ENTERED")

    if appends:
        # 排序讓表比較整齊
        appends.sort(key=lambda r: (_norm_date_str(r[i_date]), _norm_symbol(r[i_sym])))
        ws_prices.append_rows(appends, value_input_option="USER_ENTERED")

    print(f"[DONE] updated_cells={len(updates)}, appended_rows={len(appends)}")


if __name__ == "__main__":
    main()


