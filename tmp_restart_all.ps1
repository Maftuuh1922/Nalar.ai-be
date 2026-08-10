$ErrorActionPreference = 'SilentlyContinue'

$be = Get-NetTCPConnection -State Listen -LocalPort 8087 -ErrorAction SilentlyContinue | Select-Object -First 1
if ($be) { Stop-Process -Id $be.OwningProcess -Force; Start-Sleep -Seconds 2 }

Set-Location 'C:\Users\Administrator\Documents\project ta\Nalar.ai-be'
Start-Process -FilePath '.\venv\Scripts\python.exe' -ArgumentList 'run_dev.py' -WorkingDirectory (Get-Location) -WindowStyle Hidden -RedirectStandardOutput 'C:\Users\Administrator\Documents\project ta\Nalar.ai-be\server-codex.out.log' -RedirectStandardError 'C:\Users\Administrator\Documents\project ta\Nalar.ai-be\server-codex.err.log' -PassThru | Out-Null

$fe = Get-NetTCPConnection -State Listen -LocalPort 3000 -ErrorAction SilentlyContinue | Select-Object -First 1
if ($fe) { Stop-Process -Id $fe.OwningProcess -Force; Start-Sleep -Seconds 2 }

Set-Location 'C:\Users\Administrator\Documents\project ta\Nalar.ai_fe'
Start-Process -FilePath 'npm.cmd' -ArgumentList 'run','start','--','-p','3000' -WorkingDirectory (Get-Location) -WindowStyle Hidden -RedirectStandardOutput 'C:\Users\Administrator\Documents\project ta\fe-3000.log' -RedirectStandardError 'C:\Users\Administrator\Documents\project ta\fe-3000.err.log' -PassThru | Out-Null

Start-Sleep -Seconds 15
for ($i = 0; $i -lt 40; $i++) {
    $l = Get-NetTCPConnection -State Listen -LocalPort 8087 -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($l) { Write-Output "backend_listening_pid=$($l.OwningProcess)"; break }
    Start-Sleep -Milliseconds 500
}
for ($i = 0; $i -lt 40; $i++) {
    $l = Get-NetTCPConnection -State Listen -LocalPort 3000 -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($l) { Write-Output "frontend_listening_pid=$($l.OwningProcess)"; break }
    Start-Sleep -Milliseconds 500
}
