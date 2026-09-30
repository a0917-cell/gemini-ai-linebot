# Gemini AI — LINE 個人助手

給台灣營造業施工繪圖／BIM 工程師用的 LINE 個人助手，核心是 **Gemini File Search**（Google 託管的 RAG）。

改寫自 [kkdai/linebot-multimodal-rag](https://github.com/kkdai/linebot-multimodal-rag)：保留它的上傳、索引、以圖搜尋與使用者隔離設計，加上共用法規知識庫、提醒、來源標示，部署改在 Render。

> **隱私**：用的是 Gemini 免費方案，內容可能被 Google 用來改進產品。請不要上傳公司機密或客戶資料；傳檔時 bot 也會提醒。

---

## 能做什麼

| 你在 LINE 做的事 | 助手的反應 |
|------|------|
| 一般問題、寫作、翻譯（中、越、日、英） | 直接回答 |
| 問法規、規範、數值 | 先查共用知識庫；有引用會附來源，沒引用會加警告 |
| 傳 **PDF／文件／圖片** | 問你要「📥 存入資料庫」還是「🔍 作為搜尋」 |
| 問自己存過的資料 | 只從**你自己的**檔案和共用知識庫找，附檔名 |
| 「明天 9 點提醒我繳圖」 | 回「⏰ 已設定：9/30（三）09:00 繳圖」，時間到推播 |
| 「我的提醒」 | 列出還沒到的提醒，每筆可按鈕取消 |

### 回答結尾的來源行

```
📎 來源：送審單.pdf                                            你上傳的檔案
📜 法規條文（全國法規資料庫，快照 2026-09-18）：建築技術規則建築設計施工編／第三章 建築物之防火
📚 法規知識庫（HJPLUS，CC BY-SA 4.0）：建築設計施工編/樓梯欄杆坡道
⚠️ 引用的法規知識庫內容尚未全部查證（待查證），請以法規原文或主管機關公告為準。
⚠️ 這個回答沒有引用法規知識庫，條文與數值請以全國法規資料庫原文為準。
```

來源行是程式依「實際檢索到的文件」產生的，不是模型自己寫的。模型要不要查知識庫由它自己決定（API 沒辦法強制），所以法規題沒查到時一定會出現最後那行警告。

### 共用知識庫

- **法規條文**：12 部核心法規（建築技術規則 4 編、建築法、各類場所消防安全設備設置標準、消防法、營造安全衛生設施標準、職業安全衛生設施規則、營造業法、建築物室內裝修管理辦法、都市計畫法），1,949 條，按章切成 104 份。來源是全國法規資料庫 2026-09-18 的快照（政府資料開放授權條款第 1 版），**現行條文請以全國法規資料庫為準**。
- **HJPLUS 台灣建築師知識庫**：332 份實務筆記（CC BY-SA 4.0）。多數沒有標示查證狀態，所以引用時會加「待查證」。

### 限制

- 圖片、PDF、TXT、CSV、Markdown 可以；音訊、影片不行（File Search 的限制），單檔上限 100 MB
- 傳檔後 5 分鐘內要選「存入」或「搜尋」
- 提醒只做單次，不做每週、每月重複；會晚 0–5 分鐘送達
- 句子裡有「提醒我」就會走提醒流程（例如「請提醒我建築法規第幾條」會被反問時間）

---

## 架構

```
LINE ─► Render（FastAPI，免費方案，1 個 instance）
          ├─ 文字 ─► 提醒指令？─► Google Sheets
          │         └─ 其他 ─► Gemini File Search（本人檔案＋共用知識庫）─► 回覆／推播
          ├─ 圖片／檔案 ─► 暫存 ─► 存入資料庫／作為搜尋
          └─ /cron/tick ◄─ UptimeRobot 每 5 分鐘（送出到期提醒＋保持喚醒）
```

- 生成模型：`gemini-3.8-flash`，忙線時改用 `gemini-3.5-flash-lite`
- 嵌入模型：`gemini-embedding-2`
- 所有人共用一個 File Search Store，用 metadata 的 `user_id` 隔離：查詢條件是「本人 OR 共用知識庫」，看不到別人的檔案

詳細設計見 [spec/architecture.md](spec/architecture.md)，部署與環境變數見 [spec/deployment.md](spec/deployment.md)，需求與任務紀錄見 [spec/personal-assistant.md](spec/personal-assistant.md)。

---

## 本機開發

```bash
python -m venv .venv
.venv\Scripts\activate            # macOS/Linux: source .venv/bin/activate
pip install -r requirements-dev.txt
copy .env.example .env            # 填入 LINE、Gemini、GEMINI_STORE_NAME 等
uvicorn app.main:app --reload --port 8080
pytest -q
```

對外開放 webhook 可以用 cloudflared quick tunnel 或 ngrok，把 `https://<網址>/webhook` 填進 LINE Developers Console。

⚠️ 本機 `.env` 的 Gemini 金鑰如果跟正式環境是同一把，本機測試會吃掉正式環境的免費額度。

### 腳本

| 腳本 | 用途 |
|------|------|
| `scripts/ingest_laws.py` | 把法規條文按章上傳到知識庫（預設 dry run） |
| `scripts/ingest_kb.py` | 上傳 HJPLUS 筆記（預設 dry run） |
| `scripts/setup_reminder_sheet.py` | 一次性：Google 登入並建立提醒試算表；`--smoke` 冒煙測試 |
| `scripts/measure_kb_retrieval.py` | 量法規題有多常引用知識庫（會用到真的額度） |

---

## API 端點

| 端點 | 方法 | 說明 |
|------|------|------|
| `/webhook` | POST | LINE Webhook |
| `/health` | GET | 只回 `{"status": "ok"}` |
| `/cron/tick?key=…` | GET、HEAD | 送出到期提醒；沒有 `CRON_SECRET` 或 key 不對一律 403 |
| `/store/info` | GET | 管理用，要帶 `X-Admin-Token`；沒設 `ADMIN_TOKEN` 時一律 403 |

---

## 疑難排解

**回覆很慢或說「AI 忙線中」**：Gemini 免費額度用完或模型過載，會自動改用備援模型；額度每天重置。

**提醒沒送到**：看 UptimeRobot 監控是不是 Up、LINE 每月 200 則推播額度有沒有用完、試算表那一列的 `status`。

**Bot 完全沒反應**：確認 LINE Console 的 Webhook URL 和「Use webhook」、`/health` 有回應、Render 免費時數（每月 750 小時，跟同帳號其他服務共用）沒有用完。

---

## 參考資料

- [Expanded Gemini API File Search: multimodal RAG](https://blog.google/innovation-and-ai/technology/developers-tools/expanded-gemini-api-file-search-multimodal-rag/)
- [Multimodal RAG with the Gemini API File Search tool: A Developer Guide](https://dev.to/googleai/multimodal-rag-with-the-gemini-api-file-search-tool-a-developer-guide-5878)
- [File Search API documentation](https://ai.google.dev/gemini-api/docs/file-search?hl=zh-tw)
- 上游專案：[kkdai/linebot-multimodal-rag](https://github.com/kkdai/linebot-multimodal-rag)
