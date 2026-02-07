import os
import json
import datetime as dt
from typing import Optional, Dict, Any

import pandas as pd
import requests
import gspread
from google.oauth2.service_account import Credentials


# ===== Settings =====
SHEET_URL = os.environ["SHEET_URL"]
SA_JSON = os.environ["GOOGLE_SERVICE_ACCOUNT_JSON"]

TAB_NAME = os.getenv("TWII_TPEX_SHEET", "TWII/TPEX")
LOOKBACK = int(os.getenv("REGIME_LOOKBACK_DAYS", "120"))

FINMIND_TOKEN = os.getenv("FINMIND_TOKEN", "").strip()
FINMIND_URL = os.getenv("FINMIND_URL", "https://api.finmindtrade.com/api/v4/data").strip()

# FinMind 指數（報酬指數）代碼（FinMind 文件用這兩個）
TAIEX_ID = os.getenv("FINMIND_TAIEX_ID", "TAIEX").strip()
TPEX_ID = os.getenv("FINMIND_TPEX_ID", "TPEx").strip()


def get_client():
    info = json.loads(SA_JSON)
    scopes = [
        "https://www.googleapis.com/auth/spreadsheets",
        "https://www.googleapis.com/auth/drive",
    ]
    creds = Credentials.from_service_account_info(info, scopes=scopes)
    return gspread.authorize(creds)


def ensure_worksheet(ss, title: str):
    try:
        return ss.worksheet(title)
    except gspread.WorksheetNotFound:
        return ss.add_worksheet(title=title, rows=2000, cols=10)


def finmind_get(dataset: str, data_id: str, start_date: str, end_date: str) -> pd.DataFrame:
    """
    FinMind v4:
      GET /api/v4/data?dataset=...&data_id=...&start_date=...&end_date=...
      header Authorization: Bearer <token>
    """
    if not FINMIND_TOKEN:
        raise RuntimeError("Missing FINMIND_TOKEN env. Please set secrets.FINMIND_TOKEN and pass to this step.")

    headers = {"Authorization": f"Bearer {FINMIND_TOKEN}"}
    params = {
        "dataset": dataset,
        "data_id": data_id,
        "start_date": start_date,
        "end_date": end_date,
    }
    r = requests.get(FINMIND_URL, headers=headers, params=params, timeout=30)
    r.raise_for_status()
    j: Dict[str, Any] = r.json()

    # FinMind 通常有 status / msg / data
    data = j.get("data", [])
    if not data:
        raise RuntimeError(f"FinMind empty data: dataset={dataset} data_id={data_id} msg={j.get('msg')}")

    df = pd.DataFrame(data)
    return df


def fetch_index_series(data_id: str, out_col: str, start_date: str, end_date: str) -> pd.DataFrame:
    """
    dataset: TaiwanStockTotalReturnIndex
    columns (doc): price, stock_id, date
    """
    df = finmind_get("TaiwanStockTotalReturnIndex", data_id, start_date, end_date)

    # normalize
    if "date" not in df.columns or "price" not in df.columns:
        raise RuntimeError(f"Unexpected schema from FinMind for {data_id}: cols={list(df.columns)[:30]}")

    df["date"] = pd.to_datetime(df["date"], errors="coerce")
    df["price"] = pd.to_numeric(df["price"], errors="coerce")
    df = df.dropna(subset=["date", "price"]).sort_values("date")

    out = pd.DataFrame({
        "DATE": df["date"].dt.strftime("%Y-%m-%d"),
        out_col: df["price"].astype(float),
    })
    return out


def write_df(ws, df: pd.DataFrame):
    # 你這頁是專用指數表：直接清空重寫最乾淨
    ws.clear()
    values = [df.columns.tolist()] + df.values.tolist()
    ws.update(values, value_input_option="RAW")


def main():
    # 抓 LOOKBACK 的交易日：用較寬的日曆區間避免遇到假日
    end = dt.date.today()
    start = end - dt.timedelta(days=max(LOOKBACK * 2, 260))  # 120日回看，用2倍日曆緩衝
    start_date = start.strftime("%Y-%m-%d")
    end_date = end.strftime("%Y-%m-%d")

    twii = fetch_index_series(TAIEX_ID, "TWII", start_date, end_date)
    tpex = fetch_index_series(TPEX_ID, "TPEX", start_date, end_date)

    merged = pd.merge(twii, tpex, on="DATE", how="inner").sort_values("DATE")
    merged = merged.tail(LOOKBACK).reset_index(drop=True)

    if len(merged) < min(60, LOOKBACK):
        raise RuntimeError(f"Not enough merged rows: {len(merged)} (need ~60+).")

    gc = get_client()
    ss = gc.open_by_url(SHEET_URL)
    ws = ensure_worksheet(ss, TAB_NAME)
    write_df(ws, merged)

    print(f"✅ Updated tab '{TAB_NAME}' with {len(merged)} rows. Last={merged['DATE'].iloc[-1]}")


if __name__ == "__main__":
    main()
