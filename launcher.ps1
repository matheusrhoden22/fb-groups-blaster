# Sobe o servidor do Postador (fb-groups-blaster) se preciso e abre numa janela do Chrome.
function Test-Up {
  try { $r = Invoke-WebRequest "http://127.0.0.1:5050/api/state" -TimeoutSec 1 -UseBasicParsing -ErrorAction Stop; return ($r.StatusCode -eq 200) } catch { return $false }
}
if (-not (Test-Up)) {
  $py = "$env:LOCALAPPDATA\Programs\Python\Python310\python.exe"
  if (-not (Test-Path $py)) { $py = "python" }
  $env:PYTHONUTF8 = "1"
  Start-Process -WindowStyle Hidden -FilePath $py -ArgumentList 'server.py' -WorkingDirectory 'C:\Users\Matheus\fb-groups-blaster'
  for ($i = 0; $i -lt 24; $i++) { Start-Sleep -Milliseconds 500; if (Test-Up) { break } }
}
$chrome = $null
try { $chrome = (Get-ItemProperty 'HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\chrome.exe' -ErrorAction Stop).'(default)' } catch {}
if (-not $chrome -or -not (Test-Path $chrome)) {
  foreach ($c in @("$env:ProgramFiles\Google\Chrome\Application\chrome.exe","${env:ProgramFiles(x86)}\Google\Chrome\Application\chrome.exe","$env:LOCALAPPDATA\Google\Chrome\Application\chrome.exe")) { if (Test-Path $c) { $chrome = $c; break } }
}
if (-not $chrome) { $chrome = 'chrome' }
Start-Process $chrome -ArgumentList "--app=http://127.0.0.1:5050/","--window-size=1320,860","--window-position=110,60"
