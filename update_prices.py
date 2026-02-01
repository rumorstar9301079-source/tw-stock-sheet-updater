import os
import json
import datetime as dt
from typing import Dict, Tuple, List

import pandas as pd
import yfinance as yf
import gspread
from google.oauth2.service_account import Credentials


# ✅ 來源表：先吃 WS_SOURCE_SHEET；沒有才吃 WS_WATCHLIST（雙保險）
SHEET_SOURCE = os.getenv("WS_SOURCE_SHEET", "").strip() or os.getenv("WS_WATCHLIST", "SECTOR_MAP_MASTER_ALL_PLUS").strip()
SHEET_PRICES = os.getenv("WS_PRICES", "PRICES").strip()

LOOKBACK_CAL_DAYS = int(os.getenv("LOOKBACK_CAL_DAYS", "120"))
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
    s = str(x or "").strip()
    if not s:
        return ""
    try:
        d = pd.to_datetime(s, errors="coerce")
        if pd.isna(d):
            return s
        return d.strftime("%Y-%m-%d")
    except Exception:
        return s


def fetch_history_yf(symbol: str, market: str) -> pd.DataFrame:
    symbol = _norm_symbol(symbol)

    end = dt.datetime.utcnow()
    start = end - dt.timedelta(days=LOOKBACK_CAL_DAYS)

    m = _norm(market)
    prefer = [f"{symbol}.TWO", f"{symbol}.TW"] if m == "otc" else [f"{symbol}.TW", f"{symbol}.TWO"]

    last_err = None
    for ticker in prefer:
        try:
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
                continue
            if not {"Close", "Volume"}.issubset(set(df.columns)):
                continue

            df = df[["Close", "Volume"]].copy()
            df = df.dropna(subset=["Close"])
            if not df.empty:
                return df
        except Exception as e:
            last_err = e
            continue

    if last_err:
        print(f"[WARN] {symbol} yfinance error: {last_err}")
    return pd.DataFrame()


def get_prices_header(ws_prices) -> Tuple[List[str], Dict[str, int]]:
    vals = ws_prices.get_all_values()
    if not vals:
        raise RuntimeError("PRICES 沒有任何資料（至少要有表頭列）")

    header = [h.strip() for h in vals[0]]
    hmap = {h.strip().lower(): i for i, h in enumerate(header) if h.strip()}

    missing = [h for h in REQUIRED_HEADERS if h.lower() not in hmap]
    if missing:
        raise RuntimeError(f"PRICES 表頭缺少必要欄位：{missing}（你目前表頭：{header}）")

    return header, hmap


def build_prices_index_from_vals(vals: List[List[str]], hmap: Dict[str, int]) -> Dict[Tuple[str, str], int]:
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


def _find_header_row(vals: List[List[str]], max_scan: int = 60) -> int:
    keys = ["symbol", "股票代碼", "股票代號", "代號", "證券代號", "ticker", "stockno"]
    scan = vals[: min(max_scan, len(vals))]
    for i, row in enumerate(scan):
        text = "|".join(_norm(x) for x in row)
        if any(_norm(k) in text for k in keys):
            return i
    return 0


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


def _normalize_market(mkt: str) -> str:
    m = _norm(mkt)
    if m in ("otc", "tpex", "two") or "上櫃" in m:
        return "otc"
    if m in ("tse", "twse", "tw", "上市") or "上市" in m:
        return "tse"
    return "tse"


def _open_ws_by_title_strip(sh, title: str):
    target = (title or "").strip()
    for ws in sh.worksheets():
        if ws.title.strip() == target:
            return ws
    raise gspread.exceptions.WorksheetNotFound(f"Worksheet not found: {title}")


def read_source_items(ws_source) -> List[Tuple[str, str]]:
    vals = ws_source.get_all_values()
    if len(vals) < 2:
        raise RuntimeError(f"{ws_source.title} 沒有資料")

    hrow = _find_header_row(vals, max_scan=60)
    header_raw = vals[hrow]
    rows = vals[hrow + 1:]

    idx_symbol = _col_index(header_raw, "symbol")
    if idx_symbol < 0:
        raise RuntimeError(f"{ws_source.title} 找不到 Symbol 欄（表頭可能不在前 60 列）")

    idx_market = _col_index(header_raw, "market")  # 可沒有

    out: List[Tuple[str, str]] = []
    for r in rows:
        if len(r) <= idx_symbol:
            continue
        sym = _norm_symbol(r[idx_symbol])
        if not sym:
            continue

        mkt = ""
        if idx_market >= 0 and len(r) > idx_market:
            mkt = str(r[idx_market]).strip()
        mkt = _normalize_market(mkt)
        out.append((sym, mkt))

    # Symbol 去重（同股多族群只抓一次）
    seen = set()
    uniq = []
    for sym, mkt in out:
        if sym in seen:
            continue
        seen.add(sym)
        uniq.append((sym, mkt))
    return uniq


def main():
    sheet_url = os.environ["SHEET_URL"]
    gc = get_client()
    sh = gc.open_by_url(sheet_url)

    titles = [ws.title for ws in sh.worksheets()]
    print("[DBG] worksheets:", titles)
    print("[DBG] SHEET_SOURCE=", SHEET_SOURCE, "SHEET_PRICES=", SHEET_PRICES)

    ws_source = _open_ws_by_title_strip(sh, SHEET_SOURCE)
    ws_prices = _open_ws_by_title_strip(sh, SHEET_PRICES)

    header, hmap = get_prices_header(ws_prices)
    i_date = hmap["date"]
    i_sym = hmap["symbol"]
    i_close = hmap["close"]
    i_vol = hmap["volume"]

    prices_vals = ws_prices.get_all_values()
    price_index = build_prices_index_from_vals(prices_vals, hmap)

    items = read_source_items(ws_source)
    print(f"[DBG] source_items={len(items)} first_20={[x[0] for x in items[:20]]}")

    updates = []
    appends = []

    now_tpe = dt.datetime.utcnow() + dt.timedelta(hours=8)
    today_tpe = now_tpe.date()

    ok_cnt = 0
    empty_cnt = 0

    for sym, mkt in items:
        df = fetch_history_yf(sym, mkt)
        if df.empty:
            empty_cnt += 1
            print(f"[WARN] {sym}({mkt}) no data from Yahoo (.TW/.TWO tried)")
            continue

        ok_cnt += 1
        df = df.tail(90)

        last_dt = pd.to_datetime(df.index.max())
        last_date = last_dt.date()
        if last_date < today_tpe and now_tpe.hour >= 20:
            print(f"[WARN] {sym} Yahoo not updated today: last_date={last_date}, today={today_tpe}")

        sym_key = _norm_symbol(sym)

        for dtt, row in df.iterrows():
            d_str = pd.to_datetime(dtt).strftime("%Y-%m-%d")
            close = float(row["Close"])

            # ✅ yfinance Volume=股數 → PRICES Volume=張數
            vol_shares = float(row["Volume"]) if pd.notna(row["Volume"]) else 0.0
            vol_lots = int(round(vol_shares / 1000.0))

            key = (d_str, sym_key)
            if key in price_index:
                row_no = price_index[key]
                a1_close = gspread.utils.rowcol_to_a1(row_no, i_close + 1)
                a1_vol = gspread.utils.rowcol_to_a1(row_no, i_vol + 1)
                updates.append({"range": a1_close, "values": [[close]]})
                updates.append({"range": a1_vol, "values": [[vol_lots]]})
            else:
                new_row = [""] * len(header)
                new_row[i_date] = d_str
                new_row[i_sym] = sym_key
                new_row[i_close] = close
                new_row[i_vol] = vol_lots
                appends.append(new_row)

    print(f"[DBG] yfinance_ok={ok_cnt} yfinance_empty={empty_cnt}")

    if updates:
        ws_prices.batch_update(updates, value_input_option="USER_ENTERED")
    if appends:
        appends.sort(key=lambda r: (_norm_date_str(r[i_date]), _norm_symbol(r[i_sym])))
        ws_prices.append_rows(appends, value_input_option="USER_ENTERED")

    print(f"[DONE] updated_cells={len(updates)}, appended_rows={len(appends)}")


if __name__ == "__main__":
    main()
