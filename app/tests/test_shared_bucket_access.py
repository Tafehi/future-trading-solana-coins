"""The shared history bucket (bigdata-shared-history-<account>) lets this bot in by its OWN role's tags (Pump-fun-S3
D-131): the bucket policy lists no project, so this repo owns its access. Without the tags, the bucket policy denies
every read and every futures/ write; without the IAM grants, AWS denies them anyway. Both halves are pinned here."""

from pathlib import Path

IAM = (Path(__file__).resolve().parents[2] / "infra" / "iam.tf").read_text()


def _block(start: str) -> str:
    i = IAM.index(start)
    j = IAM.find('\nresource "', i + 1)
    return IAM[i : j if j > 0 else len(IAM)]


def test_the_role_carries_both_opt_in_tags() -> None:
    role = _block('resource "aws_iam_role" "bot"')
    assert '"bigdata-history-read"  = "yes"' in role
    assert '"bigdata-history-write" = "futures"' in role


def test_the_role_may_read_the_shared_bucket() -> None:
    rd = _block('resource "aws_iam_role_policy" "shared_history_read"')
    assert 'Action   = "s3:GetObject"' in rd
    assert 'Resource = "arn:aws:s3:::${local.shared_bucket}/*"' in rd
    assert 'Action   = "s3:ListBucket"' in rd
    assert "Delete" not in rd and "PutObject" not in rd


def test_the_role_writes_only_futures_and_never_deletes() -> None:
    wr = _block('resource "aws_iam_role_policy" "shared_futures_write"')
    assert 'Resource = "arn:aws:s3:::${local.shared_bucket}/futures/*"' in wr
    assert "Delete" not in wr
