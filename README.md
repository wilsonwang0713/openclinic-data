# openclinic-data

「開診」App 的公開資料饋送。由 GitHub Actions 每日自動更新,App 讀取本 repo 的 raw JSON。

主程式碼在私有 repo；本 repo 只放對外公開的每日資料與其爬蟲。

## 資料檔

| 檔案 | 說明 | 更新 |
|------|------|------|
| `blood_stock.json` | 台灣血液基金會 4 中心 × ABO 血型庫存等級(normal/low/urgent) | 每日 08:30 / 16:30(台北) |
| `donation_sites.json` | 捐血點(固定點已入 App 內建 DB,此檔為動態備用) | 同上 |

App 讀取:`https://raw.githubusercontent.com/wilsonwang0713/openclinic-data/main/blood_stock.json`

## 爬蟲

`scraper/` 為純爬蟲工具,`.github/workflows/blood-scraper.yml` 每日 cron 執行 → 產出 JSON commit 進本 repo 根目錄。解析失敗時 keep-last-good(不覆寫舊資料)並自動開 issue 告警。資料來源與結構考證見主 repo `docs/blood-api-research.md`。

資料為台灣血液基金會公開資訊之彙整,僅供參考,實際以官方為準。
