#!/bin/sh
# Refuse anything that looks like it came from a private wiki: private IP ranges, home paths, chat ids, secrets.
# Also checks every line of $JUST_WIKI_DENYLIST (default ~/.config/just-wiki/denylist, kept outside the repo)
# as a fixed string. Run in CI and as the local pre-push hook (ln -s ../../scripts/privacy-scan.sh .git/hooks/pre-push).
cd "$(git rev-parse --show-toplevel)" || exit 1
files=$(git ls-files | grep -v '^scripts/privacy-scan.sh$')
fail=0
if echo "$files" | xargs grep -nIE '(^|[^0-9.])(10\.[0-9]+\.[0-9]+\.[0-9]+|192\.168\.[0-9]+\.[0-9]+|172\.(1[6-9]|2[0-9]|3[01])\.[0-9]+\.[0-9]+)|/home/[a-z]|@g\.us|ghp_[A-Za-z0-9]{20}|sk-[A-Za-z0-9]{20}' ; then
  echo "privacy-scan: private address, home path, chat id or token above" >&2; fail=1
fi
list=${JUST_WIKI_DENYLIST:-$HOME/.config/just-wiki/denylist}
if [ -f "$list" ]; then
  grep -v '^\s*$' "$list" | grep -v '^#' > /tmp/gw-deny.$$
  if echo "$files" | xargs grep -nIiF -f /tmp/gw-deny.$$; then echo "privacy-scan: denylisted string above" >&2; fail=1; fi
  rm -f /tmp/gw-deny.$$
fi
exit $fail
