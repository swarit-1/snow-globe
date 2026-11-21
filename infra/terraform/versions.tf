# Snow Globe's AWS infrastructure (PRD §11.2, §11.3, §15.2, §17). Serverless only:
#   Lambda (one arm64 container image, two functions) + EventBridge Scheduler + S3 + ECR
#   + CloudWatch (logs, alarms) + SNS (alarm email) + AWS Budgets.
# Adapted from Oculus infra/terraform (bootstrap/, storage.tf, monitoring.tf). Oculus's ALB, ECS/Fargate,
# ElastiCache and NAT gateway are not carried over: Oculus's own PRD puts that stack at about $60 a month.
#
# State: S3 backend with S3-native locking (use_lockfile; generally available since Terraform 1.11, so no
# DynamoDB table). The bucket comes from ./bootstrap and is passed with -backend-config (infra/README.md).
# `terraform init -backend=false` works offline for fmt, validate and `terraform test` (mocked provider).
#
# Never applied without the owner (CLAUDE.md). infra/deploy.sh is the owner's entry point.

terraform {
  required_version = ">= 1.11, < 2.0.0"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 6.73"
    }
  }

  backend "s3" {}
}

provider "aws" {
  region = var.region

  default_tags {
    tags = local.tags
  }
}
