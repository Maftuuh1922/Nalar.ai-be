@echo off
echo Memulai Backend (Nalar.ai-be)...
start "Nalar Backend" cmd /k "call venv\Scripts\activate.bat && python -m uvicorn app.main:app --host 127.0.0.1 --port 8087 --reload"

echo Memulai Frontend (Nalar.ai_fe)...
cd ..\Nalar.ai_fe
start "Nalar Frontend" cmd /k "npm run dev"

echo Selesai! Server berjalan di jendela terpisah.
