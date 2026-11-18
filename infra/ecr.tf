# The image registry. CI pushes :latest and :sha-<commit>; the box's solana-future-coin-update.timer pulls :latest every
# 5 minutes. Name = the aws-foundation resource prefix, exactly.

resource "aws_ecr_repository" "bot" {
  name                 = local.prefix
  image_tag_mutability = "MUTABLE" # the update timer follows :latest, so the tag has to move

  image_scanning_configuration {
    scan_on_push = true
  }

  encryption_configuration {
    encryption_type = "AES256"
  }
}

resource "aws_ecr_lifecycle_policy" "bot" {
  repository = aws_ecr_repository.bot.name

  policy = jsonencode({
    rules = [{
      rulePriority = 1
      description  = "keep the last 10 images"
      selection = {
        tagStatus   = "any"
        countType   = "imageCountMoreThan"
        countNumber = 10
      }
      action = { type = "expire" }
    }]
  })
}
