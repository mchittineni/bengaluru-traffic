# Unattended ORR travel-time collector:
# EventBridge Scheduler (every 15 min, 06:00-22:45 IST) -> Lambda -> Amazon Location routes -> S3.

terraform {
  required_version = ">= 1.5"
  required_providers {
    aws     = { source = "hashicorp/aws", version = "~> 5.70" }
    archive = { source = "hashicorp/archive", version = "~> 2.4" }
  }
}

provider "aws" {
  region = var.region
}

variable "region" {
  description = "AWS region; must offer Amazon Location Service routes (v2)."
  type        = string
  default     = "ap-south-1"
}

variable "collection_enabled" {
  description = "Set false to pause collection without destroying anything."
  type        = bool
  default     = true
}

locals {
  name = "blr-orr-traffic"
  src  = "${path.module}/.."
}

# ---------- Storage ----------

resource "aws_s3_bucket" "data" {
  bucket_prefix = "${local.name}-"
}

resource "aws_s3_bucket_public_access_block" "data" {
  bucket                  = aws_s3_bucket.data.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

# ---------- Lambda ----------

data "archive_file" "lambda" {
  type        = "zip"
  output_path = "${path.module}/build/collector.zip"

  source {
    content  = file("${local.src}/lambda_function.py")
    filename = "lambda_function.py"
  }
  source {
    content  = file("${local.src}/providers.py")
    filename = "providers.py"
  }
  source {
    content  = file("${local.src}/config.py")
    filename = "config.py"
  }
}

resource "aws_iam_role" "lambda" {
  name = "${local.name}-collector"
  assume_role_policy = jsonencode({
    Version   = "2012-10-17"
    Statement = [{ Effect = "Allow", Action = "sts:AssumeRole", Principal = { Service = "lambda.amazonaws.com" } }]
  })
}

resource "aws_iam_role_policy" "lambda" {
  role = aws_iam_role.lambda.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect   = "Allow"
        Action   = "geo-routes:CalculateRoutes"
        Resource = "arn:aws:geo-routes:${var.region}::provider/default"
      },
      {
        Effect   = "Allow"
        Action   = "s3:PutObject"
        Resource = "${aws_s3_bucket.data.arn}/raw/*"
      },
      {
        Effect   = "Allow"
        Action   = ["logs:CreateLogStream", "logs:PutLogEvents"]
        Resource = "${aws_cloudwatch_log_group.lambda.arn}:*"
      },
    ]
  })
}

resource "aws_cloudwatch_log_group" "lambda" {
  name              = "/aws/lambda/${local.name}-collector"
  retention_in_days = 14
}

resource "aws_lambda_function" "collector" {
  function_name    = "${local.name}-collector"
  role             = aws_iam_role.lambda.arn
  runtime          = "python3.13"
  handler          = "lambda_function.handler"
  filename         = data.archive_file.lambda.output_path
  source_code_hash = data.archive_file.lambda.output_base64sha256
  timeout          = 120
  memory_size      = 256

  environment {
    variables = { BUCKET = aws_s3_bucket.data.bucket }
  }

  depends_on = [aws_cloudwatch_log_group.lambda]
}

# ---------- Schedule ----------

resource "aws_iam_role" "scheduler" {
  name = "${local.name}-scheduler"
  assume_role_policy = jsonencode({
    Version   = "2012-10-17"
    Statement = [{ Effect = "Allow", Action = "sts:AssumeRole", Principal = { Service = "scheduler.amazonaws.com" } }]
  })
}

resource "aws_iam_role_policy" "scheduler" {
  role = aws_iam_role.scheduler.id
  policy = jsonencode({
    Version   = "2012-10-17"
    Statement = [{ Effect = "Allow", Action = "lambda:InvokeFunction", Resource = aws_lambda_function.collector.arn }]
  })
}

resource "aws_scheduler_schedule" "collect" {
  name                         = "${local.name}-every-15-min"
  schedule_expression          = "cron(0/15 6-22 * * ? *)"
  schedule_expression_timezone = "Asia/Kolkata"
  state                        = var.collection_enabled ? "ENABLED" : "DISABLED"

  flexible_time_window {
    mode = "OFF"
  }

  target {
    arn      = aws_lambda_function.collector.arn
    role_arn = aws_iam_role.scheduler.arn
    retry_policy {
      maximum_retry_attempts = 0 # a late retry would land in the wrong 15-min slot
    }
  }
}

# ---------- Outputs ----------

output "bucket" {
  value = aws_s3_bucket.data.bucket
}

output "function_name" {
  value = aws_lambda_function.collector.function_name
}
