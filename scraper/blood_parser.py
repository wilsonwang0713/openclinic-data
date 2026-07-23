"""台灣血液基金會血庫存量 / 捐血點解析(純函式,zero-dep,stdlib only)。

依 docs/blood-api-research.md 結論:
- 血庫存量:官網首頁 server-render 的 `.IndexInventory` 區塊(免 JS/token),
  以 `img` 的 `alt` 文字(正常/偏低/急缺)為主要載體,SVG 檔名(StorageIcon001~003)為備援。
- 捐血點:`/xcevent?Display=Y` 頁面內嵌 `var Data = [...]` JSON(含 WGS84 座標)。

本模組刻意只用標準函式庫(供 GitHub Actions 零相依執行,並可離線 fixture 測試);
網路抓取與檔案輸出分離在 scrape_stock.py / scrape_sites.py。

防禦式解析(research §4.4):結構不符一律丟 StockParseError,由呼叫端 keep-last-good、
不覆寫上一版 JSON 並以非零碼結束(讓 Actions 失敗告警)。
"""
from __future__ import annotations

import json
import re
from datetime import datetime, timedelta, timezone
from html.parser import HTMLParser

# 台北時區(來源時間戳與 updated 皆用 +08:00)
TAIPEI_TZ = timezone(timedelta(hours=8))

# 血庫存量三等級(CONTRACTS §6D:normal|low|urgent;無法辨識→unknown 不整檔失敗)
LEVEL_NORMAL = "normal"
LEVEL_LOW = "low"
LEVEL_URGENT = "urgent"
LEVEL_UNKNOWN = "unknown"

# alt 文字 → 等級(主要載體,比 class/檔名穩定)
LEVEL_BY_ALT = {"正常": LEVEL_NORMAL, "偏低": LEVEL_LOW, "急缺": LEVEL_URGENT}
# SVG 檔名 → 等級(alt 缺失時的備援:001=急缺、002=偏低、003=正常)
LEVEL_BY_ICON = {
    "StorageIcon001": LEVEL_URGENT,
    "StorageIcon002": LEVEL_LOW,
    "StorageIcon003": LEVEL_NORMAL,
}

# 血型顯示順序(schema 預留 8 鍵,現填 ABO 4 型;無 Rh 陰陽公開資料)
BLOOD_TYPES = ["A", "B", "O", "AB"]

# 中心名稱關鍵字 → 穩定 id(台南捐血中心已裁併入高雄 → 只 4 中心)
_CENTER_KEYWORDS = [
    ("台北", "taipei"), ("臺北", "taipei"),
    ("新竹", "hsinchu"),
    ("台中", "taichung"), ("臺中", "taichung"),
    ("高雄", "kaohsiung"),
]
# 4 中心的顯示中繼資料(coverage 供人工檢視;app 端 UI 目前只用 name)
CENTER_META = {
    "taipei": {"name": "台北捐血中心", "coverage": "基隆/雙北/宜蘭/花蓮/金馬",
               "url": "https://www.tp.blood.org.tw/"},
    "hsinchu": {"name": "新竹捐血中心", "coverage": "桃竹苗",
                "url": "https://www.sc.blood.org.tw/"},
    "taichung": {"name": "台中捐血中心", "coverage": "中彰投",
                 "url": "https://www.tc.blood.org.tw/"},
    "kaohsiung": {"name": "高雄捐血中心", "coverage": "雲嘉南/高屏/澎東",
                  "url": "https://www.ks.blood.org.tw/"},
}
# 期望的 4 中心 id 集合(缺一即視為改版,fail loud)
EXPECTED_CENTER_IDS = {"taipei", "hsinchu", "taichung", "kaohsiung"}

# 官網 legend 門檻文字(research §2:變更代表定義變動 → 軟性告警,不中止)
DEFAULT_THRESHOLDS = {
    "normal": "庫存量7日以上",
    "low": "庫存量4-7日",
    "urgent": "庫存量4日以下",
}

# EventType(捐血點)→ 類別;CenterId(數字)→ 中心 id(research §3/§4.3)
_EVENT_TYPE_NAME = {1: "station", 2: "mobile", 3: "drive"}
_CENTER_ID_BY_NUM = {"2": "taipei", "3": "hsinchu", "4": "taichung", "7": "kaohsiung"}

# 座標合理範圍(台澎金馬;0,0 或超界視為無座標)
_LAT_RANGE = (21.0, 26.5)
_LNG_RANGE = (117.0, 123.0)

_UPDATE_TIME_RE = re.compile(
    r"最新更新時間[：: ]*\s*(\d{4})年\s*(\d{1,2})月\s*(\d{1,2})日\s*(\d{1,2})時\s*(\d{1,2})分"
)
_ITEM_CLASS_RE = re.compile(r"^item\d+$")


class StockParseError(Exception):
    """血庫頁面結構不符(版面改版訊號);呼叫端據此 keep-last-good 並失敗告警。"""


def center_id_for(name: str) -> str | None:
    """由中心名稱關鍵字對應穩定 id;無法對應回 None(該候選會被丟棄)。"""
    for keyword, cid in _CENTER_KEYWORDS:
        if keyword in name:
            return cid
    return None


def parse_update_time(page_html: str) -> datetime:
    """解析「最新更新時間: YYYY年MM月DD日 HH時mm分」→ 帶 +08:00 的 datetime。

    找不到時間戳一律丟 StockParseError(改版 / 頁面異常)。
    """
    match = _UPDATE_TIME_RE.search(page_html)
    if not match:
        raise StockParseError("找不到「最新更新時間」時間戳(頁面可能改版)")
    year, month, day, hour, minute = (int(g) for g in match.groups())
    try:
        return datetime(year, month, day, hour, minute, tzinfo=TAIPEI_TZ)
    except ValueError as exc:
        raise StockParseError(f"更新時間數值不合法:{match.group(0)}") from exc


class _InventoryHTMLParser(HTMLParser):
    """掃 `.IndexInventory` 區塊 → 每個捐血中心的 name + {血型: 等級}。

    以語意錨定(research §4.4):中心名稱取 titleBar 內第一個非空 `<a>` 文字,
    血型以 `class="text"` 內的 A/B/O/AB 標籤配對其後 `<img>` 的 alt/檔名。
    只在看過 InventoryList/IndexInventory 後才收集,避免誤抓頁首輪播的 item1。
    """

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.saw_inventory = False       # 是否見過庫存區塊(assertion 用)
        self._in_inventory = False       # 目前是否在庫存區塊內(遇 legend 關閉)
        self.centers: list[dict] = []    # [{name, stock:{type:level}}]
        self._current: dict | None = None
        self._capture_name = False       # 在 titleBar 的 <a> 內
        self._name_done = False          # 本中心名稱已取得
        self._capture_text = False       # 在 class="text" 的元素內
        self._pending_type: str | None = None  # 已見血型標籤、等待配對 img

    def handle_starttag(self, tag: str, attrs: list) -> None:
        attr = dict(attrs)
        class_value = attr.get("class") or ""
        tokens = class_value.split()

        # 進入庫存區塊(IndexInventory 區段或 InventoryList 清單)
        lowered = class_value.lower()
        if "indexinventory" in lowered or "inventorylist" in lowered:
            self.saw_inventory = True
            self._in_inventory = True
            return
        if not self._in_inventory:
            return

        # 遇 legend(class="info")→ 收尾並離開庫存區塊,避免圖例 img 汙染最後一個中心
        if "info" in tokens:
            self._finalize_current()
            self._in_inventory = False
            return

        # 新中心區塊:class 帶 item1 / item2 …
        if any(_ITEM_CLASS_RE.match(token) for token in tokens):
            self._finalize_current()
            self._current = {"name": None, "stock": {}}
            self._name_done = False
            self._pending_type = None
            return

        if self._current is None:
            return

        if tag == "a" and not self._name_done:
            self._capture_name = True
            return
        if tag == "div" and "text" in tokens:
            self._capture_text = True
            return
        if tag == "img":
            self._record_icon(attr)

    def handle_endtag(self, tag: str) -> None:
        if tag == "a" and self._capture_name:
            self._capture_name = False
            # 取得非空名稱才視為完成,容忍 titleBar 前置的 icon 連結
            if self._current and (self._current.get("name") or "").strip():
                self._name_done = True
        elif tag == "div" and self._capture_text:
            self._capture_text = False

    def handle_data(self, data: str) -> None:
        text = data.strip()
        if not text or self._current is None:
            return
        if self._capture_name and not self._name_done:
            self._current["name"] = (self._current.get("name") or "") + text
        elif self._capture_text:
            upper = text.upper()
            if upper in ("A", "B", "O", "AB"):
                self._pending_type = upper

    def close(self) -> None:  # noqa: D401 - HTMLParser 介面
        super().close()
        self._finalize_current()

    def _record_icon(self, attr: dict) -> None:
        """配對「待處理血型標籤」與其存量 img(alt 優先、檔名備援)。"""
        if self._pending_type is None:
            return
        level = self._level_from_icon(attr)
        self._current["stock"][self._pending_type] = level
        self._pending_type = None

    @staticmethod
    def _level_from_icon(attr: dict) -> str:
        alt = (attr.get("alt") or "").strip()
        if alt in LEVEL_BY_ALT:
            return LEVEL_BY_ALT[alt]
        src = attr.get("src") or ""
        for key, value in LEVEL_BY_ICON.items():
            if key in src:
                return value
        # 有 alt 但無法辨識、或連檔名都對不上 → unknown(不整檔失敗)
        return LEVEL_UNKNOWN

    def _finalize_current(self) -> None:
        if self._current is not None:
            self.centers.append(self._current)
            self._current = None


def parse_inventory(page_html: str) -> list[dict]:
    """`.IndexInventory` → 依固定順序回 4 中心 [{id,name,stock:{A,B,O,AB}}]。

    防禦式斷言(research §4.4):
    - 從未見庫存區塊 → StockParseError
    - 任一中心缺 A/B/O/AB 任一血型「列」→ StockParseError(結構改版)
    - 解析後的中心集合 ≠ 4 個預期中心 → StockParseError(<4 中心即中止)
    等級無法辨識(alt/檔名皆對不上)→ 填 unknown,不視為錯誤。
    """
    handler = _InventoryHTMLParser()
    handler.feed(page_html)
    handler.close()

    if not handler.saw_inventory:
        raise StockParseError("找不到 IndexInventory / InventoryList 區塊(頁面可能改版)")

    resolved: dict[str, dict] = {}
    for center in handler.centers:
        name = (center.get("name") or "").strip()
        cid = center_id_for(name)
        if cid is None:
            continue  # 非血庫中心的雜訊區塊(如輪播),略過
        found_types = set(center["stock"].keys())
        missing = [t for t in BLOOD_TYPES if t not in found_types]
        if missing:
            raise StockParseError(f"中心「{name}」缺血型列 {missing}(結構可能改版)")
        resolved[cid] = {
            "id": cid,
            "name": CENTER_META.get(cid, {}).get("name", name),
            "stock": {t: center["stock"][t] for t in BLOOD_TYPES},
        }

    if set(resolved.keys()) != EXPECTED_CENTER_IDS:
        raise StockParseError(
            f"捐血中心不符預期(解析到 {sorted(resolved.keys())},應為 4 中心 "
            f"{sorted(EXPECTED_CENTER_IDS)})"
        )
    # 固定輸出順序:台北 → 新竹 → 台中 → 高雄
    return [resolved[cid] for cid in ("taipei", "hsinchu", "taichung", "kaohsiung")]


def parse_legend(page_html: str) -> dict:
    """抓 legend 門檻文字(如「庫存量7日以上」);抓不到的等級不放進結果(軟性)。"""
    out: dict[str, str] = {}
    for level_zh, key in (("正常", "normal"), ("偏低", "low"), ("急缺", "urgent")):
        match = re.search(r"(庫存量[^<>()]{1,24})\(" + level_zh + r"\)", page_html)
        if match:
            out[key] = match.group(1).strip()
    return out


def legend_warnings(legend: dict) -> list[str]:
    """比對 legend 與已知門檻定義;有落差回警告字串(呼叫端印出,但不中止)。

    刻意軟性:app 端等級門檻是靜態文案,門檻用字微調(全形括號/連字號)不該讓每日
    抓取整批失敗;真正的結構改版由 parse_inventory 的硬斷言擋下。
    """
    warnings: list[str] = []
    checks = {"normal": "7日以上", "low": "4", "urgent": "4日以下"}
    for key, needle in checks.items():
        text = legend.get(key)
        if text and needle not in text:
            warnings.append(f"legend「{key}」門檻文字疑似變更:{text!r}(預期含 {needle!r})")
    return warnings


def _iso_z(moment: datetime) -> str:
    """datetime → UTC 的 ISO8601「…Z」字串。"""
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def build_stock_document(centers: list[dict], updated: datetime, fetched_at: datetime,
                         legend: dict | None = None,
                         source: str = "https://www.blood.org.tw/") -> dict:
    """組 blood_stock.json 內容(CONTRACTS §6D:`updated` + `centers[].stock`)。

    另附 schema_version / fetched_at / source / thresholds / coverage 供人工檢視;
    app 端 BloodClient 只讀 updated 與 centers[id/name/stock],多餘欄位忽略不影響解碼。
    """
    thresholds = {**DEFAULT_THRESHOLDS, **(legend or {})}
    return {
        "schema_version": 1,
        "fetched_at": _iso_z(fetched_at),
        "source": source,
        "updated": updated.isoformat(),
        "thresholds": thresholds,
        "centers": [
            {
                "id": center["id"],
                "name": center["name"],
                "coverage": CENTER_META.get(center["id"], {}).get("coverage", ""),
                "url": CENTER_META.get(center["id"], {}).get("url", ""),
                "stock": center["stock"],
            }
            for center in centers
        ],
    }


# ---------------------------------------------------------------------------
# 捐血點(xcevent var Data)— donation_sites.json(可選;捐血點固定點已入 sqlite,此檔備用)
# ---------------------------------------------------------------------------

def extract_var_data(page_html: str) -> list[dict]:
    """xcevent?Display=Y → `var Data = [...]` JSON;找不到丟 StockParseError。"""
    marker = page_html.find("var Data")
    if marker < 0:
        raise StockParseError("xcevent 頁面找不到 var Data(版面可能改版)")
    start = page_html.find("[", marker)
    if start < 0:
        raise StockParseError("xcevent var Data 後找不到 JSON 陣列起點")
    data, _ = json.JSONDecoder().raw_decode(page_html[start:])
    if not isinstance(data, list):
        raise StockParseError("xcevent var Data 不是陣列")
    return data


def _parse_pos(item: dict) -> tuple[float | None, float | None]:
    pos = item.get("Pos") or {}
    try:
        lat = float(pos.get("lat"))
        lng = float(pos.get("lng"))
    except (TypeError, ValueError):
        return None, None
    if _LAT_RANGE[0] <= lat <= _LAT_RANGE[1] and _LNG_RANGE[0] <= lng <= _LNG_RANGE[1]:
        return lat, lng
    return None, None


def normalize_event(item: dict) -> dict:
    """xcevent 一筆 → donation_sites.json event(座標 0,0/超界→null)。"""
    lat, lng = _parse_pos(item)
    sid = ((item.get("SId") or {}).get("Value") or "").strip()
    return {
        "sid": sid,
        "center_id": _CENTER_ID_BY_NUM.get(str(item.get("CenterId") or "").strip()),
        "type": _EVENT_TYPE_NAME.get(item.get("EventType")),
        "name": (item.get("ActivityName") or "").strip(),
        "date": item.get("DonationDate"),
        "time": (item.get("DonationTime") or "").strip(),
        "time_desc": (item.get("DonationTimeDesc") or "").strip(),
        "address": (item.get("ActivityPlace") or "").strip(),
        "address_note": (item.get("ActivityPlaceDesc") or "").strip(),
        "tel": (item.get("Tel") or "").strip() or None,
        "lat": lat,
        "lng": lng,
    }


def build_sites_document(items: list[dict], date: str, fetched_at: datetime,
                         source: str = "https://www.blood.org.tw/xcevent?Display=Y") -> dict:
    """組 donation_sites.json(全類別 station/mobile/drive;缺名稱或地址者略過)。"""
    events = [normalize_event(item) for item in items]
    events = [event for event in events if event["name"] and event["address"]]
    return {
        "schema_version": 1,
        "fetched_at": _iso_z(fetched_at),
        "date": date,
        "source": source,
        "events": events,
    }
