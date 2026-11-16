# 04 — Deploy, infrastructure, security

Back to the index: [CLAUDE.md](../../CLAUDE.md).

The same pattern as the KuCoin bot (`future-trading-kucoins`), with its own names: every AWS name starts `solana-future-coin`
(D-19, D-22; was `futsol-bot` until 2026-11-16). The KuCoin bot's `kucoin-future-coin` names (and the old
`futcoins-bot` ones) are never touched from here.

## AWS (account 267850879774, eu-central-1 only)

| Thing | Name |
|---|---|
| EC2 | `solana-future-coin`, t4g.micro arm64, AL2023, 20 GB gp3 encrypted, IMDSv2, no key pair, public IP (not Elastic: paper needs none, B-3), no inbound rule |
| Network | aws-foundation's shared VPC `vpc-06f42723ee12cfa5b`, public subnets a/c/b (deploy tries them in turn) |
| Role | `solana-future-coin-role` (boundary `gha-workload-boundary`): SSM core, ECR pull, read `Kucoin-bot`, own bucket, PutObject `bigdata-shared-history-<acct>/futures/*` |
| ECR | `solana-future-coin` (`:latest` + `:sha-<commit>`, keep 10) |
| Bucket | `solana-future-coin-data-<acct>`: `live/` `daily/` backups, `host/` scripts, `diag/` evidence (30 days) |
| State | `terraform-state-267850879774/future-trading-solana-coins/terraform.tfstate` (lockfile) |

## Before the FIRST deploy (user, in this order)

1. **aws-foundation B-34**: `terraform plan` / `apply` / `./scripts/verify.sh`. Sets this repo's prefix to
   `solana-future-coin` (state `future-trading-solana-coins/`; B-33 of 2026-11-16 had added it as `futsol-bot`).
2. **Pump-fun-S3 push** (its D-129): the shared bucket's policy then lets `solana-future-coin-role` write `futures/*`.
   Without it the recordings wait in the spool and `statusCheck.sh` section 5 fails (B-4).
3. **This repo**: personal git identity (set: `Tafehi` / `ehsan.tafehi@gmail.com`), `./precommit.sh`, first commit,
   `scripts/verify_clone.sh`, push to `main`. The first deploy creates the box (t4g.micro), the ECR repo and the bucket.
   No backup yet, so the bot starts a fresh paper account (by design, unlike the KuCoin bot).
4. **~2026-12-06 (B-4)**: Pump-fun-S3's stack is destroyed; the shared bucket and its policy need a new owner first.

## CI (`.github/workflows/`)

- `deploy.yml` on push to `main`: test (checks, ruff, mypy, pytest, mutants, shell, terraform validate) ->
  infrastructure (OIDC `gha-aws-foundation-apply`, adopt existing resources, plan/apply per AZ) -> image (arm64 push).
  Infra runs before the image so the first deploy creates the ECR repo it pushes to.
- `terraform-plan.yml` on PRs: `gha-aws-foundation-plan`, `-lock=false`, plan in the job summary.
- No job sets `environment:` (the roles trust only `main` and PRs; `scripts/checks.py` enforces it).

## The box pulls everything (CI never runs a command on it)

`user_data.sh` (first boot only) -> `hostSetup.sh` (re-runnable). Timers: ECR login 6 h, `.env` refresh 1 h, host-file
sync 5 min, image update 5 min (restart only on a new image id), SQLite backup hourly :17, spool upload 15 min,
status check daily 06:23 UTC. SSM Session Manager is the only way in; sshd is masked once SSM is proven.
Rollback: retag an older `sha-` image as `latest` in ECR.

## Never

Use local AWS credentials (global rule). Put a secret in a file. Add an inbound rule or an SSH key (aws-foundation
guard rails refuse both). Read `Kucoin-bot` from Terraform (CI roles cannot).
