# DR cutover Docker mount gate

The current Compose stack mounts named volumes directly at the configured
`MEDIA_ROOT` (`/app/mediafiles`) and `PRIVATE_MEDIA_ROOT` (`/app/private_media`).
The offline cutover renames each configured root directory, so its preflight
rejects either root when it is a mount point. This is a fail-closed blocker;
`dr_cutover_prepare` must not be forced past it.

The DR crash harness uses synthetic ordinary directories. Its PostgreSQL 16
fault coverage does not prove the Docker named-volume topology or inode identity
across a host mount and a container mount. A future topology needs a stable
parent volume with per-generation child directories, or another tested way to
switch the volume references while every web, bot, and proxy writer is stopped.
It must keep staging and live generations on one filesystem and prove that
startup generation checks see the same inodes after Docker remounts them.

No production cutover was attempted. Until the topology and mounted-volume
simulation pass, use the existing offline restore procedure without claiming
that the Docker stack can switch generations automatically.
