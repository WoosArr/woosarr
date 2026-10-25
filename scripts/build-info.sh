#!/bin/sh
# What an image is, for CI: prints version=, channel=, commit=, date= (append to $GITHUB_OUTPUT).
#
#   v1.2.3 tag      -> version 1.2.3,            channel release
#   develop branch  -> version 1.3.0-dev.N,      channel develop   (N = commits since v1.2.3)
#   anything else   -> version 1.3.0-dev.N,      channel dev       (main pushes are tested, not published)
#
# N counts develop's commits that the newest release doesn't contain, so a higher number is
# always a newer build. Releases are merge commits on main, so the newest release tag is found by
# version, not by ancestry. The date is the commit's, not the build's, so every job builds the
# same image. Needs the full history and tags (actions/checkout fetch-depth: 0).
set -eu

ref="${GITHUB_REF:-$(git symbolic-ref -q HEAD || true)}"
case "$ref" in
  refs/tags/v[0-9]*) channel=release ;;
  refs/heads/develop) channel=develop ;;
  *) channel=dev ;;
esac

last=$(git tag -l 'v*.*.*' --sort=-v:refname | grep -E '^v[0-9]+\.[0-9]+\.[0-9]+$' | head -n 1 || true)
if [ "$channel" = release ]; then
  version="${ref#refs/tags/v}"
else
  [ -n "$last" ] || last=v0.0.0
  # shellcheck disable=SC2046
  set -- $(echo "${last#v}" | tr . ' ')
  if [ "$last" = v0.0.0 ]; then n=$(git rev-list --count HEAD); else n=$(git rev-list --count HEAD "^$last"); fi
  version="$1.$(( $2 + 1 )).0-dev.$n"
fi

echo "version=$version"
echo "channel=$channel"
echo "commit=$(git rev-parse HEAD)"
echo "date=$(git log -1 --format=%cI HEAD)"
