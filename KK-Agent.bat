@echo off
cd /d "%~dp0"
title KK-Agent

rem ---- detect if service is running (TCP probe 8788) ----
powershell -NoProfile -Command "$t=New-Object Net.Sockets.TcpClient; $ok=$t.ConnectAsync('127.0.0.1',8788).Wait(800); $t.Close(); if($ok){exit 0}else{exit 1}"
if %errorlevel%==0 goto :stop

:start
echo [KK-Agent] Starting... a browser window will pop up automatically
start "KK-Agent Server" /min cmd /c "python webapp.py"
ping -n 5 127.0.0.1 > nul
start "" http://127.0.0.1:8788
exit /b

:stop
powershell -NoProfile -Command "Get-CimInstance Win32_Process -Filter \"Name='python.exe'\" | Where-Object { $_.CommandLine -match 'webapp' } | ForEach-Object { Stop-Process -Id $_.ProcessId -Force }"
echo [KK-Agent] Stopped. Double-click again to start.
ping -n 3 127.0.0.1 > nul
