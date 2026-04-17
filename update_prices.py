import os
import json
import datetime as dt
from typing import Dict, Tuple, List

import pandas as pd
import requests
import gspread
from google.oauth2.service_account import Credentials


# ✅ 來源表：先吃 WS_SOURCE_SHEET；沒有才吃 WS_WATCHLIST（雙保險）
SHEET_SOURCE = os.getenv("WS_SOURCE_SHEET", "").strip() or os.getenv("WS_WATCHLIST", "SECTOR_MAP_MASTER_ALL_PLUS").strip()
SHEET_PRICES = os.getenv("WS_PRICES", "PRICES").strip()

LOOKBACK_CAL_DAYS = int(os.getenv("LOOKBACK_CAL_DAYS", "120"))
TAIL_DAYS = int(os.getenv("PRICES_TAIL_DAYS", "90"))  # 寫入 PRICES 的天數上限（避免爆量）
REQUIRED_HEADERS = ["Date", "Symbol", "Close", "Volume"]

FINMIND_TOKEN = os.getenv("FINMIND_TOKEN", "").strip()
FINMIND_URL = "https://api.finmindtrade.com/api/v4/data"


def get_client():
    info = json.loads(os.environ["GOOGLE_SERVICE_ACCOUNT_JSON"])
    scopes = [
        "https://www.googleapis.com/auth/spreadsheets",
        "https://www.googleapis.com/auth/drive",
    ]
    creds = Credentials.from_service_account_info(info, scopes=scopes)
    return gspread.authorize(creds)


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


def fetch_history_finmind(session: requests.Session, symbol: str) -> pd.DataFrame:
    """
    ✅ 改用 FinMind 抓台股日線
    回傳欄位固定為：Close, Volume
    index 為 DatetimeIndex
    """
    symbol = _norm_symbol(symbol)

    if not FINMIND_TOKEN:
        print("[WARN] FINMIND_TOKEN is empty")
        return pd.DataFrame()

    end = dt.date.today()
    start = end - dt.timedelta(days=LOOKBACK_CAL_DAYS)

    headers = {
        "Authorization": f"Bearer {FINMIND_TOKEN}"
    }
    params = {
        "dataset": "TaiwanStockPrice",
        "data_id": symbol,
        "start_date": start.strftime("%Y-%m-%d"),
        "end_date": end.strftime("%Y-%m-%d"),
    }

    try:
        resp = session.get(FINMIND_URL, headers=headers, params=params, timeout=30)
    except Exception as e:
        print(f"[WARN] {symbol} FinMind request failed: {e}")
        return pd.DataFrame()

    if resp.status_code in (401, 402, 403, 429) or resp.status_code >= 500:
        msg = (resp.text or "")[:200].replace("\n", " ")
        print(f"[WARN] {symbol} FinMind HTTP {resp.status_code}: {msg}")
        return pd.DataFrame()

    try:
        resp.raise_for_status()
        js = resp.json()
    except Exception as e:
        msg = (resp.text or "")[:200].replace("\n", " ")
        print(f"[WARN] {symbol} FinMind parse/http failed: {e} | {msg}")
        return pd.DataFrame()

    data = js.get("data", [])
    if not data:
        return pd.DataFrame()

    df = pd.DataFrame(data)
    if df.empty:
        return pd.DataFrame()

    # FinMind 欄位：date, stock_id, open, max, min, close, Trading_Volume ...
    if "date" not in df.columns or "close" not in df.columns:
        print(f"[WARN] {symbol} FinMind missing required columns")
        return pd.DataFrame()

    out = pd.DataFrame()
    out["Close"] = pd.to_numeric(df["close"], errors="coerce")

    # ✅ 成交量優先用 Trading_Volume；沒有就退而求其次
    if "Trading_Volume" in df.columns:
        out["Volume"] = pd.to_numeric(df["Trading_Volume"], errors="coerce")
    elif "trading_volume" in df.columns:
        out["Volume"] = pd.to_numeric(df["trading_volume"], errors="coerce")
    else:
        out["Volume"] = 0

    idx = pd.to_datetime(df["date"], errors="coerce")
    out.index = idx

    out = out.dropna(subset=["Close"])
    out = out[~out.index.isna()]
    out = out.sort_index()

    return out


def get_prices_header(ws_prices) -> Tuple[List[str], Dict[str, int], int]:
    """
    ✅ 自動找 PRICES 的表頭列（不一定在第 1 列）
    return: (header, hmap, header_row_0based)
    """
    vals = ws_prices.get_all_values()
    if not vals:
        raise RuntimeError("PRICES 沒有任何資料（至少要有表頭列）")

    header_row = -1
    header = []
    for i in range(min(300, len(vals))):
        row = [c.strip() for c in vals[i]]
        hmap_tmp = {c.strip().lower(): j for j, c in enumerate(row) if c.strip()}
        if all(h.lower() in hmap_tmp for h in REQUIRED_HEADERS):
            header_row = i
            header = row
            break

    if header_row < 0:
        raise RuntimeError("PRICES 找不到含 Date/Symbol/Close/Volume 的表頭列（前 300 列都沒有）")

    hmap = {h.strip().lower(): i for i, h in enumerate(header) if h.strip()}
    return header, hmap, header_row


def build_prices_index_from_vals(vals_from_header: List[List[str]], hmap: Dict[str, int], header_row_0based: int) -> Dict[Tuple[str, str], int]:
    """
    vals_from_header: 從「表頭列」開始的 values（第 0 列為表頭）
    回傳的 row_no 是 Google Sheet 的實際列號（1-based）
    """
    if len(vals_from_header) < 2:
        return {}

    i_date = hmap["date"]
    i_sym = hmap["symbol"]

    index: Dict[Tuple[str, str], int] = {}
    for i, r in enumerate(vals_from_header[1:], start=0):
        real_row_no = header_row_0based + 2 + i
        if len(r) <= max(i_date, i_sym):
            continue
        d = _norm_date_str(r[i_date])
        s = _norm_symbol(r[i_sym])
        if not d or not s:
            continue
        index[(d, s)] = real_row_no
    return index


def _find_header_row(vals: List[List[str]], max_scan: int = 300) -> int:
    keys = ["symbol", "股票代碼", "股票代號", "代號", "證券代號", "ticker", "stockno"]
    scan = vals[: min(max_scan, len(vals))]
    for i, row in enumerate(scan):
        text = "|".join(_norm(x) for x in row)
        if any(_norm(k) in text for k in keys):
            return i

    for i, row in enumerate(vals):
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

    hrow = _find_header_row(vals, max_scan=300)
    header_raw = vals[hrow]
    rows = vals[hrow + 1:]

    idx_symbol = _col_index(header_raw, "symbol")
    if idx_symbol < 0:
        raise RuntimeError(f"{ws_source.title} 找不到 Symbol 欄（表頭可能不在前 300 列）")

    idx_market = _col_index(header_raw, "market")

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
    print("[DBG] LOOKBACK_CAL_DAYS=", LOOKBACK_CAL_DAYS, "TAIL_DAYS=", TAIL_DAYS)

    ws_source = _open_ws_by_title_strip(sh, SHEET_SOURCE)
    ws_prices = _open_ws_by_title_strip(sh, SHEET_PRICES)

    header, hmap, prices_hrow = get_prices_header(ws_prices)
    i_date = hmap["date"]
    i_sym = hmap["symbol"]
    i_close = hmap["close"]
    i_vol = hmap["volume"]

    prices_vals_all = ws_prices.get_all_values()
    prices_vals_from_header = prices_vals_all[prices_hrow:]
    price_index = build_prices_index_from_vals(prices_vals_from_header, hmap, prices_hrow)

    items = read_source_items(ws_source)
    src_syms = [s for s, _ in items]
    print(f"[DBG] source_items={len(items)} first_20={src_syms[:20]}")
    print(f"[DBG] last_20_symbols_in_source={src_syms[-20:]}")

    updates = []
    appends = []

    now_tpe = dt.datetime.utcnow() + dt.timedelta(hours=8)
    today_tpe = now_tpe.date()

    ok_cnt = 0
    empty_cnt = 0
    empty_list = []

    session = requests.Session()

    for sym, mkt in items:
        df = fetch_history_finmind(session, sym)
        if df.empty:
            empty_cnt += 1
            empty_list.append(sym)
            print(f"[WARN] {sym}({mkt}) no data from FinMind")
            continue

        ok_cnt += 1
        df = df.tail(TAIL_DAYS)

        last_dt = pd.to_datetime(df.index.max())
        last_date = last_dt.date()
        if last_date < today_tpe and now_tpe.hour >= 20:
            print(f"[WARN] {sym} FinMind not updated today: last_date={last_date}, today={today_tpe}")

        sym_key = _norm_symbol(sym)

        for dtt, row in df.iterrows():
            d_str = pd.to_datetime(dtt).strftime("%Y-%m-%d")
            close = float(row["Close"])

            # ✅ FinMind Trading_Volume 通常是股數，這裡統一轉張數
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

    print(f"[DBG] finmind_ok={ok_cnt} finmind_empty={empty_cnt}")
    if empty_list:
        print(f"[DBG] finmind_empty_list_first_30={empty_list[:30]}")

    if updates:
        ws_prices.batch_update(updates, value_input_option="USER_ENTERED")

    if appends:
        appends.sort(key=lambda r: (_norm_date_str(r[i_date]), _norm_symbol(r[i_sym])))
        ws_prices.append_rows(appends, value_input_option="USER_ENTERED")

    print(f"[DONE] updated_cells={len(updates)}, appended_rows={len(appends)}")


if __name__ == "__main__":
    main()
