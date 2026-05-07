# cowork-usage Homebrew tap

Public Homebrew tap for distributing the `cowork-usage` cost analyzer. Users install with one command — no GitHub auth, no token, no env vars.

## Auth model

The analyzer Lambda is unauthenticated. The Function URL's 32-char random ID is treated as low-grade secret (it's in the public formula, so anyone who finds the tap can call the Lambda). The real backstop against abuse is a daily Bedrock spend cap on your AWS account — set that BEFORE you publish the tap.

If you want token auth, set `SHARED_SECRET` on the Lambda env vars and add `COWORK_ANALYZER_TOKEN=...` to the formula's `config.env` template. The script supports both modes.

## Repo layout

ONE public repo. Naming requirement: it must be named `homebrew-<something>` for `brew tap your-org/<something>` to work without explicit URLs. Suggested: `homebrew-cowork-usage`.

```
your-org/homebrew-cowork-usage/
├── Formula/
│   └── cowork-usage.rb
├── cowork_token_usage.py
└── README.md
```

Both the formula and the script live here. The formula's `url` points to this repo's own release tarball — when Homebrew extracts it, the script is right there at the root, ready to install.

## One-time setup (you, the maintainer)

### 1. Create the repo

```bash
# In a new public repo named homebrew-cowork-usage:
git init
mkdir Formula
cp /path/to/cowork_token_usage.py .
cp /path/to/cowork-usage.rb Formula/
git add .
git commit -m "initial"
git tag v0.1.0
git push -u origin main --tags
```

### 2. Get the SHA256 of the release tarball

```bash
curl -L https://github.com/your-org/homebrew-cowork-usage/archive/refs/tags/v0.1.0.tar.gz \
  | shasum -a 256
```

### 3. Update the formula with the SHA256

Edit `Formula/cowork-usage.rb` and fill in:

- `homepage` → this repo's URL
- `url` → the release tarball URL (substitute `your-org`)
- `sha256` → the value from step 2
- `ANALYZER_URL` → your Lambda Function URL (already filled in if you copied this from the working session)

Commit and push the formula update. (You can amend the v0.1.0 tag and re-push, or cut v0.1.1 — Homebrew doesn't care, it just downloads whatever the formula's `url` points at.)

## How users install

```bash
brew tap your-org/cowork-usage
brew install cowork-usage
cowork-usage --analyze
```

Three commands. No GitHub auth, no env vars, no token. The analyzer URL gets baked into `~/.config/cowork-usage/config.env` automatically.

(The tap name `your-org/cowork-usage` corresponds to the repo `your-org/homebrew-cowork-usage` — Homebrew strips the `homebrew-` prefix when you reference it.)

## Updating the script

When you change `cowork_token_usage.py`:

```bash
# Commit the script change, then cut a new release in the same repo
git add cowork_token_usage.py
git commit -m "v0.1.1"
git tag v0.1.1
git push --tags

# Update the formula to point at the new release
NEW_SHA=$(curl -L https://github.com/your-org/homebrew-cowork-usage/archive/refs/tags/v0.1.1.tar.gz | shasum -a 256 | cut -d' ' -f1)
# Hand-edit Formula/cowork-usage.rb: bump version, url, sha256
git commit -am "formula: bump to v0.1.1"
git push
```

Users get the update with:

```bash
brew update && brew upgrade cowork-usage
```

## Rotating the URL

If you ever delete and re-create the Lambda Function URL (which generates a new random ID), edit `ANALYZER_URL` in the formula, bump the version, push. **But:** the formula deliberately doesn't overwrite an existing `config.env`, so users will keep hitting the old URL until they:

```bash
rm ~/.config/cowork-usage/config.env
brew reinstall cowork-usage
```

For a forced rotation, send an email with those two lines.

## What's deliberately NOT in the formula

- No `--days` default override; the script defaults to 7 days
- No CLI completion (could add later if useful)
- No icon/Brewfile entry — this is a CLI tool, not a `.app`
- No Sparkle-style auto-update — users get updates via standard `brew upgrade`

## Troubleshooting

**"Error: Formula not found"** — user forgot the `your-org/internal/` prefix. They can use `brew install your-org/internal/cowork-usage` to be explicit.

**"Permission denied (publickey)"** — only happens with a private tap. If you've made the tap public, this shouldn't fire.

**Analyzer URL is visible in the public formula** — yes, that's expected. The URL by itself can be hit by anyone who finds the formula. The Bedrock spend cap (set up in your AWS account) is what bounds the worst-case abuse cost. If that's not enough, re-enable the bearer token (`SHARED_SECRET` env var on the Lambda + token in the formula's config template) or switch to AWS IAM / OAuth auth.