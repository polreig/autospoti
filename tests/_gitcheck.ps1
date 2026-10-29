$out = Join-Path $env:TEMP 'autospoti_gitcheck.txt'
Set-Location 'C:\Users\polre\Documents\autospoti'
$lines = @()
$lines += 'git: ' + (git --version 2>&1)
$lines += 'user.name set: ' + [bool](git config user.name 2>$null)
$lines += 'user.email set: ' + [bool](git config user.email 2>$null)
$lines += 'tiene .git: ' + (Test-Path .git)
$lines += 'gh instalado: ' + [bool](Get-Command gh -ErrorAction SilentlyContinue)
$lines | Out-File -Encoding utf8 $out
