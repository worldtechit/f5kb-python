"""Upload config.yaml's `types:` block to s3://<bucket>/lambda/config/types.json.

The cloud pipeline needs this file: without it the Dump Lambda falls back to
document_type = type_key, and the underscored key ("Support_Solution") never
matches Coveo's real filter value ("Support Solution") — multi-word types
return zero articles. It also loses the per-type metadata/content field split,
so bodies indexed by Coveo (e.g. Policy's sfdetails__c) never land in content.

Run after every config.yaml change, per stage:

    uv run python scripts/sync_lambda_config.py --bucket f5kb-articles-<acct>-staging
    make sync-config BUCKET=f5kb-articles-<acct>-staging

Use --dry-run to print the JSON without uploading.
"""

from __future__ import annotations

import argparse
import json
import sys

S3_KEY = "lambda/config/types.json"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--bucket", help="target S3 bucket (required unless --dry-run)")
    parser.add_argument("--config", default="config.yaml", help="path to config.yaml")
    parser.add_argument("--dry-run", action="store_true", help="print JSON, do not upload")
    args = parser.parse_args()

    from f5kb.config.loader import types_for_lambda

    types = types_for_lambda(args.config)
    body = json.dumps(types, indent=2) + "\n"

    if args.dry_run:
        print(body)
        return 0

    if not args.bucket:
        parser.error("--bucket is required unless --dry-run")

    import boto3

    boto3.client("s3").put_object(
        Bucket=args.bucket,
        Key=S3_KEY,
        Body=body.encode("utf-8"),
        ContentType="application/json",
    )
    print(f"uploaded {len(types)} type configs to s3://{args.bucket}/{S3_KEY}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
