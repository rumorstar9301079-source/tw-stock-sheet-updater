import os
import json
import datetime as dt
import time
from typing import List, Set

import pandas as pd
import requests
import gspread
from google.oauth2.service_account import Credentials


SHEET_SOURCE = os.getenv("WS_SOURCE_SHEET", "SECTOR_MAP_MASTER_ALL_PLUS").strip()
SHEET_REVENUE = os.getenv("WS_REVENUE", "REVENUE").strip()

REVENUE_HEADERS = ["Month", "Symbol", "Revenue", "YoY", "YoY3M"]

FINMIND_TOKEN = os.getenv("FINMIND_TOKEN", "").strip()
FINMIND_URL = "https://api.finmindtrade.com/api/v4/data"

# ✅ 首次重建才用較長資料，之後只抓最近幾個月
FETCH_YEARS = int(os.getenv("REVENUE_FETCH_YEARS", "2"))
KEEP_MONTHS = int(os.getenv("REVENUE_KEEP_MONTHS", "24"))
INCREMENTAL_FETCH_MONTHS = int(os.getenv("REVENUE_INCREMENTAL_FETCH_MONTHS", "6"))

# ✅ 預設加速；若 FinMind 429 再調慢
REQUEST_SLEEP_SEC = float(os.getenv("REVENUE_REQUEST_SLEEP_SEC", "0"))
REQUEST_PAUSE_EVERY = int(os.getenv("REVENUE_REQUEST_PAUSE_EVERY", "100"))
REQUEST_PAUSE_SEC = float(os.getenv("REVENUE_REQUEST_PAUSE_SEC", "0.5"))


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
        if sym:
            out.append(sym)

    seen = set()
    uniq = []
    for s in out:
        if s not in seen:
            seen.add(s)
            uniq.append(s)
    return uniq


def read_existing_revenue(ws_rev) -> pd.DataFrame:
    vals = ws_rev.get_all_values()
    if not vals:
        return pd.DataFrame(columns=REVENUE_HEADERS)

    header = vals[0]
    rows = vals[1:]
    if not rows:
        return pd.DataFrame(columns=REVENUE_HEADERS)

    # 補齊欄位數，避免 rows 比 header 短時 DataFrame 失敗
    fixed_rows = []
    for r in rows:
        rr = list(r[:len(header)])
        if len(rr) < len(header):
            rr += [""] * (len(header) - len(rr))
        fixed_rows.append(rr)

    df = pd.DataFrame(fixed_rows, columns=header)
    for col in REVENUE_HEADERS:
        if col not in df.columns:
            df[col] = ""

    df["Month"] = df["Month"].astype(str).str.strip()
    df["Symbol"] = df["Symbol"].map(_norm_symbol)
    df["Revenue"] = pd.to_numeric(df["Revenue"], errors="coerce")
    df["YoY"] = pd.to_numeric(df["YoY"], errors="coerce")
    df["YoY3M"] = pd.to_numeric(df["YoY3M"], errors="coerce")

    df = df[df["Month"] != ""].copy()
    df = df[df["Symbol"] != ""].copy()
    return df[REVENUE_HEADERS].copy()


def has_broken_revenue_rows(df: pd.DataFrame) -> bool:
    if df.empty:
        return False

    bad = df[
        df["Revenue"].isna() &
        (df["YoY"].notna() | df["YoY3M"].notna())
    ].copy()

    if not bad.empty:
        print(f"[WARN] broken revenue rows detected: {len(bad)}")
        print("[WARN] sample broken rows:")
        print(bad.head(10).to_dict("records"))
        return True
    return False


def finmind_month_revenue(session: requests.Session, symbol: str, start_date: str) -> pd.DataFrame:
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
        resp = session.get(FINMIND_URL, headers=headers, params=params, timeout=30)
    except Exception as e:
        print(f"[WARN] FinMind request exception: {symbol} | {e}")
        return pd.DataFrame()

    if resp.status_code in (401, 403, 429) or resp.status_code >= 500:
        msg = (resp.text or "")[:200].replace("\n", " ")
        print(f"[WARN] FinMind HTTP {resp.status_code}: {symbol} | {msg}")
        return pd.DataFrame()

    if resp.status_code == 402:
        msg = (resp.text or "")[:200].replace("\n", " ")
        print(f"[WARN] FinMind HTTP 402: {symbol} | {msg}")
        return pd.DataFrame()

    try:
        resp.raise_for_status()
        js = resp.json()
    except Exception as e:
        msg = (resp.text or "")[:200].replace("\n", " ")
        print(f"[WARN] FinMind parse/http failed: {symbol} | {e} | {msg}")
        return pd.DataFrame()

    data = js.get("data", [])
    if not data:
        return pd.DataFrame()

    df = pd.DataFrame(data)
    if "revenue" not in df.columns:
        return pd.DataFrame()

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
        df["Month"] = (d.dt.to_period("M") - 1).astype(str)

    df["Revenue"] = pd.to_numeric(df["revenue"], errors="coerce")
    df["Symbol"] = symbol

    df = df[["Month", "Symbol", "Revenue"]].dropna(subset=["Revenue"])
    df = df[df["Symbol"] != ""].copy()
    df["Revenue"] = df["Revenue"].astype("int64")
    df = df.drop_duplicates(subset=["Month", "Symbol"], keep="last")
    df = df.sort_values(["Symbol", "Month"]).reset_index(drop=True)
    return df


def compute_yoy_fields_fast(df: pd.DataFrame) -> pd.DataFrame:
    if df.empty:
        return df

    df = df.sort_values("Month").reset_index(drop=True).copy()
    df["Revenue"] = pd.to_numeric(df["Revenue"], errors="coerce")

    s = df["Revenue"]
    base_12 = s.shift(12)
    df["YoY"] = (s / base_12) - 1.0

    cur3 = s + s.shift(1) + s.shift(2)
    pre3 = s.shift(12) + s.shift(13) + s.shift(14)
    df["YoY3M"] = (cur3 / pre3) - 1.0

    df.loc[base_12.isna() | (base_12 == 0), "YoY"] = pd.NA
    df.loc[pre3.isna() | (pre3 == 0), "YoY3M"] = pd.NA

    return df


def month_n_months_ago(n: int) -> str:
    return (pd.Timestamp.today().to_period("M") - n).strftime("%Y-%m")


def ensure_header(ws_rev):
    vals = ws_rev.get_all_values()
    if not vals:
        ws_rev.update(values=[REVENUE_HEADERS], range_name="A1:E1")
        return

    header = vals[0]
    if header[:5] != REVENUE_HEADERS:
        ws_rev.clear()
        ws_rev.update(values=[REVENUE_HEADERS], range_name="A1:E1")


def write_full_revenue(ws_rev, df_all: pd.DataFrame):
    if df_all is None or df_all.empty:
        print("[WARN] write_full_revenue skipped because df_all is empty")
        return

    ws_rev.clear()
    ws_rev.update(values=[REVENUE_HEADERS], range_name="A1:E1")

    rows = []
    for _, r in df_all.iterrows():
        rows.append([
            str(r["Month"]),
            str(r["Symbol"]),
            int(r["Revenue"]) if pd.notna(r["Revenue"]) else "",
            "" if pd.isna(r["YoY"]) else float(r["YoY"]),
            "" if pd.isna(r["YoY3M"]) else float(r["YoY3M"]),
        ])

    if rows:
        ws_rev.append_rows(rows, value_input_option="USER_ENTERED")


def recompute_all_by_symbol(df: pd.DataFrame, keep_months: int) -> pd.DataFrame:
    if df.empty:
        return pd.DataFrame(columns=REVENUE_HEADERS)

    df = df[["Month", "Symbol", "Revenue"]].copy()
    df["Symbol"] = df["Symbol"].map(_norm_symbol)
    df["Revenue"] = pd.to_numeric(df["Revenue"], errors="coerce")
    df = df.dropna(subset=["Revenue"])
    df = df[df["Month"].astype(str).str.strip() != ""].copy()
    df = df[df["Symbol"] != ""].copy()
    df = df.drop_duplicates(subset=["Month", "Symbol"], keep="last")
    df = df.sort_values(["Symbol", "Month"]).reset_index(drop=True)

    parts = []
    for sym, g in df.groupby("Symbol", sort=False):
        gg = compute_yoy_fields_fast(g)
        gg = gg.tail(keep_months).copy()
        parts.append(gg)

    if not parts:
        return pd.DataFrame(columns=REVENUE_HEADERS)

    out = pd.concat(parts, ignore_index=True)
    out = out[REVENUE_HEADERS]
    out = out.sort_values(["Month", "Symbol"]).reset_index(drop=True)
    return out


def main():
    sheet_url = os.environ["SHEET_URL"]
    gc = get_client()
    sh = gc.open_by_url(sheet_url)

    ws_source = _open_ws_by_title_strip(sh, SHEET_SOURCE)
    ws_rev = _open_ws_by_title_strip(sh, SHEET_REVENUE)

    ensure_header(ws_rev)

    symbols = read_source_symbols(ws_source)
    symbol_set: Set[str] = set(symbols)
    existing = read_existing_revenue(ws_rev)
    existing = existing[existing["Symbol"].isin(symbol_set)].copy()

    latest_ok = (pd.Timestamp.today().to_period("M") - 1).strftime("%Y-%m")

    broken_existing = has_broken_revenue_rows(existing)
    is_first_build = existing.empty or broken_existing

    if is_first_build:
        if broken_existing:
            print("[MODE] force rebuild due to broken revenue rows")
        else:
            print("[MODE] first build")
        start_date = (dt.date.today().replace(day=1) - dt.timedelta(days=365 * FETCH_YEARS)).strftime("%Y-%m-%d")
    else:
        print("[MODE] incremental update: fetch recent months only, merge with existing sheet")
        start_date = (
            (pd.Timestamp.today().to_period("M") - INCREMENTAL_FETCH_MONTHS)
            .to_timestamp()
            .date()
            .strftime("%Y-%m-%d")
        )

    print(f"[DBG] source_symbols={len(symbols)} first_20={symbols[:20]}")
    print(f"[DBG] existing_rows={len(existing)} start_date={start_date} latest_ok={latest_ok}")

    session = requests.Session()
    new_parts = []
    empty_symbols = []

    for i, sym in enumerate(symbols, start=1):
        df_new = finmind_month_revenue(session, sym, start_date=start_date)

        if df_new.empty:
            empty_symbols.append(sym)
        else:
            df_new = df_new[df_new["Month"] <= latest_ok].copy()
            if not df_new.empty:
                new_parts.append(df_new)

        if REQUEST_SLEEP_SEC > 0:
            time.sleep(REQUEST_SLEEP_SEC)

        if REQUEST_PAUSE_EVERY > 0 and i % REQUEST_PAUSE_EVERY == 0:
            print(f"[DBG] fetched {i}/{len(symbols)} symbols ...")
            if REQUEST_PAUSE_SEC > 0:
                time.sleep(REQUEST_PAUSE_SEC)

    fetched = (
        pd.concat(new_parts, ignore_index=True)
        if new_parts else
        pd.DataFrame(columns=["Month", "Symbol", "Revenue"])
    )

    fetched = fetched[fetched["Symbol"].isin(symbol_set)].copy()
    fetched = fetched[fetched["Month"] <= latest_ok].copy()

    print(f"[DBG] fetched_watchlist_rows={len(fetched)} empty_symbols={len(empty_symbols)}")
    if empty_symbols:
        print(f"[DBG] empty_symbols_first_30={empty_symbols[:30]}")

    if fetched.empty:
        if is_first_build:
            print("[WARN] fetched is empty during first build; keep existing sheet unchanged")
        else:
            print("[DONE] no new revenue rows; keep existing sheet unchanged")
        return

    if is_first_build:
        final_df = recompute_all_by_symbol(fetched, KEEP_MONTHS)

        if final_df.empty:
            print("[WARN] final_df empty in rebuild; keep existing sheet unchanged")
            return

        write_full_revenue(ws_rev, final_df)
        print(f"[DONE] rebuild rows={len(final_df)} latest_ok={latest_ok}")
        return

    # ✅ 核心修改：只抓最近 6 個月，但 YoY / YoY3M 用「合併後完整資料」重新計算
    # 這樣不會因為只拿最近 6 個月，導致 YoY 基準月份不見。
    merged = pd.concat(
        [
            existing[["Month", "Symbol", "Revenue"]],
            fetched[["Month", "Symbol", "Revenue"]],
        ],
        ignore_index=True,
    )

    merged = merged.drop_duplicates(subset=["Month", "Symbol"], keep="last")
    merged = merged[merged["Symbol"].isin(symbol_set)].copy()
    merged = merged[merged["Month"] <= latest_ok].copy()

    final_df = recompute_all_by_symbol(merged, KEEP_MONTHS)

    if final_df.empty:
        print("[WARN] incremental final_df empty; keep existing sheet unchanged")
        return

    write_full_revenue(ws_rev, final_df)
    cutoff_month = month_n_months_ago(INCREMENTAL_FETCH_MONTHS)
    print(f"[DONE] incremental rows={len(final_df)} latest_ok={latest_ok} recent_fetch_from={cutoff_month}")


if __name__ == "__main__":
    main()
