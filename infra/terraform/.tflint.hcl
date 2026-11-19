# tflint for infra/terraform and infra/terraform/bootstrap (infra/test.sh runs it when tflint or Docker exists).
#   tflint --init && tflint --recursive
# Deep checking (which calls AWS) stays off.

plugin "terraform" {
  enabled = true
  preset  = "recommended"
}

plugin "aws" {
  enabled = true
  version = "0.50.0"
  source  = "github.com/terraform-linters/tflint-ruleset-aws"
}
