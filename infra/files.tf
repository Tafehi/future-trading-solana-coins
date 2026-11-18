# The host files the box pulls from s3://<bucket>/host/ every 5 minutes (solana-future-coin-sync.timer). The etag makes a
# changed file a diff in the plan, so a push that edits statusCheck.sh reaches the box by itself.

locals {
  host_files = ["statusCheck.sh", "hostSetup.sh", "docker-compose.yml"]
}

resource "aws_s3_object" "host" {
  for_each = toset(local.host_files)

  bucket       = aws_s3_bucket.store.id
  key          = "host/${each.value}"
  source       = "${path.module}/../${each.value}"
  etag         = filemd5("${path.module}/../${each.value}")
  content_type = endswith(each.value, ".yml") ? "text/yaml" : "text/x-shellscript"
}
