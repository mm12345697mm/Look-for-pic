# 番號辨識 · Look-for-pic Web（靜態 PWA）

手機優先的靜態網頁（非 Expo），適用 **iPhone Safari**。選圖／輸入番號後**自動跑完整流程**，直接進入畫廊（無確認、無多選畫面）。

## 功能

1. **首頁**：大按鈕「選取照片」「離線示範 MIDA-616」、番號輸入、可選相機（`capture=environment`）
2. **選圖**：以 CDN 載入的 Tesseract.js（eng）辨識番號；多個時取最佳；沒有則請你輸入一次
3. **自動畫廊**：正規化 → DMM CID → 封面 `…/{cid}pl.jpg` + 劇照 `…jp-1.jpg`…  
   - **MIDA-616／示範**：主作品＋主題＋女優全部自動帶入  
   - **其他番號**：僅 CDN 主卡＋提示（相關需示範／seed）
4. **畫廊卡片**：番號、片名、女優、封面（`object-fit: contain` 不裁切）、橫滑劇照
5. 新圖／新番號＝新基準（不沿用上一任務）

僅使用 `pics.dmm.co.jp` 圖片；無磁力／盜版連結。


## 後端啟動（OCR／看圖辨識）

本專案可用 Flask 伺服器（`server.py`）做伺服端辨識：

```bash
cd /workspace/look-for-pic-web   # 或你的專案路徑
# 可選：設定 Gemini API Key 後啟用「看圖辨識」（讀截圖番號＋片名）
export GEMINI_API_KEY="你的金鑰"
./start.sh
# 開啟 http://0.0.0.0:8787/ （區網請改用電腦 IP）
```

未設定 `GEMINI_API_KEY` 時仍可用：手動／OCR 番號＋示範包／線上查詢；上傳圖會提示看圖辨識不可用並走 OCR。


## 在 iPhone Safari 開啟

### 方式 A：同一區網（推薦）

1. 電腦與 iPhone 連同一個 Wi‑Fi
2. 在電腦執行：

```bash
cd /workspace/look-for-pic-web   # 或你解壓後的路徑
./serve.sh
# 等同：python3 -m http.server 8787
```

3. 查電腦區網 IP（例：`192.168.1.23`）
4. iPhone Safari 開啟：`http://192.168.1.23:8787/`
5. 可選：分享 → **加入主畫面**，當成 Web App 使用

### 方式 B：部署到任意靜態主機

把整個資料夾上傳到 GitHub Pages、Cloudflare Pages、Netlify、S3 等，用 HTTPS URL 開啟即可（PWA／相機在 HTTPS 下較穩定）。

> 注意：從 `file://` 直接開可能無法 `fetch` 示範 JSON、也無法用相機；請用 HTTP(S) 伺服器。

## 建議先試

點 **「離線示範 MIDA-616」** → 應立刻看到主作品＋6 部相關的畫廊卡片。封面／劇照需網路（DMM CDN）。

## 目錄

```
look-for-pic-web/
  index.html
  app.js
  styles.css
  manifest.webmanifest
  serve.sh
  README_zh.md
  data/demo-package.json
  icons/…
```

不需 Node、不需 Expo。使用者端只要瀏覽器。

## 限制／注意

- 未設 `GEMINI_API_KEY` 時以伺服端 Tesseract OCR 找番號；設了金鑰則優先 Gemini 看圖讀番號＋日文片名。失敗可手動輸入
- 非 MIDA-616 無完整線上目錄／相關推薦 API，僅組 CDN URL
- DMM 圖片需能連外網；部分網路環境可能擋 CDN
- 封面刻意用 `object-fit: contain`，不裁切；劇照縮圖可用 cover

## 與 Expo 版差異

Expo 版（`Look-for-pic`）有確認主作品、多選相關步驟。本 Web 版依需求**自動全選並跳轉畫廊**。

## 私人站

請在 Railway 設定環境變數 `SITE_PASSWORD`。打開網站會先要求輸入此密碼；只有你分享密碼的人能用。登出路徑：`/logout`。

主人手機免密：設定 `OWNER_DEVICE_TOKEN` 後，用 Safari 打開一次 `https://你的網域/d/<token>`，可加入主畫面；之後此裝置免輸入分享密碼。訪客仍走 `/login`。
