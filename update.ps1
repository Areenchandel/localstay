# One command update: takes the newest localstay-project zip from Downloads,
# puts its files into this project, then deploys.
#   .\update.ps1
$ErrorActionPreference = "Stop"
$root = $PSScriptRoot

$zip = Get-ChildItem "$env:USERPROFILE\Downloads\localstay-project*.zip" -ErrorAction SilentlyContinue |
       Sort-Object LastWriteTime -Descending | Select-Object -First 1
if (-not $zip) { throw "No localstay-project zip found in Downloads. Download it first." }
Write-Host "Using $($zip.Name) (downloaded $($zip.LastWriteTime))"

$tmp = Join-Path $env:TEMP "localstay_update"
if (Test-Path $tmp) { Remove-Item $tmp -Recurse -Force }
Expand-Archive -Path $zip.FullName -DestinationPath $tmp

# copy new files over the project; never touch Terraform state, and keep these two scripts as they are
robocopy "$tmp\localstay-project" $root /E /XF update.ps1 deploy.ps1 terraform.tfstate terraform.tfstate.backup /XD .terraform /NFL /NDL /NJH /NJS | Out-Null
if ($LASTEXITCODE -ge 8) { throw "copying files failed (robocopy code $LASTEXITCODE)" }
Remove-Item $tmp -Recurse -Force

& "$root\deploy.ps1"
