@echo off
start "" /b powershell.exe -NoProfile -Command "$url='http://127.0.0.1:8849'; for ($i=0; $i -lt 30; $i++) { try { Invoke-WebRequest -UseBasicParsing -Uri $url -TimeoutSec 1 | Out-Null; Start-Process 'http://localhost:8849'; break } catch { Start-Sleep -Seconds 1 } }"
uv run uvicorn app.main:app --port 8849 %*
