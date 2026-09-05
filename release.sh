#!/usr/bin/env bash
# SursumAI — cut a release.
#
#   bash release.sh              # release the version in the VERSION file
#   bash release.sh --dry-run    # build and hash, publish nothing
#
# Builds the tarball the installer downloads, hashes it, creates the tag and
# the GitHub release, and uploads both the tarball and SHA256SUMS.
#
# The tarball is built here and uploaded as a release asset on purpose: the
# archives GitHub generates from a tag are not byte-stable over time, and the
# installer aborts when the checksum does not match — so pinning a hash to one
# of those would eventually break installs by itself. An uploaded asset is
# immutable once published.

set -euo pipefail

cd "$(dirname "$0")"

DRY_RUN=0
[ "${1:-}" = "--dry-run" ] && DRY_RUN=1

ok()   { printf '\033[32m✓\033[0m %s\n' "$*"; }
warn() { printf '\033[33m!\033[0m %s\n' "$*"; }
fail() { printf '\033[31m✗\033[0m %s\n' "$*"; exit 1; }

VERSION="$(tr -d '[:space:]' < VERSION)"
TAG="v$VERSION"
ASSET="sursumai-$VERSION.tar.gz"

echo "◆ SursumAI release $TAG"
echo

# --- the release must match what the installer will fetch ---------------------
grep -q "SURSUMAI_VERSION:-$TAG}" install.sh \
  || fail "install.sh does not pin $TAG — bump it together with VERSION"
grep -q "/raw/$TAG/install.sh" README.md \
  || fail "README.md does not install $TAG — bump it together with VERSION"
ok "install.sh and README.md agree on $TAG"

# --- refuse to release something broken ---------------------------------------
[ -z "$(git status --porcelain)" ] || fail "working tree is dirty — commit first"
ok "working tree is clean"

command -v git >/dev/null || fail "git not found"
command -v gh  >/dev/null || fail "gh not found (https://cli.github.com)"

if git rev-parse "$TAG" >/dev/null 2>&1; then
  fail "tag $TAG already exists — bump VERSION for a new release"
fi

# pytest may live in the venv or in the system python, depending on how the
# machine was set up; say so plainly instead of failing on a missing module
PYTEST=""
for candidate in ".venv/bin/python" "python3" "python"; do
  if "$candidate" -m pytest --version >/dev/null 2>&1; then
    PYTEST="$candidate"
    break
  fi
done
[ -n "$PYTEST" ] || fail "pytest not found — run: pip install -r requirements-dev.txt"

"$PYTEST" -m pytest -q || fail "tests failed — not releasing"
ok "tests pass ($PYTEST)"

for script in install.sh setup.sh start.sh release.sh; do
  bash -n "$script" || fail "$script does not parse"
done
ok "shell scripts parse"

# --- build ---------------------------------------------------------------------
BUILD="$(mktemp -d)"
trap 'rm -rf "$BUILD"' EXIT

# git archive ships exactly what is committed: no .venv, no database, no logs
git archive --format=tar.gz --prefix="sursumai-$VERSION/" \
  -o "$BUILD/$ASSET" HEAD
ok "built $ASSET ($(du -h "$BUILD/$ASSET" | cut -f1))"

# A tarball that cannot start the app is worse than no release.
# The manifest goes to a file first: `tar | grep -q` looks right but grep exits
# on the first match, tar dies of SIGPIPE, and pipefail turns that into a
# failure on a perfectly good archive.
tar -tzf "$BUILD/$ASSET" > "$BUILD/manifest.txt"
for required in start.sh setup.sh install.sh requirements.txt VERSION \
                central/app.py agent/app.py web/index.html sursumai/bin/sursumai; do
  grep -qx "sursumai-$VERSION/$required" "$BUILD/manifest.txt" \
    || fail "the tarball is missing $required"
done
ok "tarball contains the app ($(wc -l < "$BUILD/manifest.txt") files)"

# the venv, the database and the logs must never ship
for forbidden in ".venv/" "sursumai.db" "sursumai-logs/" "llama-models/" ".env"; do
  if grep -q "$forbidden" "$BUILD/manifest.txt"; then
    fail "the tarball contains $forbidden - it must ship only committed source"
  fi
done
ok "tarball has no local state"

( cd "$BUILD" && sha256sum "$ASSET" > SHA256SUMS )
ok "SHA256SUMS: $(cut -d' ' -f1 < "$BUILD/SHA256SUMS")"

if [ "$DRY_RUN" -eq 1 ]; then
  cp "$BUILD/$ASSET" "$BUILD/SHA256SUMS" .
  warn "dry run — nothing published. Artifacts left in $(pwd)"
  exit 0
fi

# --- publish --------------------------------------------------------------------
git tag -a "$TAG" -m "SursumAI $VERSION"
git push origin "$TAG"
ok "tag $TAG pushed"

gh release create "$TAG" \
  "$BUILD/$ASSET" "$BUILD/SHA256SUMS" \
  --title "SursumAI $VERSION" \
  --notes-file <(cat <<EOF
Install:

\`\`\`bash
curl -fsSL https://github.com/Ga0512/SursumAI/raw/$TAG/install.sh | bash
\`\`\`

The installer downloads \`$ASSET\` from this release and verifies it against
\`SHA256SUMS\` before installing anything.
EOF
)
ok "release $TAG published"
echo
echo "Install command:"
echo "  curl -fsSL https://github.com/Ga0512/SursumAI/raw/$TAG/install.sh | bash"
