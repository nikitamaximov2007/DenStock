# Public Catalog Stage 14: photos

## Audit of existing media (before this stage)

| Store | What it holds | Reachable how | Public-safe? |
| --- | --- | --- | --- |
| `catalog.PartTypeImage` | photos of a part type, uploaded in the part card (Layer 24) | files in `MEDIA_ROOT/part-types/<part id>/<uuid>.<ext>`, served by Caddy `/media/*` on the **internal** host | No. No provenance, no confirmation, may show internal context |
| `inventory.PartItemImage` | photos of one physical serial item (condition, marking) | `MEDIA_ROOT/part-items/...` | No, never: they describe a specific warehouse object |
| `PRIVATE_MEDIA_ROOT` | AI support screenshots | authenticated Django views only | No, never |

Upload validation already exists (`apps.core.files`): extension allowlist,
10 MB limit, magic-byte sniffing, UUID file names. It proves a file is an
image; it says nothing about whether the image is the right part, where it
came from, or whether a person agreed to show it to customers.

The local production snapshot of 2026-09-06 has 8 part photos (4 active) and
no item photos. None has recorded provenance, so none can be published by
migration.

## Model

`catalog.PublicPartPhoto` is one human decision about one internal
`PartTypeImage` (one-to-one). No row means "not public".

| Field | Meaning |
| --- | --- |
| `public_id` | opaque UUID used in public URLs; the warehouse PK never leaves the server |
| `part`, `source_image` | the part and the internal upload the decision is about |
| `status` | `published` or `rejected` (rejected keeps the photo out of the review queue) |
| `source`, `source_note` | provenance: own photo, manufacturer, supplier, plus a free note |
| `is_primary`, `sort_order` | which published photo leads on cards and the part page |
| `version` | hash of the renditions, part of the public URL for cache busting |
| `confirmed_at/by`, `rejected_at/by` | audit, in the same style as analog confirmation |

Database constraints make the rules independent of any form:

* `public_photo_published_has_provenance`: published requires a source and a confirmation time;
* `public_photo_primary_is_published`: only a published photo can be primary;
* `uniq_public_photo_primary`: at most one published primary per part.

`catalog.PublicPartPhotoRendition` holds the bytes actually served: a
`card` JPEG (longest edge 480 px) and a `detail` JPEG (1200 px). Pillow
decodes the upload once, applies EXIF orientation, flattens transparency on
white and re-encodes, so EXIF (camera, GPS, time) and anything hidden in the
original file are dropped. A 40 MP pixel cap refuses decompression bombs;
a rendition over 900 KB is re-encoded at lower quality or refused. At most
8 photos per part can be published.

Migration `catalog.0010_public_part_photos` is schema only and creates no
rows. Historical uploads stay candidates until a person decides.

## Why renditions live in the database

* The public container mounts no media volume at all. The restricted role
  can read renditions only through SQL, and row-level security limits it to
  published rows, so even a bug in a public view cannot read a rejected or
  unreviewed image.
* Publication and withdrawal are transactional: there is no file left
  behind after an unpublish, and no second volume to back up or to copy
  into the preview. The preview, restored from a production backup, gets
  exactly the published photos.
* Size is bounded: a published photo costs roughly 30-150 KB. A thousand
  published photos add well under 200 MB to the database.

## Moderation (internal DenisStock only)

The part card shows "Фото в публичном каталоге" for every active internal
photo: its status, source and the actions. Publishing requires a source and
the checkbox "На фото именно эта деталь". "Сделать главным", "Снять с
публикации" and "Не публиковать" complete the workflow. `/parts/public-photos/`
lists candidates, published and rejected photos across parts. All of it
uses `can_manage_parts`, the permission that already confirms analogs:
publishing is a catalog decision, not warehouse photo work, so the
storekeeper's image permission alone is not enough. Deleting an internal
photo withdraws its public copy in the same request.

## Public serving

`/photos/<public_id>/<card|detail>.jpg` reads the rendition with one query
that re-checks, on every request, that the photo is published and its part
is public and active. There is no filesystem path anywhere in the request:
guessed paths, internal media paths and traversal attempts resolve to 404.
Responses carry `ETag` (the rendition hash), `Cache-Control: public,
max-age=86400` and the public CSP. A withdrawn photo is 404 at once; a
browser may keep its own copy for up to a day, and a re-published photo
gets a new URL version.

Result cards and the part page ask for all primary photos of the shown
parts in one query, so photos add no per-row queries.
