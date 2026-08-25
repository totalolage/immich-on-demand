# Nautilus 50 thumbnail route

Status: decision-ready research
Date: 2026-08-25
Examined baseline: Nautilus 50.2.2 (`c6592e9c`), GLib/GIO 2.89.4 (`fa41d356`), and libgnome-desktop 44.5 (`c214a5f3`)

## Decision

Populate the user's FreeDesktop **global thumbnail cache** from Immich's server-generated previews. Key each cache entry by the final mounted `file://` URI. Do this in the core service, not in a MIME thumbnailer or Nautilus extension.

For every virtual file that Nautilus may show, create one of these files before Nautilus can start normal thumbnail generation:

1. A valid cached PNG made from the Immich preview endpoint; or
2. A valid per-file failed-thumbnail record when 1.0 does not support that preview or the preview fetch fails.

On a cache miss, Nautilus can invoke the desktop thumbnailer registered for the file's MIME type. That thumbnailer may open the FUSE file and hydrate the original. A valid failed-thumbnail record suppresses that fallback only for the named mounted URI. GLib checks the successful cache before the failure cache, so a later successful preview takes precedence. See [GLib's local thumbnail lookup](https://gitlab.gnome.org/GNOME/glib/-/blob/fa41d356ee4936264c45cf11fa6c2640a89fbdda/gio/glocalfileinfo.c#L1420-1523) and [Nautilus's cache-miss guard](https://gitlab.gnome.org/GNOME/nautilus/-/blob/c6592e9c7fce37ad685d0ba24720893955b7835d/src/nautilus-file.c#L4767-4784).

GNOME exposes `gnome_desktop_thumbnail_factory_create_failed_thumbnail()` to create this record with the correct URI and mtime metadata. See [the libgnome-desktop implementation](https://gitlab.gnome.org/GNOME/gnome-desktop/-/blob/c214a5f3ff96d6add49bd88372c0c449bcab1967/libgnome-desktop/gnome-desktop-thumbnail.c#L1514-1550). The library marks its thumbnail-factory API unstable. The FreeDesktop success cache is the portable contract. Keep the failed-thumbnail call in GNOME-specific code.

This route is per URI, uses a desktop standard, and does not register anything for unrelated local JPEGs or videos.

## Verified behavior

### Nautilus consumes the cache without reading the source

Nautilus 50 asks GIO for `thumbnail::*`, records `thumbnail::path`, `thumbnail::is-valid`, and `thumbnail::failed`, then reads the returned thumbnail path as a separate local file. The source asset is not opened on this cache-hit path. See [the GIO attribute query](https://gitlab.gnome.org/GNOME/nautilus/-/blob/c6592e9c7fce37ad685d0ba24720893955b7835d/src/nautilus-directory-async.c#L3259-3273), [the result handling](https://gitlab.gnome.org/GNOME/nautilus/-/blob/c6592e9c7fce37ad685d0ba24720893955b7835d/src/nautilus-file.c#L2938-2965), and [the read of the returned cache path](https://gitlab.gnome.org/GNOME/nautilus/-/blob/c6592e9c7fce37ad685d0ba24720893955b7835d/src/nautilus-directory-async.c#L3484-3525).

For a local path, including a kernel-visible FUSE mount, GLib's `GLocalFile` lexically canonicalizes the path and turns it into a `file:` URI with `g_filename_to_uri()`. It does not resolve filesystem aliases such as symlinks to a single identity. See [GLocalFile construction and URI generation](https://gitlab.gnome.org/GNOME/glib/-/blob/fa41d356ee4936264c45cf11fa6c2640a89fbdda/gio/glocalfile.c#L228-305).

When GIO is asked for the generic thumbnail attributes, its local-file implementation:

- Computes the canonical file URI.
- MD5-hashes the URI bytes, not the media bytes.
- Searches `$XDG_CACHE_HOME/thumbnails/{xx-large,x-large,large,normal}/<md5>.png` from largest to smallest.
- `stat`s the source and validates the cache PNG against it.
- Reads/maps the cache PNG for validation, not the original media.

Those operations are implemented directly in [GLib's `get_thumbnail_attributes()`](https://gitlab.gnome.org/GNOME/glib/-/blob/fa41d356ee4936264c45cf11fa6c2640a89fbdda/gio/glocalfileinfo.c#L1420-1528). The public GIO attributes describe `thumbnail::path` as the biggest available thumbnail and `thumbnail::is-valid` as its freshness result in the [GIO thumbnail attribute documentation](https://docs.gtk.org/gio/const.FILE_ATTRIBUTE_THUMBNAIL_PATH.html) and [validity documentation](https://docs.gtk.org/gio/const.FILE_ATTRIBUTE_THUMBNAIL_IS_VALID.html).

Nautilus itself chooses 256, 512, or 1024 pixels for newly generated thumbnails according to the maximum monitor scale factor. It never selects 128 pixels in this code path. See [Nautilus's size selection](https://gitlab.gnome.org/GNOME/nautilus/-/blob/c6592e9c7fce37ad685d0ba24720893955b7835d/src/nautilus-thumbnails.c#L155-241). GIO's generic lookup will nevertheless accept any standard size that exists and return the largest one.

### Exact cache contract

For a mounted path such as `/home/alice/Photos/example one.jpg`:

1. Construct the same absolute, lexically canonical file URI that GIO will use, including canonical percent encoding: `file:///home/alice/Photos/example%20one.jpg`.
2. Compute the lowercase hexadecimal MD5 of those URI bytes.
3. Save the thumbnail as `<hash>.png` under one standard size directory:
   - `normal`: at most 128×128
   - `large`: at most 256×256
   - `x-large`: at most 512×512
   - `xx-large`: at most 1024×1024
4. Preserve aspect ratio and encode an 8-bit, non-interlaced PNG.
5. Put these PNG `tEXt` entries in it:
   - `Thumb::URI`: the exact URI used for the MD5
   - `Thumb::MTime`: decimal whole seconds, exactly matching the FUSE `stat` result
   - `Thumb::Size`: decimal original byte size; optional in the standard, but recommended because the catalog already knows it
   - `Thumb::Mimetype`: optional and unnecessary for lookup
6. Create the cache directories with mode `0700`, the file with mode `0600`, write to a temporary file in the destination directory, then atomically rename it.

The authoritative format, metadata, hash, location, permissions, and atomic-write rules are in the [FreeDesktop Thumbnail Managing Standard](https://specifications.freedesktop.org/thumbnail/latest-single/). GNOME's own saver follows the same temp-file, permissions, PNG metadata, and rename pattern in [libgnome-desktop](https://gitlab.gnome.org/GNOME/gnome-desktop/-/blob/c214a5f3ff96d6add49bd88372c0c449bcab1967/libgnome-desktop/gnome-desktop-thumbnail.c#L1273-1360).

GLib 2.89.4 requires exact `Thumb::URI` and `Thumb::MTime` matches. `Thumb::Size` is optional, but if present it must match. Its verifier does not consult `Thumb::Mimetype`. See [the required fields](https://gitlab.gnome.org/GNOME/glib/-/blob/fa41d356ee4936264c45cf11fa6c2640a89fbdda/gio/thumbnail-verify.c#L27-115) and [comparison with the source `stat`](https://gitlab.gnome.org/GNOME/glib/-/blob/fa41d356ee4936264c45cf11fa6c2640a89fbdda/gio/thumbnail-verify.c#L224-253).

Use a GLib-produced `GFile` URI in the prototype as the oracle rather than hand-building one. The cache identity changes when the mount path or filename changes, even if the Immich asset ID does not. Such a change needs a new cache entry; the old entry is only an orphan eligible for cleanup.

### Invalidation rules for the FUSE model

The cache contract makes FUSE metadata part of thumbnail identity:

- Report a stable, nonzero mtime in whole seconds for every remote asset. Embed that exact value.
- Report the original's stable byte size. If `Thumb::Size` is embedded, any discrepancy invalidates the thumbnail.
- A renamed virtual file or changed mount root has a different URI and therefore a different hash.
- If an asset's bytes are treated as immutable, its mtime and size must remain stable across remounts and catalog refreshes.
- A new upload is a new entry and gets a new thumbnail contract. Existing remote assets are not overwritten in the agreed 1.0 model.

The standard requires mtime equality, not a newer-than comparison, and explains why size is a useful additional check in its [modification-detection section](https://specifications.freedesktop.org/thumbnail/latest-single/#detect-modifications).

### MIME handling and filename extensions

A successful cache lookup is not selected by MIME type. It is selected by URI hash and validated by URI, mtime, and optional size. This makes the route usable for any original format for which Immich can return a visual preview; the cached representation is always PNG. `Thumb::Mimetype` can be omitted.

MIME matters on a cache miss. Nautilus passes the file MIME type to `GnomeDesktopThumbnailFactory`, and the factory selects external thumbnailers from their MIME registrations. See [Nautilus's miss path](https://gitlab.gnome.org/GNOME/nautilus/-/blob/c6592e9c7fce37ad685d0ba24720893955b7835d/src/nautilus-file.c#L4767-4888) and [the MIME-based thumbnailer format](https://gitlab.gnome.org/GNOME/gnome-desktop/-/blob/c214a5f3ff96d6add49bd88372c0c449bcab1967/libgnome-desktop/gnome-desktop-thumbnail.c#L34-86).

Preserve a recognizable original extension in each visible filename. GIO first guesses content type from the basename; if the result is uncertain, its non-fast query opens the file and reads up to 16 KiB to sniff it. That is enough to trigger FUSE hydration. See [GLib's content-type path](https://gitlab.gnome.org/GNOME/glib/-/blob/fa41d356ee4936264c45cf11fa6c2640a89fbdda/gio/glocalfileinfo.c#L1300-1380). Whether every target suffix (`.jpg`, `.jpeg`, `.png`, `.gif`, `.mp4`, `.mov`, `.m4v`) is considered certain depends on the installed shared MIME database and must be checked on the target system.

The 1.0 format boundary applies only to useful previews, not to upload or download. Files outside the preview allowlist still need a valid failure record. Otherwise, an installed RAW, HEIF, or video thumbnailer can hydrate them when the directory opens.

Nautilus also applies user-visible policy after cache discovery. Version 50 defaults to showing thumbnails only on filesystems classified as local and sets a 50 MB limit for image MIME types; its grid code can decline to load the cache buffer for an over-limit image. See [the preview and size gate](https://gitlab.gnome.org/GNOME/nautilus/-/blob/c6592e9c7fce37ad685d0ba24720893955b7835d/src/nautilus-file.c#L4568-4605), [the buffer gate](https://gitlab.gnome.org/GNOME/nautilus/-/blob/c6592e9c7fce37ad685d0ba24720893955b7835d/src/nautilus-directory-async.c#L1690-1703), and [the default settings](https://gitlab.gnome.org/GNOME/nautilus/-/blob/c6592e9c7fce37ad685d0ba24720893955b7835d/data/org.gnome.nautilus.gschema.xml#L131-140). Most ordinary JPEG/PNG/GIF files fit under this ceiling; files above it require the user to raise the Nautilus limit if previews are desired.

## Rejected integration routes

### A global `.thumbnailer` file

Reject. GNOME thumbnailers are registered by MIME type and receive the source URI/path. Registering for `image/jpeg`, `image/png`, or common video types would participate in thumbnailing every matching file, not only this mount. The factory's lookup is a MIME-to-one-thumbnailer map, with no supported mount predicate. See [the thumbnailer registration code](https://gitlab.gnome.org/GNOME/gnome-desktop/-/blob/c214a5f3ff96d6add49bd88372c0c449bcab1967/libgnome-desktop/gnome-desktop-thumbnail.c#L330-383) and [`can_thumbnail()`](https://gitlab.gnome.org/GNOME/gnome-desktop/-/blob/c214a5f3ff96d6add49bd88372c0c449bcab1967/libgnome-desktop/gnome-desktop-thumbnail.c#L934-983).

### A Nautilus extension thumbnail provider

Reject as the core route. Nautilus 50 exposes no public thumbnail-provider interface. `NautilusFileInfo` permits reading identity/MIME/location and adding emblems or string attributes; `NautilusInfoProvider` can update that extension information asynchronously. Neither API can return `thumbnail::path`, a thumbnail buffer, or invalidate Nautilus's thumbnail state. See the complete public [`NautilusFileInfo` interface](https://gitlab.gnome.org/GNOME/nautilus/-/blob/c6592e9c7fce37ad685d0ba24720893955b7835d/libnautilus-extension/nautilus-file-info.h#L35-104) and [`NautilusInfoProvider` interface](https://gitlab.gnome.org/GNOME/nautilus/-/blob/c6592e9c7fce37ad685d0ba24720893955b7835d/libnautilus-extension/nautilus-info-provider.h#L68-97).

An extension could write the standard cache as a side effect, but it adds a process/lifecycle dependency without a supported ordering guarantee against Nautilus's thumbnail request. Keep extensions for later shell actions and emblems.

### `preview::icon` from the filesystem

Reject for an ordinary FUSE path. `preview::icon` is a supported GIO backend attribute containing a `GIcon`, and libgnome-desktop prefers it when a backend supplies one. See the [GIO attribute contract](https://docs.gtk.org/gio/const.FILE_ATTRIBUTE_PREVIEW_ICON.html) and [libgnome-desktop's load path](https://gitlab.gnome.org/GNOME/gnome-desktop/-/blob/c214a5f3ff96d6add49bd88372c0c449bcab1967/libgnome-desktop/gnome-desktop-thumbnail.c#L988-1055).

FUSE supplies POSIX file operations and xattrs, not an in-process `GIcon` object. GLib's hook for augmenting local-file `GFileInfo` is a private `GVfsClass.local_file_add_info` virtual method, so using it would require a replacement/custom GIO VFS implementation rather than an ordinary FUSE mount. See [the private GVfs class hook](https://gitlab.gnome.org/GNOME/glib/-/blob/fa41d356ee4936264c45cf11fa6c2640a89fbdda/gio/gvfs.h#L78-116).

### `.sh_thumbnails` inside the mount

Do not rely on it. The FreeDesktop standard defines read-only shared repositories under `.sh_thumbnails`, but the examined GLib 2.89.4 local-file lookup only searches the user's global XDG cache and `fail/gnome-thumbnail-factory`; it does not check `.sh_thumbnails`. Compare the [shared-repository specification](https://specifications.freedesktop.org/thumbnail/latest-single/#shared) with [the actual GIO lookup](https://gitlab.gnome.org/GNOME/glib/-/blob/fa41d356ee4936264c45cf11fa6c2640a89fbdda/gio/glocalfileinfo.c#L1467-1523).

## Minimal 1.0 implementation shape

1. Keep the canonical mounted path, reported mtime, and original size in the catalog row used by FUSE.
2. During exposure of an entry, derive its GLib-equivalent `file://` URI and cache hash.
3. Before exposing the directory entry, install either a successful thumbnail or a failed-thumbnail record.
4. For JPEG, PNG, GIF, and the chosen basic video MIME types, fetch only the Immich server preview. Convert it to a standard-sized PNG, add the required metadata, and atomically install it in the global cache.
5. Remove the failure record after successful installation. This cleanup is optional because GIO prefers a successful thumbnail.
6. Leave non-preview formats with generic icons and valid failure records. Their originals remain available on explicit file reads.

Only one successful size entry is required for correctness because GIO's generic lookup selects the largest available. Do not generate all four sizes. Choose between `large`, `x-large`, and `xx-large` after checking the target monitor scale and the dimensions returned by Immich; Nautilus's own scale mapping is 256/512/1024.

## Prototype questions

The source establishes the integration contract, but not these runtime facts:

1. Does a successful thumbnail refresh the icon after Nautilus has cached a valid failure record? If not, which file-monitor notification or view reload makes Nautilus re-query `thumbnail::*`? This result decides whether entries can appear before their previews arrive.
2. With both successful thumbnails and failure records, does opening a large directory cause zero FUSE `open` or `read` operations on original inodes on the actual Nautilus 50 and Arch package set?
3. Do all selected common suffixes avoid source reads during `standard::*` enumeration on the target shared MIME database?
4. Which single standard size matches both the target monitor scale and the Immich preview resolution without visible upscaling?
5. Which URI does Nautilus use when the mount is reached through the configured path, a symlink, or a file chooser bookmark? Cache creation must use that exact URI.
6. Does the target FUSE mount pass Nautilus's default `local-only` preview preference without a setting change? Nautilus honors both its user setting and GIO filesystem preview hints in [its preview decision](https://gitlab.gnome.org/GNOME/nautilus/-/blob/c6592e9c7fce37ad685d0ba24720893955b7835d/src/nautilus-file.c#L4490-4605).

The prototype's acceptance check should combine `gio info -a 'thumbnail::*' <mounted-file>`, Nautilus visual inspection, and FUSE operation logging. A cache hit must report a nonempty `thumbnail::path`, `thumbnail::is-valid: TRUE`, and no original-content `open` or `read`.
