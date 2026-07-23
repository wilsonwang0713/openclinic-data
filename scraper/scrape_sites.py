#!/usr/bin/env python3
"""捐血點爬蟲 → donation_sites.json(可選;固定點已入 institutions.sqlite,此檔為動態備用)。

抓 `/xcevent?Display=Y` 頁面內嵌的 `var Data = [...]` JSON(含 WGS84 座標),
正規化為 donation_sites.json(全類別 station/mobile/drive)。

固定點(EventType=1)由 data-pipeline 併進 institutions.sqlite 隨版打包(CONTRACTS §6D);
本檔提供每日動態(巡迴車/活動)與固定點的備援快照,app 端目前不強制使用。

keep-last-good:失敗不覆寫、非零碼結束(同 scrape_stock.py)。

用法:
    python scrape_sites.py --out donation_sites.json
    python scrape_sites.py --from-file fixtures/xcevent_display.html --out /tmp/sites.json
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import blood_parser as bp
from scrape_stock import USER_AGENT

XCEVENT_URL = "https://www.blood.org.tw/xcevent?Display=Y"
HTTP_TIMEOUT = 30


def fetch_xcevent(url: str = XCEVENT_URL) -> str:
    import requests  # 延遲匯入

    response = requests.get(url, headers={"User-Agent": USER_AGENT}, timeout=HTTP_TIMEOUT)
    response.raise_for_status()
    response.encoding = "utf-8"
    return response.text


def scrape(page_html: str, fetched_at: datetime) -> dict:
    """純函式:HTML → donation_sites.json dict。"""
    items = bp.extract_var_data(page_html)
    date = fetched_at.astimezone(bp.TAIPEI_TZ).strftime("%Y-%m-%d")
    return bp.build_sites_document(items, date=date, fetched_at=fetched_at)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="台灣血液基金會捐血點爬蟲(donation_sites.json)")
    parser.add_argument("--out", default="donation_sites.json", help="輸出 JSON 路徑")
    parser.add_argument("--from-file", help="改讀本地 HTML(離線測試/重跑),不連網")
    args = parser.parse_args(argv)

    fetched_at = datetime.now(timezone.utc)
    try:
        if args.from_file:
            page_html = Path(args.from_file).read_text(encoding="utf-8")
        else:
            page_html = fetch_xcevent()
        document = scrape(page_html, fetched_at)
    except bp.StockParseError as exc:
        print(f"[blood-scraper] 解析失敗(疑似改版),保留上一版 {args.out}:{exc}", file=sys.stderr)
        return 1
    except Exception as exc:  # noqa: BLE001
        print(f"[blood-scraper] 抓取失敗,保留上一版 {args.out}:{exc}", file=sys.stderr)
        return 1

    Path(args.out).write_text(
        json.dumps(document, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(f"[blood-scraper] 已更新 {args.out}:{len(document['events'])} 個捐血點/活動")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
