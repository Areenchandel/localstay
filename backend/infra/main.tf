terraform {
  required_providers {
    aws     = { source = "hashicorp/aws", version = "~> 5.0" }
    archive = { source = "hashicorp/archive", version = "~> 2.0" }
  }
}

variable "region" {
  default = "ap-south-1"
}
variable "budget_email" {
  type = string
}
variable "allowed_origin" {
  default = "*" # put your CloudFront URL here later
}
variable "demo_payments" {
  default = "true" # set "false" once the Razorpay webhook is live
}

provider "aws" {
  region = var.region
}

# ---------- database: on-demand, so ~zero cost when idle ----------
resource "aws_dynamodb_table" "main" {
  name         = "LocalStay"
  billing_mode = "PAY_PER_REQUEST"
  hash_key     = "PK"
  range_key    = "SK"

  attribute {
    name = "PK"
    type = "S"
  }
  attribute {
    name = "SK"
    type = "S"
  }
  attribute {
    name = "GSI1PK"
    type = "S"
  }
  attribute {
    name = "GSI1SK"
    type = "S"
  }
  attribute {
    name = "GSI2PK"
    type = "S"
  }
  attribute {
    name = "GSI2SK"
    type = "S"
  }

  global_secondary_index {
    name            = "GSI1"
    hash_key        = "GSI1PK"
    range_key       = "GSI1SK"
    projection_type = "ALL"
  }
  global_secondary_index {
    name            = "GSI2"
    hash_key        = "GSI2PK"
    range_key       = "GSI2SK"
    projection_type = "ALL"
  }
  ttl {
    attribute_name = "ttl"
    enabled        = true
  }
}

# ---------- Lambda (ARM, 7-day logs) with least-privilege IAM ----------
resource "aws_cloudwatch_log_group" "fn" {
  name              = "/aws/lambda/localstay-api"
  retention_in_days = 7
}

data "aws_iam_policy_document" "assume" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["lambda.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "fn" {
  name               = "localstay-api-role"
  assume_role_policy = data.aws_iam_policy_document.assume.json
}

resource "aws_iam_role_policy" "fn" {
  role = aws_iam_role.fn.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect   = "Allow"
        Action   = ["dynamodb:GetItem", "dynamodb:PutItem", "dynamodb:UpdateItem", "dynamodb:Query", "dynamodb:Scan"]
        Resource = [aws_dynamodb_table.main.arn, "${aws_dynamodb_table.main.arn}/index/*"]
      },
      {
        Effect   = "Allow"
        Action   = ["s3:PutObject", "s3:GetObject"]
        Resource = "${aws_s3_bucket.site.arn}/photos/*"
      },
      {
        Effect   = "Allow"
        Action   = ["ssm:GetParameter", "ssm:GetParameters"]
        Resource = "arn:aws:ssm:${var.region}:${data.aws_caller_identity.me.account_id}:parameter/localstay/razorpay/*"
      },
      {
        Effect    = "Allow"
        Action    = ["kms:Decrypt"]
        Resource  = "*"
        Condition = { StringEquals = { "kms:ViaService" = "ssm.${var.region}.amazonaws.com" } }
      },
      {
        Effect   = "Allow"
        Action   = ["ses:SendEmail"]
        Resource = "*"
      },
      {
        Effect   = "Allow"
        Action   = ["logs:CreateLogStream", "logs:PutLogEvents"]
        Resource = "${aws_cloudwatch_log_group.fn.arn}:*"
      }
    ]
  })
}

data "archive_file" "api" {
  type        = "zip"
  source_dir  = "${path.module}/../api"
  output_path = "${path.module}/api.zip"
}

resource "aws_lambda_function" "api" {
  function_name    = "localstay-api"
  role             = aws_iam_role.fn.arn
  handler          = "handler.handler"
  runtime          = "python3.12"
  architectures    = ["arm64"]
  filename         = data.archive_file.api.output_path
  source_code_hash = data.archive_file.api.output_base64sha256
  timeout          = 10
  memory_size      = 256

  environment {
    variables = {
      TABLE         = aws_dynamodb_table.main.name
      DEMO_PAYMENTS = var.demo_payments
      PHOTO_BUCKET  = aws_s3_bucket.site.bucket
      PHOTO_BASE    = "https://${aws_cloudfront_distribution.site.domain_name}"
      RAZORPAY_PARAM_PREFIX = "/localstay/razorpay"
      SENDER_EMAIL          = var.budget_email
    }
  }
  depends_on = [aws_cloudwatch_log_group.fn]
}

# ---------- login: Cognito (first 10,000 users free) ----------
resource "aws_cognito_user_pool" "up" {
  name                     = "localstay"
  username_attributes      = ["email"]
  auto_verified_attributes = ["email"]
  password_policy {
    minimum_length = 8
  }
}

resource "aws_cognito_user_pool_client" "web" {
  name                = "localstay-web"
  user_pool_id        = aws_cognito_user_pool.up.id
  generate_secret     = false
  explicit_auth_flows = ["ALLOW_USER_SRP_AUTH", "ALLOW_USER_PASSWORD_AUTH", "ALLOW_REFRESH_TOKEN_AUTH"]
}

# ---------- API: HTTP API (cheaper than REST API) ----------
resource "aws_apigatewayv2_api" "api" {
  name          = "localstay"
  protocol_type = "HTTP"
  cors_configuration {
    allow_origins = [var.allowed_origin]
    allow_methods = ["GET", "POST", "OPTIONS"]
    allow_headers = ["authorization", "content-type"]
  }
}

resource "aws_apigatewayv2_authorizer" "jwt" {
  api_id           = aws_apigatewayv2_api.api.id
  authorizer_type  = "JWT"
  name             = "cognito"
  identity_sources = ["$request.header.Authorization"]
  jwt_configuration {
    audience = [aws_cognito_user_pool_client.web.id]
    issuer   = "https://${aws_cognito_user_pool.up.endpoint}"
  }
}

resource "aws_apigatewayv2_integration" "fn" {
  api_id                 = aws_apigatewayv2_api.api.id
  integration_type       = "AWS_PROXY"
  integration_uri        = aws_lambda_function.api.invoke_arn
  payload_format_version = "2.0"
}

locals {
  routes = { # route => needs login?
    "GET /listings"                      = false
    "GET /listings/{id}"                 = false
    "POST /listings"                     = true
    "POST /listings/{id}/photo-url"      = true
    "POST /listings/{id}/photos"         = true
    "POST /bookings"                     = true
    "GET /bookings/me"                   = true
    "POST /bookings/{lid}/{bid}/confirm" = true
    "POST /bookings/{lid}/{bid}/pay-order" = true
    "POST /bookings/{lid}/{bid}/verify"  = true
    "GET /config"                        = false
    "POST /razorpay/webhook"             = false
    "POST /reviews"                      = true
    "POST /local/apply"                  = true
    "GET /local/me"                      = true
    "POST /local/reviews"                = true
    "GET /local/reviews"                 = false
  }
}

resource "aws_apigatewayv2_route" "r" {
  for_each           = local.routes
  api_id             = aws_apigatewayv2_api.api.id
  route_key          = each.key
  target             = "integrations/${aws_apigatewayv2_integration.fn.id}"
  authorization_type = each.value ? "JWT" : "NONE"
  authorizer_id      = each.value ? aws_apigatewayv2_authorizer.jwt.id : null
}

resource "aws_apigatewayv2_stage" "default" {
  api_id      = aws_apigatewayv2_api.api.id
  name        = "$default"
  auto_deploy = true
  default_route_settings { # protects your free tier from abuse
    throttling_burst_limit = 20
    throttling_rate_limit  = 10
  }
}

resource "aws_lambda_permission" "apigw" {
  statement_id  = "AllowAPIGateway"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.api.function_name
  principal     = "apigateway.amazonaws.com"
  source_arn    = "${aws_apigatewayv2_api.api.execution_arn}/*/*"
}

# ---------- surprise-bill protection ----------
resource "aws_budgets_budget" "monthly" {
  name         = "localstay-monthly"
  budget_type  = "COST"
  limit_amount = "5"
  limit_unit   = "USD"
  time_unit    = "MONTHLY"
  notification {
    comparison_operator        = "GREATER_THAN"
    threshold                  = 80
    threshold_type             = "PERCENTAGE"
    notification_type          = "ACTUAL"
    subscriber_email_addresses = [var.budget_email]
  }
}

output "api_url" {
  value = aws_apigatewayv2_api.api.api_endpoint
}
output "user_pool_id" {
  value = aws_cognito_user_pool.up.id
}
output "client_id" {
  value = aws_cognito_user_pool_client.web.id
}

# Sender address for booking emails. AWS emails this address a link; click it once.
resource "aws_ses_email_identity" "sender" {
  email = var.budget_email
}
