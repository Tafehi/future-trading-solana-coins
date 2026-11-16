# future-trading-solana-coins infrastructure: one small EC2 box in FRANKFURT (eu-central-1) running the solana-future-coin bot in
# Docker. The same pattern as future-trading-kucoins (the KuCoin bot this repo was split from on 2026-11-14): no
# inbound port, SSM only, image from ECR pushed by GitHub Actions, the box pulls everything itself with systemd timers
# (CI never runs a command on it).
#
# CI deploys with the shared aws-foundation OIDC roles. They only touch names under this project's
# `resource_prefix` (solana-future-coin) and state under its `state_key_prefix` (future-trading-solana-coins). That entry must be
# in Tafehi/aws-foundation (variables.tf) and applied BY THE USER before the first deploy (.claude/docs/04-deploy.md).

terraform {
  required_version = ">= 1.10"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.70"
    }
  }

  backend "s3" {
    bucket       = "terraform-state-267850879774"
    key          = "future-trading-solana-coins/terraform.tfstate"
    region       = "eu-central-1"
    encrypt      = true
    use_lockfile = true
  }
}

provider "aws" {
  region = var.aws_region

  default_tags {
    # Plain ASCII only (S3 rejects some characters in tag values). Never Repo = aws-foundation: that tag is what
    # aws-foundation's guard rails protect.
    tags = {
      Project   = "solana-future-coin"
      ManagedBy = "terraform"
      Repo      = "future-trading-solana-coins"
    }
  }
}

data "aws_caller_identity" "current" {}

variable "aws_region" {
  type    = string
  default = "eu-central-1"
  validation {
    condition     = var.aws_region == "eu-central-1"
    error_message = "Frankfurt only (user, 2026-11-13)."
  }
}

variable "instance_type" {
  type = string
  # Speed does not matter here (user, 2026-11-13), so the cheapest Graviton size that fits: 2 burstable vCPUs, 1 GB.
  # The bot is one Python process (~200-350 MB); hostSetup.sh adds 1 GB of swap as a cushion. ai-kucoin-future ran
  # three containers on the same size. Changing it is an in-place stop/start (minutes, data kept).
  default = "t4g.micro"
}

# The shared Frankfurt network from aws-foundation (output frankfurt_vpc), adopted by ID as data, never managed here.
variable "vpc_id" {
  type    = string
  default = "vpc-06f42723ee12cfa5b" # shared-frankfurt, 10.20.0.0/16
  validation {
    condition     = can(regex("^vpc-[0-9a-f]{8,17}$", var.vpc_id))
    error_message = "vpc_id must look like vpc-..."
  }
}

variable "subnets_by_az" {
  type = map(string)
  default = {
    "eu-central-1a" = "subnet-0e3797e30a303ae8d"
    "eu-central-1b" = "subnet-0d49f4e57a3846fc5"
    "eu-central-1c" = "subnet-061962906b0c5fc42"
  }
  validation {
    condition     = alltrue([for id in values(var.subnets_by_az) : can(regex("^subnet-[0-9a-f]{8,17}$", id))])
    error_message = "every subnets_by_az value must look like subnet-..."
  }
}

# deploy.yml tries a, c, b in turn (AWS can run out of t4g in one AZ, Pump-fun-S1 DEBUG D-4). Once the box exists its
# subnet is ignored, so a later deploy never moves or replaces it.
variable "availability_zone" {
  type    = string
  default = "eu-central-1a"
  validation {
    condition     = contains(keys(var.subnets_by_az), var.availability_zone)
    error_message = "availability_zone must be one of the AZs in subnets_by_az."
  }
}

data "aws_vpc" "main" {
  id = var.vpc_id
}

data "aws_subnet" "public" {
  id = var.subnets_by_az[var.availability_zone]

  lifecycle {
    postcondition {
      condition     = self.vpc_id == var.vpc_id && self.availability_zone == var.availability_zone
      error_message = "subnet ${self.id} is not the ${var.availability_zone} subnet of VPC ${var.vpc_id}."
    }
  }
}

data "aws_ssm_parameter" "al2023_arm64" {
  name = "/aws/service/ami-amazon-linux-latest/al2023-ami-kernel-default-arm64"
}

locals {
  prefix     = "solana-future-coin"
  account_id = data.aws_caller_identity.current.account_id
  # <prefix>-data-<account>: the name aws-foundation's rule already grants, so no extra bucket entry is needed there.
  bucket_name = "${local.prefix}-data-${data.aws_caller_identity.current.account_id}"
  # The hand-made secret (user, 2026-11-13), SHARED with the KuCoin bot for now (user, 2026-11-14; BUG B-3). Named only in the instance role's policy, never read by Terraform: the CI
  # roles may only touch secrets under solana-future-coin/*, so a data source on it would fail plan and apply.
  secret_name   = "solana-future-bot"
  shared_bucket = "bigdata-shared-history-${data.aws_caller_identity.current.account_id}"
  apply_role    = "arn:aws:iam::${data.aws_caller_identity.current.account_id}:role/gha-aws-foundation-apply"
  plan_role     = "arn:aws:iam::${data.aws_caller_identity.current.account_id}:role/gha-aws-foundation-plan"
  root_arn      = "arn:aws:iam::${data.aws_caller_identity.current.account_id}:root"
  operator_user = "arn:aws:iam::${data.aws_caller_identity.current.account_id}:user/eHsan"
  object_rw     = ["s3:GetObject", "s3:GetObjectVersion", "s3:PutObject", "s3:DeleteObject", "s3:DeleteObjectVersion"]
}

# ── security group: egress only, no inbound port ───────────────────────────────────────────────────

resource "aws_security_group" "bot" {
  name        = "${local.prefix}-sg"
  description = "solana-future-coin: egress only, no inbound, SSM is the only way in"
  vpc_id      = data.aws_vpc.main.id

  # Written out, not omitted: an omitted ingress block silently keeps whatever rules already exist.
  ingress = []

  egress {
    description = "HTTPS: KuCoin, Solana RPC, Telegram, ECR, S3, Secrets Manager, SSM"
    from_port   = 443
    to_port     = 443
    protocol    = "tcp"
    cidr_blocks = ["0.0.0.0/0"]
  }

  egress {
    description = "HTTP: dnf repositories"
    from_port   = 80
    to_port     = 80
    protocol    = "tcp"
    cidr_blocks = ["0.0.0.0/0"]
  }

  egress {
    description = "DNS over TCP"
    from_port   = 53
    to_port     = 53
    protocol    = "tcp"
    cidr_blocks = ["0.0.0.0/0"]
  }

  egress {
    description = "DNS over UDP"
    from_port   = 53
    to_port     = 53
    protocol    = "udp"
    cidr_blocks = ["0.0.0.0/0"]
  }

  tags = { Name = "${local.prefix}-sg" }

  lifecycle {
    create_before_destroy = true
  }
}

# ── the box ────────────────────────────────────────────────────────────────────────────────────────

resource "aws_instance" "bot" {
  ami                         = data.aws_ssm_parameter.al2023_arm64.value
  instance_type               = var.instance_type
  subnet_id                   = data.aws_subnet.public.id
  associate_public_ip_address = true # no NAT in the shared VPC: the public IP is the only way out
  vpc_security_group_ids      = [aws_security_group.bot.id]
  iam_instance_profile        = aws_iam_instance_profile.bot.name

  # No key_name: aws-foundation's guard rails refuse a key pair, and sshd is masked by hostSetup.sh.

  root_block_device {
    volume_size = 20
    volume_type = "gp3"
    encrypted   = true
    tags        = { Name = "${local.prefix}-root" }
  }

  metadata_options {
    http_endpoint               = "enabled"
    http_tokens                 = "required" # IMDSv2 only (aws-foundation guard rail)
    http_put_response_hop_limit = 2
  }

  # Gzipped: EC2 limits user data to 16 KB. scripts/check_user_data_size.py guards the compressed size.
  user_data_base64            = base64gzip(file("${path.module}/user_data.sh"))
  user_data_replace_on_change = false

  tags = { Name = local.prefix }

  # The first boot pulls host/hostSetup.sh from the bucket; it must exist first (Pump-fun-S1 DEBUG D-24).
  depends_on = [aws_s3_object.host]

  timeouts {
    create = "10m"
  }

  lifecycle {
    # The box holds the ledger between hourly backups; destroying it must be deliberate.
    prevent_destroy = true
    ignore_changes  = [ami, user_data_base64, subnet_id]
  }
}

# No Elastic IP. Paper makes no signed KuCoin call, so the box's IP does not matter. The KuCoin key (in solana-future-bot; the same account key as the KuCoin bot's today) is
# IP-locked to the KuCoin bot's box, so LIVE from here needs its own KuCoin sub-account and key first (BUG B-3); an
# Elastic IP would come with that key.

# ── the project bucket ─────────────────────────────────────────────────────────────────────────────
#
#   live/ daily/   database backups         the instance role
#   diag/          statusCheck --evidence   the instance role (30 days)
#   host/          statusCheck.sh,          written by Terraform (apply role), read by the instance
#                  hostSetup.sh, compose
# The futures recordings go to the SHARED bucket (bigdata-shared-history-<account>/futures/), not here.

resource "aws_s3_bucket" "store" {
  bucket = local.bucket_name

  lifecycle {
    prevent_destroy = true
  }
}

resource "aws_s3_bucket_public_access_block" "store" {
  bucket                  = aws_s3_bucket.store.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_server_side_encryption_configuration" "store" {
  bucket = aws_s3_bucket.store.id
  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "AES256"
    }
  }
}

resource "aws_s3_bucket_versioning" "store" {
  bucket = aws_s3_bucket.store.id
  versioning_configuration {
    status = "Enabled"
  }
}

resource "aws_s3_bucket_lifecycle_configuration" "store" {
  bucket = aws_s3_bucket.store.id

  rule {
    id     = "live-hourly-versions"
    status = "Enabled"
    filter {
      prefix = "live/"
    }
    noncurrent_version_expiration {
      noncurrent_days           = 3
      newer_noncurrent_versions = 48
    }
  }

  rule {
    # statusCheck.sh --evidence tarballs (box logs for scripts/collect_evidence.sh): a month is plenty.
    id     = "diag-30-days"
    status = "Enabled"
    filter {
      prefix = "diag/"
    }
    expiration {
      days = 30
    }
  }

  rule {
    id     = "expire-old-versions"
    status = "Enabled"
    filter {}
    noncurrent_version_expiration {
      noncurrent_days = 30
    }
    abort_incomplete_multipart_upload {
      days_after_initiation = 7
    }
  }
}

resource "aws_s3_bucket_policy" "store" {
  bucket = aws_s3_bucket.store.id

  # Denies use ArnNotLike aws:PrincipalArn, never NotPrincipal (the bot role carries a permissions boundary), and
  # every Deny exempts the operator user/eHsan (aws-foundation trap 30).
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid       = "DenyInsecureTransport"
        Effect    = "Deny"
        Principal = "*"
        Action    = "s3:*"
        Resource  = [aws_s3_bucket.store.arn, "${aws_s3_bucket.store.arn}/*"]
        Condition = { Bool = { "aws:SecureTransport" = "false" } }
      },
      {
        Sid         = "DataIsTheInstancesOnly"
        Effect      = "Deny"
        Principal   = "*"
        Action      = local.object_rw
        NotResource = ["${aws_s3_bucket.store.arn}/host/*", aws_s3_bucket.store.arn]
        Condition   = { ArnNotLike = { "aws:PrincipalArn" = [aws_iam_role.bot.arn, local.operator_user, local.root_arn] } }
      },
      {
        Sid       = "OnlyTerraformWritesTheHostScripts"
        Effect    = "Deny"
        Principal = "*"
        Action    = ["s3:PutObject", "s3:DeleteObject", "s3:DeleteObjectVersion"]
        Resource  = "${aws_s3_bucket.store.arn}/host/*"
        Condition = { ArnNotLike = { "aws:PrincipalArn" = [local.apply_role, local.operator_user, local.root_arn] } }
      },
      {
        Sid       = "HostScriptsReadByTheBoxAndTerraform"
        Effect    = "Deny"
        Principal = "*"
        Action    = ["s3:GetObject", "s3:GetObjectVersion"]
        Resource  = "${aws_s3_bucket.store.arn}/host/*"
        Condition = {
          ArnNotLike = { "aws:PrincipalArn" = [aws_iam_role.bot.arn, local.apply_role, local.plan_role, local.operator_user, local.root_arn] }
        }
      },
      {
        Sid       = "OperatorFullControl"
        Effect    = "Allow"
        Principal = { AWS = local.operator_user }
        Action    = "s3:*"
        Resource  = [aws_s3_bucket.store.arn, "${aws_s3_bucket.store.arn}/*"]
      },
    ]
  })

  depends_on = [aws_s3_bucket_public_access_block.store]
}
