# Private data bucket (PRD §8.7, §15.2) and the ECR repository for the Lambda image.
# Adapted from Oculus storage.tf. Nothing here is public: every frame stays private until NPS confirms (PRD §15).

# ---- data bucket: frames/, capture_log/, forecast/, forecast_log/, state/, cache/ (CLAUDE.md data layout) ------
# Versioned (see aws_s3_bucket_versioning.data). Each job's key access is in local.jobs (main.tf).

resource "aws_s3_bucket" "data" {
  bucket        = local.data_bucket
  force_destroy = var.data_bucket_force_destroy
}

resource "aws_s3_bucket_ownership_controls" "data" {
  bucket = aws_s3_bucket.data.id

  rule {
    object_ownership = "BucketOwnerEnforced"
  }
}

resource "aws_s3_bucket_public_access_block" "data" {
  bucket                  = aws_s3_bucket.data.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_server_side_encryption_configuration" "data" {
  bucket = aws_s3_bucket.data.id

  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "AES256"
    }
  }
}

# Frames, forecast files and logs are evaluation evidence (PRD §6.2: scoring reads the forecast file the app
# showed). The workers may overwrite keys (s3:PutObject), and S3Storage appends to a daily log by rewriting the
# whole object, so a buggy run could replace or truncate evidence. With versioning the previous version survives
# for var.data_noncurrent_version_days (lifecycle below), and no worker role may delete a version.
# S3's conditional writes and deletes, which lookout_common's S3 lock uses, act on the current version.
resource "aws_s3_bucket_versioning" "data" {
  bucket = aws_s3_bucket.data.id

  versioning_configuration {
    status = "Enabled"
  }
}

resource "aws_s3_bucket_policy" "data" {
  bucket = aws_s3_bucket.data.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Sid       = "DenyInsecureTransport"
      Effect    = "Deny"
      Principal = "*"
      Action    = "s3:*"
      Resource  = [local.data_bucket_arn, "${local.data_bucket_arn}/*"]
      Condition = { Bool = { "aws:SecureTransport" = "false" } }
    }]
  })

  depends_on = [aws_s3_bucket_public_access_block.data]
}

resource "aws_s3_bucket_lifecycle_configuration" "data" {
  bucket = aws_s3_bucket.data.id

  # cache/ holds only deletable files (GRIB subsets and the like), so its old versions go after a day too.
  rule {
    id     = "expire-cache"
    status = "Enabled"

    filter {
      prefix = "cache/"
    }

    expiration {
      days = local.cache_expiration_days
    }

    noncurrent_version_expiration {
      noncurrent_days = 1
    }
  }

  # Nothing current expires outside cache/. Previous versions (an overwrite, a released lock) are kept for
  # var.data_noncurrent_version_days, then removed, and so are delete markers left with no versions.
  rule {
    id     = "expire-noncurrent-versions"
    status = "Enabled"

    filter {}

    noncurrent_version_expiration {
      noncurrent_days = var.data_noncurrent_version_days
    }

    expiration {
      expired_object_delete_marker = true
    }
  }

  # Every logged record rewrites the whole day's log (S3 has no append), so old log versions pile up by the
  # gigabyte; they go sooner. Where rules overlap, S3 honours the shorter expiration.
  dynamic "rule" {
    for_each = local.log_prefixes

    content {
      id     = "expire-old-log-versions-${trimsuffix(rule.value, "/")}"
      status = "Enabled"

      filter {
        prefix = rule.value
      }

      noncurrent_version_expiration {
        noncurrent_days = var.log_noncurrent_version_days
      }
    }
  }

  rule {
    id     = "abort-incomplete-multipart-uploads"
    status = "Enabled"

    filter {}

    abort_incomplete_multipart_upload {
      days_after_initiation = 1
    }
  }

  # Noncurrent-version rules need versioning in place first (provider docs).
  depends_on = [aws_s3_bucket_versioning.data]
}

# ---- ECR: one image for every job ---------------------------------------------------------------------------

resource "aws_ecr_repository" "lambda" {
  name = "${var.project}/lambda"
  # Tags are git-sha based and never reused (infra/lambda/build.sh). Lambda pins the digest at deploy time, so a
  # re-pointed tag would not reach the functions anyway.
  image_tag_mutability = "IMMUTABLE"
  # Images can be rebuilt from git, so `terraform destroy` may delete them.
  force_delete = true

  image_scanning_configuration {
    scan_on_push = true
  }

  encryption_configuration {
    encryption_type = "AES256"
  }
}

resource "aws_ecr_lifecycle_policy" "lambda" {
  repository = aws_ecr_repository.lambda.name

  policy = jsonencode({
    rules = [{
      rulePriority = 1
      description  = "Keep the last ${local.ecr_keep_images} images"
      selection = {
        tagStatus   = "any"
        countType   = "imageCountMoreThan"
        countNumber = local.ecr_keep_images
      }
      action = { type = "expire" }
    }]
  })
}

# Lets the Lambda service pull the image for this project's functions only.
resource "aws_ecr_repository_policy" "lambda" {
  repository = aws_ecr_repository.lambda.name

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Sid       = "LambdaImagePull"
      Effect    = "Allow"
      Principal = { Service = "lambda.amazonaws.com" }
      Action    = ["ecr:BatchGetImage", "ecr:GetDownloadUrlForLayer"]
      Condition = {
        StringLike = {
          "aws:sourceArn" = "arn:${local.partition}:lambda:${var.region}:${local.account_id}:function:${var.project}-*"
        }
      }
    }]
  })
}
