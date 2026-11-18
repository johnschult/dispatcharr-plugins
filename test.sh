#!/usr/bin/env bash
# Tests for our Dispatcharr plugins.
#   unit:        each plugin's tests/ with uv (no Dispatcharr needed)
#   integration: each plugin's integration/ inside the Dispatcharr image in ./IMAGE, against its own
#                Postgres in a throwaway container, with the plugins loaded from /data/plugins
#
#   ./test.sh                 both
#   ./test.sh unit            unit only
#   ./test.sh integration     integration only
# Env: DISPATCHARR_IMAGE overrides the image in ./IMAGE; CONTAINER reuses a running container
# (started from that image with -v /data) and leaves it running.
set -euo pipefail
cd "$(dirname "$0")"

what="${1:-all}"
image="${DISPATCHARR_IMAGE:-$(cat IMAGE)}"
plugins=(plugins/*/)

unit() {
  echo "== unit: publish"
  (cd publish && uv run -q --no-project --python 3.13 --with pytest python -m pytest -q -p no:cacheprovider tests)
  for p in "${plugins[@]}"; do
    echo "== unit: $(basename "$p")"
    (cd "$p" && uv run -q --no-project --python 3.13 --with pytest python -m pytest -q -p no:cacheprovider tests)
  done
}

integration() {
  local c="${CONTAINER:-}"
  if [[ -z "$c" ]]; then
    c="dispatcharr-plugin-tests-$$"
    echo "== starting $image"
    docker run -d --name "$c" -v /data -e DISPATCHARR_ENV=aio "$image" >/dev/null
    trap "docker rm -f $c >/dev/null 2>&1 || true" EXIT  # expanded now: \$c is local and gone by exit
  fi
  local x=(docker exec -w /app -e DJANGO_SECRET_KEY=integration-tests -e PYTHONPATH=/tests "$c")

  echo "== waiting for Dispatcharr to finish migrating"
  for _ in $(seq 1 200); do
    [[ "$(docker inspect -f '{{.State.Running}}' "$c")" == true ]] || { docker logs --tail 40 "$c"; exit 1; }
    "${x[@]}" python manage.py migrate --check >/dev/null 2>&1 && break
    sleep 3
  done
  "${x[@]}" python manage.py migrate --check >/dev/null

  "${x[@]}" uv pip install -q --python /dispatcharrpy/bin/python pytest==9.2.0 pytest-django==4.14.0

  # Plugins go where Dispatcharr loads them from; tests go to /tests, one folder per plugin
  docker exec "$c" rm -rf /tests
  docker exec "$c" mkdir -p /tests
  docker cp integration/. "$c:/tests/"
  docker cp publish/publish.py "$c:/tests/publish.py"
  for p in "${plugins[@]}"; do
    name="$(basename "$p")"
    COPYFILE_DISABLE=1 tar -C plugins --no-xattrs -cf - --exclude=__pycache__ --exclude=tests --exclude=integration "$name" \
      | docker exec -i "$c" sh -c "rm -rf /data/plugins/$name && tar -xf - -C /data/plugins"
    if [[ -d "$p/integration" ]]; then
      docker exec "$c" mkdir -p "/tests/$name"
      docker cp "$p/integration/." "$c:/tests/$name/"
    fi
  done

  echo "== integration"
  "${x[@]}" python -m pytest /tests -q -p no:cacheprovider \
    --ds=dispatcharr.settings --rootdir=/tests
}

case "$what" in
  unit) unit ;;
  integration) integration ;;
  all) unit; integration ;;
  *) echo "usage: $0 [unit|integration|all]" >&2; exit 2 ;;
esac
