# Plan-only tests against a mocked AWS provider: the Terraform state bucket.

mock_provider "aws" {
  override_data {
    target = data.aws_caller_identity.current
    values = { account_id = "123456789012" }
  }
  override_data {
    target = data.aws_partition.current
    values = { partition = "aws" }
  }
}

# Every variable is pinned to its default, so the suite checks the shipped defaults whatever the shell exports
# (TF_VAR_region, say) or a *.auto.tfvars file in the module holds. infra/tests/test_static.sh fails if a variable
# is missing here or a pin differs from its default.
variables {
  region                  = "us-east-1"
  project                 = "snow-globe"
  state_bucket_name       = ""
  state_key               = "snow-globe/terraform.tfstate"
  noncurrent_version_days = 90
  force_destroy           = false
}

run "state_bucket_is_versioned_encrypted_and_private" {
  command = plan

  assert {
    condition     = aws_s3_bucket.tfstate.bucket == "snow-globe-tfstate-123456789012"
    error_message = "state bucket name"
  }
  assert {
    condition     = aws_s3_bucket_versioning.tfstate.versioning_configuration[0].status == "Enabled"
    error_message = "state bucket must be versioned"
  }
  assert {
    condition     = one([for r in aws_s3_bucket_server_side_encryption_configuration.tfstate.rule : r.apply_server_side_encryption_by_default[0].sse_algorithm]) == "AES256"
    error_message = "state bucket must be encrypted (SSE-S3)"
  }
  assert {
    condition = (
      aws_s3_bucket_public_access_block.tfstate.block_public_acls &&
      aws_s3_bucket_public_access_block.tfstate.block_public_policy &&
      aws_s3_bucket_public_access_block.tfstate.ignore_public_acls &&
      aws_s3_bucket_public_access_block.tfstate.restrict_public_buckets
    )
    error_message = "all four public-access blocks must be on"
  }
  assert {
    condition     = aws_s3_bucket_ownership_controls.tfstate.rule[0].object_ownership == "BucketOwnerEnforced"
    error_message = "ACLs must be disabled"
  }
  assert {
    condition     = jsondecode(aws_s3_bucket_policy.tfstate.policy).Statement[0].Effect == "Deny" && jsondecode(aws_s3_bucket_policy.tfstate.policy).Statement[0].Condition.Bool["aws:SecureTransport"] == "false"
    error_message = "state bucket must refuse non-TLS requests"
  }
  assert {
    condition     = aws_s3_bucket.tfstate.force_destroy == false
    error_message = "state bucket must not be force-destroyed by default"
  }
}

run "old_state_versions_expire" {
  command = plan

  assert {
    condition     = aws_s3_bucket_lifecycle_configuration.tfstate.rule[0].noncurrent_version_expiration[0].noncurrent_days == 90
    error_message = "superseded state versions expire after 90 days"
  }
  assert {
    condition     = length(aws_s3_bucket_lifecycle_configuration.tfstate.rule[0].expiration) == 0
    error_message = "the current state must never expire"
  }
}

run "backend_config_uses_native_s3_locking" {
  command = plan

  assert {
    condition     = strcontains(output.backend_hcl, "use_lockfile = true")
    error_message = "the root backend must lock with S3-native lock files"
  }
  assert {
    condition     = strcontains(output.backend_hcl, "bucket       = \"snow-globe-tfstate-123456789012\"")
    error_message = "backend_hcl must name the state bucket"
  }
  assert {
    condition     = strcontains(output.backend_hcl, "key          = \"snow-globe/terraform.tfstate\"") && strcontains(output.backend_hcl, "region       = \"us-east-1\"")
    error_message = "backend_hcl must carry the state key and region"
  }
  assert {
    condition     = !strcontains(output.backend_hcl, "dynamodb")
    error_message = "no DynamoDB locking"
  }
}

run "explicit_bucket_name_wins" {
  command = plan

  variables {
    state_bucket_name = "my-snow-globe-state"
  }

  assert {
    condition     = aws_s3_bucket.tfstate.bucket == "my-snow-globe-state" && strcontains(output.backend_hcl, "\"my-snow-globe-state\"")
    error_message = "state_bucket_name must reach the bucket and backend_hcl"
  }
}
