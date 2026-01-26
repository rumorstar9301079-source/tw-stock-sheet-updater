import os
import json
import datetime as dt
from typing import List, Tuple, Dict

import requests
import pandas as pd
import gspread
from google.oauth2.service_account import Credentials


SHEET_WATCHLIST = os.getenv("WS_WATCHLIST", "WATCHLIST")
SHEET_REVENUE = os.getenv("WS_REVENUE", "REVENUE")  # ✅ 新分頁：月營收
REVENUE_HEADERS = ["YM", "Symbol", "Revenue"]       # ✅ 固定欄位

MONTHS_BACK = int(os.getenv("REVENUE_MONTHS_BACK", "36"))


def get_client():
    info = json.loads(os.environ["GOOGLE_SERVICE_ACCOUNT_JSON"])
    scopes = [
        "https://www.googleapis.com/auth/spreadsheets",
        "https://www.googleapis.com/auth/drive",
    ]
    creds = Credentials.from_service_account_info(info, scopes=scopes)
    return gspread.authorize(creds)


# ====== WATCHLIST 讀取（沿用你原本邏輯）======
def _norm(s: str) -> str:
    return (s or "").replace("\u00a0", " ").strip().lower()


def _find_header_row(vals: List[List[str]], max_scan: int = 10) -> int:
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
        ]
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


def read_watchlist_symbols(ws_watch) -> List[str]:
    vals = ws_watch.get_all_values()
    if len(vals) < 2:
        raise RuntimeError("WATCHLIST 沒有資料")

    hrow = _find_header_row(vals, max_scan=10)
    header_raw = vals[hrow]
    rows = vals[hrow + 1:]

    idx_symbol = _col_index(header_raw, "symbol")
    if idx_symbol < 0:
        raise RuntimeError("WATCHLIST 找不到 Symbol/股票代碼/代號 欄位（表頭可能不在前 10 列）")

    out = []
    for r in rows:
        if len(r) <= idx_symbol:
            continue
        sym = str(r[idx_symbol]).strip()
        if sym:
            # WATCHLIST 可能會是 2359 或 2359.0
            if sym.endswith(".0") and sym.replace(".0", "").isdigit():
                sym = sym.replace(".0", "")
            out.append(sym)

    # 去重但保序
    seen = set()
    uniq = []
    for s in out:
        if s in seen:
            continue
        seen.add(s)
        uniq.append(s)
    return uniq


# ====== Revenue Sheet ======
def ensure_revenue_header(ws_rev):
    vals = ws_rev.get_all_values()
    if not vals:
        ws_rev.append_row(REVENUE_HEADERS)
        return

    header = [h.strip() for h in vals[0]]
    lower = [h.lower() for h in header]
    want = [h.lower() for h in REVENUE_HEADERS]
    if lower[: len(want)] != want:
        ws_rev.update("A1:C1", [REVENUE_HEADERS])


def ym_n_months_ago(n: int) -> str:
    # 回傳 YYYY-MM
    today = dt.date.today()
    y = today.year
    m = today.month - n
    while m <= 0:
        m += 12
        y -= 1
    return f"{y:04d}-{m:02d}"


def finmind_fetch_monthly_revenue(symbol: str, months_back: int) -> pd.DataFrame:
    """
    用 FinMind 抓月營收：近 months_back 個月
    回傳欄位：YM, Revenue
    """
    token = os.environ.get("FINMIND_TOKEN", "").strip()
    if not token:
        raise RuntimeError("缺少 FINMIND_TOKEN（請在 GitHub Secrets 或環境變數設定）")

    start_ym = ym_n_months_ago(months_back + 2)  # 多抓一點避免缺月
    start_date = start_ym + "-01"

    url = "https://api.finmindtrade.com/api/v4/data"
    params = {
        "dataset": "TaiwanStockMonthRevenue",
        "data_id": symbol,
        "start_date": start_date,
        "token": token,
    }
    r = requests.get(url, params=params, timeout=30)
    r.raise_for_status()
    data = r.json()

    if data.get("status") != 200:
        raise RuntimeError(f"FinMind API error: {data}")

    df = pd.DataFrame(data.get("data", []))
    if df.empty:
        return pd.DataFrame(columns=["YM", "Revenue"])

    # FinMind 的 date 是 YYYY-MM-DD（通常是該月第一天），轉成 YYYY-MM
    df["YM"] = pd.to_datetime(df["date"]).dt.strftime("%Y-%m")
    # month_revenue 是當月營收
    df["Revenue"] = pd.to_numeric(df["month_revenue"], errors="coerce").fillna(0).astype(int)

    df = df[["YM", "Revenue"]].drop_duplicates().sort_values("YM")
    # 只留近 months_back
    return df.tail(months_back)


def build_rev_index(ws_rev) -> Dict[Tuple[str, str], int]:
    """
    建立 (YM, Symbol) -> row_no (1-based)
    """
    vals = ws_rev.get_all_values()
    if not vals or len(vals) < 2:
        return {}

    header = [h.strip() for h in vals[0]]
    lower = [h.lower() for h in header]
    i_ym = lower.index("ym") if "ym" in lower else -1
    i_sym = lower.index("symbol") if "symbol" in lower else -1
    if i_ym < 0 or i_sym < 0:
        return {}

    idx = {}
    for row_no, r in enumerate(vals[1:], start=2):
        if len(r) <= max(i_ym, i_sym):
            continue
        ym = str(r[i_ym]).strip()
        sym = str(r[i_sym]).strip()
        if ym and sym:
            idx[(ym, sym)] = row_no
    return idx


def main():
    sheet_url = os.environ["SHEET_URL"]
    gc = get_client()
    sh = gc.open_by_url(sheet_url)

    ws_watch = sh.worksheet(SHEET_WATCHLIST)

    # ✅ REVENUE 分頁：不存在就建立（這是唯一「新增」的分頁）
    try:
        ws_rev = sh.worksheet(SHEET_REVENUE)
    except gspread.WorksheetNotFound:
        ws_rev = sh.add_worksheet(title=SHEET_REVENUE, rows=2000, cols=10)

    ensure_revenue_header(ws_rev)

    symbols = read_watchlist_symbols(ws_watch)
    print("[DBG] WATCHLIST symbols:", symbols)

    rev_index = build_rev_index(ws_rev)

    updates = []   # (rangeA1, [[...]])
    new_rows = []  # [[YM, Symbol, Revenue]]

    updated = 0
    appended = 0

    # 欄位位置固定 A:YM B:Symbol C:Revenue
    for sym in symbols:
        try:
            df = finmind_fetch_monthly_revenue(sym, MONTHS_BACK)
        except Exception as e:
            print(f"[WARN] {sym} revenue fetch failed: {e}")
            continue

        if df.empty:
            print(f"[WARN] {sym} revenue empty")
            continue

        for _, row in df.iterrows():
            ym = str(row["YM"])
            rev = int(row["Revenue"])
            key = (ym, sym)

            if key in rev_index:
                row_no = rev_index[key]
                # C 欄更新
                a1 = gspread.utils.rowcol_to_a1(row_no, 3)
                updates.append((a1, [[rev]]))
                updated += 1
            else:
                new_rows.append([ym, sym, rev])
                appended += 1

    if updates:
        body = [{"range": rng, "values": vals} for rng, vals in updates]
        ws_rev.batch_update(body, value_input_option="USER_ENTERED")

    if new_rows:
        new_rows.sort(key=lambda x: (x[0], x[1]))
        ws_rev.append_rows(new_rows, value_input_option="USER_ENTERED")

    print(f"[DONE] revenue updated={updated}, appended={appended}")


if __name__ == "__main__":
    main()
