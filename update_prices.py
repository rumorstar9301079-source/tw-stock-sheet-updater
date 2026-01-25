import os, json, time, datetime as dt
import pandas as pd
import yfinance as yf
import gspread
from google.oauth2.service_account import Credentials

WS_SYMBOLS = "SYMBOLS"
WS_PRICES = "PRICES"
LOOKBACK_CAL_DAYS = 30
SLEEP_SEC = 0.2

def get_client():
    info = json.loads(os.environ["GOOGLE_SERVICE_ACCOUNT_JSON"])
    scopes = [
        "https://www.googleapis.com/auth/spreadsheets",
        "https://www.googleapis.com/auth/drive",
    ]
    creds = Credentials.from_service_account_info(info, scopes=scopes)
    return gspread.authorize(creds)

def fetch_latest_yf(symbol: str, market: str):
    suffix = ".TW" if market == "tse" else ".TWO"
    ticker = f"{symbol}{suffix}"

    end = dt.datetime.utcnow()
    start = end - dt.timedelta(days=LOOKBACK_CAL_DAYS)

    df = yf.download(
        ticker,
        start=start.strftime("%Y-%m-%d"),
        end=(end + dt.timedelta(days=1)).strftime("%Y-%m-%d"),
        interval="1d",
        progress=False,
        auto_adjust=False,
        threads=False,
        group_by="column",   # ✅ 避免回傳奇怪的分組格式
    )
    if df is None or df.empty:
        return None

    # ✅ 如果是 MultiIndex 欄位（有兩層），把它壓扁成單層
    if isinstance(df.columns, pd.MultiIndex):
        # 常見格式：('Close','2330.TW') -> 'Close'
        df.columns = [c[0] for c in df.columns]

    last_row = df.tail(1)

    # 取日期
    d = last_row.index[0].strftime("%Y-%m-%d")

    # ✅ 保證取到單一值
    close = float(last_row["Close"].values[0])
    vol_shares = float(last_row["Volume"].values[0])

    # 上櫃(otc)寫回「張」；上市(tse)維持「股」
    vol_out = int(round(vol_shares / 1000.0)) if market == "otc" else vol_shares
    return d, close, vol_out


def main():
    sheet_url = os.environ["SHEET_URL"]
    gc = get_client()
    sh = gc.open_by_url(sheet_url)

    ws_sym = sh.worksheet(WS_SYMBOLS)
    vals = ws_sym.get_all_values()
    df = pd.DataFrame(vals[1:], columns=[c.strip().lower() for c in vals[0]])

    df = df[df["active"] != "0"].copy()
    df["symbol"] = df["symbol"].astype(str).str.strip()
    df["market"] = df["market"].astype(str).str.lower().str.strip()
    df = df[df["market"].isin(["tse", "otc"])].copy()
    if df.empty:
        raise RuntimeError("SYMBOLS 沒有可用股票（tse/otc & Active!=0）")

    ws_p = sh.worksheet(WS_PRICES)
    pvals = ws_p.get_all_values()
    exist = set()
    if pvals and len(pvals) > 1:
        header = [h.strip().lower() for h in pvals[0]]
        dfp = pd.DataFrame(pvals[1:], columns=header)
        if "date" in dfp.columns and "symbol" in dfp.columns:
            exist = set(zip(dfp["date"].astype(str), dfp["symbol"].astype(str)))

    rows = []
    for _, r in df.iterrows():
        sym = r["symbol"]
        market = r["market"]

        res = fetch_latest_yf(sym, market)
        if not res:
            print(f"[WARN] {sym}({market}) no data")
            time.sleep(SLEEP_SEC)
            continue

        d, close, vol = res
        if (d, sym) in exist:
            print(f"[SKIP] {sym} {d} exists")
            time.sleep(SLEEP_SEC)
            continue

        rows.append([d, sym, close, vol])
        unit = "張" if market == "otc" else "股"
        print(f"[OK] {sym}({market}) {d} close={close} vol({unit})={vol}")
        time.sleep(SLEEP_SEC)

    if rows:
        ws_p.append_rows(rows, value_input_option="USER_ENTERED")
        print(f"[DONE] appended {len(rows)} rows")
    else:
        print("[DONE] nothing to append")

if __name__ == "__main__":
    main()
