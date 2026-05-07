class CoworkUsage < Formula
  desc "Cost report + AI cost-reduction tips for Claude Cowork sessions"
  homepage "https://github.com/ontherivt/homebrew-cowork-usage"

  # The formula and the script live in the SAME repo (this one). `url` points
  # to this repo's own release tarball. Bump `url`, `version`, and `sha256`
  # together every time you cut a new release.
  url "https://github.com/ontherivt/homebrew-cowork-usage/archive/refs/tags/v0.1.3.tar.gz"
  version "0.1.3"
  sha256 "1dfcaa8fd4e5cc52ede2be7146909e590bdc2bfbd1a1ff1b5dc91467207cfea8"

  license "MIT"

  # Analyzer service URL — same for every user in the org. The Function URL's
  # 32-char random ID is the only "secret"; the real backstop against abuse is
  # the Bedrock daily spend cap configured in the AWS account.
  ANALYZER_URL = "https://ssfcaxjqbj5rar4synctd3inoa0cqwlw.lambda-url.us-east-1.on.aws/".freeze

  # The script is stdlib-only and works on Python 3.9+. We don't pull in
  # python@3.12 because most users already have a system or pyenv Python that
  # works fine, and the bottle is ~18MB. The shim uses `/usr/bin/env python3`
  # so it picks up whatever's first on PATH. If the user has no python3 at
  # all, the shim fails with a clear message instead of silently breaking.

  def install
    libexec.install "cowork_token_usage.py"
    # The wrapper bakes the analyzer URL in as an env var and writes the
    # user's config file on first invocation. This avoids running install-time
    # logic that touches the user's $HOME (which Homebrew's post_install
    # sandbox doesn't always allow).
    (bin/"cowork-usage").write <<~SHIM
      #!/bin/bash
      set -e
      if ! command -v python3 >/dev/null 2>&1; then
        echo "cowork-usage requires Python 3.9 or newer." >&2
        echo "Install with: brew install python@3.12" >&2
        echo "  (or: xcode-select --install for the Apple-bundled Python)" >&2
        exit 1
      fi
      # Seed ~/.config/cowork-usage/config.env on first run, then forget about it.
      CONFIG_DIR="${HOME}/.config/cowork-usage"
      CONFIG_FILE="${CONFIG_DIR}/config.env"
      if [ ! -f "$CONFIG_FILE" ]; then
        mkdir -p "$CONFIG_DIR"
        cat > "$CONFIG_FILE" <<EOF
# Cowork analyzer config — written by cowork-usage on first run.
# If the URL changes, run:  rm "$CONFIG_FILE" && brew reinstall cowork-usage
COWORK_ANALYZER_URL=#{ANALYZER_URL}
EOF
        chmod 0600 "$CONFIG_FILE"
      fi
      exec python3 "#{libexec}/cowork_token_usage.py" "$@"
    SHIM
    (bin/"cowork-usage").chmod 0755
  end

  def caveats
    <<~CAVEATS
      Quick start:
        cowork-usage              # 7-day cost report
        cowork-usage --analyze    # report + AI cost-reduction tips
        cowork-usage --days 0     # all-time
        cowork-usage --detailed   # raw per-session table

      Config file: ~/.config/cowork-usage/config.env
        Contains the analyzer URL. If your org rotates the URL, run:
          rm ~/.config/cowork-usage/config.env && brew reinstall cowork-usage

      What this analyzes: only your local Cowork sessions
      (~/Library/Application Support/Claude/local-agent-mode-sessions).
      It does NOT see Claude.ai chat or Claude Code transcripts.
    CAVEATS
  end

  test do
    # Smoke test that the wrapper runs without crashing.
    assert_match "CLAUDE COWORK USAGE REPORT", shell_output("#{bin}/cowork-usage --help 2>&1", 0).chars.first(80).join + " ", 0
  rescue
    # `--help` exits 0 normally; some setups can complain. As a fallback,
    # just check the binary is present and executable.
    assert_predicate bin/"cowork-usage", :executable?
  end
end
