#!/bin/sh
# ──────────────────────────────────────────────────────────────────────
# Playwright CLI installer (no pre-existing Node/npm required).
#
#   curl -fsSL https://raw.githubusercontent.com/kirodotdev/KiroCrew/main/playwright-cli.sh | sh
#   curl -fsSL https://raw.githubusercontent.com/kirodotdev/KiroCrew/main/playwright-cli.sh | sh -s -- --version 0.1.18
#
# Read before you run (many enterprises forbid piping a script into a shell):
#   curl -fsSLO https://raw.githubusercontent.com/kirodotdev/KiroCrew/main/playwright-cli.sh
#   less playwright-cli.sh
#   sh playwright-cli.sh --version 0.1.18
#
# Installs the `@playwright/cli` npm package into a PRIVATE prefix (no sudo, no
# writes outside $HOME) and drops a `playwright-cli` wrapper into ~/.local/bin.
# Upstream ships this tool only through the npm registry, so this script does
# not pretend a portable archive exists; what it removes is the requirement that
# the user ALREADY have a working Node toolchain and an unblocked default
# registry. When Node is missing or below the package's floor it downloads the
# release build for the detected platform and verifies it against that release's
# SHASUMS256 manifest before running it.
#
# Every failure mode an enterprise network produces — a mirror demanding a
# login, a proxy swallowing the connection, a TLS-terminating CA, a mirror that
# never proxied these packages, a blocked browser CDN — exits with its own code
# and prints the specific command that resolves it.
#
# Windows: use playwright-cli.ps1 instead (same flags, PowerShell spelling).
# ──────────────────────────────────────────────────────────────────────
set -eu

usage() {
  cat <<'EOF'
Install the Playwright CLI (@playwright/cli), bootstrapping Node if needed.

Usage: sh playwright-cli.sh [options]

Options:
  --version <X.Y.Z>       package version to install (default: latest)
  --registry <url>        npm registry to install FROM (default: the public
                          registry, so an ambient .npmrc pointing at an expired
                          private mirror cannot break a public package). Point
                          this at a corporate mirror when the public registry
                          is unreachable.
  --isolated-npmrc        ignore ~/.npmrc and the global npm config entirely
  --node-version <X.Y.Z>  Node version to bootstrap when none is usable
  --node-mirror <url>     base URL serving <base>/v<ver>/<file> (default:
                          nodejs.org, or unofficial-builds.nodejs.org on musl
                          and pre-2.28-glibc hosts)
  --download-host <url>   PLAYWRIGHT_DOWNLOAD_HOST for the browser binaries
  --skip-browsers         do not download browser binaries during install
  --prefix <dir>          private install prefix
  --bin-dir <dir>         where the playwright-cli wrapper is written
  --force                 reinstall even when the pinned version is present
  --dry-run               print the resolved plan and exit without changes
  -h, --help              this text

Environment:
  KIROCREW_HOME                 data home (default ~/.kiro/crew)
  KIROCREW_PLAYWRIGHT_CLI_HOME  overrides --prefix
  KIROCREW_NPM_REGISTRY         overrides --registry
  KIROCREW_NODE_BIN_DIR         an existing Node bin dir to reuse
  HTTPS_PROXY / NO_PROXY        honored by curl, wget and npm
  NODE_EXTRA_CA_CERTS           CA bundle for a TLS-terminating proxy

Exit codes:
   0  success                 12  Node checksum mismatch
   1  unclassified failure    13  registry rejected auth (login/token needed)
   2  usage error             14  registry unreachable (DNS/proxy/TLS)
  10  missing prerequisite    15  package or version does not exist
  11  Node bootstrap failed   16  browser download blocked
                              17  prefix or bin dir not writable
                              18  installed CLI failed to run
EOF
}

# The package this installer exists to deliver. `latest` is the default version
# because pinning one here would go stale on every upstream release; --version
# covers the reproducible and offline-mirror cases.
PACKAGE="@playwright/cli"
PACKAGE_VERSION="latest"
WRAPPER_NAME="playwright-cli"

# The public registry is pinned rather than inherited. A corporate .npmrc that
# redirects the DEFAULT registry at a private mirror makes a PUBLIC package 401
# as soon as that mirror's token expires, which is the most common way this
# install fails inside an enterprise network. --registry re-points it for the
# opposite case: public registry firewalled, mirror reachable.
PUBLIC_NPM_REGISTRY="https://registry.npmjs.org/"
REGISTRY="${KIROCREW_NPM_REGISTRY:-$PUBLIC_NPM_REGISTRY}"
ISOLATED_NPMRC=0

# @playwright/cli declares engines.node >= 18, so an existing 18/20 toolchain is
# accepted rather than replaced. What gets INSTALLED when nothing is usable is
# the current Node 22 LTS.
MIN_NODE_MAJOR=18
NODE_VERSION="22.23.2"
NODE_OFFICIAL_MIRROR="https://nodejs.org/dist"
# nodejs.org publishes no musl build and nothing for pre-2.28 glibc; the
# unofficial-builds project does, under the same <base>/v<ver>/<file> layout
# with its own SHASUMS256.txt.
NODE_UNOFFICIAL_MIRROR="https://unofficial-builds.nodejs.org/download/release"
NODE_MIRROR=""
MIN_GLIBC_FOR_OFFICIAL="2.28"

# Written into a Node tree this installer unpacked. Its ABSENCE is what stops
# the bootstrap from recursively deleting a `node` directory it did not create
# (reachable with --prefix "$HOME" on a host that also has ~/node).
NODE_STAMP_NAME=".kirocrew-playwright-cli-node"

DATA_HOME="${KIROCREW_HOME:-$HOME/.kiro/crew}"
PREFIX="${KIROCREW_PLAYWRIGHT_CLI_HOME:-$DATA_HOME/playwright-cli}"
BIN_DIR="$HOME/.local/bin"
DOWNLOAD_HOST=""
SKIP_BROWSERS=0
FORCE=0
DRY_RUN=0
SKIP_INSTALL=0

EX_USAGE=2
EX_MISSING_TOOL=10
EX_NODE_BOOTSTRAP=11
EX_CHECKSUM=12
EX_REGISTRY_AUTH=13
EX_REGISTRY_UNREACHABLE=14
EX_PACKAGE_NOT_FOUND=15
EX_BROWSER_DOWNLOAD=16
EX_NOT_WRITABLE=17
EX_VERIFY=18

SELF="playwright-cli-install"

say()  { echo "$SELF: $*"; }
warn() { echo "$SELF: $*" >&2; }
# Every failure exits through here, so each exit code is deliberate and
# documented in usage() rather than being an incidental $?.
die()  { _code="$1"; shift; echo "$SELF: $*" >&2; exit "$_code"; }

while [ $# -gt 0 ]; do
  case "$1" in
    --version) PACKAGE_VERSION="${2:?--version needs a value}"; shift 2 ;;
    --version=*) PACKAGE_VERSION="${1#*=}"; shift ;;
    --registry) REGISTRY="${2:?--registry needs a value}"; shift 2 ;;
    --registry=*) REGISTRY="${1#*=}"; shift ;;
    --isolated-npmrc) ISOLATED_NPMRC=1; shift ;;
    --node-version) NODE_VERSION="${2:?--node-version needs a value}"; shift 2 ;;
    --node-version=*) NODE_VERSION="${1#*=}"; shift ;;
    --node-mirror) NODE_MIRROR="${2:?--node-mirror needs a value}"; shift 2 ;;
    --node-mirror=*) NODE_MIRROR="${1#*=}"; shift ;;
    --download-host) DOWNLOAD_HOST="${2:?--download-host needs a value}"; shift 2 ;;
    --download-host=*) DOWNLOAD_HOST="${1#*=}"; shift ;;
    --skip-browsers) SKIP_BROWSERS=1; shift ;;
    --prefix) PREFIX="${2:?--prefix needs a value}"; shift 2 ;;
    --prefix=*) PREFIX="${1#*=}"; shift ;;
    --bin-dir) BIN_DIR="${2:?--bin-dir needs a value}"; shift 2 ;;
    --bin-dir=*) BIN_DIR="${1#*=}"; shift ;;
    --force) FORCE=1; shift ;;
    --dry-run) DRY_RUN=1; shift ;;
    -h|--help) usage; exit 0 ;;
    *) echo "$SELF: unknown argument '$1' (try --help)" >&2; exit "$EX_USAGE" ;;
  esac
done

# Trailing separators are stripped repeatedly, so "path//" and "///" both reduce
# fully. A root reduces to the EMPTY string, which the absolutisation below would
# resolve against $PWD -- silently installing into the working directory instead
# of the root that was named. That is refused rather than preserved: this
# installer's contract is a private prefix inside the user profile that needs no
# elevation, which no filesystem root satisfies, and preserving "/" would also
# build "//node", whose leading "//" POSIX leaves implementation-defined.
# Written out per variable, inline: indirect assignment in POSIX sh needs `eval`,
# and a helper would need a command substitution -- which strips a TRAILING
# NEWLINE and so would rob the guard below of the very character it rejects.
while :; do
  case "$PREFIX" in
    */) PREFIX="${PREFIX%/}" ;;
    *) break ;;
  esac
done
while :; do
  case "$BIN_DIR" in
    */) BIN_DIR="${BIN_DIR%/}" ;;
    *) break ;;
  esac
done
[ -n "$PREFIX" ] || die "$EX_USAGE" "--prefix may not be the filesystem root"
[ -n "$BIN_DIR" ] || die "$EX_USAGE" "--bin-dir may not be the filesystem root"

# Both paths are embedded in a script this installer GENERATES, and $PREFIX is
# additionally round-tripped through `dirname`, whose command substitution
# strips trailing newlines. An embedded newline therefore survives quoting but a
# trailing one silently resolves to a different path, so the whole class is
# refused here with a named error instead of failing later as "the tool does not
# exist".
# Only a TRAILING newline is refused. An embedded one survives: the wrapper
# quoting keeps it, and the tool runs. A trailing one cannot survive, because
# $PREFIX is round-tripped through `dirname`, whose command substitution strips
# it -- the install would then resolve to a different path and fail later as
# "the tool does not exist". The pattern holds a LITERAL newline; $(printf '\n')
# would be stripped to the empty string and `case` would match every path.
for _generated_path in "$PREFIX" "$BIN_DIR"; do
  case "$_generated_path" in
    *"
") die "$EX_USAGE" "paths may not end with a newline (got '$_generated_path')" ;;
  esac
done

# Both paths are written into a wrapper script that outlives this run, so a
# relative one ("--prefix ./pw") would resolve against whatever directory the
# CALLER happens to be in -- the wrapper works from here and nowhere else. Made
# absolute after the newline check above, which has to see the value the user
# actually passed.
_absolute() {
  case "$1" in
    /*) printf '%s' "$1" ;;
    *) printf '%s' "$PWD/$1" ;;
  esac
}
PREFIX="$(_absolute "$PREFIX")"
BIN_DIR="$(_absolute "$BIN_DIR")"

# The version string reaches both an npm spec and a URL path, so it is held to
# the characters a semver-ish tag can contain rather than letting a shell or URL
# metacharacter through.
case "$PACKAGE_VERSION" in
  ""|*[!A-Za-z0-9._+-]*) die "$EX_USAGE" "invalid --version '$PACKAGE_VERSION'" ;;
esac
case "$NODE_VERSION" in
  ""|*[!0-9.]*) die "$EX_USAGE" "invalid --node-version '$NODE_VERSION' (expected X.Y.Z)" ;;
esac

# Bytes are only ever fetched over TLS: an http:// mirror or registry would let
# anything on the path substitute its own Node tarball, and the SHASUMS manifest
# travels the same channel it is supposed to protect.
_redact_urls() {
  # The character class excludes `/` but NOT `@`, so the match runs to the LAST
  # `@` in the authority rather than the first. A token containing a literal `@`
  # (`//user:p@ss@host/`) would otherwise be cut at the first one, leaving the
  # rest of the password -- `//***@ss@host/` -- in the log. Excluding `/` is what
  # keeps it inside the authority, so a `@` in a PATH is left alone.
  sed 's|//[^/[:space:]]*@|//***@|g'
}
# Any URL this script prints goes through here first. Three of the four
# URL-valued options are caller-supplied and may legitimately carry userinfo, so
# the redaction is a property of PRINTING a URL, not of one particular option.
# Fails SAFE, not fatal: if the filter cannot run at all (no usable sed), the
# script must neither print the raw URL nor die with no message -- an unreadable
# diagnostic is a better outcome than a leaked token or a silent exit 1.
_redact_url() {
  [ -n "${1:-}" ] || return 0
  _redacted="$(printf '%s' "$1" | _redact_urls 2>/dev/null)" || _redacted=""
  [ -n "$_redacted" ] || _redacted="<url hidden: redaction unavailable>"
  printf '%s' "$_redacted"
}
_require_https() { # label url
  case "$2" in
    https://*) : ;;
    *) die "$EX_USAGE" "$1 must be an https:// URL (got '$(_redact_url "$2")')" ;;
  esac
}
_require_https "--registry" "$REGISTRY"
[ -z "$NODE_MIRROR" ] || _require_https "--node-mirror" "$NODE_MIRROR"
[ -z "$DOWNLOAD_HOST" ] || _require_https "--download-host" "$DOWNLOAD_HOST"

# A registry URL may legitimately carry userinfo (https://user:token@host/), and
# the documented enterprise path invites exactly that. Anything this script
# prints about the registry uses the redacted form, because the failure path
# echoes it and also dumps npm's log to stderr, where a terminal, a CI log or a
# pasted bug report would capture the token.
# Strip userinfo from every URL in whatever is piped through. npm echoes the
# resolved registry URL into its own output, so redacting only the values THIS
# script prints would still leak the token through the log and its tail.
REGISTRY_DISPLAY="$(_redact_url "$REGISTRY")"
DOWNLOAD_HOST_DISPLAY="$(_redact_url "$DOWNLOAD_HOST")"

# Render $1 as a single-quoted shell word safe to embed in a GENERATED script:
# each embedded single quote is closed, escaped and reopened. Without this a
# path holding a space merely breaks the wrapper, while one holding $(...) or a
# backtick would execute when the wrapper runs.
_shell_quote() {
  printf "'%s'" "$(printf '%s' "$1" | sed "s/'/'\\\\''/g")"
}

# ── prerequisites ────────────────────────────────────────────────────
DOWNLOADER=""
WGET_HTTPS_ONLY=""
if command -v curl >/dev/null 2>&1; then
  DOWNLOADER="curl"
elif command -v wget >/dev/null 2>&1; then
  DOWNLOADER="wget"
  if wget --help 2>&1 | grep -q -- '--https-only'; then
    WGET_HTTPS_ONLY="--https-only"
  fi
fi

# Checked where a download is actually reached, not at startup: a host that
# already has a usable Node needs no downloader at all, and refusing it there
# would turn a working install into a hard failure.
_assert_can_download() {
  if [ -z "$DOWNLOADER" ]; then
    die "$EX_MISSING_TOOL" "need curl or wget to download Node; install either and re-run"
  fi
  # Only GNU wget can pin every redirect hop to HTTPS. BusyBox wget — the
  # default on Alpine, which is also the main musl target — cannot, and it is
  # the checksum MANIFEST that travels this channel: a single 302 to http:// on
  # a hostile network would let an attacker supply both the tarball and the hash
  # that blesses it. Refuse rather than verify against a manifest we cannot
  # trust.
  if [ "$DOWNLOADER" = "wget" ] && [ -z "$WGET_HTTPS_ONLY" ]; then
    die "$EX_MISSING_TOOL" \
      "the only downloader here is a wget build without --https-only (BusyBox), which cannot stop a redirect from downgrading the checksum manifest to http; install curl (Alpine: apk add curl) and re-run"
  fi
}

SHA_CMD=""
if command -v sha256sum >/dev/null 2>&1; then
  SHA_CMD="sha256sum"
elif command -v shasum >/dev/null 2>&1; then
  SHA_CMD="shasum -a 256"
fi

_fetch() { # url dest
  if [ "$DOWNLOADER" = "curl" ]; then
    # --proto '=https' constrains redirects too, not just the initial request.
    curl -fsS --proto '=https' -L "$1" -o "$2"
  else
    # Unquoted on purpose: expands to one flag, or to nothing on BusyBox wget.
    # shellcheck disable=SC2086
    wget -q $WGET_HTTPS_ONLY -O "$2" "$1"
  fi
}

# Dotted numeric compare, "$1 >= $2". `sort -V` is neither POSIX nor present in
# every BusyBox build, so a field-wise numeric sort is used instead.
_version_ge() {
  [ "$(printf '%s\n%s\n' "$2" "$1" \
       | sort -t. -k1,1n -k2,2n -k3,3n 2>/dev/null | head -n 1)" = "$2" ]
}

# ── platform detection ───────────────────────────────────────────────
OS_NAME="$(uname -s 2>/dev/null || echo unknown)"
ARCH_NAME="$(uname -m 2>/dev/null || echo unknown)"
case "$OS_NAME" in
  Darwin) NODE_OS="darwin" ;;
  Linux)  NODE_OS="linux" ;;
  *)
    die "$EX_MISSING_TOOL" \
      "unsupported OS '$OS_NAME'; macOS and Linux use this script, Windows uses playwright-cli.ps1"
    ;;
esac
case "$ARCH_NAME" in
  x86_64|amd64) NODE_ARCH="x64" ;;
  arm64|aarch64) NODE_ARCH="arm64" ;;
  armv7l) NODE_ARCH="armv7l" ;;
  *) die "$EX_MISSING_TOOL" "unsupported CPU architecture '$ARCH_NAME'" ;;
esac

# Which C library the host uses decides which Node build can execute at all: an
# official glibc tarball reports "not found" on Alpine, and fails GLIBC_2.28
# symbol lookups on CentOS/RHEL 7-era hosts.
LIBC="glibc"
GLIBC_VERSION=""
if [ "$NODE_OS" = "linux" ]; then
  for _musl_loader in /lib/ld-musl-*.so.1; do
    if [ -e "$_musl_loader" ]; then LIBC="musl"; break; fi
  done
  if [ "$LIBC" = "glibc" ] && ldd --version 2>&1 | head -n 1 | grep -qi musl; then
    LIBC="musl"
  fi
  if [ "$LIBC" = "glibc" ]; then
    GLIBC_VERSION="$(getconf GNU_LIBC_VERSION 2>/dev/null | awk '{print $NF}')"
    if [ -z "$GLIBC_VERSION" ]; then
      GLIBC_VERSION="$(ldd --version 2>/dev/null | head -n 1 | awk '{print $NF}')"
    fi
    case "$GLIBC_VERSION" in
      ""|*[!0-9.]*) GLIBC_VERSION="" ;;
    esac
  fi
fi

# Node artifact for this host, as globals rather than a printed pair so no
# caller has to word-split the result.
NODE_ART_MIRROR=""
NODE_ART_MIRROR_DISPLAY=""
NODE_ART_BASE=""
_resolve_node_artifact() {
  NODE_ART_MIRROR="$NODE_MIRROR"
  NODE_ART_BASE="node-v$NODE_VERSION-$NODE_OS-$NODE_ARCH"
  if [ "$NODE_OS" = "linux" ] && [ "$LIBC" = "musl" ]; then
    NODE_ART_BASE="node-v$NODE_VERSION-linux-$NODE_ARCH-musl"
    [ -n "$NODE_ART_MIRROR" ] || NODE_ART_MIRROR="$NODE_UNOFFICIAL_MIRROR"
  elif [ "$NODE_OS" = "linux" ] && [ "$NODE_ARCH" = "x64" ] && [ -n "$GLIBC_VERSION" ] \
       && ! _version_ge "$GLIBC_VERSION" "$MIN_GLIBC_FOR_OFFICIAL"; then
    # unofficial-builds publishes this variant for x64 only, so an old-glibc
    # arm64 host keeps the official tarball and gets a clear runtime error
    # instead of a 404 from a URL that was never published.
    NODE_ART_BASE="node-v$NODE_VERSION-linux-x64-glibc-217"
    [ -n "$NODE_ART_MIRROR" ] || NODE_ART_MIRROR="$NODE_UNOFFICIAL_MIRROR"
  fi
  [ -n "$NODE_ART_MIRROR" ] || NODE_ART_MIRROR="$NODE_OFFICIAL_MIRROR"
  NODE_ART_MIRROR="${NODE_ART_MIRROR%/}"
  NODE_ART_MIRROR_DISPLAY="$(_redact_url "$NODE_ART_MIRROR")"
}

# ── locate a usable Node ─────────────────────────────────────────────
NODE=""
NODE_BIN_DIR=""
# Set by _bootstrap_node so the npm fallback below cannot loop: a missing npm in a
# tree we just unpacked is a broken archive, not a reusable-Node problem.
NODE_WAS_BOOTSTRAPPED=0
NPM=""

_try_node() { # candidate node path
  [ -n "${1:-}" ] || return 1
  # A directory also answers -x (it is traversable), so require a regular file
  # rather than leaning on the exec attempt below to fail.
  [ -f "$1" ] && [ -x "$1" ] || return 1
  _cand_major="$("$1" -p 'process.versions.node.split(".")[0]' 2>/dev/null || true)"
  case "$_cand_major" in
    ""|*[!0-9]*) return 1 ;;
  esac
  [ "$_cand_major" -ge "$MIN_NODE_MAJOR" ] || return 1
  NODE="$1"
  # Also embedded in the generated wrapper, and this is the one place every
  # candidate funnels through -- $KIROCREW_NODE_BIN_DIR, the marker file and a
  # PATH lookup can each hand us a relative path.
  NODE_BIN_DIR="$(_absolute "$(dirname "$1")")"
  return 0
}

# Preference order, most specific first: a Node this installer bootstrapped
# earlier, then one the caller named, then the toolchain Kiro Crew's own
# ensure-node.sh recorded (so the two installers share a single download), then
# whatever is on PATH.
_resolve_node() {
  _try_node "$PREFIX/node/bin/node" && return 0
  if [ -n "${KIROCREW_NODE_BIN_DIR:-}" ]; then
    _try_node "$KIROCREW_NODE_BIN_DIR/node" && return 0
  fi
  if [ -f "$DATA_HOME/node-bin-dir" ]; then
    _marked="$(head -n 1 "$DATA_HOME/node-bin-dir" 2>/dev/null || true)"
    if [ -n "$_marked" ]; then
      _try_node "$_marked/node" && return 0
    fi
  fi
  _try_node "$DATA_HOME/node-glibc217/bin/node" && return 0
  _try_node "$(command -v node 2>/dev/null || true)" && return 0
  return 1
}

# npm normally sits beside node, but a PATH-provided node may have npm
# elsewhere on PATH (Homebrew, distro packages, version managers).
_resolve_npm() {
  if [ -x "$NODE_BIN_DIR/npm" ]; then
    NPM="$NODE_BIN_DIR/npm"
    return 0
  fi
  NPM="$(command -v npm 2>/dev/null || true)"
  [ -n "$NPM" ]
}

# Refuse a platform whose artifact was never published, rather than letting the
# download 404 and be reported as a network fault.
_assert_bootstrap_supported() {
  if [ "$LIBC" = "musl" ] && [ "$NODE_ARCH" = "armv7l" ]; then
    die "$EX_MISSING_TOOL" \
      "no musl Node build is published for armv7l; install Node >= $MIN_NODE_MAJOR from your distribution, then re-run"
  fi
}

_bootstrap_node() {
  NODE_WAS_BOOTSTRAPPED=1
  _assert_bootstrap_supported
  _assert_can_download
  _resolve_node_artifact
  _tarball="$NODE_ART_BASE.tar.gz"
  _url="$NODE_ART_MIRROR/v$NODE_VERSION/$_tarball"
  _sums="$NODE_ART_MIRROR/v$NODE_VERSION/SHASUMS256.txt"
  _url_display="$NODE_ART_MIRROR_DISPLAY/v$NODE_VERSION/$_tarball"
  _sums_display="$NODE_ART_MIRROR_DISPLAY/v$NODE_VERSION/SHASUMS256.txt"

  [ -n "$SHA_CMD" ] || die "$EX_MISSING_TOOL" \
    "need sha256sum or shasum to verify the Node download; refusing to install an unverified toolchain"
  command -v tar >/dev/null 2>&1 || die "$EX_MISSING_TOOL" "need tar to unpack Node"

  say "no usable Node found (need >= $MIN_NODE_MAJOR); installing Node $NODE_VERSION for $NODE_OS-$NODE_ARCH ($LIBC)"
  _tmp="$(mktemp -d)"
  trap 'rm -rf "$_tmp"' EXIT INT TERM

  _fetch "$_sums" "$_tmp/SHASUMS256.txt" || die "$EX_NODE_BOOTSTRAP" \
    "could not fetch the Node checksum manifest $_sums_display — check proxy access, or pass --node-mirror pointing at an internal Node mirror"
  _fetch "$_url" "$_tmp/node.tar.gz" || die "$EX_NODE_BOOTSTRAP" \
    "could not download $_url_display — check proxy access, or pass --node-mirror pointing at an internal Node mirror"

  # $SHA_CMD is unquoted on purpose: it expands to one or two words.
  # shellcheck disable=SC2086
  _got="$($SHA_CMD "$_tmp/node.tar.gz" | awk '{print $1}')"
  _want="$(awk -v f="$_tarball" '$2 == f || $2 == "./" f {print $1}' \
           "$_tmp/SHASUMS256.txt" | head -n 1)"
  if [ -z "$_want" ]; then
    die "$EX_CHECKSUM" "$_tarball is absent from $_sums_display — refusing to install an unverified Node"
  fi
  if [ "$_want" != "$_got" ]; then
    die "$EX_CHECKSUM" \
      "Node checksum mismatch for $_tarball (expected $_want, got $_got) — refusing to install"
  fi
  say "verified Node SHA-256"

  tar -xzf "$_tmp/node.tar.gz" -C "$_tmp" \
    || die "$EX_NODE_BOOTSTRAP" "could not unpack $_tarball"
  [ -x "$_tmp/$NODE_ART_BASE/bin/node" ] && [ -x "$_tmp/$NODE_ART_BASE/bin/npm" ] \
    || die "$EX_NODE_BOOTSTRAP" "the Node archive is missing bin/node or bin/npm"

  # --prefix is caller-supplied, so $PREFIX/node can name a directory this
  # installer never created (--prefix "$HOME" with an existing ~/node source
  # checkout, which _try_node rejects for lacking bin/node). Only a tree
  # carrying our stamp may be replaced.
  if [ -e "$PREFIX/node" ] && [ ! -f "$PREFIX/node/$NODE_STAMP_NAME" ]; then
    die "$EX_NODE_BOOTSTRAP" \
      "$PREFIX/node already exists and was not created by this installer; move it aside or pass a different --prefix"
  fi
  : >"$_tmp/$NODE_ART_BASE/$NODE_STAMP_NAME"

  # Staging gets a FRESH name inside the prefix rather than a fixed one: the
  # final step must be a same-filesystem rename to be atomic, but `rm -rf` on a
  # predictable path would destroy an unrelated directory that happens to carry
  # that name under a caller-supplied --prefix. mktemp guarantees we created it.
  _stage="$(mktemp -d "$PREFIX/node.incoming.XXXXXX")" \
    || die "$EX_NOT_WRITABLE" "cannot create a staging directory in $PREFIX"
  mv "$_tmp/$NODE_ART_BASE" "$_stage/tree"
  rm -rf -- "$PREFIX/node"
  mv "$_stage/tree" "$PREFIX/node"
  rm -rf -- "$_stage" "$_tmp"
  trap - EXIT INT TERM

  _try_node "$PREFIX/node/bin/node" \
    || die "$EX_NODE_BOOTSTRAP" "the Node just installed at $PREFIX/node does not run on this host"
  say "installed Node $NODE_VERSION at $PREFIX/node"
}

# ── plan ─────────────────────────────────────────────────────────────
SPEC="$PACKAGE@$PACKAGE_VERSION"
LOG="$PREFIX/install.log"
_resolve_node || true

if [ "$DRY_RUN" = 1 ]; then
  _resolve_node_artifact
  echo "$SELF: plan"
  echo "  package        $SPEC"
  echo "  registry       $REGISTRY_DISPLAY"
  echo "  isolated npmrc $ISOLATED_NPMRC"
  echo "  platform       $OS_NAME/$ARCH_NAME -> $NODE_OS-$NODE_ARCH ($LIBC${GLIBC_VERSION:+ $GLIBC_VERSION})"
  echo "  prefix         $PREFIX"
  echo "  wrapper        $BIN_DIR/$WRAPPER_NAME"
  if [ "$SKIP_BROWSERS" = 1 ]; then
    echo "  browsers       skipped"
  else
    echo "  browsers       download${DOWNLOAD_HOST:+ from $DOWNLOAD_HOST_DISPLAY}"
  fi
  if [ -n "$NODE" ]; then
    echo "  node           reuse $NODE"
  else
    _assert_bootstrap_supported
    _assert_can_download
    echo "  node           install $NODE_ART_MIRROR_DISPLAY/v$NODE_VERSION/$NODE_ART_BASE.tar.gz"
  fi
  exit 0
fi

mkdir -p -- "$PREFIX" 2>/dev/null || die "$EX_NOT_WRITABLE" "cannot create the install prefix $PREFIX"
[ -w "$PREFIX" ] || die "$EX_NOT_WRITABLE" "install prefix $PREFIX is not writable"
mkdir -p -- "$BIN_DIR" 2>/dev/null || die "$EX_NOT_WRITABLE" "cannot create the wrapper directory $BIN_DIR"
[ -w "$BIN_DIR" ] || die "$EX_NOT_WRITABLE" "wrapper directory $BIN_DIR is not writable"

# Now that both exist, resolve them to canonical form. The earlier step only
# guaranteed they were ABSOLUTE, which is enough to be correct but still leaves
# "$PWD/./relative-prefix" to be written into the wrapper and compared against
# the npm target by the self-reference check below. Only $PREFIX and $BIN_DIR are
# canonicalised -- both are directories this installer owns. $NODE_BIN_DIR is
# deliberately left as-is: it may be a version manager's shim directory, and
# resolving that symlink would pin the wrapper past the shim to one exact
# toolchain the manager no longer controls.
PREFIX="$(cd -P -- "$PREFIX" && pwd)" \
  || die "$EX_NOT_WRITABLE" "cannot resolve the install prefix $PREFIX"
BIN_DIR="$(cd -P -- "$BIN_DIR" && pwd)" \
  || die "$EX_NOT_WRITABLE" "cannot resolve the wrapper directory $BIN_DIR"

# npm writes the registry URL into its output, so the log can hold a token the
# ambient umask would otherwise publish to every account on the host. Create it
# owner-only BEFORE npm can write; the later redirect truncates without
# changing the mode. The umask subshell is what actually makes that true: a
# plain redirect creates the file at the ambient mode and leaves it readable
# until the chmod lands. A chmod that cannot restrict the log is fatal, because
# continuing would run npm against a credentialed registry with its output
# landing in a file other principals can read.
if ! (umask 077; : >"$LOG") 2>/dev/null; then
  die "$EX_NOT_WRITABLE" "cannot write the install log $LOG"
fi
chmod 600 -- "$LOG" 2>/dev/null \
  || die "$EX_NOT_WRITABLE" "cannot restrict the install log $LOG to owner-only"

# Empty stand-ins for npm's user and global config scopes, used by
# --isolated-npmrc. They must be two separate paths (see _npm_install).
ISOLATED_NPMRC_DIR="$PREFIX/isolated-npmrc"
if [ "$ISOLATED_NPMRC" = 1 ]; then
  mkdir -p -- "$ISOLATED_NPMRC_DIR" 2>/dev/null \
    || die "$EX_NOT_WRITABLE" "cannot create $ISOLATED_NPMRC_DIR"
  : >"$ISOLATED_NPMRC_DIR/user" 2>/dev/null \
    && : >"$ISOLATED_NPMRC_DIR/global" 2>/dev/null \
    || die "$EX_NOT_WRITABLE" "cannot create the empty npm config files in $ISOLATED_NPMRC_DIR"
fi

[ -n "$NODE" ] || _bootstrap_node
# A Node without npm beside it is an ordinary Debian/Ubuntu install: `nodejs` and
# `npm` are separate packages, so `apt install nodejs` alone lands here. Telling
# that user to go and install npm would hand back the exact prerequisite this
# installer exists to remove, so the reusable Node is abandoned and our own is
# bootstrapped instead -- the tarball bundles npm. Only worth doing if we have
# not already bootstrapped: then npm really is absent from a tree we unpacked.
if ! _resolve_npm; then
  if [ "$NODE_WAS_BOOTSTRAPPED" = 1 ]; then
    die "$EX_NODE_BOOTSTRAP" \
      "the Node just installed at $PREFIX/node has no npm; the archive may be truncated"
  fi
  say "found Node at $NODE but no npm beside it; installing a private Node that bundles npm"
  NODE=""
  NODE_BIN_DIR=""
  _bootstrap_node
  _resolve_npm || die "$EX_NODE_BOOTSTRAP" \
    "the Node just installed at $PREFIX/node has no npm; the archive may be truncated"
fi
say "using Node $("$NODE" -p 'process.versions.node' 2>/dev/null || echo '?') at $NODE"

# ── already installed? ───────────────────────────────────────────────
# Only a PINNED version can be compared; `latest` is a moving target, so npm
# gets to make that decision itself.
# The executable must exist too, not just a matching manifest version: an
# install interrupted after npm wrote package.json but before it created the
# global bin would otherwise be skipped on every retry, and the verification
# below would fail every time, until the user discovered --force.
if [ "$FORCE" = 0 ] && [ "$PACKAGE_VERSION" != "latest" ] && [ -x "$PREFIX/bin/$WRAPPER_NAME" ]; then
  _have="$("$NODE" -e '
    const fs = require("fs");
    const file = process.argv[1] + "/lib/node_modules/" + process.argv[2] + "/package.json";
    try { process.stdout.write(JSON.parse(fs.readFileSync(file, "utf8")).version); } catch (e) { }
  ' "$PREFIX" "$PACKAGE" 2>/dev/null || true)"
  if [ "$_have" = "$PACKAGE_VERSION" ]; then
    say "$SPEC is already installed in $PREFIX (use --force to reinstall)"
    SKIP_INSTALL=1
  fi
fi

# ── install ──────────────────────────────────────────────────────────
# npm's own output is the only evidence available when an enterprise network
# refuses the install, so it is kept on disk and classified rather than streamed
# past the user at whatever verbosity npm chose.
_npm_install() {
  (
    PATH="$NODE_BIN_DIR:$PATH"; export PATH
    npm_config_registry="$REGISTRY"; export npm_config_registry
    # --global writes into npm_config_prefix, which keeps the install inside
    # $HOME and means sudo is never involved.
    npm_config_prefix="$PREFIX"; export npm_config_prefix
    npm_config_fund="false"; export npm_config_fund
    npm_config_audit="false"; export npm_config_audit
    npm_config_update_notifier="false"; export npm_config_update_notifier
    if [ "$ISOLATED_NPMRC" = 1 ]; then
      # Two DISTINCT empty files, not /dev/null twice: npm refuses to load one
      # path as two scopes ("double-loading config as global, previously loaded
      # as user") and exits before it resolves anything, which would break the
      # very flag that exists to rescue an enterprise install.
      npm_config_userconfig="$ISOLATED_NPMRC_DIR/user"; export npm_config_userconfig
      npm_config_globalconfig="$ISOLATED_NPMRC_DIR/global"; export npm_config_globalconfig
    fi
    if [ "$SKIP_BROWSERS" = 1 ]; then
      PLAYWRIGHT_SKIP_BROWSER_DOWNLOAD="1"; export PLAYWRIGHT_SKIP_BROWSER_DOWNLOAD
    fi
    if [ -n "$DOWNLOAD_HOST" ]; then
      PLAYWRIGHT_DOWNLOAD_HOST="$DOWNLOAD_HOST"; export PLAYWRIGHT_DOWNLOAD_HOST
    fi
    exec "$NPM" install --global "$SPEC"
  ) >"$LOG" 2>&1
}

# npm writes the resolved registry URL into its own output, so a URL carrying a
# token lands in the log and then in the tail printed on failure -- a terminal, a
# CI log, or a pasted bug report. Sanitizing the file itself covers both, and
# keeps the mode the pre-created file already has.
LOG_SUPPRESSED=0
_sanitize_log() {
  [ -f "$LOG" ] || return 0
  if (umask 077; _redact_urls <"$LOG" >"$LOG.redacted" 2>/dev/null) \
     && chmod 600 -- "$LOG.redacted" 2>/dev/null \
     && mv -- "$LOG.redacted" "$LOG"; then
    return 0
  fi
  # Redaction could not run, or the redacted copy could not be restricted to the
  # owner (a full filesystem, an unreadable log). The log may carry a token, so
  # it is EMPTIED rather than left for _show_log_tail to print: failing open
  # here would defeat the redaction entirely.
  rm -f -- "$LOG.redacted"
  : >"$LOG" 2>/dev/null || true
  LOG_SUPPRESSED=1
}

# Order matters: the browser CDN and the registry fail with the SAME transport
# errors, so the more specific signature is matched first.
_classify_failure() {
  if grep -qiE 'cdn\.playwright\.dev|playwright\.azureedge\.net|Failed to (download|install) (browser|chromium|firefox|webkit)|Download failure.*(chromium|firefox|webkit)' "$LOG"; then
    return "$EX_BROWSER_DOWNLOAD"
  fi
  if grep -qiE '\bE401\b|\bE403\b|ENEEDAUTH|EAUTHUNKNOWN|401 Unauthorized|403 Forbidden|Incorrect or missing password|npm login|auth(entication)? (required|token)' "$LOG"; then
    return "$EX_REGISTRY_AUTH"
  fi
  if grep -qiE '\bE404\b|404 Not Found|\bETARGET\b|No matching version|is not in this registry' "$LOG"; then
    return "$EX_PACKAGE_NOT_FOUND"
  fi
  if grep -qiE 'ENOTFOUND|EAI_AGAIN|ECONNREFUSED|ECONNRESET|ETIMEDOUT|ERR_SOCKET_TIMEOUT|network timeout|SELF_SIGNED_CERT|UNABLE_TO_(GET_ISSUER_CERT|VERIFY_LEAF_SIGNATURE)|CERT_HAS_EXPIRED|DEPTH_ZERO_SELF_SIGNED_CERT|tunneling socket could not be established|proxy' "$LOG"; then
    return "$EX_REGISTRY_UNREACHABLE"
  fi
  return 1
}

_show_log_tail() {
  echo "" >&2
  if [ "$LOG_SUPPRESSED" = 1 ]; then
    echo "  npm's output could not be scrubbed of credentials, so it was discarded" >&2
    echo "  rather than shown. Re-run with --isolated-npmrc to reproduce without" >&2
    echo "  any token in play." >&2
    return 0
  fi
  echo "  full npm log: $LOG" >&2
  echo "  ── last 20 lines of npm output ───────────────────────────" >&2
  tail -n 20 "$LOG" 2>/dev/null | sed 's/^/  | /' >&2
  echo "  ──────────────────────────────────────────────────────────" >&2
}

if [ "$SKIP_INSTALL" != 1 ]; then
  say "installing $SPEC into $PREFIX (registry: $REGISTRY_DISPLAY)"
  if ! _npm_install; then
    # Classify BEFORE scrubbing: the classifier greps this log, and emptying it
    # first would reduce every enterprise failure to "unclassified".
    _code=0
    _classify_failure || _code=$?
    _sanitize_log
    case "$_code" in
      "$EX_REGISTRY_AUTH")
        warn "the npm registry refused the request: authentication required."
        warn ""
        warn "  registry used: $REGISTRY_DISPLAY"
        warn ""
        warn "  This is what an enterprise network looks like when npm is pointed at a"
        warn "  private mirror that needs a login, or whose token has expired. Pick the"
        warn "  branch that matches your situation:"
        warn ""
        warn "  a) your ~/.npmrc redirects npm at a corporate mirror but this package is"
        warn "     public — bypass the ambient config entirely:"
        warn "       sh playwright-cli.sh --isolated-npmrc"
        warn ""
        warn "  b) the public registry is firewalled and the mirror is the only way out —"
        warn "     log in to it, then install from it:"
        warn "       npm login --registry https://npm.your-company.example/"
        warn "       sh playwright-cli.sh --registry https://npm.your-company.example/"
        warn ""
        warn "  c) the mirror does not carry these packages yet — ask whoever runs it to"
        warn "     proxy $PACKAGE, playwright and playwright-core"
        _show_log_tail
        exit "$EX_REGISTRY_AUTH"
        ;;
      "$EX_REGISTRY_UNREACHABLE")
        warn "could not reach the npm registry $REGISTRY_DISPLAY (DNS, proxy or TLS failure)."
        warn ""
        warn "  Behind an HTTP proxy, export it and re-run:"
        warn "    export HTTPS_PROXY=http://proxy.your-company.example:8080"
        warn "    export NO_PROXY=localhost,127.0.0.1,.your-company.example"
        warn "  Where the network terminates TLS with an internal certificate authority,"
        warn "  point Node at that CA bundle rather than disabling verification:"
        warn "    export NODE_EXTRA_CA_CERTS=/path/to/corporate-ca.pem"
        warn "  Where the public registry is blocked outright, install from the mirror:"
        warn "    sh playwright-cli.sh --registry https://npm.your-company.example/"
        _show_log_tail
        exit "$EX_REGISTRY_UNREACHABLE"
        ;;
      "$EX_PACKAGE_NOT_FOUND")
        warn "the registry has no '$SPEC'."
        warn ""
        warn "  Check which versions it actually carries:"
        warn "    npm view $PACKAGE versions --registry $REGISTRY_DISPLAY"
        warn "  A private mirror often holds only the versions someone already pulled"
        warn "  through it, so a version that exists publicly can still 404 there."
        _show_log_tail
        exit "$EX_PACKAGE_NOT_FOUND"
        ;;
      "$EX_BROWSER_DOWNLOAD")
        warn "the npm package installed, but downloading the browser binaries failed."
        warn ""
        warn "  Browser builds come from the Playwright CDN, not the npm registry, so a"
        warn "  network that allows npm can still block them. Either mirror them:"
        warn "    sh playwright-cli.sh --download-host https://playwright.your-company.example/"
        warn "  or install the CLI now and supply browsers separately:"
        warn "    sh playwright-cli.sh --skip-browsers"
        _show_log_tail
        exit "$EX_BROWSER_DOWNLOAD"
        ;;
      *)
        warn "npm failed to install $SPEC, and the failure matched no known cause."
        _show_log_tail
        exit 1
        ;;
    esac
  fi
  _sanitize_log
  say "installed $SPEC"
fi

# ── wrapper ──────────────────────────────────────────────────────────
# A symlink to the npm-generated bin would inherit its `#!/usr/bin/env node`
# shebang, which resolves against the CALLER's PATH — so a user whose Node this
# installer had to bootstrap (or whose PATH Node is too old) would get "node:
# not found" or an engine error from a tool that installed cleanly. The wrapper
# pins the exact Node that was verified at install time.
TARGET="$PREFIX/bin/$WRAPPER_NAME"
[ -x "$TARGET" ] || die "$EX_VERIFY" \
  "npm reported success but $TARGET does not exist; see $LOG"

WRAPPER="$BIN_DIR/$WRAPPER_NAME"
# --bin-dir "$PREFIX/bin" makes the wrapper AND its target the same file, so the
# wrapper would exec itself and the verification below would spin until the
# process ran out of stack. Compare canonical directories, because the two paths
# can name one directory by different routes.
_canon_dir() { ( cd "$1" 2>/dev/null && pwd -P ) 2>/dev/null || printf '%s' "$1"; }
if [ "$(_canon_dir "$BIN_DIR")/$WRAPPER_NAME" = "$(_canon_dir "$PREFIX/bin")/$WRAPPER_NAME" ]; then
  die "$EX_USAGE" \
    "--bin-dir must not be the installed package's own bin directory ($PREFIX/bin); the wrapper would replace the tool it wraps"
fi
cat >"$WRAPPER.incoming" <<EOF
#!/bin/sh
# Generated by playwright-cli.sh — re-run that installer to regenerate.
PATH=$(_shell_quote "$NODE_BIN_DIR"):\$PATH
export PATH
exec $(_shell_quote "$TARGET") "\$@"
EOF
chmod 755 "$WRAPPER.incoming"
mv "$WRAPPER.incoming" "$WRAPPER"

# ── verify ───────────────────────────────────────────────────────────
VERSION_OUT="$("$WRAPPER" --version 2>/dev/null || true)"
if [ -z "$VERSION_OUT" ]; then
  "$WRAPPER" --help >/dev/null 2>&1 \
    || die "$EX_VERIFY" "$WRAPPER was installed but does not run; see $LOG"
  VERSION_OUT="(version unavailable)"
fi

say "$WRAPPER_NAME $VERSION_OUT"
say "installed at $WRAPPER"
case ":$PATH:" in
  *":$BIN_DIR:"*) : ;;
  *)
    echo ""
    echo "$BIN_DIR is not on your PATH. Add this line to your shell profile"
    echo "(~/.bashrc, ~/.zshrc or ~/.config/fish/config.fish) and reopen the terminal:"
    echo "  export PATH=\"$BIN_DIR:\$PATH\""
    ;;
esac
echo ""
echo "Next steps:"
echo "  $WRAPPER_NAME --help"
if [ "$SKIP_BROWSERS" = 1 ]; then
  echo "  browsers were skipped — supply them from your own mirror before first use"
fi
