"""blood-scraper 離線 fixture 測試(zero-dep,stdlib unittest)。

以 fixtures/ 的離線 HTML 驗證純解析函式與防禦式斷言,不連網。
執行:  python3 -m unittest -v   (在 blood-scraper/ 目錄下)
"""
from __future__ import annotations

import json
import unittest
from datetime import datetime, timezone
from pathlib import Path

import blood_parser as bp
import scrape_sites
import scrape_stock

FIXTURES = Path(__file__).resolve().parent / "fixtures"
INDEX_HTML = (FIXTURES / "index_inventory.html").read_text(encoding="utf-8")
XCEVENT_HTML = (FIXTURES / "xcevent_display.html").read_text(encoding="utf-8")
FETCHED_AT = datetime(2026, 7, 22, 0, 35, 0, tzinfo=timezone.utc)


class UpdateTimeTests(unittest.TestCase):
    def test_parses_taipei_timestamp(self):
        updated = bp.parse_update_time(INDEX_HTML)
        self.assertEqual(updated.isoformat(), "2026-07-22T07:31:00+08:00")

    def test_missing_timestamp_raises(self):
        with self.assertRaises(bp.StockParseError):
            bp.parse_update_time("<html>no time here</html>")


class InventoryParseTests(unittest.TestCase):
    def setUp(self):
        self.centers = bp.parse_inventory(INDEX_HTML)

    def test_four_centers_in_fixed_order(self):
        ids = [c["id"] for c in self.centers]
        self.assertEqual(ids, ["taipei", "hsinchu", "taichung", "kaohsiung"])

    def test_real_values_2026_07_22(self):
        by_id = {c["id"]: c["stock"] for c in self.centers}
        # 台北四型皆偏低
        self.assertEqual(by_id["taipei"], {"A": "low", "B": "low", "O": "low", "AB": "low"})
        # 新竹 B 型正常、其餘偏低
        self.assertEqual(by_id["hsinchu"]["B"], "normal")
        self.assertEqual(by_id["hsinchu"]["A"], "low")
        # 台中 A/B/O 急缺、AB 偏低
        self.assertEqual(by_id["taichung"], {"A": "urgent", "B": "urgent", "O": "urgent", "AB": "low"})
        # 高雄 B 型正常
        self.assertEqual(by_id["kaohsiung"]["B"], "normal")

    def test_decoy_carousel_item1_ignored(self):
        # 頁首輪播也有 class="item1",不得混進中心清單
        self.assertEqual(len(self.centers), 4)

    def test_center_names_use_canonical_meta(self):
        names = {c["id"]: c["name"] for c in self.centers}
        self.assertEqual(names["kaohsiung"], "高雄捐血中心")


class DefensiveAssertionTests(unittest.TestCase):
    def test_no_inventory_section_raises(self):
        with self.assertRaises(bp.StockParseError):
            bp.parse_inventory("<html><body><p>維護中</p></body></html>")

    def test_missing_center_raises(self):
        # 拿掉高雄整塊 → 只剩 3 中心,應中止(<4 中心)
        cut = INDEX_HTML.split('<li><div class="item item4">')[0] + "</ul></section></body></html>"
        with self.assertRaises(bp.StockParseError):
            bp.parse_inventory(cut)

    def test_missing_blood_type_row_raises(self):
        # 移除台北 AB 那一列 → 該中心缺血型列,應中止
        broken = INDEX_HTML.replace(
            '<li><div class="text">AB</div><div class="icon"><img src="/img/StorageIcon002.svg" alt="偏低"></div></li>',
            "", 1,
        )
        with self.assertRaises(bp.StockParseError):
            bp.parse_inventory(broken)

    def test_unrecognized_icon_becomes_unknown(self):
        # alt 與檔名皆無法辨識 → 該格 unknown,但不整檔失敗(其餘中心照常)
        mutated = INDEX_HTML.replace(
            '<img src="/img/StorageIcon002.svg" alt="偏低">',
            '<img src="/img/StorageIcon999.svg" alt="施工中">', 1,
        )
        centers = bp.parse_inventory(mutated)
        self.assertEqual(centers[0]["stock"]["A"], "unknown")
        self.assertEqual(len(centers), 4)

    def test_icon_filename_fallback_when_alt_absent(self):
        # 移掉台北 A 的 alt,只靠 StorageIcon002 檔名 → low
        mutated = INDEX_HTML.replace('src="/img/StorageIcon002.svg" alt="偏低"',
                                     'src="/img/StorageIcon002.svg"', 1)
        centers = bp.parse_inventory(mutated)
        self.assertEqual(centers[0]["stock"]["A"], "low")


class StockDocumentTests(unittest.TestCase):
    def setUp(self):
        self.doc = scrape_stock.scrape(INDEX_HTML, FETCHED_AT)

    def test_contract_shape(self):
        # CONTRACTS §6D:updated + centers[].stock
        self.assertEqual(self.doc["updated"], "2026-07-22T07:31:00+08:00")
        self.assertEqual(len(self.doc["centers"]), 4)
        first = self.doc["centers"][0]
        self.assertEqual(set(first["stock"].keys()), {"A", "B", "O", "AB"})

    def test_stock_values_are_contract_enum(self):
        allowed = {"normal", "low", "urgent", "unknown"}
        for center in self.doc["centers"]:
            for level in center["stock"].values():
                self.assertIn(level, allowed)

    def test_json_roundtrip_serializable(self):
        text = json.dumps(self.doc, ensure_ascii=False)
        self.assertIn("台北捐血中心", text)

    def test_legend_thresholds_captured(self):
        self.assertEqual(self.doc["thresholds"]["normal"], "庫存量7日以上")


class SitesParseTests(unittest.TestCase):
    def setUp(self):
        self.doc = scrape_sites.scrape(XCEVENT_HTML, FETCHED_AT)

    def test_desc_with_bracket_semicolon_not_truncated(self):
        # 備註含 "];" 仍需完整 raw_decode(不被截斷)
        events = self.doc["events"]
        # fixture 4 筆:1 缺名稱者略過 → 3 筆
        self.assertEqual(len(events), 3)

    def test_event_type_and_center_mapping(self):
        by_name = {e["name"]: e for e in self.doc["events"]}
        self.assertEqual(by_name["東興捐血室"]["type"], "station")
        self.assertEqual(by_name["東興捐血室"]["center_id"], "kaohsiung")
        self.assertEqual(by_name["好事達獅會捐血活動"]["type"], "drive")
        self.assertEqual(by_name["好事達獅會捐血活動"]["center_id"], "taipei")

    def test_zero_coordinate_becomes_null(self):
        by_name = {e["name"]: e for e in self.doc["events"]}
        self.assertIsNone(by_name["台中巡迴捐血車"]["lat"])
        self.assertIsNone(by_name["台中巡迴捐血車"]["lng"])

    def test_valid_coordinate_kept(self):
        by_name = {e["name"]: e for e in self.doc["events"]}
        self.assertAlmostEqual(by_name["東興捐血室"]["lat"], 22.9997488, places=5)


if __name__ == "__main__":
    unittest.main(verbosity=2)
