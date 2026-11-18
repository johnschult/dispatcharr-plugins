#!/usr/bin/env bash
# Makes a new manifest signing key: the public half goes to ../signing-key.asc (commit it), the private half
# straight into this repo's SIGNING_KEY Actions secret. The private key is never written to a file; the
# throwaway container's keyring disappears with it. Needs Docker (for gpg) and an authenticated gh.
# Afterwards, paste signing-key.asc into the repository's public key in Dispatcharr.
set -euo pipefail
cd "$(dirname "$0")/.."

repo="johnschult/dispatcharr-plugins"
name="Schultiverse plugin signing"
image="$(cat IMAGE)"  # the Dispatcharr image ships gpg
out="$(mktemp -d)"
trap 'rm -rf "$out"' EXIT

docker run --rm -i -v "$out:/out" --entrypoint sh "$image" -c "
  set -e
  export GNUPGHOME=\$(mktemp -d)
  gpg --batch --quiet --passphrase '' --quick-gen-key '$name' ed25519 sign never 2>/dev/null
  gpg --armor --export '$name' > /out/signing-key.asc
  gpg --batch --armor --export-secret-keys '$name'
" | gh secret set SIGNING_KEY -R "$repo"

cp "$out/signing-key.asc" signing-key.asc
echo "SIGNING_KEY set on $repo; public key written to $(pwd)/signing-key.asc"
