# Release image identity

`DENSTOCK_APP_COMMIT` in `.env` is not a release source of truth. Compose uses
`DENSTOCK_BUILD_REVISION` to select the SHA-tagged image and pass the build
revision. The Docker image stores that value as `DENSTOCK_APP_COMMIT` and the
OCI `org.opencontainers.image.revision` label. The image also records the
SHA-256 of the exact `git archive` source context. These fields contain only
public hashes, never secrets.

## Build and verify before an approved release

From a clean checkout at the exact approved SHA, after the release window and
backup gates have passed:

```bash
python3 scripts/operations/release_image_identity.py build \
  --sha <full-40-character-sha> --services web
python3 scripts/operations/release_image_identity.py verify \
  --sha <full-40-character-sha> --services web
```

The builder requires HEAD and the release-control files to match the requested
SHA, creates its Docker context from that commit's Git archive, builds only the
named services, then checks the image ID, OCI revision, source archive digest,
and image environment. It fails closed if any value differs. Its output omits
environment contents and build logs. The temporary source archive is removed
when the command exits.

Only after separate deployment approval, recreate the named service without a
second build and verify the running container against the image:

```bash
sed -i '/^DENSTOCK_APP_COMMIT=/d' .env
sed -i '/^DENSTOCK_BUILD_REVISION=/d' .env
printf 'DENSTOCK_BUILD_REVISION=%s\n' '<full-40-character-sha>' >> .env
docker compose up -d --no-build --no-deps --wait web
python3 scripts/operations/release_image_identity.py verify \
  --sha <full-40-character-sha> --services web --runtime
```

For multiple services, pass the same comma-separated list to both commands.
The first release using this procedure removes the legacy `.env` override and
sets the build revision used to select the immutable SHA-tagged image. The
running process gets `DENSTOCK_APP_COMMIT` from the image itself. Preserve the
verified image IDs before recreation for rollback. These `.env` edits are part
of a separately approved deployment only; never run them during read-only
preflight.

An ad-hoc Compose build without the release tool is labelled `unverified` and
must not pass the release identity check. Do not put credentials, tokens,
environment dumps or customer data in Docker build arguments or labels.
