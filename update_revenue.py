import os
import json
import datetime as dt
from typing import Dict, Tuple, List

import pandas as pd
import requests
import gspread
from google.oauth2.service_account import Credentials


SHEET_WATCHLIST = os.getenv("WS_WATCHLIST", "WATCHLIST")
SHEET_REVENUE = os.getenv("WS_REVENUE", "REVENUE")

REVENUE_HEADERS = ["Month", "Symbol", "Revenue", "YoY", "YoY3M"]
LOOKBACK_YEARS = int(os.getenv("REVENUE_LOOKBACK_YEARS", "3"))

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

    out: List[str] = []
    for r in rows:
        if len(r) <= idx_symbol:
            continue
        sym = str(r[idx_symbol]).strip()
        if sym:
            if sym.endswith(".0"):
                sym = sym[:-2]
            out.append(sym)

    # unique preserve order
    seen = set()
    uniq = []
    for s in out:
        if s in seen:
            continue
        seen.add(s)
        uniq.append(s)
    return uniq


def ensure_revenue_header(ws_rev) -> Dict[str, int]:
    vals = ws_rev.get_all_values()
    if not vals:
        ws_rev.append_row(REVENUE_HEADERS)
        return {h.lower(): i for i, h in enumerate(REVENUE_HEADERS)}

    header = [h.strip() for h in vals[0]]
    lower = [h.lower() for h in header]
    need = [h.lower() for h in REVENUE_HEADERS]
    if lower[: len(need)] != need:
        ws_rev.update("A1:E1", [REVENUE_HEADERS])
        return {h.lower(): i for i, h in enumerate(REVENUE_HEADERS)}

    # 若有人多打空格/大小寫，仍建立 map
    return {h.lower(): i for i, h in enumerate(header)}


def build_rev_index(ws_rev, hmap: Dict[str, int]) -> Dict[Tuple[str, str], int]:
    vals = ws_rev.get_all_values()
    if not vals or len(vals) == 1:
        return {}

    i_month = hmap.get("month")
    i_sym = hmap.get("symbol")

    idx: Dict[Tuple[str, str], int] = {}
    for row_no, r in enumerate(vals[1:], start=2):
        if i_month is None or i_sym is None:
            continue
        if len(r) <= max(i_month, i_sym):
            continue
        m = str(r[i_month]).strip()
        s = str(r[i_sym]).strip()
        if m and s:
            idx[(m, s)] = row_no
    return idx


def finmind_month_revenue(stock_id: str, start_date: str, token: str) -> pd.DataFrame:
    headers = {"Authorization": f"Bearer {token}"}
    params = {
        "dataset": "TaiwanStockMonthRevenue",
        "data_id": stock_id,
        "start_date": start_date,
    }
    resp = requests.get(FINMIND_URL, headers=headers, params=params, timeout=30)
    resp.raise_for_status()
    js = resp.json()
    data = js.get("data", []) or []
    if not data:
        return pd.DataFrame()
    df = pd.DataFrame(data)
    # expected columns: date, stock_id, revenue, revenue_month, revenue_year ... :contentReference[oaicite:1]{index=1}
    if "date" not in df.columns or "revenue" not in df.columns:
        return pd.DataFrame()
    df["date"] = pd.to_datetime(df["date"], errors="coerce")
    df = df.dropna(subset=["date"])
    df["Month"] = df["date"].dt.strftime("%Y-%m")
    df["Revenue"] = pd.to_numeric(df["revenue"], errors="coerce")
    df = df.dropna(subset=["Revenue"])
    df["Symbol"] = stock_id
    df = df[["Month", "Symbol", "Revenue"]].copy()
    df = df.sort_values(["Symbol", "Month"])
    return df


def add_yoy_features(df: pd.DataFrame) -> pd.DataFrame:
    # YoY = 本月 / 去年同月 - 1
    df = df.copy()
    df["MonthDate"] = pd.to_datetime(df["Month"] + "-01", errors="coerce")
    df = df.dropna(subset=["MonthDate"])
    df["Revenue"] = pd.to_numeric(df["Revenue"], errors="coerce")

    # map last year same month
    df["MonthLY"] = (df["MonthDate"] - pd.DateOffset(years=1)).dt.strftime("%Y-%m")
    key_cur = list(zip(df["Symbol"], df["Month"]))
    rev_map = {(s, m): r for s, m, r in zip(df["Symbol"], df["Month"], df["Revenue"])}

    yoy = []
    for s, mly, r in zip(df["Symbol"], df["MonthLY"], df["Revenue"]):
        base = rev_map.get((s, mly))
        if base is None or not pd.notna(base) or base == 0:
            yoy.append("")
        else:
            yoy.append(float(r) / float(base) - 1.0)
    df["YoY"] = yoy

    # YoY3M = 近3月營收合計 / 去年同期間3月合計 - 1
    df["Rev3M"] = df.groupby("Symbol")["Revenue"].rolling(3, min_periods=3).sum().reset_index(level=0, drop=True)

    # last year 3M sum: shift 12 months on monthly series
    df["Rev3M_LY"] = df.groupby("Symbol")["Rev3M"].shift(12)

    yoy3m = []
    for r3, r3ly in zip(df["Rev3M"], df["Rev3M_LY"]):
        if pd.isna(r3) or pd.isna(r3ly) or r3ly == 0:
            yoy3m.append("")
        else:
            yoy3m.append(float(r3) / float(r3ly) - 1.0)
    df["YoY3M"] = yoy3m

    df = df[["Month", "Symbol", "Revenue", "YoY", "YoY3M"]].copy()
    return df


def main():
    sheet_url = os.environ["SHEET_URL"]
    token = os.environ["FINMIND_TOKEN"].strip()
    if not token:
        raise RuntimeError("FINMIND_TOKEN is empty")

    gc = get_client()
    sh = gc.open_by_url(sheet_url)

    ws_watch = sh.worksheet(SHEET_WATCHLIST)
    ws_rev = sh.worksheet(SHEET_REVENUE)

    hmap = ensure_revenue_header(ws_rev)
    idx = build_rev_index(ws_rev, hmap)

    symbols = read_watchlist_symbols(ws_watch)
    print("[DBG] symbols:", symbols)

    start = (dt.date.today() - dt.timedelta(days=365 * LOOKBACK_YEARS + 40)).strftime("%Y-%m-%d")

    all_rows = []
    for sym in symbols:
        try:
            df = finmind_month_revenue(sym, start, token)
        except Exception as e:
            print(f"[WARN] {sym} finmind fetch failed: {e}")
            continue
        if df.empty:
            print(f"[WARN] {sym} no revenue data")
            continue
        all_rows.append(df)

    if not all_rows:
        print("[DONE] no revenue fetched")
        return

    big = pd.concat(all_rows, ignore_index=True)
    big = add_yoy_features(big)

    # 寫回：以 (Month, Symbol) 更新/append
    updates = []
    new_rows = []

    def _fmt_pct(x):
        if x == "" or x is None or (isinstance(x, float) and not pd.notna(x)):
            return ""
        return float(x)

    for _, r in big.iterrows():
        month = str(r["Month"])
        sym = str(r["Symbol"])
        revenue = int(round(float(r["Revenue"]))) if pd.notna(r["Revenue"]) else ""
        yoy = _fmt_pct(r["YoY"])
        yoy3m = _fmt_pct(r["YoY3M"])

        key = (month, sym)
        row_vals = [month, sym, revenue, yoy, yoy3m]

        if key in idx:
            row_no = idx[key]
            a1 = gspread.utils.rowcol_to_a1(row_no, 1)
            e1 = gspread.utils.rowcol_to_a1(row_no, 5)
            updates.append((f"{a1}:{e1}", [row_vals]))
        else:
            new_rows.append(row_vals)

    if updates:
        body = [{"range": rng, "values": vals} for rng, vals in updates]
        ws_rev.batch_update(body, value_input_option="USER_ENTERED")

    if new_rows:
        # 依月份排序後 append
        new_rows.sort(key=lambda x: (x[0], x[1]))
        ws_rev.append_rows(new_rows, value_input_option="USER_ENTERED")

    print(f"[DONE] updated={len(updates)}, appended={len(new_rows)}")


if __name__ == "__main__":
    main()

