#!/usr/bin/env python3
"""血庫存量爬蟲 → blood_stock.json(CONTRACTS §6D)。

抓官網首頁 → 解析 `.IndexInventory` → 產 blood_stock.json。
GitHub Actions 每日 cron 跑在 openclinic-data repo,commit 進 repo;app 讀 raw URL。

keep-last-good(research §4.4):解析失敗一律**不覆寫**上一版 JSON 並以非零碼結束,
讓 Actions 失敗告警;上一版 JSON 保留在 repo,app 端顯示最後更新時間(不 crash 不空白)。

用法:
    python scrape_stock.py --out blood_stock.json           # 線上抓取
    python scrape_stock.py --from-file fixtures/index_inventory.html --out /tmp/x.json  # 離線/驗證
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import blood_parser as bp

HOMEPAGE_URL = "https://www.blood.org.tw/"
# UA 標明專案與聯絡處(research §4.4 禮貌性:對方為公益基金會)
USER_AGENT = (
    "Mozilla/5.0 (compatible; openclinic-blood-scraper/1.0; "
    "+https://github.com/wilsonwang0713/openclinic-data)"
)
HTTP_TIMEOUT = 30


def fetch_homepage(url: str = HOMEPAGE_URL) -> str:
    """抓官網首頁 HTML;HTTP 錯誤 raise(由 main 轉 keep-last-good)。"""
    import requests  # 延遲匯入:純解析 / 測試不需 requests

    response = requests.get(url, headers={"User-Agent": USER_AGENT}, timeout=HTTP_TIMEOUT)
    response.raise_for_status()
    # 官網為 UTF-8;requests 對無明確 charset 的頁面預設 ISO-8859-1,強制 UTF-8
    response.encoding = "utf-8"
    return response.text


def scrape(page_html: str, fetched_at: datetime) -> dict:
    """純函式:HTML → blood_stock.json dict(結構不符會丟 StockParseError)。"""
    updated = bp.parse_update_time(page_html)
    centers = bp.parse_inventory(page_html)
    legend = bp.parse_legend(page_html)
    for warning in bp.legend_warnings(legend):
        print(f"[blood-scraper] 警告:{warning}", file=sys.stderr)
    return bp.build_stock_document(centers, updated=updated, fetched_at=fetched_at, legend=legend)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="台灣血液基金會血庫存量爬蟲")
    parser.add_argument("--out", default="blood_stock.json", help="輸出 JSON 路徑")
    parser.add_argument("--from-file", help="改讀本地 HTML(離線測試/重跑),不連網")
    args = parser.parse_args(argv)

    fetched_at = datetime.now(timezone.utc)
    try:
        if args.from_file:
            page_html = Path(args.from_file).read_text(encoding="utf-8")
        else:
            page_html = fetch_homepage()
        document = scrape(page_html, fetched_at)
    except bp.StockParseError as exc:
        print(f"[blood-scraper] 解析失敗(疑似改版),保留上一版 {args.out}:{exc}", file=sys.stderr)
        return 1
    except Exception as exc:  # noqa: BLE001 - 網路/IO 等一律 keep-last-good
        print(f"[blood-scraper] 抓取失敗,保留上一版 {args.out}:{exc}", file=sys.stderr)
        return 1

    out_path = Path(args.out)
    out_path.write_text(
        json.dumps(document, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    urgent = sum(
        1 for center in document["centers"] for level in center["stock"].values()
        if level == "urgent"
    )
    print(
        f"[blood-scraper] 已更新 {args.out}:來源更新時間 {document['updated']}、"
        f"{len(document['centers'])} 中心、急缺 {urgent} 項"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
