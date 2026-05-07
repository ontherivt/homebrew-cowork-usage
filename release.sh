#!/usr/bin/env bash
# release.sh — cut a new release of cowork-usage
#
# Run from the root of the homebrew-cowork-usage repo.
#
# Usage:
#   ./release.sh                # auto-bump patch (latest tag's last digit + 1)
#   ./release.sh 0.2.0          # explicit version
#
# Workflow:
#   1. Refuses to run if working tree is dirty (commit your script changes first)
#   2. Computes / accepts the new version, creates and pushes the git tag
#   3. Fetches GitHub's auto-generated release tarball, computes SHA256
#   4. Updates Formula/cowork-usage.rb with new version, url, sha256
#   5. Commits and pushes the formula update
#
# Idempotent: if a step after tagging fails (e.g. network), re-run the script
# with the same version and it'll pick up where it left off.

set -euo pipefail

# ── Pre-flight ──────────────────────────────────────────────────────
if [ -n "$(git status --porcelain)" ]; then
  echo "✗ Working tree is dirty. Commit or stash your changes first." >&2
  exit 1
fi
[ -f "Formula/cowork-usage.rb" ] || {
  echo "✗ Run from the repo root (Formula/cowork-usage.rb not found)." >&2
  exit 1
}
git remote get-url origin >/dev/null 2>&1 || {
  echo "✗ No 'origin' remote configured." >&2
  exit 1
}

# ── Determine version ──────────────────────────────────────────────
if [ -n "${1:-}" ]; then
  NEW_VERSION="$1"
else
  LAST=$(git tag --list 'v*' --sort=-v:refname | head -1 || true)
  if [ -z "$LAST" ]; then
    NEW_VERSION="0.1.0"
  else
    IFS='.' read -ra P <<< "${LAST#v}"
    if [ "${#P[@]}" -ne 3 ]; then
      echo "✗ Latest tag $LAST is not vMAJOR.MINOR.PATCH; pass an explicit version." >&2
      exit 1
    fi
    NEW_VERSION="${P[0]}.${P[1]}.$((P[2] + 1))"
  fi
fi
NEW_TAG="v$NEW_VERSION"
echo "→ Releasing $NEW_TAG"

# ── Tag and push (idempotent) ──────────────────────────────────────
if git ls-remote --tags origin "refs/tags/$NEW_TAG" 2>/dev/null | grep -q "$NEW_TAG"; then
  echo "  tag $NEW_TAG already on origin — skipping tag creation"
else
  if ! git rev-parse "refs/tags/$NEW_TAG" >/dev/null 2>&1; then
    git tag "$NEW_TAG"
  fi
  git push origin "$NEW_TAG"
fi

# ── Fetch tarball, compute SHA256 ───────────────────────────────────
ORIGIN=$(git remote get-url origin)
REPO=$(echo "$ORIGIN" | sed -E 's|^git@github\.com:||; s|^https://github\.com/||; s|\.git$||')
TARBALL_URL="https://github.com/${REPO}/archive/refs/tags/${NEW_TAG}.tar.gz"
echo "→ Fetching tarball: $TARBALL_URL"

NEW_SHA=""
for i in 1 2 3 4 5; do
  if SHA=$(curl -fsSL "$TARBALL_URL" 2>/dev/null | shasum -a 256 | cut -d' ' -f1); then
    if [ -n "$SHA" ] && [ "${#SHA}" -eq 64 ]; then
      NEW_SHA="$SHA"
      break
    fi
  fi
  echo "  (tarball not ready, retrying in 3s — $i/5)"
  sleep 3
done
[ -n "$NEW_SHA" ] || {
  echo "✗ Could not compute SHA256 after retries. Tag is pushed; re-run this script with the same version once GitHub has the tarball." >&2
  exit 1
}
echo "  SHA256: $NEW_SHA"

# ── Update formula ──────────────────────────────────────────────────
FORMULA="Formula/cowork-usage.rb"

# BSD sed (macOS default) needs -i ''; GNU sed wants -i alone
if sed --version >/dev/null 2>&1; then
  SED_I=(-i)
else
  SED_I=(-i '')
fi

sed "${SED_I[@]}" \
  -e "s|archive/refs/tags/v[0-9][0-9.]*\.tar\.gz|archive/refs/tags/${NEW_TAG}.tar.gz|g" \
  -e "s|version \"[0-9][0-9.]*\"|version \"${NEW_VERSION}\"|" \
  -e "s|sha256 \"[a-f0-9]\{64\}\"|sha256 \"${NEW_SHA}\"|" \
  "$FORMULA"

# ── Commit and push ────────────────────────────────────────────────
if [ -z "$(git status --porcelain "$FORMULA")" ]; then
  echo "  formula already up to date with $NEW_TAG / $NEW_SHA — nothing to commit"
else
  git add "$FORMULA"
  git commit -m "formula: bump to $NEW_TAG"
  git push
fi

echo
echo "✓ Released $NEW_TAG"
echo "  Users update with: brew update && brew upgrade cowork-usage"
