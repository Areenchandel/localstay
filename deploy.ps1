# One command deploy: updates AWS (Terraform) and uploads the website.
#   .\deploy.ps1
param([string]$Email = "areenchandel0111@gmail.com")
$ErrorActionPreference = "Stop"
$root = $PSScriptRoot

Set-Location "$root\backend\infra"
terraform apply -auto-approve -var "budget_email=$Email"
if ($LASTEXITCODE -ne 0) { throw "terraform apply failed. Read the error above." }

$bucket = terraform output -raw site_bucket
aws s3 cp "$root\frontend\index.html" "s3://$bucket/index.html" --content-type text/html
if ($LASTEXITCODE -ne 0) { throw "website upload failed. Read the error above." }

Write-Host ""
Write-Host "Done. Live at: $(terraform output -raw site_url)" -ForegroundColor Green
Set-Location $root
