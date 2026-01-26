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

FINMIND_TOKEN = os.getenv("FINMIND_TOKEN", "").strip()
FINMIND_URL = "https://api.finmindtrade.com/api/v4/data"

# 近 3 年要算 YoY / YoY3M，至少需要再多 12 個月當基期 + 2 個月湊 3M
# 這裡抓 4 年比較保險
FETCH_YEARS = int(os.getenv("REVENUE_FETCH_YEARS", "4"))
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
        # 去掉 2330.0 這種
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


def ensure_revenue_header(ws_rev) -> Dict[str, int]:
    vals = ws_rev.get_all_values()
    if not vals:
        ws_rev.append_row(REVENUE_HEADERS)
        return {h.lower(): i for i, h in enumerate(REVENUE_HEADERS)}

    header = [str(x).strip() for x in vals[0]]
    lower = [h.lower() for h in header]

    # 若表頭不完整，直接覆蓋 A1:E1（不新增欄）
    need = [h.lower() for h in REVENUE_HEADERS]
    if any(h not in lower for h in need):
        ws_rev.update("A1:E1", [REVENUE_HEADERS])
        return {h.lower(): i for i, h in enumerate(REVENUE_HEADERS)}

    return {h.lower(): i for i, h in enumerate(header)}


def build_revenue_index(ws_rev, hmap: Dict[str, int]) -> Tuple[Dict[Tuple[str, str], int], int]:
    vals = ws_rev.get_all_values()
    if not vals:
        return {}, 1

    i_month = hmap.get("month")
    i_sym = hmap.get("symbol")
    if i_month is None or i_sym is None:
        return {}, len(vals)

    idx: Dict[Tuple[str, str], int] = {}
    for row_no, r in enumerate(vals[1:], start=2):
        if len(r) <= max(i_month, i_sym):
            continue
        m = str(r[i_month]).strip()
        s = str(r[i_sym]).strip()
        if not m or not s:
            continue
        idx[(m, s)] = row_no
    return idx, len(vals)


def finmind_month_revenue(symbol: str, start_date: str) -> pd.DataFrame:
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
    # schema: date, stock_id, country, revenue, revenue_month, revenue_year :contentReference[oaicite:1]{index=1}
    if "date" not in df.columns or "revenue" not in df.columns:
        return pd.DataFrame()

    df["date"] = pd.to_datetime(df["date"], errors="coerce")
    df = df.dropna(subset=["date"])
    df["Month"] = df["date"].dt.strftime("%Y-%m")
    df["Revenue"] = pd.to_numeric(df["revenue"], errors="coerce").fillna(0).astype("int64")
    df["Symbol"] = str(symbol)

    df = df[["Month", "Symbol", "Revenue"]].drop_duplicates(subset=["Month", "Symbol"])
    df = df.sort_values(["Symbol", "Month"]).reset_index(drop=True)
    return df


def compute_yoy_fields(df: pd.DataFrame) -> pd.DataFrame:
    """
    df columns: Month, Symbol, Revenue (sorted by Month)
    YoY   = Revenue / Revenue(12M ago) - 1
    YoY3M = sum(Revenue last 3M) / sum(Revenue same 3M last year) - 1
    """
    if df.empty:
        return df

    # Month -> period index
    df["Period"] = pd.PeriodIndex(df["Month"], freq="M")

    # map period -> revenue
    rev_map = dict(zip(df["Period"], df["Revenue"]))

    yoy = []
    yoy3m = []
    for p, rev in zip(df["Period"], df["Revenue"]):
        p12 = p - 12
        base = rev_map.get(p12, None)
        if base is None or base == 0:
            yoy.append("")
        else:
            yoy.append(float(rev) / float(base) - 1.0)

        # 3M window: p, p-1, p-2 vs p-12, p-13, p-14
        cur3 = [rev_map.get(p - k, None) for k in (0, 1, 2)]
        pre3 = [rev_map.get((p - 12) - k, None) for k in (0, 1, 2)]
        if any(v is None for v in cur3) or any(v is None for v in pre3):
            yoy3m.append("")
        else:
            s_cur = sum(cur3)
            s_pre = sum(pre3)
            if s_pre == 0:
                yoy3m.append("")
            else:
                yoy3m.append(float(s_cur) / float(s_pre) - 1.0)

    df["YoY"] = yoy
    df["YoY3M"] = yoy3m
    df = df.drop(columns=["Period"])
    return df


def main():
    sheet_url = os.environ["SHEET_URL"]
    gc = get_client()
    sh = gc.open_by_url(sheet_url)

    ws_watch = sh.worksheet(SHEET_WATCHLIST)
    ws_rev = sh.worksheet(SHEET_REVENUE)

    # ensure header
    hmap = ensure_revenue_header(ws_rev)
    # 我們固定只用 A~E（不新增欄）
    # 重新對齊成標準表頭避免 index 對不到
    ws_rev.update("A1:E1", [REVENUE_HEADERS])
    hmap = {h.lower(): i for i, h in enumerate(REVENUE_HEADERS)}

    rev_index, _ = build_revenue_index(ws_rev, hmap)

    symbols = read_watchlist_symbols(ws_watch)
    print("[DBG] WATCHLIST symbols:", symbols)

    today = dt.date.today()
    start_date = (today.replace(day=1) - dt.timedelta(days=365 * FETCH_YEARS)).strftime("%Y-%m-%d")

    updates = []   # (rangeA1, [[...]])
    new_rows = []  # [[Month, Symbol, Revenue, YoY, YoY3M]]

    i_month = hmap["month"]
    i_sym = hmap["symbol"]
    i_rev = hmap["revenue"]
    i_yoy = hmap["yoy"]
    i_yoy3m = hmap["yoy3m"]

    for sym in symbols:
        df = finmind_month_revenue(sym, start_date=start_date)
        if df.empty:
            print(f"[WARN] {sym} no revenue data")
            continue

        df = compute_yoy_fields(df)

        # 只保留近 36 個月（但 YoY/YoY3M 已用更長歷史算完）
        df = df.sort_values("Month").tail(KEEP_MONTHS).reset_index(drop=True)

        for _, r in df.iterrows():
            month = str(r["Month"])
            symbol = str(r["Symbol"])
            revenue = int(r["Revenue"])

            yoy = r["YoY"]
            yoy3m = r["YoY3M"]

            # sheet values：空字串就留白；有值就寫數字
            v_yoy = "" if yoy == "" else float(yoy)
            v_yoy3m = "" if yoy3m == "" else float(yoy3m)

            key = (month, symbol)
            if key in rev_index:
                row_no = rev_index[key]
                # 更新 C~E（Revenue/YoY/YoY3M）
                c1 = gspread.utils.rowcol_to_a1(row_no, i_rev + 1)
                e1 = gspread.utils.rowcol_to_a1(row_no, i_yoy3m + 1)
                rng = f"{c1}:{e1}"
                updates.append((rng, [[revenue, v_yoy, v_yoy3m]]))
            else:
                new_rows.append([month, symbol, revenue, v_yoy, v_yoy3m])

    if updates:
        body = [{"range": rng, "values": vals} for rng, vals in updates]
        ws_rev.batch_update(body, value_input_option="USER_ENTERED")

    if new_rows:
        # 先排序再 append，讓表格好看
        new_rows.sort(key=lambda x: (x[0], x[1]))
        ws_rev.append_rows(new_rows, value_input_option="USER_ENTERED")

    print(f"[DONE] updated={len(updates)}, appended={len(new_rows)}")


if __name__ == "__main__":
    main()


