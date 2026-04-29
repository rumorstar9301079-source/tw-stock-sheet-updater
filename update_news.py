import os
import re
import time
import json
import feedparser
import pandas as pd
import gspread
from datetime import datetime, timezone, timedelta
from google.oauth2.service_account import Credentials

SHEET_URL = os.environ["SHEET_URL"]
SERVICE_ACCOUNT_JSON = os.environ["GOOGLE_SERVICE_ACCOUNT_JSON"]

WS_SOURCE_SHEET = os.getenv("WS_SOURCE_SHEET", "SECTOR_MAP_MASTER_ALL_PLUS")
WS_NEWS = os.getenv("WS_NEWS", "NEWS")
NEWS_LOOKBACK_DAYS = int(os.getenv("NEWS_LOOKBACK_DAYS", "7"))

HEADERS = [
    "FetchedAt", "Symbol", "Name", "Sector", "SubSector",
    "Title", "Source", "PubDate", "Link", "Keywords", "Score"
]

SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive"
]


def auth_client():
    info = json.loads(SERVICE_ACCOUNT_JSON)
    creds = Credentials.from_service_account_info(info, scopes=SCOPES)
    return gspread.authorize(creds)


def get_ws(ss, title):
    try:
        return ss.worksheet(title)
    except gspread.WorksheetNotFound:
        return ss.add_worksheet(title=title, rows=1000, cols=len(HEADERS))


def read_source_symbols(ss):
    ws = ss.worksheet(WS_SOURCE_SHEET)
    values = ws.get_all_values()
    header = values[0]

    def idx(name):
        return header.index(name) if name in header else -1

    i_symbol = idx("Symbol")
    i_name = idx("Name")
    i_sector = idx("Sector")
    i_sub = idx("SubSector")

    rows = []
    for r in values[1:]:
        if i_symbol < 0 or i_symbol >= len(r):
            continue

        symbol = str(r[i_symbol]).strip()
        if not re.match(r"^\d{4}$", symbol):
            continue

        rows.append({
            "Symbol": symbol,
            "Name": r[i_name].strip() if i_name >= 0 and i_name < len(r) else "",
            "Sector": r[i_sector].strip() if i_sector >= 0 and i_sector < len(r) else "",
            "SubSector": r[i_sub].strip() if i_sub >= 0 and i_sub < len(r) else "",
        })

    seen = set()
    out = []
    for x in rows:
        if x["Symbol"] not in seen:
            out.append(x)
            seen.add(x["Symbol"])
    return out


def fetch_google_news(symbol, name):
    query = f"{symbol} {name} 股票"
    url = "https://news.google.com/rss/search?q=" + query + "&hl=zh-TW&gl=TW&ceid=TW:zh-Hant"

    feed = feedparser.parse(url)
    items = []

    for e in feed.entries[:5]:
        title = e.get("title", "")
        link = e.get("link", "")
        source = ""
        if "source" in e:
            source = e.source.get("title", "")

        pub = e.get("published", "")
        items.append({
            "Title": title,
            "Source": source,
            "PubDate": pub,
            "Link": link,
        })

    return items


def calc_score(title):
    keywords = []
    score = 0

    key_list = [
        "AI", "伺服器", "CPO", "光通訊", "記憶體", "DRAM", "NAND",
        "漲價", "轉強", "營收", "創高", "接單", "擴產",
        "台積電", "輝達", "NVIDIA", "ASIC", "BBU", "電力"
    ]

    for k in key_list:
        if k.lower() in title.lower():
            keywords.append(k)
            score += 1

    return ",".join(keywords), score


def main():
    gc = auth_client()
    ss = gc.open_by_url(SHEET_URL)

    symbols = read_source_symbols(ss)
    ws_news = get_ws(ss, WS_NEWS)

    fetched_at = datetime.now(timezone(timedelta(hours=8))).strftime("%Y-%m-%d %H:%M:%S")

    output = [HEADERS]
    seen = set()

    for i, stock in enumerate(symbols, 1):
        symbol = stock["Symbol"]
        name = stock["Name"]

        print(f"[{i}/{len(symbols)}] fetch news: {symbol} {name}")

        try:
            items = fetch_google_news(symbol, name)
        except Exception as e:
            print(f"ERROR {symbol}: {e}")
            continue

        for item in items:
            title = item["Title"]
            link = item["Link"]

            dedup_key = symbol + "|" + title
            if dedup_key in seen:
                continue
            seen.add(dedup_key)

            keywords, score = calc_score(title)

            output.append([
                fetched_at,
                symbol,
                name,
                stock["Sector"],
                stock["SubSector"],
                title,
                item["Source"],
                item["PubDate"],
                link,
                keywords,
                score
            ])

        time.sleep(0.2)

    ws_news.clear()
    ws_news.update(values=output, range_name="A1")

    print(f"NEWS updated: {len(output) - 1} rows")


if __name__ == "__main__":
    main()
