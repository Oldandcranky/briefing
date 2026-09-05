#!/bin/bash
# Deploy this repo to the host that runs the briefing, verifying before swapping.
#
#   ./deploy.sh            fetch, test in a candidate image, then swap and rebuild
#   ./deploy.sh --check    report only: what's live, what's upstream, any drift
#
# Nothing is swapped in until a candidate image builds, the test suite passes
# *inside that image*, and the machine's own config.yaml still satisfies the new
# code. docker-compose.yml is treated as local configuration: drift is reported,
# never overwritten, because the volume paths are specific to this host.
set -euo pipefail

REPO="${REPO:-Oldandcranky/briefing}"
BRANCH="${BRANCH:-main}"
DEPLOY_DIR="${DEPLOY_DIR:-$(cd "$(dirname "$0")" && pwd)}"
DOCKER="${DOCKER:-$(command -v docker || echo /usr/local/bin/docker)}"
STATE="$DEPLOY_DIR/.deployed"
CHECK_ONLY=false
[ "${1:-}" = "--check" ] && CHECK_ONLY=true

say() { printf '\n\033[1m== %s\033[0m\n' "$*"; }
ok()  { printf '   \033[32mok\033[0m   %s\n' "$*"; }
bad() { printf '   \033[31mFAIL\033[0m %s\n' "$*" >&2; }
note(){ printf '        %s\n' "$*"; }

[ -x "$DOCKER" ] || { bad "docker not found (set DOCKER=/path/to/docker)"; exit 1; }
cd "$DEPLOY_DIR"

# The output dir is wherever compose mounts /data; that's where config.yaml lives.
DATA_DIR="${DATA_DIR:-$(sed -n 's|^[[:space:]]*-[[:space:]]*\(/[^:]*\):/data$|\1|p' \
    docker-compose.yml 2>/dev/null | head -1)}"
CONFIG_FILE="${CONFIG_FILE:-${DATA_DIR:-}/config.yaml}"

say "Upstream"
API="https://api.github.com/repos/$REPO"
SHA=$(curl -fsSL -H "Accept: application/vnd.github+json" "$API/commits/$BRANCH" \
      | sed -n 's/.*"sha"[[:space:]]*:[[:space:]]*"\([0-9a-f]\{40\}\)".*/\1/p' | head -1)
[ -n "$SHA" ] || { bad "could not resolve $REPO@$BRANCH"; exit 1; }
note "$REPO@$BRANCH is ${SHA:0:12}"

# Fetch the tarball for that exact commit. Branch tarballs and raw.githubusercontent
# are CDN-cached and will happily hand back the previous commit for a few minutes.
STAGE=$(mktemp -d)
trap 'rm -rf "$STAGE"' EXIT
curl -fsSL "https://github.com/$REPO/archive/$SHA.tar.gz" | tar xz -C "$STAGE"
SRC="$STAGE/$(basename "$REPO")-$SHA"
[ -f "$SRC/briefing.py" ] || { bad "tarball missing briefing.py"; exit 1; }
ok "fetched ${SHA:0:12}"

say "Drift"
LIVE_SUM=$(md5sum briefing.py 2>/dev/null | cut -d' ' -f1 || echo none)
NEW_SUM=$(md5sum "$SRC/briefing.py" | cut -d' ' -f1)
if [ -f "$STATE" ]; then
    read -r WAS_SHA WAS_SUM < "$STATE"
    note "last deployed ${WAS_SHA:0:12}"
    [ "$LIVE_SUM" = "$WAS_SUM" ] \
        && ok "briefing.py matches what was deployed" \
        || bad "briefing.py was edited on this host since the last deploy — it will be replaced"
else
    note "no deploy record yet"
fi
if [ "$LIVE_SUM" = "$NEW_SUM" ]; then
    ok "already running this commit's briefing.py"
else
    note "briefing.py differs from upstream (live $LIVE_SUM, new $NEW_SUM)"
fi
if ! diff -q docker-compose.yml "$SRC/docker-compose.yml" >/dev/null 2>&1; then
    note "docker-compose.yml differs from upstream (local config; not touched):"
    diff docker-compose.yml "$SRC/docker-compose.yml" | sed 's/^/          /' || true
fi

# This script does not deploy itself. Bash reads a script as it executes, so
# overwriting the running file mid-run can make it resume at the wrong byte.
# Stage the new one beside it and let the operator swap it when nothing is running.
SELF="$DEPLOY_DIR/deploy.sh"
if [ -f "$SELF" ] && ! diff -q "$SELF" "$SRC/deploy.sh" >/dev/null 2>&1; then
    cp "$SRC/deploy.sh" "$SELF.new" && chmod +x "$SELF.new"
    bad "deploy.sh itself is out of date ($(diff "$SELF" "$SRC/deploy.sh" | grep -c '^[<>]') lines differ)"
    note "a running script cannot safely overwrite itself, so the new one is staged as"
    note "deploy.sh.new — install it once this run has finished:"
    note "    mv $SELF.new $SELF"
else
    rm -f "$SELF.new"
fi

say "Candidate image"
CAND="briefing:candidate-${SHA:0:12}"
"$DOCKER" build -q -t "$CAND" "$SRC" >/dev/null
ok "built $CAND"

say "Tests, inside the image"
"$DOCKER" run --rm -v "$SRC/tests:/app/tests:ro" "$CAND" python tests/test_briefing.py \
    | tail -3 | sed 's/^/        /'
ok "suite passed against the image that would ship"

say "This host's config, against the new code"
if [ -n "${CONFIG_FILE:-}" ] && [ -f "$CONFIG_FILE" ]; then
    if ! CFG_OUT=$("$DOCKER" run --rm -e BRIEFING_OUT=/tmp/out \
        -e BRIEFING_CONFIG=/tmp/config.yaml \
        -v "$CONFIG_FILE:/tmp/config.yaml:ro" "$CAND" python -c "
import briefing as b
assert b.CFG['feeds'], 'no feeds configured'
assert b.CFG['feed']['base_url'], 'feed.base_url missing'
assert b.CFG['feed']['keep_episodes'] > 0, 'keep_episodes must be positive'
assert b.CFG['email']['to'], 'email.to missing'
assert int(b.CFG.get('bullets', 12)) > 0, 'bullets must be positive'
print('%d feeds, %d bullets, %sh window, %sd ledger, keep %d' % (
    len(b.CFG['feeds']), b.CFG.get('bullets', 12),
    b.CFG.get('max_age_hours', 48), b.CFG.get('ledger_days', 7),
    b.CFG['feed']['keep_episodes']))
" 2>&1); then
        # Whatever went wrong, say so. Hiding this behind /dev/null once turned a
        # stale assertion in this script into a deploy that stopped with no reason given.
        printf '%s\n' "$CFG_OUT" | tail -5 | sed 's/^/        /'
        bad "$CONFIG_FILE does not satisfy the new code"
        exit 1
    fi
    printf '%s\n' "$CFG_OUT" | sed 's/^/        /'
    ok "$CONFIG_FILE still satisfies the new code"
else
    bad "config not found (set CONFIG_FILE=...); skipping this check"
fi

if $CHECK_ONLY; then
    say "Check only — nothing changed"
    exit 0
fi

say "Deploying"
BACKUP="$DEPLOY_DIR/backup-$(date +%Y%m%d-%H%M%S)"
mkdir -p "$BACKUP"
cp -p briefing.py Dockerfile "$BACKUP"/ 2>/dev/null || true
ok "backed up to $(basename "$BACKUP")"

cp "$SRC/briefing.py" "$SRC/Dockerfile" "$DEPLOY_DIR"/
cp "$SRC/config.yaml.example" "$DEPLOY_DIR"/ 2>/dev/null || true
rm -rf "$DEPLOY_DIR/tests" && cp -r "$SRC/tests" "$DEPLOY_DIR"/
ok "files in place"

if "$DOCKER" compose build 2>&1 | tail -1 | sed 's/^/        /'; then
    ok "compose image rebuilt"
else
    bad "compose build failed — rolling back"
    cp -p "$BACKUP"/briefing.py "$BACKUP"/Dockerfile "$DEPLOY_DIR"/
    "$DOCKER" compose build >/dev/null 2>&1 || bad "rollback rebuild also failed; check by hand"
    exit 1
fi

FINAL=$(md5sum briefing.py | cut -d' ' -f1)
[ "$FINAL" = "$NEW_SUM" ] || { bad "deployed file doesn't match upstream"; exit 1; }
echo "$SHA $FINAL" > "$STATE"
"$DOCKER" image rm "$CAND" >/dev/null 2>&1 || true

say "Done"
note "running ${SHA:0:12}; next scheduled run picks it up"
note "roll back with: cp $BACKUP/briefing.py $DEPLOY_DIR/ && $DOCKER compose build"
