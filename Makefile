# Root Makefile — convenience targets for local dev.
# SAM build targets live in layer/Makefile and src/Makefile.

.PHONY: test lint typecheck build update-deps sync-config

test:
	uv run pytest tests/

lint:
	uv run ruff check .

typecheck:
	uv run mypy f5kb/

build:
	sam build

# Upload config.yaml's types: block to s3://$(BUCKET)/lambda/config/types.json.
# REQUIRED after deploy and after every config.yaml change — without it the
# Dump Lambda queries Coveo with the underscored type key (zero results for
# multi-word types) and loses the metadata/content field split.
sync-config:
	uv run python scripts/sync_lambda_config.py --bucket $(BUCKET)

# Regenerate layer/requirements.txt from uv.lock after any dep change.
# --no-hashes avoids pip's require-hashes mode (which forces all transitive
# deps pinned); we strip only the editable project self-reference.
update-deps:
	uv export --no-dev --no-hashes --format requirements-txt \
		| grep -v '^-e ' \
		| grep -v '^\.' \
		| grep -vE '^\s*#' \
		| grep -v '^f5kb' \
		> layer/requirements.txt
