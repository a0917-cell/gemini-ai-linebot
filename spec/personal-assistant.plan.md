# Plan：Gemini AI bot → 個人助手

依據 `spec/personal-assistant.md`（已定案）· 2026-09-29

**會動到的檔案**：`app/gemini_service.py`（`SYSTEM_PROMPT`、`_generate_with_retry`、`_user_filter`、`query_with_text`）、`app/line_handler.py`（`handle_text_message`、`_reply`/`_push`）、`app/main.py`（路由、新增 `/cron/tick`）。新增：`app/reminders.py`、`scripts/ingest_kb.py`、`tests/`。參考寫法：`~/.gemini/line-archiver-bot/google_services.py`（Sheets OAuth）。

**已查證**（官方文件，2026-09-29）：`gemini-3.8-flash`、`gemini-3.5-flash-lite` 都支援 File Search，也都有免費方案；File Search 免費方案上限 1 GB，建索引免費；HJPLUS `raw\` 18 MB，採 CC BY-SA 4.0。

## 任務

- [x] T0：建測試骨架 `tests/` + `conftest.py`，**在測試裡擋掉真的 genai、LINE、Sheets client** — done when：`pytest -q` 能跑，而且拿掉 conftest 防護時，「建出真 client 就失敗」那條測試會轉紅 (depends: —)
  - 2026-09-29 完成：3 passed；拿掉防護時 3 條都轉紅；mutation 3/3 caught。Sheets 的防護留到 T9 再加。conftest 裡重設 `_client`、`_store_name` 快取的那兩行，目前沒有測試會因為拿掉它們而轉紅，屬於預防性的寫法，不算進覆蓋率。
- [x] T1：主模型改成 `gemini-3.8-flash`，新增 `GEMINI_FALLBACK_MODEL=gemini-3.5-flash-lite`；主模型重試用完後改用備援 — done when：模擬主模型連續回 503，確認最後改呼叫備援模型（AC1） (depends: T0)
  - 2026-09-29 完成：9 passed（6 條新測試）；mutation 5/5 caught（第一輪 3 個因為 CRLF 比對不到，沒有跑到，改成單行比對後重跑）。**部署注意**：`.env`（`gemini-2.5-flash`）和 Render 上的 `GEMINI_MODEL` 都會蓋掉程式預設值，T13 要把 Render 的改成 `gemini-3.8-flash`，並新增 `GEMINI_FALLBACK_MODEL`。
- [x] T2：所有錯誤都轉成中文說明，回覆裡不會出現原始錯誤字串 — done when：模擬兩個模型都失敗，回覆裡沒有 `503`、`{'error'`（AC2） (depends: T1)
  - 2026-09-29 完成：5 個外洩點（文字查詢、圖片、檔案、背景存入 push、以圖搜尋）都改走 `_friendly_error()`，完整錯誤只寫進 log。20 passed（新增 11 條，每條路徑都測過載和一般錯誤兩種）；mutation 6/6 caught，另外用 UTF-8 重跑一個 mutant，確認失敗原因是「raw error leaked」那條斷言。
- [x] T3：改寫 `SYSTEM_PROMPT`：沒有相關文件時照樣直接回答，用到文件就附檔名，支援中越日翻譯 — done when：沒上傳文件的帳號問一般問題，得到直接回答（AC3，手動） (depends: T0)
  - 2026-09-29 完成（2b10f1b 已部署）：使用者在 LINE 實測「改寫句子」直接得到兩種改寫版本，傳圖時出現 🔒 保密提醒（依決定 (a)）。
- [x] T4：文字訊息改成先回 200、在背景產生回覆，用 reply token 回；超過 50 秒才改用 push — done when：模擬模型很慢時，webhook 仍在 1 秒內回 200 (depends: T0)
  - 2026-09-29 完成：webhook 改成 `background_tasks.add_task`，測試直接呼叫 webhook，斷言文字處理被排進背景、沒有當場執行（驗證的是這個結構，沒有量實際的毫秒數）。背景任務會先顯示 LINE 的「輸入中」動畫（失敗不會影響回答），50 秒內用 reply token 回（依據 LINE 文件「reply token 必須在收到 webhook 後 1 分鐘內使用」），超過 50 秒或 reply 被拒才改用 push。36 passed，mutation 5/5 caught。風險 #2（push 有免費則數上限）仍然存在：只有慢回覆時才會用到 push。
- [x] T5：**驗證** File Search 的 `metadata_filter` 支不支援 `user_id="U…" OR user_id="__kb__"`：建一個測試用 store、放兩份文件、各查一次，驗完刪掉 — done when：腳本輸出證明兩份都查得到、第三份（別的 user_id）查不到 (depends: —) ⚠ 用真的 API，但免費；不支援的話退回「兩個 store 分開查」，T7 跟著改
  - 2026-09-29 完成：`scripts/spike_or_filter.py` 輸出 `VERDICT: OR filter WORKS`。依 grounding metadata，OR 篩選取回 {A, KB}、沒取回 C；對照組只篩 C 時只取回 {C}；測試 store 已刪。使用者同意這次用付費金鑰跑（費用不到 NT$1，有 NT$10 支出上限）。T7 維持單一 store 加 OR filter 的設計。
  - ⚠ **T6 的前置條件**：正式的 store（`linebot-multimodal-rag`）在**免費專案** `gen-lang-client-0356711356`，只有免費金鑰（`…WlPM`）存取得到。本機 `.env` 是付費專案的金鑰，看不到那個 store。T6 上傳 KB 一定要用免費金鑰。
- [x] T6a（2026-09-29 插入，Stop-the-Line；同日完成：38 passed，關掉「指定優先」的 mutant 會被抓到）：免費專案裡有**兩個**同名 store（`…c1v9232tcirj`、`…4xwnfoqme9q8`），**都是 0 份文件**；bot 靠「GCS 讀名稱失敗 → 列出來挑第一個同名的」決定用哪個，結果不保證固定（Render log 目前是 `…c1v9232tcirj`）。新增 `GEMINI_STORE_NAME` 環境變數，有設就直接用 — done when：測試證明有設時不會去列出或建立 store；T13 部署時在 Render 設成 `fileSearchStores/linebotmultimodalrag-c1v9232tcirj`；T6 也上傳到同一個 store (depends: T0)。另一個空 store 刪不刪由使用者決定。
- [ ] T6：`scripts/ingest_kb.py`：把 HJPLUS `raw\` 的 `.md` 上傳，標 `user_id="__kb__"`、`source=HJPLUS`；用顯示名稱判斷避免重複上傳；預設 dry-run — done when：dry-run 列出 332 份；實跑後 store 裡的 KB 文件數 = 332 (depends: T5) ⚠ 文件寫進正式 store；rollback：依 metadata 刪掉 `__kb__` 文件
- [ ] T7：查詢 filter 改成「本人 OR KB」，prompt 要求法規答案附出處，KB 內容標示未查證的要加「待查證」 — done when：單元測試檢查 filter 字串（AC5）；手動問一題防火區劃，回答有出處 (depends: T5, T6)
- [ ] T8：使用者隔離的回歸測試：filter 裡一定有本人的 `user_id`，而且永遠不會只剩 KB — done when：AC4 的單元測試通過 (depends: T7)
- [ ] T9：`app/reminders.py`：以 Sheets 為儲存（add / list_pending / list_due / mark_sent / cancel），測試用記憶體版替身 — done when：替身和介面契約測試通過；用真的 Sheet 手動寫一筆、讀一筆 (depends: T0) ⚠ Sheet 欄位格式等於資料格式，定了就不好改；rollback：換一張新 Sheet
- [ ] T10：解析提醒時間：Gemini 結構化輸出 `{when, text}`（prompt 帶入現在的台北時間），程式再擋掉過去的時間、缺少的時間 — done when：過去、缺少時間、正常三種輸入各有單元測試（AC7） (depends: T1)
- [ ] T11：把提醒指令接進文字處理：「…提醒我…」建立、「我的提醒」列出、quick reply 取消 — done when：單元測試走完建立 → 列出 → 取消 (depends: T4, T9, T10)
- [ ] T12：`GET /cron/tick?key=…`：驗證密鑰，把到期的提醒 push 出去並標記已送出 — done when：密鑰錯誤回 403；替身 store 裡的到期提醒被送出，而且只送一次 (depends: T9)
- [ ] T13：部署：Render 環境變數（新模型 id、`CRON_SECRET`、Sheets 憑證、`REMINDER_SHEET_ID`，**金鑰由使用者貼上**）；UptimeRobot 每 5 分鐘呼叫 `/cron/tick`（使用者登入） — done when：AC6（10 分鐘後的提醒準時到）、AC8（閒置 1 小時後 30 秒內回覆），手動各測一次 (depends: T1–T12)
- [ ] T14：更新 `CLAUDE.md` 和 `spec/architecture.md`：部署在 Render 而不是 Cloud Run，補上模型、KB、提醒的設計 — done when：文件裡找不到過時的 Cloud Run 部署說明 (depends: T13)

- [ ] T15（2026-09-29 新增）：查清楚 Render 上設了 `GCS_BUCKET`，卻沒有任何 GCP 憑證的環境變數。`gcs.Client()` 很可能在上傳檔案時失敗 — done when：在 Render log 確認一次上傳走的是 GCS 還是失敗；失敗的話，改成拿掉 `GCS_BUCKET`（走本機 fallback）或補上憑證 (depends: T0)

- [x] T16（2026-09-29 新增，⚠ 安全；同日完成：要帶 `X-Admin-Token`，沒設 `ADMIN_TOKEN` 時預設關閉、回 403，錯誤不回傳原始內容，`/health` 只回 status；27 passed，mutation 4/4 caught；**要 push 部署才會在線上生效**）：`GET /store/info` 不需要驗證就會列出 store 裡**所有使用者上傳的檔名**，而且出錯時把原始錯誤回給呼叫者（`main.py:69`）；`/health` 也公開了 store 名稱 — done when：沒帶密鑰呼叫 `/store/info` 回 403（或整個端點移除），`/health` 只回 `{"status": "ok"}`，都有測試 (depends: T0)。**建議排在 T3 之前做**，因為它現在就在線上。

**可以平行做的**：T5 可以隨時先做；T0 完成後，T1/T3/T4/T9 彼此獨立。

## 風險

1. ⚠ **免費方案的資料會被 Google 用來改進產品**（官方價目表每個模型都標「用於改善我們的產品：是」，付費方案標「否」）。上傳公司文件前要使用者決定，見下方待決。
2. **LINE push 有每月免費則數上限**：提醒和慢回覆都會用到 push。T11 開工前先查「System／Gemini AI」官方帳號方案的免費則數。
3. **`OR` filter 沒驗證過**：T5 就是為了先排除這個風險，T7 之前一定要做。
4. **UptimeRobot 保溫同時也是提醒的觸發來源**：監控停了，提醒就不會送。T13 要把這個依賴寫進 CLAUDE.md。

## 決定

- 2026-09-29 使用者選 (a)：維持免費方案，照常上傳，但只上傳不敏感的資料，由使用者自己把關（知情同意：免費方案的內容會被 Google 用來改進產品）。T3 的 prompt 或上傳確認訊息裡要加一句提醒。
