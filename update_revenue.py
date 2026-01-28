import os
import json
import datetime as dt
from typing import List

import pandas as pd
import requests
import gspread
from google.oauth2.service_account import Credentials


SHEET_WATCHLIST = os.getenv("WS_WATCHLIST", "WATCHLIST")
SHEET_REVENUE = os.getenv("WS_REVENUE", "REVENUE")

REVENUE_HEADERS = ["Month", "Symbol", "Revenue", "YoY", "YoY3M"]

FINMIND_TOKEN = os.getenv("FINMIND_TOKEN", "").strip()
FINMIND_URL = "https://api.finmindtrade.com/api/v4/data"

FETCH_YEARS = int(os.getenv("REVENUE_FETCH_YEARS", "6"))
KEEP_MONTHS = int(os.getenv("REVENUE_KEEP_MONTHS", "36"))


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


def _find_header_row(vals: List[List[str]], max_scan: int = 40) -> int:
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
        if any((c in hh) or (hh in c) for c in candidates):
            return i
    return -1


def read_watchlist_symbols(ws_watch) -> List[str]:
    vals = ws_watch.get_all_values()
    if len(vals) < 2:
        raise RuntimeError("WATCHLIST 沒有資料")

    hrow = _find_header_row(vals, max_scan=40)
    header = vals[hrow]
    rows = vals[hrow + 1:]

    idx_symbol = _col_index(header, "symbol")
    if idx_symbol < 0:
        raise RuntimeError("WATCHLIST 找不到 Symbol/股票代碼/代號 欄位（表頭可能不在前 40 列）")

    out = []
    for r in rows:
        if len(r) <= idx_symbol:
            continue
        sym = str(r[idx_symbol]).strip()
        if not sym:
            continue
        if sym.endswith(".0") and sym.replace(".", "", 1).isdigit():
            sym = sym[:-2]
        out.append(sym)

    # unique keep order
    seen = set()
    uniq = []
    for s in out:
        if s in seen:
            continue
        seen.add(s)
        uniq.append(s)
    return uniq


def finmind_month_revenue(symbol: str, start_date: str) -> pd.DataFrame:
    """
    Month 一律以「營收月份」口徑：
    - 優先 revenue_year + revenue_month（若有且有效）
    - 否則用 date（公告/資料日），並回推 1 個月當營收月
    """
    if not FINMIND_TOKEN:
        raise RuntimeError("FINMIND_TOKEN 未設定（請放到 GitHub Secrets: FINMIND_TOKEN）")

    headers = {"Authorization": f"Bearer {FINMIND_TOKEN}"}
    params = {
        "dataset": "TaiwanStockMonthRevenue",
        "data_id": symbol,
        "start_date": start_date,
    }
    resp = requests.get(FINMIND_URL, headers=headers, params=params, timeout=30)
    resp.raise_for_status()
    js = resp.json()

    data = js.get("data", [])
    if not data:
        return pd.DataFrame()

    df = pd.DataFrame(data)
    if "revenue" not in df.columns:
        return pd.DataFrame()

    # --- Month 生成 ---
    used_year_month = False
    if "revenue_year" in df.columns and "revenue_month" in df.columns:
        y = pd.to_numeric(df["revenue_year"], errors="coerce")
        m = pd.to_numeric(df["revenue_month"], errors="coerce")
        ok = y.notna() & m.notna()
        if ok.any():
            df = df[ok].copy()
            df["Month"] = (
                y[ok].astype(int).astype(str)
                + "-"
                + m[ok].astype(int).astype(str).str.zfill(2)
            )
            used_year_month = True

    if not used_year_month:
        if "date" not in df.columns:
            return pd.DataFrame()
        d = pd.to_datetime(df["date"], errors="coerce")
        ok = d.notna()
        df = df[ok].copy()
        d = d[ok]
        # 公告月 -> 營收月
        df["Month"] = (d.dt.to_period("M") - 1).astype(str)

    df["Revenue"] = pd.to_numeric(df["revenue"], errors="coerce").fillna(0).astype("int64")
    df["Symbol"] = str(symbol)

    df = df[["Month", "Symbol", "Revenue"]].drop_duplicates(subset=["Month", "Symbol"])
    df = df.sort_values(["Symbol", "Month"]).reset_index(drop=True)
    return df


def compute_yoy_fields(df: pd.DataFrame) -> pd.DataFrame:
    """
    df: Month(YYYY-MM), Symbol, Revenue（同一 Symbol）
    """
    if df.empty:
        return df

    df = df.sort_values("Month").reset_index(drop=True)
    df["Period"] = pd.PeriodIndex(df["Month"], freq="M")
    rev_map = dict(zip(df["Period"], df["Revenue"]))

    yoy, yoy3m = [], []
    for p, rev in zip(df["Period"], df["Revenue"]):
        base = rev_map.get(p - 12, None)
        if base is None or base == 0:
            yoy.append("")
        else:
            yoy.append(float(rev) / float(base) - 1.0)

        cur3 = [rev_map.get(p - k, None) for k in (0, 1, 2)]
        pre3 = [rev_map.get((p - 12) - k, None) for k in (0, 1, 2)]
        if any(v is None for v in cur3) or any(v is None for v in pre3):
            yoy3m.append("")
        else:
            s_cur, s_pre = sum(cur3), sum(pre3)
            if s_pre == 0:
                yoy3m.append("")
            else:
                yoy3m.append(float(s_cur) / float(s_pre) - 1.0)

    df["YoY"] = yoy
    df["YoY3M"] = yoy3m
    return df.drop(columns=["Period"])


def main():
    sheet_url = os.environ["SHEET_URL"]
    gc = get_client()
    sh = gc.open_by_url(sheet_url)

    ws_watch = sh.worksheet(SHEET_WATCHLIST)
    ws_rev = sh.worksheet(SHEET_REVENUE)

    symbols = read_watchlist_symbols(ws_watch)
    print("[DBG] WATCHLIST symbols:", symbols)

    today = dt.date.today()
    start_date = (today.replace(day=1) - dt.timedelta(days=365 * FETCH_YEARS)).strftime("%Y-%m-%d")

    # ✅ 可用營收月份：本月 - 1（避免未公告月份提前寫入）
    latest_ok = (pd.Timestamp.today().to_period("M") - 1).strftime("%Y-%m")

    all_rows = []

    for sym in symbols:
        df = finmind_month_revenue(sym, start_date=start_date)
        if df.empty:
            print(f"[WARN] {sym} no revenue data")
            continue

        # 不寫入未公告月份
        df = df[df["Month"] <= latest_ok].copy()
        if df.empty:
            continue

        df = compute_yoy_fields(df)

        # 只留近 KEEP_MONTHS
        df = df.sort_values("Month").tail(KEEP_MONTHS).reset_index(drop=True)

        # 組成要寫入的 rows
        for _, r in df.iterrows():
            yoy = "" if r["YoY"] == "" else float(r["YoY"])
            yoy3m = "" if r["YoY3M"] == "" else float(r["YoY3M"])
            all_rows.append([str(r["Month"]), str(r["Symbol"]), int(r["Revenue"]), yoy, yoy3m])

    # 全表排序：先 Month 再 Symbol
    all_rows.sort(key=lambda x: (x[0], x[1]))

    # ✅ 重建 REVENUE：清掉舊錯月份列（例如 2026-01 其實是 2025-12）
    ws_rev.clear()
    ws_rev.update("A1:E1", [REVENUE_HEADERS])
    if all_rows:
        ws_rev.append_rows(all_rows, value_input_option="USER_ENTERED")

    print(f"[DONE] rebuilt rows={len(all_rows)} latest_ok={latest_ok}")


if __name__ == "__main__":
    main()

if __name__ == "__main__":
    main()

