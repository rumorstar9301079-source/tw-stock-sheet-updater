import os
import json
from io import StringIO
from typing import Optional

import pandas as pd
import requests
import gspread
from google.oauth2.service_account import Credentials


SHEET_NAME = os.getenv("TWII_TPEX_SHEET", "TWII/TPEX")
LOOKBACK = int(os.getenv("REGIME_LOOKBACK_DAYS", "120"))

# stooq daily CSV
STOOQ_TWII = os.getenv("TAIEX_CSV_URL", "https://stooq.com/q/d/l/?s=twii&i=d")
STOOQ_TPEX = os.getenv("OTC_CSV_URL", "https://stooq.com/q/d/l/?s=tpex&i=d")

SPREADSHEET_ID = os.environ["SPREADSHEET_ID"]
SA_JSON = os.environ["GOOGLE_SERVICE_ACCOUNT_JSON"]


def get_client():
    info = json.loads(SA_JSON)
    scopes = [
        "https://www.googleapis.com/auth/spreadsheets",
        "https://www.googleapis.com/auth/drive",
    ]
    creds = Credentials.from_service_account_info(info, scopes=scopes)
    return gspread.authorize(creds)


def fetch_stooq_close(url: str, col_name: str) -> pd.DataFrame:
    """
    Return DataFrame: Date (YYYY-MM-DD), <col_name> (float)
    stooq columns: Date,Open,High,Low,Close,Volume
    """
    r = requests.get(url, timeout=30, headers={"User-Agent": "Mozilla/5.0"})
    r.raise_for_status()
    txt = r.text.strip()
    if not txt.startswith("Date,"):
        raise RuntimeError(f"Unexpected response (not CSV) from {url}: {txt[:120]}")

    df = pd.read_csv(StringIO(txt))
    if "Date" not in df.columns or "Close" not in df.columns:
        raise RuntimeError(f"Missing Date/Close columns from {url}")

    df["Date"] = pd.to_datetime(df["Date"]).dt.strftime("%Y-%m-%d")
    df["Close"] = pd.to_numeric(df["Close"], errors="coerce")
    df = df.dropna(subset=["Close"])
    df = df.sort_values("Date")
    df = df[["Date", "Close"]].rename(columns={"Close": col_name})
    return df


def ensure_sheet(ss, title: str):
    try:
        return ss.worksheet(title)
    except gspread.WorksheetNotFound:
        return ss.add_worksheet(title=title, rows=1000, cols=10)


def write_df_to_sheet(ws, df: pd.DataFrame):
    # clear & write
    ws.clear()
    values = [df.columns.tolist()] + df.values.tolist()
    ws.update(values, value_input_option="RAW")


def main():
    # 1) Fetch both series
    twii = fetch_stooq_close(STOOQ_TWII, "TWII")
    tpex = fetch_stooq_close(STOOQ_TPEX, "TPEX")

    # 2) Merge on Date, keep last N rows (trading days)
    merged = pd.merge(twii, tpex, on="Date", how="inner").sort_values("Date")
    merged = merged.tail(LOOKBACK).copy()
    merged.rename(columns={"Date": "DATE"}, inplace=True)

    if merged.empty or len(merged) < min(60, LOOKBACK):
        raise RuntimeError(f"Not enough merged rows: {len(merged)} (need ~60+)")

    # 3) Write to Google Sheet tab
    gc = get_client()
    ss = gc.open_by_key(SPREADSHEET_ID)
    ws = ensure_sheet(ss, SHEET_NAME)
    write_df_to_sheet(ws, merged)

    print(f"✅ Updated {SHEET_NAME}: {len(merged)} rows, last={merged['DATE'].iloc[-1]}")


if __name__ == "__main__":
    main()
