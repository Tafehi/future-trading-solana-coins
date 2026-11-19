output "instance_id" {
  description = "For: aws ssm start-session --target <this> (or Session Manager in the AWS console)"
  value       = aws_instance.bot.id
}

output "ecr_repository_url" {
  description = "Push target for the build job."
  value       = aws_ecr_repository.bot.repository_url
}

output "data_bucket" {
  description = "live/ daily/ (database backups), host/ (scripts)."
  value       = aws_s3_bucket.store.id
}

output "bot_role_arn" {
  description = "Must be in the shared bucket's futures/ write exemption (owner moving out of Pump-fun-S3, BUG B-4)."
  value       = aws_iam_role.bot.arn
}

output "next_steps" {
  description = "What a human does after the first apply."
  value       = <<-EOT
    1. Connect: AWS console -> EC2 -> ${aws_instance.bot.id} -> Connect -> Session Manager. There is no SSH.
    2. Status: sudo bash /opt/solana-future-coin/logs/scripts/statusCheck.sh
    3. Trades: sudo docker exec solana-future-coin-trader python -m app.tools.trade_log
    4. Go-live check: sudo docker exec solana-future-coin-trader python -m app.tools.golive_check
    5. Wallet pool: sudo docker exec solana-future-coin-scout python -m app.tools.wallets_report
    6. Kill switch (the trader closes everything): sudo touch /opt/solana-future-coin/data/STOP
  EOT
}
