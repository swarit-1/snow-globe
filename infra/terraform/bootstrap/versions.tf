# One-time bootstrap for Snow Globe's Terraform state (PRD §11.3, adapted from Oculus infra/terraform/bootstrap).
#
# Creates only the S3 bucket that holds the root module's state. The root module locks that state with
# S3-native locking (`use_lockfile = true`, generally available since Terraform 1.11), so there is no
# DynamoDB table. Oculus's GitHub OIDC deploy role is deliberately not carried over: GitHub Actions runs CI
# only (PRD §11.3), and only the owner applies.
#
# This module keeps its own state locally (bootstrap/terraform.tfstate, gitignored). See infra/README.md.

terraform {
  required_version = ">= 1.11, < 2.0.0"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 6.73"
    }
  }
}

provider "aws" {
  region = var.region

  default_tags {
    tags = {
      project    = var.project
      managed_by = "terraform-bootstrap"
    }
  }
}
