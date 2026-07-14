"""Record a deploy to s3://BUCKET/deployments/STAGE/.

Writes three keys (all non-secret — values come from samconfig.toml + git):

  deployments/{stage}/config.json           — current deploy config snapshot
  deployments/{stage}/ssm-required.json     — SSM parameter paths (names only)
  deployments/{stage}/deploy-log.jsonl      — append-only deploy history

Run after every successful `sam deploy`:

    uv run python scripts/record_deploy.py --stage staging --bucket f5kb-articles-<acct>-staging
    (deploy.sh does this automatically)
"""

from __future__ import annotations

import argparse
import datetime
import json
import subprocess
import sys
import tomllib
from pathlib import Path

# SSM parameter NAMES (not values) per stage — keep in sync with template.yaml.
_SSM_REQUIRED = [
    "/f5kb/{stage}/slack/webhook-url",
    "/f5kb/{stage}/slack/signing-secret",
]
_SSM_OPTIONAL = [
    "/f5kb/{stage}/github/token",
]


def _git(cmd: list[str]) -> str:
    try:
        return subprocess.check_output(["git"] + cmd, text=True, stderr=subprocess.DEVNULL).strip()
    except subprocess.CalledProcessError:
        return ""


def _parse_param_overrides(raw: str) -> dict[str, str]:
    """Parse samconfig `parameter_overrides` string into a dict.

    Handles: 'Key=Value Key2="quoted value with spaces"'
    """
    import shlex
    result: dict[str, str] = {}
    for token in shlex.split(raw):
        if "=" in token:
            k, _, v = token.partition("=")
            result[k.strip()] = v.strip()
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--stage", required=True, choices=["staging", "prod"])
    parser.add_argument("--bucket", required=True, help="S3 bucket name")
    parser.add_argument("--region", default="us-east-2")
    parser.add_argument("--dry-run", action="store_true", help="print JSON, do not upload")
    args = parser.parse_args()

    stage = args.stage
    bucket = args.bucket
    now = datetime.datetime.now(datetime.timezone.utc).isoformat().replace("+00:00", "Z")

    # ── Read samconfig.toml ─────────────────────────────────────────────────
    samconfig_path = Path("samconfig.toml")
    if not samconfig_path.exists():
        print("ERROR: samconfig.toml not found — run from the repo root", file=sys.stderr)
        return 1

    with open(samconfig_path, "rb") as f:
        samconfig = tomllib.load(f)

    deploy_params = samconfig.get(stage, {}).get("deploy", {}).get("parameters", {})
    stack_name = deploy_params.get("stack_name", f"f5kb-{stage}")
    region = deploy_params.get("region", args.region)
    raw_overrides = deploy_params.get("parameter_overrides", "")
    param_overrides = _parse_param_overrides(raw_overrides) if raw_overrides else {}

    # ── Gather git context ──────────────────────────────────────────────────
    git_sha = _git(["rev-parse", "HEAD"])
    git_sha_short = git_sha[:8] if git_sha else ""
    git_branch = _git(["rev-parse", "--abbrev-ref", "HEAD"])
    git_tag = _git(["describe", "--tags", "--exact-match"]) or ""
    git_dirty = bool(_git(["status", "--porcelain"]))
    deployed_by = _git(["config", "user.email"]) or ""

    # ── Build SSM param lists (names only, no values) ───────────────────────
    ssm_required = [p.format(stage=stage) for p in _SSM_REQUIRED]
    ssm_optional = [p.format(stage=stage) for p in _SSM_OPTIONAL]

    # ── config.json (current deploy state) ──────────────────────────────────
    config = {
        "schema": "f5kb.deploy.v1",
        "stage": stage,
        "stack_name": stack_name,
        "region": region,
        "bucket": bucket,
        "parameter_overrides": param_overrides,
        "ssm_required": ssm_required,
        "ssm_optional": ssm_optional,
        "last_deploy": {
            "deployed_at": now,
            "deployed_by": deployed_by,
            "git_sha": git_sha,
            "git_sha_short": git_sha_short,
            "git_branch": git_branch,
            "git_tag": git_tag,
            "git_dirty": git_dirty,
        },
    }

    # ── ssm-required.json (stable reference for new deployers) ──────────────
    ssm_doc = {
        "schema": "f5kb.deploy.v1",
        "stage": stage,
        "note": (
            "SSM SecureStrings — create these BEFORE deploying. "
            "Never put raw values in S3 or the repo."
        ),
        "create_commands": [
            f"aws ssm put-parameter --name '{p}' --type SecureString --value '...' --region {region}"
            for p in ssm_required
        ],
        "required": ssm_required,
        "optional": ssm_optional,
    }

    # ── deploy-log.jsonl entry (append-only history) ─────────────────────────
    log_entry = {
        "deployed_at": now,
        "stage": stage,
        "stack_name": stack_name,
        "deployed_by": deployed_by,
        "git_sha": git_sha,
        "git_sha_short": git_sha_short,
        "git_branch": git_branch,
        "git_tag": git_tag,
        "git_dirty": git_dirty,
    }

    if args.dry_run:
        print("=== deployments/{stage}/config.json ===".format(stage=stage))
        print(json.dumps(config, indent=2))
        print("\n=== deployments/{stage}/ssm-required.json ===".format(stage=stage))
        print(json.dumps(ssm_doc, indent=2))
        print("\n=== deployments/{stage}/deploy-log.jsonl (append) ===".format(stage=stage))
        print(json.dumps(log_entry))
        return 0

    import boto3

    s3 = boto3.client("s3", region_name=args.region)
    prefix = f"deployments/{stage}"

    # config.json — overwrite (current state)
    s3.put_object(
        Bucket=bucket,
        Key=f"{prefix}/config.json",
        Body=(json.dumps(config, indent=2) + "\n").encode("utf-8"),
        ContentType="application/json",
    )
    print(f"    uploaded s3://{bucket}/{prefix}/config.json", file=sys.stderr)

    # ssm-required.json — overwrite (stable reference)
    s3.put_object(
        Bucket=bucket,
        Key=f"{prefix}/ssm-required.json",
        Body=(json.dumps(ssm_doc, indent=2) + "\n").encode("utf-8"),
        ContentType="application/json",
    )
    print(f"    uploaded s3://{bucket}/{prefix}/ssm-required.json", file=sys.stderr)

    # deploy-log.jsonl — append (download existing, append, re-upload)
    log_key = f"{prefix}/deploy-log.jsonl"
    try:
        existing = s3.get_object(Bucket=bucket, Key=log_key)["Body"].read().decode("utf-8")
        if existing and not existing.endswith("\n"):
            existing += "\n"
    except s3.exceptions.NoSuchKey:
        existing = ""
    except Exception:
        existing = ""

    appended = existing + json.dumps(log_entry) + "\n"
    s3.put_object(
        Bucket=bucket,
        Key=log_key,
        Body=appended.encode("utf-8"),
        ContentType="application/x-ndjson",
    )
    print(f"    appended  s3://{bucket}/{log_key}", file=sys.stderr)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
