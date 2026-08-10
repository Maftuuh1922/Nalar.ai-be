$proc = Start-Process -FilePath '.\venv\Scripts\python.exe' -ArgumentList 'run_dev.py' -WorkingDirectory (Get-Location) -RedirectStandardOutput 'server-codex.out.log' -RedirectStandardError 'server-codex.err.log' -WindowStyle Hidden -PassThru
Write-Output "Started backend PID $($proc.Id)"
