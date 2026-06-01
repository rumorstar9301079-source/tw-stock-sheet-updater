import os
import re
import time
import json
import feedparser
import gspread
from urllib.parse import urlencode
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
        return ss.add_worksheet(title=title, rows=2000, cols=len(HEADERS))


def find_header_row(values):
    for i, row in enumerate(values[:50]):
        normalized = [str(x).strip() for x in row]
        if "Symbol" in normalized and "Name" in normalized:
            return i, normalized
    raise Exception("找不到 Symbol / Name 表頭")


def col_idx(header, candidates):
    for c in candidates:
        if c in header:
            return header.index(c)
    return -1


def read_source_symbols(ss):
    ws = ss.worksheet(WS_SOURCE_SHEET)
    values = ws.get_all_values()

    header_row, header = find_header_row(values)

    i_symbol = col_idx(header, ["Symbol", "股票代號", "代號"])
    i_name = col_idx(header, ["Name", "股票名稱", "名稱"])
    i_sector = col_idx(header, ["Sector", "族群"])
    i_sub = col_idx(header, ["SubSector", "子族群"])

    rows = []

    for r in values[header_row + 1:]:
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

    params = urlencode({
        "q": query,
        "hl": "zh-TW",
        "gl": "TW",
        "ceid": "TW:zh-Hant",
    })

    url = "https://news.google.com/rss/search?" + params

    feed = feedparser.parse(url)

    items = []
    now = datetime.now(timezone.utc)

    for e in feed.entries:
        pub_time = None

        if "published_parsed" in e and e.published_parsed:
            pub_time = datetime(*e.published_parsed[:6], tzinfo=timezone.utc)

        if not pub_time:
            continue

        if (now - pub_time).days > NEWS_LOOKBACK_DAYS:
            continue

        title = e.get("title", "").strip()
        link = e.get("link", "").strip()

        if not title or not link:
            continue

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
        "AI", "伺服器", "CPO", "光通訊", "矽光子",
        "記憶體", "DRAM", "NAND", "HBM",
        "漲價", "報價", "轉強", "營收", "創高",
        "接單", "擴產", "法說", "展望",
        "台積電", "輝達", "NVIDIA", "ASIC",
        "BBU", "電力", "散熱", "低軌", "衛星",
        "機器人", "重電", "PCB", "CoWoS"
    ]

    title_lower = title.lower()

    for k in key_list:
        if k.lower() in title_lower:
            keywords.append(k)
            score += 1

    return ",".join(keywords), score



def _is_google_retryable_error(e):
    """判斷 Google Sheets API 是否為暫時性錯誤，可重試。"""
    msg = str(e)
    return any(code in msg for code in ["429", "500", "502", "503", "504"])


def safe_sheet_call(func, *args, retries=5, wait=10, action_name="Google Sheets API", **kwargs):
    """Google Sheets API 安全呼叫：遇到暫時性錯誤自動等待後重試。"""
    for i in range(retries):
        try:
            return func(*args, **kwargs)
        except gspread.exceptions.APIError as e:
            if _is_google_retryable_error(e) and i < retries - 1:
                print(f"{action_name} 暫時失敗，等待 {wait} 秒後重試... ({i + 1}/{retries}) | {e}")
                time.sleep(wait)
                continue
            raise


def main():
    gc = auth_client()
    ss = gc.open_by_url(SHEET_URL)

    symbols = read_source_symbols(ss)
    ws_news = get_ws(ss, WS_NEWS)

    fetched_at = datetime.now(
        timezone(timedelta(hours=8))
    ).strftime("%Y-%m-%d %H:%M:%S")

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

            dedup_key = symbol + "|" + link[:100]

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

    safe_sheet_call(ws_news.clear, action_name="NEWS clear")
    safe_sheet_call(ws_news.update, values=output, range_name="A1", action_name="NEWS update")

    print(f"NEWS updated: {len(output) - 1} rows")


if __name__ == "__main__":
    main()
