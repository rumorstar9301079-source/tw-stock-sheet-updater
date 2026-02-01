import os
import json
import datetime as dt
from typing import List

import pandas as pd
import requests
import gspread
from google.oauth2.service_account import Credentials


# ✅ 來源表改成 WS_SOURCE_SHEET（避免被 WS_WATCHLIST 覆蓋回 WATCHLIST）
SHEET_SOURCE = os.getenv("WS_SOURCE_SHEET", "SECTOR_MAP_MASTER_ALL_PLUS").strip()
SHEET_REVENUE = os.getenv("WS_REVENUE", "REVENUE").strip()

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


def _norm_symbol(sym: str) -> str:
    s = str(sym or "").strip().upper()
    s = s.replace(".TW", "").replace(".TWO", "").replace(".TPE", "")
    if s.endswith(".0") and s.replace(".", "", 1).isdigit():
        s = s[:-2]
    return s


def _open_ws_by_title_strip(sh, title: str):
    target = (title or "").strip()
    for ws in sh.worksheets():
        if ws.title.strip() == target:
            return ws
    raise gspread.exceptions.WorksheetNotFound(f"Worksheet not found: {title}")


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


def read_source_symbols(ws_source) -> List[str]:
    """
    ✅ 從 SECTOR_MAP_MASTER_ALL_PLUS 讀 Symbol 清單（去重保序）
    """
    vals = ws_source.get_all_values()
    if len(vals) < 2:
        raise RuntimeError(f"{ws_source.title} 沒有資料")

    hrow = _find_header_row(vals, max_scan=60)
    header = vals[hrow]
    rows = vals[hrow + 1:]

    idx_symbol = _col_index(header, "symbol")
    if idx_symbol < 0:
        raise RuntimeError(f"{ws_source.title} 找不到 Symbol 欄（表頭可能不在前 60 列）")

    out: List[str] = []
    for r in rows:
        if len(r) <= idx_symbol:
            continue
        sym = _norm_symbol(r[idx_symbol])
        if not sym:
            continue
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
    取 FinMind 月營收資料，並把 Month 統一成「營收月份」口徑：
    - 優先用 revenue_year + revenue_month（若有且有效）
    - 否則用 date（公告/資料日），回推 1 個月當營收月

    ✅ 任何權限/付費/限流/伺服器錯誤：不 raise，直接回空 df（避免 workflow 掛掉）
    """
    symbol = _norm_symbol(symbol)

    if not FINMIND_TOKEN:
        print("[WARN] FINMIND_TOKEN is empty; skip revenue fetch")
        return pd.DataFrame()

    headers = {"Authorization": f"Bearer {FINMIND_TOKEN}"}
    params = {
        "dataset": "TaiwanStockMonthRevenue",
        "data_id": symbol,
        "start_date": start_date,
    }

    try:
        resp = requests.get(FINMIND_URL, headers=headers, params=params, timeout=30)
    except Exception as e:
        print(f"[WARN] FinMind request exception: {symbol} | {e}")
        return pd.DataFrame()

    # ✅ 常見非 200：不要炸 pipeline
    if resp.status_code in (401, 402, 403):
        # 401: token 無效/過期
        # 402: Payment Required（方案/權限/資料集限制）
        # 403: Forbidden（權限不足）
        msg = (resp.text or "")[:200].replace("\n", " ")
        print(f"[WARN] FinMind HTTP {resp.status_code}: {symbol} | {msg}")
        return pd.DataFrame()

    if resp.status_code == 429:
        msg = (resp.text or "")[:200].replace("\n", " ")
        print(f"[WARN] FinMind 429 rate limited: {symbol} | {msg}")
        return pd.DataFrame()

    if resp.status_code >= 500:
        msg = (resp.text or "")[:200].replace("\n", " ")
        print(f"[WARN] FinMind {resp.status_code} server error: {symbol} | {msg}")
        return pd.DataFrame()

    try:
        resp.raise_for_status()
    except Exception as e:
        msg = (resp.text or "")[:200].replace("\n", " ")
        print(f"[WARN] FinMind HTTP error: {symbol} | {e} | {msg}")
        return pd.DataFrame()

    try:
        js = resp.json()
    except Exception as e:
        msg = (resp.text or "")[:200].replace("\n", " ")
        print(f"[WARN] FinMind JSON parse failed: {symbol} | {e} | {msg}")
        return pd.DataFrame()

    data = js.get("data", [])
    if not data:
        return pd.DataFrame()

    df = pd.DataFrame(data)
    if "revenue" not in df.columns:
        return pd.DataFrame()

    # --- Month 生成：營收月份口徑 ---
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

    # --- Revenue 整理 ---
    df["Revenue"] = pd.to_numeric(df["revenue"], errors="coerce").fillna(0).astype("int64")
    df["Symbol"] = symbol

    df = df[["Month", "Symbol", "Revenue"]].drop_duplicates(subset=["Month", "Symbol"])
    df = df.sort_values(["Symbol", "Month"]).reset_index(drop=True)
    return df



def compute_yoy_fields(df: pd.DataFrame) -> pd.DataFrame:
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

    # ✅ 印出所有工作表，避免表名有空白/不一致
    all_titles = [ws.title for ws in sh.worksheets()]
    print("[DBG] worksheets:", all_titles)
    print("[DBG] SHEET_SOURCE=", SHEET_SOURCE, "SHEET_REVENUE=", SHEET_REVENUE)

    ws_source = _open_ws_by_title_strip(sh, SHEET_SOURCE)
    ws_rev = _open_ws_by_title_strip(sh, SHEET_REVENUE)

    symbols = read_source_symbols(ws_source)
    print(f"[DBG] source_symbols={len(symbols)} first_20={symbols[:20]}")

    today = dt.date.today()
    start_date = (today.replace(day=1) - dt.timedelta(days=365 * FETCH_YEARS)).strftime("%Y-%m-%d")

    latest_ok = (pd.Timestamp.today().to_period("M") - 1).strftime("%Y-%m")

    all_rows = []

    for sym in symbols:
        df = finmind_month_revenue(sym, start_date=start_date)
        if df.empty:
            print(f"[WARN] {sym} no revenue data")
            continue

        df = df[df["Month"] <= latest_ok].copy()
        if df.empty:
            continue

        df = compute_yoy_fields(df)
        df = df.sort_values("Month").tail(KEEP_MONTHS).reset_index(drop=True)

        for _, r in df.iterrows():
            yoy = "" if r["YoY"] == "" else float(r["YoY"])
            yoy3m = "" if r["YoY3M"] == "" else float(r["YoY3M"])
            all_rows.append([str(r["Month"]), str(r["Symbol"]), int(r["Revenue"]), yoy, yoy3m])

    all_rows.sort(key=lambda x: (x[0], x[1]))

    ws_rev.clear()
    ws_rev.update("A1:E1", [REVENUE_HEADERS])
    if all_rows:
        ws_rev.append_rows(all_rows, value_input_option="USER_ENTERED")

    print(f"[DONE] rebuilt rows={len(all_rows)} latest_ok={latest_ok}")


if __name__ == "__main__":
    main()

