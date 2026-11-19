variable "region" {
  description = "Region of the state bucket. Keep it equal to the root module's region (us-east-1)."
  type        = string
  default     = "us-east-1"
}

variable "project" {
  description = "Name prefix, and the value of the `project` tag."
  type        = string
  default     = "lookout"

  validation {
    condition     = can(regex("^[a-z][a-z0-9-]{1,20}$", var.project))
    error_message = "project must be 2-21 lowercase letters, digits or hyphens, starting with a letter."
  }
}

variable "state_bucket_name" {
  description = "Name of the state bucket. Empty means <project>-tfstate-<account id>."
  type        = string
  default     = ""
}

variable "state_key" {
  description = "Object key of the root module's state inside the bucket (written into the backend_hcl output)."
  type        = string
  default     = "lookout/terraform.tfstate"
}

variable "noncurrent_version_days" {
  description = "Days to keep superseded state versions before S3 expires them."
  type        = number
  default     = 90

  validation {
    condition     = var.noncurrent_version_days >= 1
    error_message = "noncurrent_version_days must be at least 1."
  }
}

variable "force_destroy" {
  description = "Allow `terraform destroy` to delete the bucket while it still holds state versions. Leave false except for the final teardown."
  type        = bool
  default     = false
}
