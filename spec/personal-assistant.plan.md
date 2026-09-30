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
- [x] T6：`scripts/ingest_kb.py`：把 HJPLUS `raw\` 的 `.md` 上傳，標 `user_id="__kb__"`、`source=HJPLUS`；用顯示名稱判斷避免重複上傳；預設 dry-run — done when：dry-run 列出 332 份；實跑後 store 裡的 KB 文件數 = 332 (depends: T5) ⚠ 文件寫進正式 store；rollback：依 metadata 刪掉 `__kb__` 文件
  - 2026-09-29 完成：store `…c1v9232tcirj` 裡的 KB 文件數 = **332**，全部 ACTIVE，顯示名稱不重複；status：none 296、unverified 21、verified 8、draft 4。第一次跑少了 3 份：2 份是中文檔名（SDK 把檔名放進 HTTP 標頭，必須是 ASCII），修法是改用 ASCII 名稱的暫存副本上傳，並加了測試；另外 2 份是 `Server disconnected`（其中 1 份其實伺服器端已成功），重跑時因為會跳過已上傳的檔名，只補了缺的 3 份。當時看到的「exit code 0」是管線裡 `grep` 的結束碼，不是腳本本身的；重跑時沒接管線，腳本回 0。同日依使用者同意，刪除了空的重複 store `…4xwnfoqme9q8`（刪前確認 0 份文件，用 force=False 刪除）。
- [x] T7：查詢 filter 改成「本人 OR KB」，prompt 要求法規答案附出處，KB 內容標示未查證的要加「待查證」 — done when：單元測試檢查 filter 字串（AC5）；手動問一題防火區劃，回答有出處 (depends: T5, T6)
  - 2026-09-29 完成（8ca897f 已部署）：使用者在 LINE 問「樓梯最小寬度」，回答引用建築技術規則第 33 條，結尾附「📚 法規知識庫（HJPLUS，CC BY-SA 4.0）：建築設計施工編/樓梯欄杆坡道、…」。**待查證那一行不在截圖範圍內**，只有測試覆蓋，線上還沒親眼看到。後續完整截圖確認沒有待查證行：三份來源在 store 裡都沒有 status，舊規則只對 unverified/draft 警告，所以行為正確；是我預告錯了。使用者同日決定改成「只要有一份不是 verified 就警告」（332 份只有 8 份 verified；查不到 status 時也警告，不替內容背書）。
- [x] T8：使用者隔離的回歸測試：filter 裡一定有本人的 `user_id`，而且永遠不會只剩 KB — done when：AC4 的單元測試通過 (depends: T7)
  - 2026-09-29 完成：讀取端（文字查詢、以圖搜尋、handler 用寄件人的 LINE id）與寫入端（個人上傳不可標成 __kb__、extra_metadata 不可覆寫 user_id/source）共 7 條；寫入端兩個洞先 RED 再修。65 passed，mutation 4/4 caught。
- [x] T9：`app/reminders.py`：以 Sheets 為儲存（add / list_pending / list_due / mark_sent / cancel），測試用記憶體版替身 — done when：替身和介面契約測試通過；用真的 Sheet 手動寫一筆、讀一筆 (depends: T0) ⚠ Sheet 欄位格式等於資料格式，定了就不好改；rollback：換一張新 Sheet
  - 2026-09-29 完成（59f245b，未部署）：`app/reminders.py` 的契約測試同時跑記憶體版和 Sheets 版（用假的 Sheets service），125 passed，mutation 16/16。使用者決定：新 refresh token 只給 drive.file、新建專用試算表「LINE 助手提醒」（分頁 `reminders`）、時間存 ISO +08:00。`scripts/setup_reminder_sheet.py` 建表並把 4 個值寫進 .env（不顯示）；使用者自己在 PowerShell 跑的那次沒寫進任何值，原因沒查到（錯誤只在他的視窗裡），改由我在背景啟動、使用者登入後成功。`--smoke` 對真表寫入 → 讀回 → 取消都成功，另外直接讀表確認有 1 列 `cancelled` 的資料。T13 要在 Render 設 GOOGLE_CLIENT_ID／GOOGLE_CLIENT_SECRET／GOOGLE_REFRESH_TOKEN／REMINDER_SHEET_ID（使用者貼上）。
- [x] T10：解析提醒時間：Gemini 結構化輸出 `{when, text}`（prompt 帶入現在的台北時間），程式再擋掉過去的時間、缺少的時間 — done when：過去、缺少時間、正常三種輸入各有單元測試（AC7） (depends: T1)
  - 2026-09-29 完成（未部署）：`app/reminder_parse.py`。Gemini 只負責拆欄位（date／hour／minute／period_given／text，結構化輸出），時間規則都在 `resolve()`。使用者決定：只說「9 點」取最近的未來時間（09:00 或 21:00）；只給日期沒給時間就預設 09:00。156 passed，mutation 17/17；第一輪「刪掉轉台北時間」那個 mutant 沒被抓到，補了台北凌晨時 UTC 還是前一天的測試才抓到。pydantic schema 已離線用 SDK（genai 2.25.0）轉換確認 nullable 正確。**沒打真的 Gemini**：本機金鑰是付費的，改在 T13 部署後用免費金鑰實測。
- [x] T11：把提醒指令接進文字處理：「…提醒我…」建立、「我的提醒」列出、quick reply 取消 — done when：單元測試走完建立 → 列出 → 取消 (depends: T4, T9, T10)
  - 2026-09-29 完成（未部署）：文字裡有「提醒我」就建立提醒，剛好是「我的提醒」就列出，其他文字照舊走 RAG；取消用 postback `action=cancel_reminder&id=…`，在上傳 session 檢查之前處理。列表、「已設定」的回覆都附取消按鈕（最多 13 個、標籤最多 20 字）；reply token 過期改走 push 時，按鈕會一起帶上。Sheets 呼叫用 `asyncio.to_thread`。174 passed，mutation 13/14：第一輪沒抓到 push 帶按鈕的兩個 mutant，補了測試；剩下的 C2（取消前查表改成查所有人）是等價 mutant，因為 `store.cancel` 本身就會擋非本人，對外行為相同。查了 LINE 方案（tw.linebiz.com，2026-09-29）：輕用量方案每月免費 push 200 則，11/1 調價只影響中、高用量方案；reply 不算則數。「Gemini AI」帳號實際用哪個方案沒登入後台確認。已知限制：句子裡只要有「提醒我」就會走提醒流程，例如「請提醒我建築法規第幾條」會被反問時間。
- [x] T12：`GET /cron/tick?key=…`：驗證密鑰，把到期的提醒 push 出去並標記已送出 — done when：密鑰錯誤回 403；替身 store 裡的到期提醒被送出，而且只送一次 (depends: T9)
  - 2026-09-29 完成（未部署）：`app/reminder_tick.py` 和 `/cron/tick`（GET 和 HEAD 都接受，因為 UptimeRobot 的 HTTP 監控預設用 HEAD，出處是 Reddit、不是官方文件）。使用者決定：先推送再標記、推送失敗就留到下一輪重試、晚超過 10 分鐘要註明。重疊的 tick 用 process 內的鎖擋掉（Render 只跑一個 instance）。store 故障回 503，讓監控發出警報。CRON_SECRET 沒設就回 403。190 passed，mutation 15/15（第一輪 K10「mark_sent 回 False 也算成已送出」沒被抓到，已補測試）。注意：key 放在 query string，uvicorn 的 access log 會印出來；影響範圍只有「能提早觸發 tick」，而 tick 只會送本來就到期的提醒。自訂 header 是 UptimeRobot 付費功能。
- [ ] T13：部署：Render 環境變數（新模型 id、`CRON_SECRET`、Sheets 憑證、`REMINDER_SHEET_ID`，**金鑰由使用者貼上**）；UptimeRobot 每 5 分鐘呼叫 `/cron/tick`（使用者登入） — done when：AC6（10 分鐘後的提醒準時到）、AC8（閒置 1 小時後 30 秒內回覆），手動各測一次 (depends: T1–T12)
  - 2026-09-30 進度：Render 新增 7 個、改 1 個環境變數（使用者用「Import from .env」貼上機密值，按鈕由我按；貼上前重複的舊 GEMINI_MODEL 那列先刪掉）。UptimeRobot 監控「LINE assistant reminder tick」每 5 分鐘、逾時 60 秒；官方畫面證實免費方案只能用 HEAD（選 HTTP method 要付費）。Render 免費時數是整個 workspace 共用 750 小時（render.com/docs/free），全天開著 31 天就是 744 小時；9 月兩個服務合計只用 5.37 小時，使用者選擇維持每 5 分鐘。**AC6 通過**：07:52 設定「今天 8:05 提醒我測試提醒」（Gemini 正式解析成 08:05），08:06:49 標記 sent，使用者 08:06 在 LINE 收到。上線後發現 sent_at 帶微秒，已修正成寫到秒並補測試。**AC8 還沒測**（要閒置 1 小時後再傳訊息）。另外發現：gemini-3.8-flash 這次回答法規題時沒有呼叫 File Search，沒有出處也沒有待查證行，需要另開任務處理。
- [ ] T14：更新 `CLAUDE.md` 和 `spec/architecture.md`：部署在 Render 而不是 Cloud Run，補上模型、KB、提醒的設計 — done when：文件裡找不到過時的 Cloud Run 部署說明 (depends: T13)

- [ ] T15（2026-09-29 新增）：查清楚 Render 上設了 `GCS_BUCKET`，卻沒有任何 GCP 憑證的環境變數。`gcs.Client()` 很可能在上傳檔案時失敗 — done when：在 Render log 確認一次上傳走的是 GCS 還是失敗；失敗的話，改成拿掉 `GCS_BUCKET`（走本機 fallback）或補上憑證 (depends: T0)

- [x] T16（2026-09-29 新增，⚠ 安全；同日完成：要帶 `X-Admin-Token`，沒設 `ADMIN_TOKEN` 時預設關閉、回 403，錯誤不回傳原始內容，`/health` 只回 status；27 passed，mutation 4/4 caught；**要 push 部署才會在線上生效**）：`GET /store/info` 不需要驗證就會列出 store 裡**所有使用者上傳的檔名**，而且出錯時把原始錯誤回給呼叫者（`main.py:69`）；`/health` 也公開了 store 名稱 — done when：沒帶密鑰呼叫 `/store/info` 回 403（或整個端點移除），`/health` 只回 `{"status": "ok"}`，都有測試 (depends: T0)。**建議排在 T3 之前做**，因為它現在就在線上。

- [x] T18（2026-09-29 新增，使用者截圖發現）：LINE 不支援 Markdown，模型回答裡的 `**粗體**`、`## 標題` 會原樣顯示 — done when：送出前把 Markdown 轉成純文字（粗體去掉 `**`、標題去掉 `#`，條列保留），有測試；prompt 也改成要求不用 Markdown (depends: T3)
  - 2026-09-29 完成（未部署）：新增 `app/formatting.py` 的 `to_plain_text`，在 `_with_sources` 裡套用，文字、圖片主路徑、圖片 fallback 三條回答路徑都會經過；來源 footer 不經過轉換，檔名裡的 `_` 不會被吃掉。粗體、斜體、標題、行內程式碼、程式碼區塊、連結、引用、分隔線都轉成純文字；`*`/`-`/`+` 條列改成「・」並保留縮排，編號清單不動，表格改成用「｜」分隔的行。SYSTEM_PROMPT 加第 6 條要求不用 Markdown。圖片主路徑沒有帶 SYSTEM_PROMPT，這部分靠轉換處理。94 passed，mutation 13/13 抓到。

**可以平行做的**：T5 可以隨時先做；T0 完成後，T1/T3/T4/T9 彼此獨立。

## 風險

1. ⚠ **免費方案的資料會被 Google 用來改進產品**（官方價目表每個模型都標「用於改善我們的產品：是」，付費方案標「否」）。上傳公司文件前要使用者決定，見下方待決。
2. **LINE push 有每月免費則數上限**：提醒和慢回覆都會用到 push。T11 開工前先查「System／Gemini AI」官方帳號方案的免費則數。
3. **`OR` filter 沒驗證過**：T5 就是為了先排除這個風險，T7 之前一定要做。
4. **UptimeRobot 保溫同時也是提醒的觸發來源**：監控停了，提醒就不會送。T13 要把這個依賴寫進 CLAUDE.md。

## 決定

- 2026-09-29 使用者選 (a)：維持免費方案，照常上傳，但只上傳不敏感的資料，由使用者自己把關（知情同意：免費方案的內容會被 Google 用來改進產品）。T3 的 prompt 或上傳確認訊息裡要加一句提醒。
- [ ] T19（2026-09-30 新增，T13 驗收時發現）：gemini-3.8-flash 回答法規題時不一定會呼叫 File Search，沒有出處、也沒有待查證行。SDK（google-genai 2.25.0）沒有強制內建工具的設定（`FunctionCallingConfigMode.ANY` 只管 function_declarations）。使用者決定：提示詞加強＋用關鍵字判斷是否為法規題，沒有引用 KB 就由程式加警告；改之前、改之後各用本機金鑰量一次 10 題的檢索率 — done when：關鍵字判斷有單元測試（法規題與一般題兩種都有）；法規題沒有 KB 來源時一定會出現警告（測試）；改前、改後的檢索率都有數字記錄 (depends: T7, T18)
  - 2026-09-30 進度（程式已完成、未部署）：`is_law_question` 關鍵字判斷，法規題沒有 KB 來源就附 LAW_NO_KB_NOTE；提示詞第 3 條要求法規題先查知識庫。216 passed，mutation 9/9（L8 第一輪沒抓到：提示詞測試原本只檢查兩個詞，而這兩個詞在其他條也有，改成檢查整句指令）。檢索率（本機 `.env` 的 GEMINI_API_KEY 其實是免費金鑰 …WlPM，跟正式環境同一把）：改前 1/10、改後 3/10，各只量一輪。**量測把免費額度用完了**：gemini-3.8-flash 回 429 RESOURCE_EXHAUSTED，正式環境會退到 3.5-flash-lite。診斷發現：3.5-flash-lite 回答「防火區劃面積上限」時呼叫了 File Search 11 次、全部 0 份文件，所以至少有一部分問題是「有查、查不到」，不是「不查」。下一步要等額度重置，查為什麼查不到（metadata filter？chunk 內容？查詢語言？），而且要避開正式環境的金鑰。
  - 2026-09-30 離線調查（沒有打生成 API）：SDK 沒有「只檢索、不生成」的方法，檢索只能透過 generateContent 測。逐題對照本機 HJPLUS raw 的內容：樓梯、走廊、無障礙坡道都有專門內容（改後都命中）；排煙（有專門文件，「排煙」出現 33 次）、屋頂欄杆（樓梯欄杆坡道）有內容卻 0，是**真的漏掉**；建蔽率（資料夾其實是容積免計）、天花板淨高（只有地方法規）內容很弱；防火區劃（只有順帶提到）、停車位、步行距離（332 份都沒有）是**知識庫本身沒有**，0 是對的。結論：檢索修到完美，這 10 題的上限大約 5 題；更大的缺口是 KB 沒有條文原文。候選補法：`F:\01  CHOU\09_AI相關資料\openlawtw`（19,441 條、中央法規快照 2026-09-18）。
