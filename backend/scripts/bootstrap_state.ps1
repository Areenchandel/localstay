# Run ONCE from the project root:  .\backend\scripts\bootstrap_state.ps1
# Creates the S3 bucket and DynamoDB lock table that hold Terraform's state.
$ErrorActionPreference = "Stop"
$b = "localstay-tfstate-190335339393"; $r = "ap-south-1"

aws s3api create-bucket --bucket $b --region $r --create-bucket-configuration LocationConstraint=$r
if ($LASTEXITCODE -ne 0) { throw "bucket creation failed" }
aws s3api put-bucket-versioning --bucket $b --versioning-configuration Status=Enabled
aws s3api put-bucket-encryption --bucket $b --server-side-encryption-configuration "Rules=[{ApplyServerSideEncryptionByDefault={SSEAlgorithm=AES256}}]"
aws s3api put-public-access-block --bucket $b --public-access-block-configuration BlockPublicAcls=true,IgnorePublicAcls=true,BlockPublicPolicy=true,RestrictPublicBuckets=true
aws dynamodb create-table --table-name localstay-tflock --attribute-definitions AttributeName=LockID,AttributeType=S --key-schema AttributeName=LockID,KeyType=HASH --billing-mode PAY_PER_REQUEST --region $r
if ($LASTEXITCODE -ne 0) { throw "lock table creation failed" }
Write-Host "Done. Now: cd backend\infra ; terraform init -migrate-state -force-copy" -ForegroundColor Green
