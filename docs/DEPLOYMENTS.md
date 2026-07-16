# DEPLOYMENTS.md — AWS environments (staging / prod)

> **⚠️ This committed copy is an intentionally BLANK template.**
> The filled-in version — real account IDs, bucket names, and deploy provenance —
> lives in **1Password** (item: _"F5KB — AWS Deployments"_), **not** in this repo.
> We deliberately do not commit account IDs or bucket names. Copy this template
> into the 1Password secure note and fill the `<…>` placeholders there.

Deployment-specific facts for this repo's two SAM/CloudFormation stages. This is
deployment state, not project narrative or working rules, so it lives here rather
than in MEMORIES.md or CLAUDE.md.

**You must be authenticated to AWS before any `aws`/`sam` command against these
stacks.** SSO profile names are deliberately NOT recorded anywhere — every teammate
uses a different local profile name for the same account. See CLAUDE.md's
"Credentials" section for the discovery protocol (try active creds → match the
account ID from 1Password in `~/.aws/config` → ask the user if ambiguous).

## Stages

| Stage   | Account ID       | Region          | Stack name     | Bucket                                 |
|---------|------------------|-----------------|----------------|----------------------------------------|
| staging | `<account-id>`   | `<region>`      | `f5kb-staging` | `f5kb-articles-<account-id>-staging`   |
| prod    | `<account-id>`   | `<region>`      | `f5kb-prod`    | `f5kb-articles-<account-id>-prod`      |

_(Fill account IDs / regions in the 1Password copy. `prod` may be "not yet deployed"
— note the first-deploy date there.)_

## Naming pattern

Most stack resources follow `f5kb-<component>-<stage>` (Lambdas, topics, alarms, log
groups, dashboards, scheduled rules). Two exceptions:
- **Queues** are `f5kb-<component>-queue-<stage>` and their DLQs `f5kb-<component>-dlq-<stage>`
  (e.g. `f5kb-dump-queue-<stage>`, `f5kb-dump-dlq-<stage>`).
- **The bucket** is `f5kb-articles-<account-id>-<stage>`.

Given a stage's row above, every resource name is derivable without a lookup table.

## Deploy provenance

Deploy provenance for a stage always lives at
`s3://f5kb-articles-<account-id>-<stage>/deployments/<stage>/` — git SHA, CFN
parameter overrides, `deployed_by`, timestamp, and the exact `template.yaml` /
`samconfig.toml` used, written by `scripts/record_deploy.py` on every
`scripts/deploy.sh` run. That bundle plus

```
aws cloudformation describe-stacks --stack-name f5kb-<stage> --region <region>
```

answers "what's deployed where" without needing repo access at all — once you're
authenticated to the right account.
