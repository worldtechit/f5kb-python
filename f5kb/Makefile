# SAM function build. CodeUri points here (f5kb/), so SAM copies only this
# package to its scratch dir — never .venv/ at the repo root.
# SAM's makefile builder invokes `build-<LogicalResourceId>`; all 8 functions
# share one recipe. Zip layout must be f5kb/<module>.py, so copy INTO f5kb/.
# Excludes CLI entrypoints, docs, bytecode, and this Makefile.

FUNCS := OrchestratorFunction DumpFunction EnrichFunction TrackFunction \
         ApproveFunction RestoreFunction SlackAckFunction WatchdogFunction

.PHONY: $(addprefix build-,$(FUNCS))

$(addprefix build-,$(FUNCS)):
	rsync -a \
		--exclude='__pycache__/' \
		--exclude='*.pyc' \
		--exclude='*.pyo' \
		--exclude='*.md' \
		--exclude='Makefile' \
		--exclude='cmd/' \
		--exclude='cli.py' \
		--exclude='__main__.py' \
		./ "$(ARTIFACTS_DIR)/f5kb/"
