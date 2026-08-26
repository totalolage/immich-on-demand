# File-manager and distribution support boundary

Research snapshot: 2026-08-26

## Decision

Keep platform support exact and versioned.

1. Keep Arch Linux x86_64 with Nautilus 50 as the reference platform already
   exercised by the project.
2. Make Ubuntu Desktop 26.04 LTS amd64 with Nautilus 50.0 the next distribution
   target. Its native packages satisfy the project's current dependency bounds,
   and its Nautilus Python package exposes the 4.1 GI API used by the existing
   extension.
3. Treat Ubuntu Cinnamon 26.04 LTS amd64 with Nemo 6.4.5 as the only next
   file-manager candidate. Nemo uses the same GIO thumbnail attributes and the
   GNOME thumbnail factory, so the existing preview-cache design is plausible.
   Do not claim Nemo support until the exact acceptance matrix below passes.
4. Do not claim Debian 13, Fedora 44, Thunar, Dolphin, or generic Linux support.
   Each has a concrete dependency or preview-stack gap described below.

This is deliberately smaller than a portability layer. The FUSE filesystem and
desktop control protocol can remain unchanged. Nautilus support stays Nautilus
specific. If Nemo passes the runtime proof, it needs only its own thin provider
and sort-metadata reader, not a generic file-manager adapter framework.

## Existing architectural boundary

The core mount is built on pyfuse3. Linux FUSE presents a userspace filesystem
through the kernel VFS, which is the portable part of the design. The kernel
contract does not make file-manager preview behavior portable. See the
[Linux FUSE documentation](https://www.kernel.org/doc/html/latest/filesystems/fuse/fuse.html).

The current desktop integration has three narrower contracts:

- `nautilus_extension.py` requests the `Nautilus` 4.1 GI namespace and implements
  Nautilus menu and info providers. It is not a generic GIO extension.
- `previewer.py` reads `metadata::nautilus-icon-view-sort-by` and
  `metadata::nautilus-icon-view-sort-reversed`. Preview ordering is therefore
  Nautilus specific even though the preview files use a shared standard.
- `thumbnails.py` writes FreeDesktop thumbnail PNGs and a failure record under
  `fail/gnome-thumbnail-factory`. A successful cache hit is standards-based;
  suppression of a file manager's fallback read is manager-specific behavior
  that must be proved.

The current Python runtime bounds are Python 3.12 or newer, httpx 0.28 to less
than 1, Pillow 11 to less than 13, PyGObject 3.50 to less than 4, pyfuse3 3.4 to
less than 4, SecretStorage 3.3 to less than 4, and Trio 0.30 to less than 1.
These bounds, not the availability of a Python interpreter alone, define whether
a distribution can support a native package.

## Standards and session contracts

### Thumbnail cache and GIO

The latest published
[Thumbnail Managing Standard](https://specifications.freedesktop.org/thumbnail/latest-single/)
page retains a 0.8.0 document header and records the December 2020 version 0.9.0
additions in its history. Its live contract defines `$XDG_CACHE_HOME/thumbnails`,
MD5 names derived from the canonical URI, the `normal`, `large`, `x-large`, and
`xx-large` sizes, PNG metadata including `Thumb::URI` and `Thumb::MTime`, and
application-specific failure directories. It does not require every file
manager to consult the cache before reading a source file.

The [GIO file-attribute registry](https://docs.gtk.org/gio/file-attributes.html)
defines `thumbnail::path`, `thumbnail::failed`, and `thumbnail::is-valid`.
Those attributes are the useful interoperability seam for Nautilus and Nemo.
They are not evidence that Thunar or Dolphin follows the same request lifecycle.

Thumbnail identity includes the exact file URI. The support matrix therefore
uses the configured mount path directly. A symlink or alternate bind path is a
different URI and is outside the thumbnail-isolation guarantee.

### FUSE

FUSE makes the mount visible as a normal filesystem, but access is still governed
by its mount options and the daemon's user identity. The supported package keeps
the existing per-user mount and `default_permissions` behavior. It does not add
`allow_other`, a root daemon, or shared multi-user access. The
[pyfuse3 installation contract](https://pyfuse3.readthedocs.io/en/stable/install.html)
also requires libfuse 3.3 or newer and native build prerequisites when a wheel or
distribution package is unavailable. For a supported distribution, requiring a
local pyfuse3 source build is a packaging failure, not an installation step.

### Secret Service and the login session

SecretStorage talks to the
[FreeDesktop Secret Service 0.2 draft](https://specifications.freedesktop.org/secret-service/latest/),
published 2026-04-08, over the user's session D-Bus. Support requires an
available Secret Service, an unlocked default collection, and a graphical login
session. The API key must not move into the systemd unit, an environment file,
or the file-manager process.

The [XDG Base Directory specification 0.8](https://specifications.freedesktop.org/basedir/latest/)
requires `XDG_RUNTIME_DIR` to be private to the user, local, and tied to the
login lifetime. The control socket and mount lifecycle inherit that boundary.
Pre-login mounts, SSH-only/headless sessions, and persistent operation enabled
with systemd user lingering are not supported by this decision.

### systemd user service and native packages

The existing service is a `Type=exec` user unit installed in the system user-unit
directory and enabled under `default.target`. A Debian-family package should put
the same unit in the path reported by `pkg-config systemd --variable=systemduserunitdir`.
The [systemd daemon packaging guidance](https://manpages.debian.org/trixie/systemd/daemon.7.en.html)
defines that location, and
[`dh_installsystemduser`](https://manpages.debian.org/trixie/debhelper/dh_installsystemduser.1.en.html)
generates the install, upgrade, and removal maintainer-script integration for
user units. Its generated removal snippet invokes `deb-systemd-invoke --user
stop`; that helper enumerates active `user@<uid>.service` managers and addresses
each with `systemctl --user --machine`. The exact behavior is visible in the
[Ubuntu 1.69 source archive](https://archive.ubuntu.com/ubuntu/pool/main/i/init-system-helpers/init-system-helpers_1.69.tar.xz).

Build with `debhelper-compat (= 14)` and
`dh_installsystemduser --no-enable`. Compatibility level 13 omits the required
user-unit lifecycle snippets. At level 14, `deb-systemd-invoke --user restart`
checks enablement and activity, so a clean disabled and inactive install stays
inert while an enabled or already-active unit can restart on upgrade. The user
configures the server and stores the key before enabling the unit. The generated
removal snippet must stop and unmount every active user instance before package
files disappear. A user's enable choice may remain as user-owned configuration.
Remove must retain all user-owned XDG config, state, data, cache, upload recovery,
and Secret Service items. Purge semantics for user-owned state are not part of
the first package.

## Distribution snapshot

Versions below are the stable or currently available package versions observed
on 2026-08-26. A row marked `candidate` is not a support claim; it is the exact
environment to prove next.

| Platform | Relevant native versions | Decision | Reason |
| --- | --- | --- | --- |
| Arch Linux x86_64 | [Nautilus 50.2.2-1](https://archlinux.org/packages/extra/x86_64/nautilus/), [nautilus-python 4.1.0-3](https://archlinux.org/packages/extra/x86_64/nautilus-python/), [pyfuse3 3.5.0-1](https://archlinux.org/packages/extra/x86_64/python-pyfuse3/), [PyGObject 3.56.3-1](https://archlinux.org/packages/extra/x86_64/python-gobject/), [SecretStorage 3.5.0-1](https://archlinux.org/packages/extra/any/python-secretstorage/) | Existing reference | Current implementation and 1.0 acceptance were exercised here. Keep the claim at x86_64 and Nautilus 50. |
| Ubuntu Desktop 26.04 LTS amd64 | [Nautilus 1:50.0-0ubuntu2](https://packages.ubuntu.com/resolute/nautilus), [python3-nautilus 4.1.0-1build1](https://packages.ubuntu.com/resolute/amd64/python3-nautilus), [pyfuse3 3.4.0-3build5](https://packages.ubuntu.com/resolute/python3-pyfuse3), [PyGObject 3.56.2-1](https://packages.ubuntu.com/resolute/python3-gi), [SecretStorage 3.5.0-1](https://packages.ubuntu.com/resolute/python3-secretstorage), [Trio 0.32.0-1](https://packages.ubuntu.com/resolute/python3-trio) | Candidate | Native packages satisfy the current bounds and expose Nautilus GI 4.1. Prove the exact matrix before publishing a `.deb` support claim. |
| Ubuntu Cinnamon 26.04 LTS amd64 | [Nemo 6.4.5-1build1](https://packages.ubuntu.com/search?keywords=nemo), [nemo-python 6.6.0-1](https://packages.ubuntu.com/resolute/nemo-python), with the same Ubuntu core dependencies above | Conditional candidate | The thumbnail route is compatible in source, but the extension API and sort metadata differ. Claim only after a Nemo-specific provider and the complete matrix pass. |
| Ubuntu Desktop 24.04 LTS amd64 | [PyGObject 3.48.2-1](https://packages.ubuntu.com/noble/python3-gi), [pyfuse3 3.3.0-0.1](https://packages.ubuntu.com/noble/python3-pyfuse3) | Not supported | Both are below current project minimums. Do not add a private venv or locally built pyfuse3 to disguise the native-package gap. |
| Debian 13 amd64 | [Nautilus 48.3-2](https://packages.debian.org/trixie/nautilus), [python3-nautilus 4.0.1-2](https://packages.debian.org/trixie/python3-nautilus), [Trio 0.29.0-1](https://packages.debian.org/trixie/python3-trio), [pyfuse3 3.4.0-3+b3](https://packages.debian.org/trixie/python3-pyfuse3), [PyGObject 3.50.0-4+b1](https://packages.debian.org/trixie/python3-gi) | Not supported | Trio is below the current minimum, and python3-nautilus depends on the Nautilus 4.0 GI namespace while the extension requests 4.1. Reconsider only after a tested dependency decision or a newer stable release. |
| Fedora 44 x86_64 | [Nautilus 50.2.2-2.fc44](https://packages.fedoraproject.org/pkgs/nautilus/nautilus/index.html), [nautilus-python 4.1.0-2.fc44](https://packages.fedoraproject.org/pkgs/nautilus-python/nautilus-python/), [PyGObject 3.56.3-1.fc44](https://packages.fedoraproject.org/pkgs/pygobject3/python3-gobject/), [SecretStorage 3.5.0-2.fc44](https://packages.fedoraproject.org/pkgs/python-SecretStorage/python3-secretstorage/), [FUSE 3.18.2-1.fc44](https://packages.fedoraproject.org/pkgs/fuse3/fuse3/) | Not supported | Fedora's official package index exposed [fusepy 3.0.1-6.fc44](https://packages.fedoraproject.org/pkgs/python-fuse/python3-fusepy/fedora-44.html), not a maintained pyfuse3 binary package, in this snapshot. A pip or source build is outside the native-dependency rule. |

Ubuntu 26.04 also has native [Python 3.14.3-0ubuntu2](https://packages.ubuntu.com/resolute/python3),
[httpx 0.28.1-1build1](https://packages.ubuntu.com/resolute/python3-httpx),
[Pillow 12.1.1-2ubuntu1.2](https://packages.ubuntu.com/resolute-updates/python3-pil),
[libfuse 3.18.2-1](https://packages.ubuntu.com/resolute/libfuse3-4),
[libadwaita 1.9.0-0ubuntu1](https://packages.ubuntu.com/resolute/libadwaita-1-0),
[gnome-keyring 50.0-1](https://packages.ubuntu.com/resolute/gnome-keyring), and
[systemd 259.5-0ubuntu3.4](https://packages.ubuntu.com/resolute-updates/systemd). Together
with the table, these packages cover the current application, GTK desktop,
FUSE, Secret Service, and user-service dependencies without pip or a bundled
runtime.

Only amd64 is in scope. Package availability on another architecture is not
runtime acceptance on that architecture.

## File-manager determination

### Nautilus 50: support route already exists

Nautilus reads GIO thumbnail state before deciding whether it needs to create a
thumbnail. The exact Nautilus 50 source path and the project's zero-original
proof are recorded in
[nautilus-50-thumbnail-route.md](nautilus-50-thumbnail-route.md). The extension
contract and its no-network control boundary are recorded in
[nautilus-50-desktop-controls.md](nautilus-50-desktop-controls.md).

Ubuntu's `python3-nautilus` 4.1.0 package depends on `gir1.2-nautilus-4.1`, which
matches the extension's explicit GI request. This is why Ubuntu Desktop 26.04 is
the cheapest additional distribution to prove.

### Nemo 6.4.5: compatible cache, separate integration

Nemo 6.4.5 is a narrow candidate because its own source does all of the
following:

- reads `G_FILE_ATTRIBUTE_THUMBNAIL_PATH` and
  `G_FILE_ATTRIBUTE_THUMBNAILING_FAILED` in
  [`nemo-file.c`](https://github.com/linuxmint/nemo/blob/8d48119cd9b7fa1ec601294d7067905cb3338ebc/libnemo-private/nemo-file.c#L2608-L2633);
- avoids generating a thumbnail when a path exists or the failed flag is set in
  the [same file](https://github.com/linuxmint/nemo/blob/8d48119cd9b7fa1ec601294d7067905cb3338ebc/libnemo-private/nemo-file.c#L4953-L4976);
- generates, saves, and records failures through
  `GnomeDesktopThumbnailFactory` in
  [`nemo-thumbnails.c`](https://github.com/linuxmint/nemo/blob/8d48119cd9b7fa1ec601294d7067905cb3338ebc/libnemo-private/nemo-thumbnails.c#L315-L381).

This source evidence is enough to schedule a runtime test, not enough to claim
zero original reads. Nemo has its own Python GI namespace and callback shapes.
Its sort state also uses
`metadata::nemo-icon-view-sort-by`,
`metadata::nemo-icon-view-sort-reversed`,
`metadata::nemo-list-view-sort-column`, and
`metadata::nemo-list-view-sort-reversed`, as defined by
[`nemo-metadata.h`](https://github.com/linuxmint/nemo/blob/8d48119cd9b7fa1ec601294d7067905cb3338ebc/libnemo-private/nemo-metadata.h).
Nautilus sort polling cannot be relabeled as Nemo support.

If Nemo passes, keep the package boundary simple: the core package owns the
mount, service, desktop application, and control protocol; a file-manager
integration package owns only the corresponding provider and dependency. A
Nautilus install must not pull in Nemo, and a Nemo install must not pull in
Nautilus.

### Thunar 4.20.7: different thumbnail lifecycle

Ubuntu 26.04 provides
[Thunar 4.20.7](https://packages.ubuntu.com/resolute/thunar), but Thunar's
[`thunar-thumbnailer.c`](https://gitlab.xfce.org/xfce/thunar/-/raw/thunar-4.20.7/thunar/thunar-thumbnailer.c)
queues previews asynchronously through the `org.xfce.tumbler.*` D-Bus APIs.
The current GNOME failure record has not been proved to stop Tumbler from opening
a source file. Thunar is therefore outside the next support increment.

### Dolphin 25.12.3: separate KIO preview stack

Ubuntu 26.04 provides
[Dolphin 25.12.3](https://packages.ubuntu.com/resolute/kde/dolphin), but KDE
documents previews as [`KIO::PreviewJob`](https://api.kde.org/kio-previewjob.html)
work performed by KIO thumbnail creator plugins. That is a separate lifecycle
from the current GIO and GNOME thumbnail-factory route. No Dolphin support is
claimed, and this decision does not propose a KIO plugin.

Other file managers may browse the mount and explicitly open files because the
FUSE surface is ordinary POSIX I/O. They are not supported for directory
browsing: an unproved thumbnailer may open every original while rendering a
folder.

## Exact file-manager acceptance matrix

Run each row in a fresh dedicated OS user with a valid catalog. Use the exact
file-manager and distribution versions named above. Before every browsing row,
remove only the test fixture's thumbnail entries and its originals from the
content cache. Do not clear unrelated user cache or state.

An original fetch is observable as an Immich request to
`assets/<asset-id>/original`. Acceptance should also record FUSE `open` and
`read` counts for fixture assets so a cache or logging mistake cannot produce a
false zero.

| Scenario | Required observation |
| --- | --- |
| Mount discovery | The manager opens the configured mount root and sees exactly the five fixed View directories. Entering `All` enumerates the complete asset fixture set and reports expected name, size, MIME type, and modified time without an original request. |
| Grid or icon view | In `All`, open the first, middle, and final viewport and wait for previews. There are zero original endpoint requests, zero fixture originals added to the content cache, and zero fixture FUSE `open` or `read` calls attributable to directory rendering. |
| List view | Repeat the three-viewport `All` test in list view with the same three zero-original conditions. |
| Successful previews | Supported image and video fixtures display previews generated from the Immich thumbnail endpoint or an already valid thumbnail cache entry. The current implementation's retained `large` success PNG has the canonical mount URI and current mtime and size metadata. GLib or the manager may scale that cached image; acceptance does not require a manager-preferred cache size. |
| Failed and unsupported previews | A missing, invalid, or unsupported server preview produces a current failure record. Refreshing, scrolling away and back, and restarting the file manager cause no source-file open for that fixture. |
| Sort order | In `All`, test name, size, type, modified time, and created time where the manager exposes them, ascending and descending. Ignoring only the configured preview concurrency window, the first displayed uncached assets are the first preview requests. Changing the sort reprioritizes outstanding work without discarding valid results. |
| Exact manager metadata | Nautilus reads its icon-view keys. Nemo must pass both its icon-view and list-view keys. An unknown or absent key falls back safely and does not become an invented support claim. |
| Explicit file open | Open one chosen fixture after browsing. Exactly one original request occurs, one complete content-cache object appears, and its bytes and size match the server asset. Reopening uses that complete cache object. |
| Provider controls and emblems | Within the mount, status/emblem and the provider's Refresh, Manage Pending Uploads, Pin or Unpin, Retry Pinned Download, and Evict actions use the existing local control socket. Outside the mount there are no project actions. The provider receives no key and performs no Immich network request. |
| Filesystem create and upload | Copy one unique fixture into `All` through the manager's ordinary filesystem UI. The pending state is visible, one server asset is created, the catalog converges, and opening the round-tripped asset returns identical bytes. Creating in the mount root or a derived View is refused. This is a FUSE operation, not a provider context action. |
| Asset replacement | In `All`, create and write a temporary file, then use the supported rename-over flow before upload admission to replace one project-owned Test asset. Exactly one verified candidate replaces it, the old asset enters trash only after verification, the mounted name, Pin, and View aliases transfer, and opening the result returns the replacement bytes. Direct write or truncate and rename-over through a derived View are refused. Never use a Protected asset. |
| Guarded trash and restore | With the exact mutation key, delete the uploaded fixture from `All` through the manager's ordinary filesystem UI and confirm it enters Immich trash rather than permanent deletion. Restore it through the existing desktop application's restore control. A read-only key refuses mutation, and deletion through a derived View is refused. Neither operation is attributed to a provider menu item. |
| Restart | Restart the file manager while the mount stays active, then restart the user service. The manager reconnects, the mount returns, and valid preview and content cache entries remain usable. |

Search providers, content indexers, preview panes, and explicit quick-look tools
are separate I/O workloads. Disable them for the browsing-isolation rows or
attribute their opens separately. Zero original downloads applies to directory
rendering, not to a user asking another process to inspect file contents.

## Exact Ubuntu package acceptance matrix

Run these checks on a clean Ubuntu Desktop 26.04 LTS amd64 installation. Repeat
the manager-specific rows on Ubuntu Cinnamon 26.04 LTS amd64 only when Nemo is
ready for its support decision.

| Lifecycle | Required observation |
| --- | --- |
| Native install | Install locally built `.deb` files with APT. APT resolves every runtime dependency from Ubuntu packages. There is no pip invocation, venv, vendored Python runtime, or local pyfuse3 build. Package-file ownership and the user-unit location are correct. The package uses `debhelper-compat (= 14)` and `dh_installsystemduser --no-enable`; a disabled, inactive unit stays inert and no unconfigured daemon starts. |
| Configure | In a logged-in graphical session, configure the server and store read-only and mutation keys through Secret Service. No key appears in process arguments, the unit, environment files, logs, or package-owned files. |
| First enable | Enabling and starting the user unit after configuration produces one active FUSE mount and a valid control socket under `XDG_RUNTIME_DIR`. Starting before configuration exits nonzero with a configuration error and leaves no stale mount or socket. |
| File-manager behavior | Every applicable row in the file-manager matrix passes with the exact package versions in the distribution table. |
| Service restart | Restarting the user unit cleanly unmounts and remounts the configured path. Catalog, cache, pending uploads, and secrets remain intact. No second daemon or duplicate mount survives. |
| Package upgrade | Seed a catalog, one content-cache object, one pinned object, one valid thumbnail, and one resumable pending upload. Upgrade through APT. An enabled or active unit restarts at most once; a disabled and inactive unit does not start. All hashes and user-owned state remain intact, and the pending upload resumes according to its existing recovery contract. |
| Package removal | `apt remove` exercises the generated `deb-systemd-invoke --user stop` path for every active user manager, removes each active mount, then removes the unit, executable, desktop files, and provider installed by the package. XDG config, state, data, cache, upload recovery, Secret Service items, and any user-owned enable choice remain unchanged. Reinstall can use them. |
| Offline restart | With a previously synchronized catalog, restart while Immich is unreachable. The mount behavior matches the project's documented offline guarantees; no package helper substitutes a different policy. |

The first `.deb` can remain one core package plus the already supported
Nautilus integration if that is the smallest release artifact. Split the
file-manager provider only when Nemo is actually accepted. Do not create an
adapter SDK, plugin registry, cross-distribution installer, or packaging
framework in anticipation of more targets.

## Unsupported limits

The resulting support statement must be no broader than all of these limits:

- Arch Linux x86_64 with the tested Nautilus 50 package set remains the reference.
- Ubuntu Desktop 26.04 LTS amd64 with Nautilus 50.0 becomes supported only after
  its native package matrix passes.
- Ubuntu Cinnamon 26.04 LTS amd64 with Nemo 6.4.5 becomes supported only after
  both matrices pass, including Nemo-specific sort and provider checks.
- Package updates that change the file-manager major API, preview stack, Python
  ABI, pyfuse3, or libfuse require the relevant matrix again.
- No claim covers other Linux distributions, releases, architectures, desktop
  environments, file managers, or custom dependency mixes.
- No claim covers Flatpak, Snap, AppImage, containers, Nix, a pip-only install,
  or a bundled/private Python runtime.
- No claim covers root mounts, `allow_other`, multi-user sharing, a system
  service, pre-login mounts, user lingering, or SSH-only/headless operation.
- No claim covers mount aliases whose URI differs from the configured mount.
- Immich server-version compatibility remains a separate matrix from client
  platform compatibility.

This boundary is intentionally conservative. It adds only a target the current
architecture can prove cheaply and records where source evidence stops short of
runtime evidence.
