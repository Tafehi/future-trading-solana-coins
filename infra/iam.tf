# The instance role and profile. Every name starts with solana-future-coin-, the prefix aws-foundation scopes the apply
# role to, and the role carries the workload boundary aws-foundation requires of every role CI creates.

resource "aws_iam_role" "bot" {
  name                 = "${local.prefix}-role"
  permissions_boundary = "arn:aws:iam::${local.account_id}:policy/gha-workload-boundary"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Action    = "sts:AssumeRole"
      Principal = { Service = "ec2.amazonaws.com" }
    }]
  })
}

resource "aws_iam_instance_profile" "bot" {
  name = "${local.prefix}-profile"
  role = aws_iam_role.bot.name
}

# SSM Session Manager is the only way in (aws-foundation trap 14: attach AWS's managed policy, never a copy).
resource "aws_iam_role_policy_attachment" "ssm" {
  role       = aws_iam_role.bot.name
  policy_arn = "arn:aws:iam::aws:policy/AmazonSSMManagedInstanceCore"
}

resource "aws_iam_role_policy" "ecr_pull" {
  name = "${local.prefix}-ecr-pull"
  role = aws_iam_role.bot.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect   = "Allow"
        Action   = "ecr:GetAuthorizationToken"
        Resource = "*"
      },
      {
        Effect   = "Allow"
        Action   = ["ecr:BatchCheckLayerAvailability", "ecr:GetDownloadUrlForLayer", "ecr:BatchGetImage", "ecr:DescribeImages"]
        Resource = aws_ecr_repository.bot.arn
      },
    ]
  })
}

resource "aws_iam_role_policy" "secret_read" {
  name = "${local.prefix}-secret-read"
  role = aws_iam_role.bot.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect = "Allow"
      Action = ["secretsmanager:GetSecretValue", "secretsmanager:DescribeSecret"]
      # The hand-made secret; AWS appends 6 random characters to every secret ARN, hence the -*.
      Resource = "arn:aws:secretsmanager:${var.aws_region}:${local.account_id}:secret:${local.secret_name}-*"
    }]
  })
}

resource "aws_iam_role_policy" "s3_data" {
  name = "${local.prefix}-s3-data"
  role = aws_iam_role.bot.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect   = "Allow"
        Action   = ["s3:PutObject", "s3:GetObject"]
        Resource = "${aws_s3_bucket.store.arn}/*"
      },
      {
        Effect   = "Allow"
        Action   = "s3:ListBucket"
        Resource = aws_s3_bucket.store.arn
      },
    ]
  })
}

# The futures recordings (app/record/recorder.py) go to the shared bucket under futures/ only (this bot's own folders:
# futures/jupiter/ and futures/solana-bot/). PutObject only: the host timer `solana-future-coin-spool` uploads and never deletes or
# reads there. The shared bucket's OWN policy must also exempt solana-future-coin-role for futures/*; until it does, uploads
# get AccessDenied and the files wait in the spool (BUG B-4). That policy was in Tafehi/Pump-fun-S3 infra/history.tf,
# which is being removed from AWS (2026-11-14), so it needs a new owner first.
resource "aws_iam_role_policy" "shared_futures_write" {
  name = "${local.prefix}-shared-futures-write"
  role = aws_iam_role.bot.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect   = "Allow"
      Action   = "s3:PutObject"
      Resource = "arn:aws:s3:::${local.shared_bucket}/futures/*"
    }]
  })
}
