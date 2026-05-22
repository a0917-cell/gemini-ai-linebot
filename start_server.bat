@echo off
echo ==========================================
echo 啟動 LINE Bot 多模態 RAG 伺服器
echo ==========================================
echo.
echo [1/2] 正在背景啟動 FastAPI 伺服器 (Port 8000)...
start /b uvicorn app.main:app --port 8000

echo [2/2] 正在啟動 LocalTunnel 產生對外 HTTPS 網址...
echo (請將下方出現的 your url is: https://... 複製，並加上 /webhook 填入 LINE Developer Console)
echo.
npx localtunnel --port 8000
