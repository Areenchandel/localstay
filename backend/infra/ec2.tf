# Second deployment of the same API: Docker container on one EC2 instance.
# OFF by default. Turn on with:  terraform apply -var enable_ec2=true   (and off again with -var enable_ec2=false)

variable "enable_ec2" {
  type    = bool
  default = false
}
variable "ec2_instance_type" {
  default = "t3.micro" # free-tier eligible size
}
variable "repo_url" {
  default = "https://github.com/Areenchandel/localstay"
}

data "aws_vpc" "default" {
  count   = var.enable_ec2 ? 1 : 0
  default = true
}

data "aws_subnets" "default" {
  count = var.enable_ec2 ? 1 : 0
  filter {
    name   = "vpc-id"
    values = [data.aws_vpc.default[0].id]
  }
}

data "aws_ssm_parameter" "al2023" {
  count = var.enable_ec2 ? 1 : 0
  name  = "/aws/service/ami-amazon-linux-latest/al2023-ami-kernel-default-x86_64"
}

# Only web traffic in. No SSH port: we log in through SSM Session Manager instead.
resource "aws_security_group" "web" {
  count       = var.enable_ec2 ? 1 : 0
  name        = "localstay-web"
  description = "HTTP in, everything out"
  vpc_id      = data.aws_vpc.default[0].id

  ingress {
    from_port   = 80
    to_port     = 80
    protocol    = "tcp"
    cidr_blocks = ["0.0.0.0/0"]
  }
  egress {
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }
}

resource "aws_iam_role" "ec2" {
  count = var.enable_ec2 ? 1 : 0
  name  = "localstay-ec2-role"
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{ Effect = "Allow", Action = "sts:AssumeRole", Principal = { Service = "ec2.amazonaws.com" } }]
  })
}

resource "aws_iam_role_policy" "ec2_db" {
  count = var.enable_ec2 ? 1 : 0
  role  = aws_iam_role.ec2[0].id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect   = "Allow"
      Action   = ["dynamodb:GetItem", "dynamodb:PutItem", "dynamodb:UpdateItem", "dynamodb:Query", "dynamodb:Scan"]
      Resource = [aws_dynamodb_table.main.arn, "${aws_dynamodb_table.main.arn}/index/*"]
      }, {
      Effect   = "Allow"
      Action   = ["s3:PutObject", "s3:GetObject"]
      Resource = "${aws_s3_bucket.site.arn}/photos/*"
      }, {
      Effect   = "Allow"
      Action   = ["ses:SendEmail"]
      Resource = "*"
      }, {
      Effect   = "Allow"
      Action   = ["ssm:GetParameter", "ssm:GetParameters"]
      Resource = "arn:aws:ssm:${var.region}:${data.aws_caller_identity.me.account_id}:parameter/localstay/razorpay/*"
      }, {
      Effect    = "Allow"
      Action    = ["kms:Decrypt"]
      Resource  = "*"
      Condition = { StringEquals = { "kms:ViaService" = "ssm.${var.region}.amazonaws.com" } }
    }]
  })
}

resource "aws_iam_role_policy_attachment" "ec2_ssm" {
  count      = var.enable_ec2 ? 1 : 0
  role       = aws_iam_role.ec2[0].name
  policy_arn = "arn:aws:iam::aws:policy/AmazonSSMManagedInstanceCore"
}

resource "aws_iam_instance_profile" "ec2" {
  count = var.enable_ec2 ? 1 : 0
  name  = "localstay-ec2-profile"
  role  = aws_iam_role.ec2[0].name
}

resource "aws_instance" "api" {
  count                       = var.enable_ec2 ? 1 : 0
  ami                         = data.aws_ssm_parameter.al2023[0].value
  instance_type               = var.ec2_instance_type
  subnet_id                   = sort(data.aws_subnets.default[0].ids)[0]
  vpc_security_group_ids      = [aws_security_group.web[0].id]
  iam_instance_profile        = aws_iam_instance_profile.ec2[0].name
  associate_public_ip_address = true

  metadata_options {
    http_tokens                 = "required" # IMDSv2 only
    http_put_response_hop_limit = 2          # lets the container reach the instance role credentials
  }
  root_block_device {
    volume_size = 8
    encrypted   = true
  }

  user_data = <<-EOT
    #!/bin/bash
    dnf install -y docker git
    systemctl enable --now docker
    git clone ${var.repo_url} /opt/localstay
    cd /opt/localstay
    docker build -f backend/ec2/Dockerfile -t localstay .
    docker run -d --name localstay --restart unless-stopped -p 80:8000 \
      -e TABLE=${aws_dynamodb_table.main.name} -e AWS_DEFAULT_REGION=${var.region} \
      -e USER_POOL_ID=${aws_cognito_user_pool.up.id} -e CLIENT_ID=${aws_cognito_user_pool_client.web.id} \
      -e PHOTO_BUCKET=${aws_s3_bucket.site.bucket} -e PHOTO_BASE=https://${aws_cloudfront_distribution.site.domain_name} \
      -e RAZORPAY_PARAM_PREFIX=/localstay/razorpay -e SENDER_EMAIL=${var.budget_email} \
      -e DEMO_PAYMENTS=${var.demo_payments} localstay
  EOT

  tags = { Name = "localstay-api" }
}

output "ec2_url" {
  value = var.enable_ec2 ? "http://${aws_instance.api[0].public_ip}" : "EC2 is off (terraform apply -var enable_ec2=true)"
}
output "ec2_instance_id" {
  value = var.enable_ec2 ? aws_instance.api[0].id : ""
}
