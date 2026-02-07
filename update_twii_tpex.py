import os
import json
from io import StringIO

import pandas as pd
import requests
import gspread
from google.oauth2.service_account import Credentials


# ===== Settings =====
SHEET_URL = os.environ["SHEET_URL"]
SA_JSON = os.environ["GOOGLE_SERVICE_ACCOUNT_JSON"]

TAB_NAME = os.getenv("TWII_TPEX_SHEET", "TWII/TPEX")
LOOKBACK = int(os.getenv("REGIME_LOOKBACK_DAYS", "120"))

TAIEX_CSV_URL = os.getenv("TAIEX_CSV_URL", "https://stooq.com/q/d/l/?s=twii&i=d")
OTC_CSV_URL = os.getenv("OTC_CSV_URL", "https://stooq.com/q/d/l/?s=tpex&i=d")


def get_client():
    info = json.loads(SA_JSON)
    scopes = [
        "https://www.googleapis.com/auth/spreadsheets",
        "https://www.googleapis.com/auth/drive",
    ]
    creds = Credentials.from_service_account_info(info, scopes=scopes)
    return gspread.authorize(creds)


def fetch_stooq_series(url: str, out_col: str) -> pd.DataFrame:
    """
    stooq daily CSV: Date,Open,High,Low,Close,Volume
    returns: DATE (YYYY-MM-DD), <out_col> (float)
    """
    r = requests.get(url, timeout=30, headers={"User-Agent": "Mozilla/5.0"})
    r.raise_for_status()
    txt = (r.text or "").strip()

    if not txt.startswith("Date,"):
        raise RuntimeError(f"Unexpected (not CSV) from {url}: {txt[:200]}")

    df = pd.read_csv(StringIO(txt))
    if "Date" not in df.columns or "Close" not in df.columns:
        raise RuntimeError(f"Missing Date/Close in stooq CSV: {url}")

    df["Date"] = pd.to_datetime(df["Date"], errors="coerce")
    df["Close"] = pd.to_numeric(df["Close"], errors="coerce")
    df = df.dropna(subset=["Date", "Close"]).sort_values("Date")
    df["DATE"] = df["Date"].dt.strftime("%Y-%m-%d")
    df[out_col] = df["Close"].astype(float)
    return df[["DATE", out_col]]


def ensure_worksheet(ss, title: str):
    try:
        return ss.worksheet(title)
    except gspread.WorksheetNotFound:
        return ss.add_worksheet(title=title, rows=1000, cols=10)


def write_df(ws, df: pd.DataFrame):
    ws.clear()
    values = [df.columns.tolist()] + df.values.tolist()
    ws.update(values, value_input_option="RAW")


def main():
    twii = fetch_stooq_series(TAIEX_CSV_URL, "TWII")
    tpex = fetch_stooq_series(OTC_CSV_URL, "TPEX")

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
