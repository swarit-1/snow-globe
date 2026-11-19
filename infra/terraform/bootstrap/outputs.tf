output "state_bucket" {
  description = "Bucket that holds the root module's state."
  value       = aws_s3_bucket.tfstate.bucket
}

output "region" {
  description = "Region of the state bucket."
  value       = var.region
}

output "state_key" {
  description = "Object key of the root module's state."
  value       = var.state_key
}

# Write this to infra/terraform/backend.hcl (gitignored) and run
#   terraform -chdir=infra/terraform init -backend-config=backend.hcl
output "backend_hcl" {
  description = "Partial S3 backend configuration for the root module, with S3-native locking (no DynamoDB)."
  value       = <<-EOT
    bucket       = "${aws_s3_bucket.tfstate.bucket}"
    key          = "${var.state_key}"
    region       = "${var.region}"
    encrypt      = true
    use_lockfile = true
  EOT
}
