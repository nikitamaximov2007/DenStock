# Reconcile legacy customer attachments safely

The production-lineage command used to remove each public attachment as soon as
that row passed its hash check. A later conflicting or missing row could then
fail the command after an earlier public original had already been removed.

This candidate stages and hash-verifies missing private copies first, then checks
the entire referenced set before removing any public originals. If any path is
unsafe, missing, or has different bytes, it stops and keeps all public originals.
Repeated database references to one path are deduplicated for removal.

The command is still not a filesystem transaction: run it only after disabling
old writers that could mutate the legacy public tree. Keep the Caddy deny rule in
place. For a real migration, use the read-only audit, apply private copies, make
and verify a signed backup, confirm application reads and anonymous 404, then run
the removal pass in a separate approved window. This branch contains no
production file operations or deployment changes.
