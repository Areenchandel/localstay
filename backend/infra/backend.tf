# Terraform state lives in S3 (versioned, encrypted) with a DynamoDB lock, not on one laptop.
# Create the bucket and table ONCE with scripts/bootstrap_state.ps1, then run: terraform init -migrate-state
terraform {
  backend "s3" {
    bucket         = "localstay-tfstate-190335339393"
    key            = "localstay/terraform.tfstate"
    region         = "ap-south-1"
    dynamodb_table = "localstay-tflock"
    encrypt        = true
  }
}
