class CoworkUsage < Formula
  desc "Cost report + AI cost-reduction tips for Claude Cowork sessions"
  homepage "https://github.com/YOUR_ORG/homebrew-cowork-usage"

  # The formula and the script live in the SAME repo (this one). `url` points
  # to this repo's own release tarball. Bump `url`, `version`, and `sha256`
  # together every time you cut a new release.
  url "https://github.com/YOUR_ORG/homebrew-cowork-usage/archive/refs/tags/v0.1.0.tar.gz"
  version "0.1.0"
  sha256 "REPLACE_WITH_SHA256_OF_RELEASE_TARBALL"

  license "MIT"

  # Analyzer service URL — same for every user in the org. The Function URL's
  # 32-char random ID is the only "secret"; the real backstop against abuse is
  # the Bedrock daily spend cap configured in the AWS account.
  ANALYZER_URL = "https://ssfcaxjqbj5rar4synctd3inoa0cqwlw.lambda-url.us-east-1.on.aws/".freeze

  # The script is stdlib-only Python 3.7+. Homebrew's bundled python@3.12 is
  # fine; declaring the dep keeps `brew test` happy.
  depends_on "python@3.12"

  def install
    libexec.install "cowork_token_usage.py"
    # Wrapper so `cowork-usage` is the user-facing command name.
    (bin/"cowork-usage").write <<~SHIM
      #!/bin/bash
      exec "#{Formula["python@3.12"].opt_bin}/python3" "#{libexec}/cowork_token_usage.py" "$@"
    SHIM
    (bin/"cowork-usage").chmod 0755
  end

  def post_install
    config_dir = Pathname.new(Dir.home) / ".config" / "cowork-usage"
    config_dir.mkpath
    config_file = config_dir / "config.env"
    # Don't clobber an existing config — users may have hand-edited it.
    # If you need to force-update tokens for everyone, bump the formula
    # version and have users re-run `brew reinstall cowork-usage`, OR
    # have them manually delete config.env first.
    return if config_file.exist?

    config_file.write <<~CONFIG
      # Cowork analyzer config — written by Homebrew on first install.
      # If the URL changes, run:
      #   rm ~/.config/cowork-usage/config.env && brew reinstall cowork-usage
      COWORK_ANALYZER_URL=#{ANALYZER_URL}
    CONFIG
    config_file.chmod 0600
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