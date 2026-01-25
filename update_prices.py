import os, json, time, datetime as dt
import pandas as pd
import yfinance as yf
import gspread
from google.oauth2.service_account import Credentials

WS_SYMBOLS = "SYMBOLS"
WS_PRICES  = "PRICES"

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
    """
    回傳 (date_str, close, volume_out)
    - market=tse: volume_out = 股
    - market=otc: volume_out = 張 (股/1000)
    """
    suffix = ".TW" if market == "tse" else ".TWO"
    ticker = f"{symbol}{suffix}"

    def _normalize(df):
        if df is None or df.empty:
            return None

        # 可能出現 MultiIndex 欄位，把它壓扁
        if isinstance(df.columns, pd.MultiIndex):
            df.columns = [c[0] for c in df.columns]

        need = {"Close", "Volume"}
        if not need.issubset(set(df.columns)):
            return None

        last = df.tail(1)
        d = last.index[0].strftime("%Y-%m-%d")

        close = float(last["Close"].values[0])
        vol_shares = float(last["Volume"].values[0])

        vol_out = int(round(vol_shares / 1000.0)) if market == "otc" else vol_shares
        return d, close, vol_out

    # 1) 先用 download（快）
    end = dt.datetime.utcnow()
    start = end - dt.timedelta(days=LOOKBACK_CAL_DAYS)

    df1 = yf.download(
        ticker,
        start=start.strftime("%Y-%m-%d"),
        end=(end + dt.timedelta(days=1)).strftime("%Y-%m-%d"),
        interval="1d",
        progress=False,
        auto_adjust=False,
        threads=False,
        group_by="column",
    )
    res = _normalize(df1)
    if res:
        return res

    # 2) 備援：Ticker().history（對部分 OTC 更穩）
    df2 = yf.Ticker(ticker).history(period="1mo", interval="1d", auto_adjust=False)
    res = _normalize(df2)
    if res:
        return res

    return None


def main():
    sheet_url = os.environ["SHEET_URL"]
    gc = get_client()
    sh = gc.open_by_url(sheet_url)

    # 讀 SYMBOLS
    ws_sym = sh.worksheet(WS_SYMBOLS)
    vals = ws_sym.get_all_values()
    if not vals or len(vals) < 2:
        raise RuntimeError("SYMBOLS 工作表沒有資料")

    header = [c.strip().lower() for c in vals[0]]
    df = pd.DataFrame(vals[1:], columns=header)

    # 必要欄位檢查
    if "symbol" not in df.columns or "market" not in df.columns:
        raise RuntimeError("SYMBOLS 需要欄位：symbol, market（market=tse/otc）")

    # active 欄位可有可無：有就用，沒有就全算 active
    if "active" in df.columns:
        df = df[df["active"].astype(str).str.strip() != "0"].copy()

    df["symbol"] = df["symbol"].astype(str).str.strip()
    df["market"] = df["market"].astype(str).str.lower().str.strip()

    df = df[df["market"].isin(["tse", "otc"])].copy()
    if df.empty:
        raise RuntimeError("SYMBOLS 沒有可用股票（market 必須是 tse 或 otc）")

    # 讀 PRICES（避免重複寫入）
    ws_p = sh.worksheet(WS_PRICES)
    pvals = ws_p.get_all_values()

    exist = set()
    if pvals and len(pvals) > 1:
        pheader = [h.strip().lower() for h in pvals[0]]
        dfp = pd.DataFrame(pvals[1:], columns=pheader)
        if "date" in dfp.columns and "symbol" in dfp.columns:
            exist = set(zip(dfp["date"].astype(str), dfp["symbol"].astype(str)))

    rows = []
    for _, r in df.iterrows():
        sym = r["symbol"]
        market = r["market"]

        res = fetch_latest_yf(sym, market)
        if not res:
            print(f"[WARN] {sym}({market}) no data from Yahoo ticker={sym}{'.TW' if market=='tse' else '.TWO'}")
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
