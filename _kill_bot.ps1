Get-CimInstance Win32_Process -Filter "Name='python.exe'" |
  Where-Object { $_.CommandLine -match 'main.py' -or $_.CommandLine -match '_debug_' } |
  ForEach-Object {
    Write-Output ("Killing PID " + $_.ProcessId)
    Stop-Process -Id $_.ProcessId -Force
  }
Write-Output "done"
