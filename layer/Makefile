# Layer build — SAM copies only this directory (no .venv/ here).
# requirements.txt is pre-generated from uv.lock (run `make update-deps` to refresh).
#
# The --platform/--only-binary flags force LINUX x86_64 wheels regardless of the
# build host. Without them, a macOS host installs darwin wheels of the C
# extensions (lxml!) and every BeautifulSoup parse in the Enrich Lambda dies
# with "Couldn't find a tree builder with the features you requested: lxml".
# Both platform tags are offered so pip can pick whichever a package ships;
# the Lambda python3.12 runtime (AL2023, glibc 2.34) accepts manylinux_2_28.
.PHONY: build-DepsLayer

build-DepsLayer:
	pip install \
		--requirement requirements.txt \
		--target "$(ARTIFACTS_DIR)/python" \
		--platform manylinux2014_x86_64 \
		--platform manylinux_2_28_x86_64 \
		--implementation cp \
		--python-version 3.12 \
		--only-binary=:all: \
		--quiet \
		--no-cache-dir
