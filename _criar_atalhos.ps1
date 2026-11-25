# Cria atalhos do "Postador" (fb-groups-blaster: sobe servidor + abre em janela) no Desktop e Menu Iniciar.
$OutputEncoding = [System.Text.Encoding]::UTF8
$wscript = "$env:WINDIR\System32\wscript.exe"
$vbs = 'C:\Users\Matheus\fb-groups-blaster\iniciar.vbs'
$icon = 'C:\Users\Matheus\fb-groups-blaster\postador.ico,0'
$ws = New-Object -ComObject WScript.Shell
foreach ($dir in @("$env:USERPROFILE\Desktop", "$env:APPDATA\Microsoft\Windows\Start Menu\Programs")) {
  $path = Join-Path $dir 'Postador.lnk'
  $lnk = $ws.CreateShortcut($path)
  $lnk.TargetPath = $wscript
  $lnk.Arguments = '"' + $vbs + '"'
  $lnk.WorkingDirectory = 'C:\Users\Matheus\fb-groups-blaster'
  $lnk.IconLocation = $icon
  $lnk.Description = 'Postador - postar em grupos do Facebook'
  $lnk.Save()
  Write-Output "OK: $path"
}
