# 投資資產追蹤工具

記錄並每天計算：

- **正股、持股信託、美債 ETF** 的市值與損益（持股信託會分開記錄自提金額與公司獎勵金）
- **質押借款（不限用途借貸）**：維持率、股價跌多少會被追繳、累積利息，以及**質押損益**
  （用借款買進部位的損益＋股利＋已實現損益 − 利息）
- **等效槓桿**
  - 資產槓桿 = 總資產 / 淨值
  - 股票等效槓桿 = 股票曝險 / 淨值（槓桿型 ETF 用 `exposure` 填倍數，例如 00631L 填 2；債券不算股票曝險）
- **壓力測試**：股票下跌 10% / 20% / 30% / 40% 時的淨值與維持率
- **建議**：依你在設定檔裡定的門檻，輸出「緊急 / 避險 / 減碼 / 分散 / 加碼 / 維持」以及建議金額

只用 Python 3.11+ 標準函式庫，不需要安裝任何套件。

## 快速開始

```bash
cp portfolio.example.toml portfolio.toml   # 填入自己的持股與借款
python -m asset_tracker backfill           # 第一次使用：回補 4 個月歷史價格，讓均線趨勢判斷可用
python -m asset_tracker daily              # 抓收盤價、計算、存快照、產生 reports/日期.md
python -m asset_tracker history            # 看每天槓桿、維持率、質押損益的變化
```

## 圖形介面：我的錢 vs 借來的錢

```bash
python -m asset_tracker gui        # 產生 reports/dashboard.html 並用瀏覽器打開
```

`daily` 每次執行也會更新 `reports/dashboard.html`。儀表板是單一離線 HTML 檔，不載入任何外部資源，資料只留在本機。範例畫面見 `docs/dashboard-sample.html`（範例數字）。

- **總資產長條**：藍色是我的錢（淨值），橘色是借來的錢（借款本金＋未繳利息）
- **每筆資產的資金來源**：藍色＝自有資金買的、藍色斜線＝自有但質押給券商、橘色＝用借款買的（依 `loans.purchases` 股數 × 現價）
- 借款本金扣掉已登記買進成本後的餘額，視為還放在現金裡，會標成橘色
- **借款卡片**：借多少、買了什麼、質押了什麼、維持率狀態、跌多少會追繳、質押損益
- 支援深色模式與手機；滑鼠移到長條上可看精確金額，也有表格檢視

## iPhone App（`web/`）

`web/` 是可以加到 iPhone 主畫面的網頁 App（PWA），計算規則與電腦版相同（有測試確認兩邊數字一致）。

- 四個分頁：**總覽**（我的錢 vs 借來的錢、槓桿、維持率、建議、壓力測試）、**持股**、**借款**、**設定**
- 可以直接在手機上新增、修改持股與質押借款，也能手動輸入價格
- 一筆借款可以有多次撥款（例如證金擔保放款分次借），利息各自從撥款日起算，維持率以合計本金計算
- 「更新股價」讀取同一個網站上的 `prices.json`：GitHub Actions 每個交易日台北時間約 15:41、18:41 自動抓取證交所／櫃買中心全市場收盤價（公開資料），
  並附上 0050／006208 近 4 個月歷史價格（均線判斷可立即使用）、常見美債 ETF 美元價格與 USD/TWD 匯率。
  手機無法直接連證交所，是因為瀏覽器的跨網域限制（CORS），所以改由 GitHub 代抓
- 找不到的代號（例如興櫃）請在「持股」手動輸入價格
- 公開 repo 若 60 天沒有任何 commit，GitHub 會暫停排程；到 Actions 頁面重新啟用即可
- 每天打開總覽會自動記錄一筆（淨值、借款、槓桿、維持率、質押損益）
- 資料只存在手機的 Safari 儲存空間，不會上傳；請定期用「設定 → 匯出備份」存一份

### 安裝到 iPhone

App 需要放在 HTTPS 網址上才能安裝：

1. **GitHub Pages**：repo 設定 → Pages → Source 選「GitHub Actions」，推到 `master` 後 `.github/workflows/pages.yml` 會自動部署 `web/`
   （只部署程式碼，不含任何持股資料；私人 repo 使用 Pages 需要付費方案）
2. 用 iPhone 的 Safari 打開網址 → 分享 → **加入主畫面**

在電腦上試用：`cd web && python3 -m http.server 8000`，再用瀏覽器開 `http://localhost:8000`。

### 電腦與手機同步資料

```bash
python -m asset_tracker export-json     # 產生 portfolio-export.json（含持股、借款、價格歷史）
```

用 AirDrop 或 iCloud 雲碟把檔案傳到手機，在 App 的「設定 → 匯入 JSON」選擇它。
手機上也能「匯出備份」成同樣格式的 JSON。這個檔案含有完整財務資料，傳完請刪除多餘的副本。

其他用法：

```bash
python -m asset_tracker report                       # 只計算不存檔
python -m asset_tracker report --price 2330=1000     # 手動指定價格（盤中試算）
python -m asset_tracker daily --offline              # 不連網，用資料庫最後價格
```

`daily` 在建議為「緊急」（維持率低於追繳線或淨值為負）時回傳結束碼 2，可以拿來串通知。

## 國泰證券帳戶資料

本工具**不會直接登入國泰證券帳戶**：國泰沒有提供讓個人程式讀取庫存的公開介面，而且把券商帳密交給腳本保存的風險很高。
作法是從國泰 App／網頁匯出庫存 CSV，再匯入：

```bash
python -m asset_tracker import-cathay 庫存.csv
# 會產生 holdings_cathay.toml，然後在 portfolio.toml 的 [settings] 加上：
# holdings_files = ["holdings_cathay.toml"]
```

- 會自動辨識 UTF-8 / Big5 編碼、跳過標題列與合計列、合併同一檔的多列（例如整股＋零股）
- 欄位名稱對不上時：`--map 商品代碼=symbol --map 庫存數量=shares --map 付出成本=cost`
- 數量單位是「張」時加 `--lots`
- `00xxxB` 會自動歸類為債券 ETF；持股信託通常不在證券庫存裡，請直接寫在 `portfolio.toml`
- 匯入檔裡已有的股票，不要在 `portfolio.toml` 再寫一次（會報重複錯誤）

質押的擔保品和「用借款買了什麼」寫在 `[[loans]]` 底下的 `collateral` 與 `purchases`，
和庫存分開記錄，所以重新匯入庫存不會影響質押損益的計算。

## 價格來源

| 市場 | 來源 |
|---|---|
| 上市 | 證交所 OpenAPI（STOCK_DAY_ALL） |
| 上櫃（多數台灣債券 ETF，如 00679B） | 櫃買中心 OpenAPI |
| 美股（`market = "US"`，如 TLT） | Yahoo Finance，並以 USD/TWD 換算 |
| 其他 | `market = "MANUAL"` 搭配 `price`，或 `[prices]` 表 |

抓不到當天價格時會用資料庫中最近的價格，並在報告中標示 `*`。

## 每天自動執行

收盤資料約在 14:30 後更新，建議平日 15:30 執行。

Linux / macOS（`crontab -e`）：

```
30 15 * * 1-5 cd /path/to/helllo && /usr/bin/python3 -m asset_tracker daily >> data/daily.log 2>&1
```

Windows：在「工作排程器」建立每天 15:30 的工作，執行 `python -m asset_tracker daily`，起始位置設為專案資料夾。

> 不建議用 GitHub Actions 跑：持股與借款資料會需要放進 repo 或 secrets，個資外洩風險較高。

## 建議規則

全部門檻都在 `portfolio.toml` 的 `[strategy]`，依優先順序：

1. **緊急**：任何一筆質押維持率 < `maintenance_call`，或淨值 ≤ 0。會算出要還多少錢才能回到 `maintenance_safe`。
2. **避險**：維持率 < `maintenance_warn`；或債券比重 < `bond_ratio_min`。
3. **減碼**：股票等效槓桿 > `max_equity_leverage`；或大盤（`benchmark`）跌破 `trend_ma_days` 日均線且槓桿高於目標。
4. **分散**：單一股票（正股＋信託合計）曝險佔淨值 > `max_single_position`。
5. **加碼**：沒有上述任何警示、槓桿 < 目標、大盤在均線之上、維持率 ≥ 安全線。
   建議金額取「補到目標槓桿所需金額」與「借款後維持率仍 ≥ 安全線的可借額度」兩者較小值。
6. 其他情況：**維持**。

### 計算上的簡化（請留意）

- 利息以「本金 × 年利率 × 天數 / 365」單利估算，實際以券商對帳單為準；已繳的利息填在 `interest_paid`。
- 維持率 = 擔保品市值 / 借款本金。各券商算法與追繳線可能不同，請依你的契約調整 `maintenance_call`。
- 壓力測試假設所有股票同步下跌、債券價格不變，實際上股債可能同跌（例如 2022 年）。
- 本工具是規則引擎，不是預測模型，產生的內容不構成投資建議。

## 測試

```bash
python -m unittest discover -s tests -v
```

## 資料與隱私

`portfolio.toml`、`holdings_*.toml`、`data/`、`reports/` 都已列入 `.gitignore`，不會被提交。
