# Nautilus 50 desktop controls

Status: decision-ready research
Date: 2026-08-25
Examined baseline: Nautilus 50 on Arch Linux, nautilus-python 4.1.0, GTK 4, and libadwaita

## Decision

Ship two thin clients over the existing core settings API and bounded Unix-socket control protocol:

- One nautilus-python script supplies mount-scoped menu items and asynchronous emblems.
- One `Adw.Application` supplies the settings window and acts as the process launched by menu-item callbacks.

Install the extension in `/usr/share/nautilus-python/extensions`. It must not import the FUSE service, make Immich requests, or read secrets. It reads only the non-secret configured mount path, performs lexical `Gio.File` scope checks, and returns no items or emblems outside that path. It starts the desktop client with `Gio.Subprocess` and explicit argument arrays. The desktop client calls the existing settings and Secret Service APIs for configuration and the local control client for live operations.

This is the smallest supported boundary. It adds no Nautilus C module, GNOME Shell extension, D-Bus service, or client-side copy of daemon policy. It also works under Niri because the integration is inside Nautilus and the GUI is an ordinary GTK application, not a GNOME Shell component.

One acceptance statement needs correction: Nautilus cannot load a Python extension only for selected mounts. nautilus-python scans all configured extension directories and imports every script when Nautilus starts. The supported guarantee is instead: **the loaded provider returns no actions or emblems outside configured Immich mounts**. The loader behavior and search paths are explicit in the [nautilus-python 4.1.0 README](https://gitlab.gnome.org/GNOME/nautilus-python/-/blob/4.1.0/README.md#L13-27) and [loader source](https://gitlab.gnome.org/GNOME/nautilus-python/-/blob/4.1.0/src/nautilus-python.c#L223-275).

## Nautilus extension contract

### Loading and mount scope

nautilus-python 4.1.0 supports Nautilus 43 or newer and requires Python 3 and PyGObject. It searches, in order, `$XDG_DATA_HOME/nautilus-python/extensions`, the Nautilus prefix's data directory, and each `$XDG_DATA_DIRS/nautilus-python/extensions`. Scripts are loaded only at Nautilus startup and cannot be reloaded in place. `NAUTILUS_PYTHON_DEBUG=misc` enables loader diagnostics. See the [official requirements and loading instructions](https://gitlab.gnome.org/GNOME/nautilus-python/-/blob/4.1.0/README.md#L7-27).

Parse the configured mount path once as a `Gio.File`. Treat the mount root as in scope when `candidate.equal(mount)` and a child as in scope when `candidate.has_prefix(mount)`. `has_prefix()` compares complete path elements without I/O and deliberately does not consider a file its own prefix, which is why the equality check is also required. It does not resolve symbolic-link aliases. See the [`Gio.File.has_prefix()` contract](https://docs.gtk.org/gio/method.File.has_prefix.html). This avoids unsafe string-prefix cases such as treating `/home/user/Immich-copy` as a child of `/home/user/Immich`.

The extension may refresh its cached non-secret scope when the configuration file's mtime changes. Invalid, absent, non-native, or ambiguous configuration makes the provider inert. It must not ask the daemon to decide whether an unrelated path is in scope because merely opening a context menu must remain local and nonblocking.

### Menu-provider behavior

Implement `Nautilus.MenuProvider.get_background_items()` for mount-level actions such as Refresh and Settings. Implement `get_file_items()` for selection actions such as Evict and recovery-related entry points. Both methods return a list of `Nautilus.MenuItem` objects; returning an empty list is the normal way to expose nothing. This is the public [MenuProvider API](https://gnome.pages.gitlab.gnome.org/nautilus-python/class-nautilus-python-menu-provider.html), and the bridge calls these Python methods directly while Nautilus is building the menu in [nautilus-python's provider wrapper](https://gitlab.gnome.org/GNOME/nautilus-python/-/blob/4.1.0/src/nautilus-python-object.c#L164-249).

Therefore, menu construction must do only bounded local work: validate the selection count, obtain each `Nautilus.FileInfo` location, apply the cached mount predicate, and construct items. It must not open mounted files, contact Immich, connect to the control socket, wait for subprocesses, or run a Trio loop. Unsupported mixed or oversized selections return no mutation item. Cap mutation selections at 64 paths and cap the eventual serialized request below 48 KiB. Both limits are needed because a URI can be much larger than a filename.

On activation, start the desktop client with an explicit argument vector, for example:

```text
immich-on-demand-desktop --action refresh
immich-on-demand-desktop --action evict --uri file:///home/user/Immich/example.jpg
immich-on-demand-desktop
```

Use `Gio.Subprocess`, which accepts an argument vector rather than a shell command and controls child file descriptors and reaping. See the [GIO subprocess API](https://docs.gtk.org/gio/class.Subprocess.html). Never interpolate a URI into a command string, invoke a shell, or place credentials in process arguments. The callback returns immediately. The external client applies a bounded timeout to the local control request and presents sanitized success or failure in its own window or notification.

### Emblems without blocking Nautilus

Implement `Nautilus.InfoProvider.update_file_info_full()` rather than doing per-file synchronous socket calls. The API permits a provider to return `IN_PROGRESS`, complete later through the supplied closure, and handle cancellation; see the [InfoProvider API](https://gnome.pages.gitlab.gnome.org/nautilus-python/class-nautilus-python-info-provider.html) and [official asynchronous example](https://gitlab.gnome.org/GNOME/nautilus-python/-/blob/4.1.0/examples/update-file-info-async.py#L4-30).

Queue in-scope requests for a short event-loop turn and issue bounded batch queries to the daemon for at most 64 visible URIs and 48 KiB per serialized request. The response should contain local presentation state only, such as cached, pinned, busy, or recoverable. It must not contain credentials or remote-policy decisions. Apply packaged emblem names with `Nautilus.FileInfo.add_emblem()`, then complete every outstanding request, including timeout, cancellation, and daemon-error paths. The FileInfo API supplies locations, emblems, and `invalidate_extension_info()` for a later refresh; see the [FileInfo API](https://gnome.pages.gitlab.gnome.org/nautilus-python/class-nautilus-python-file-info.html).

Keep a small, short-lived in-process state cache so repeated Nautilus queries do not multiply socket traffic. Invalidate affected entries after an action subprocess completes; the TTL covers changes made through another client. Failure is fail-open for Nautilus: omit the emblem, complete the provider request, and leave the file manager responsive. Do not use the emblem provider as an authority for whether an operation is permitted. The daemon remains authoritative.

The private protocol needs only a batch `describe` operation plus the already planned actions. Retain the existing same-user socket permissions, one-request-per-connection model, 64 KiB frame ceiling, fixed timeout, unknown-field rejection, and secret-field rejection. The service validates every URI against its configured mount even though the extension has already scoped it.

## Settings application

Use one uniquely named `Adw.Application` with `Gio.ApplicationFlags.HANDLES_COMMAND_LINE`. GIO registers a unique application ID on the session bus and forwards later command lines to the primary instance; see [`Gio.Application`](https://docs.gtk.org/gio/class.Application.html), [`Application.run()`](https://docs.gtk.org/gio/method.Application.run.html), and [`ApplicationFlags`](https://docs.gtk.org/gio/flags.ApplicationFlags.html). This lets Settings, Evict, and recovery invocations reuse one process instead of inventing a second IPC service.

Build one `Adw.ApplicationWindow` from GTK 4/libadwaita widgets. `Adw.Application` performs libadwaita initialization and is the recommended application base; see [libadwaita initialization](https://gnome.pages.gitlab.gnome.org/libadwaita/doc/main/initialization.html). The window edits the one existing profile, stores keys through the existing Secret Service adapter, and controls the running service through the control client. It imports settings, secret, and control modules only. It must not import `pyfuse3`, filesystem operations, the Immich HTTP client, or the service entry point.

Do not populate secret entries with stored keys. A blank field means unchanged; a replacement is written directly through the secret adapter. UI errors contain only the sanitized service message and operation name. Configuration saves use existing validation. If a saved field requires a restart, launch `systemctl --user try-restart immich-on-demand.service` through `Gio.Subprocess` with fixed arguments and show its bounded result; the GUI must never mount, unmount, or instantiate FUSE itself.

## Arch packaging

The current Arch repositories provide [Nautilus 50](https://archlinux.org/packages/extra/x86_64/nautilus/), [nautilus-python 4.1.0](https://archlinux.org/packages/extra/x86_64/nautilus-python/), [GTK 4](https://archlinux.org/packages/extra/x86_64/gtk4/), [libadwaita](https://archlinux.org/packages/extra/x86_64/libadwaita/), and [python-gobject](https://archlinux.org/packages/extra/x86_64/python-gobject/). Add direct runtime dependencies on `nautilus-python`, `gtk4`, and `libadwaita`; `python-gobject` is already direct. PyGObject loads the system typelibs, so there is no separate pip GTK or libadwaita dependency.

Install:

- the extension script under `/usr/share/nautilus-python/extensions/`;
- the desktop entry under `/usr/share/applications/`;
- the GUI executable with the existing Python package;
- any application and emblem icons under the standard `/usr/share/icons/hicolor/` hierarchy.

Keep the extension and GUI as leaf adapters. Removing either installed file or disabling either entry point must leave the core package, daemon, and CLI importable and functional. A split Arch package is unnecessary for 1.1 unless independent installation becomes a distribution requirement.

## Acceptance plan

Automate the contract before the Reference-system check:

1. Test the `Gio.File` scope predicate for the root, descendants, siblings with a shared text prefix, spaces, non-file URIs, and invalid configuration.
2. Test providers with fake `FileInfo` objects. Out-of-scope, mixed, and oversized selections return no action and make no control call. In-scope callbacks construct the exact subprocess argument vector with the URI as one argument.
3. Test asynchronous emblem batching, the item and byte ceilings, cache hits, cancellation, timeout, malformed responses, and completion of every `IN_PROGRESS` request.
4. Test the GUI against a fake settings/secret/control boundary. Assert that displayed errors contain no key material and that importing the GUI and extension does not import `pyfuse3` or the service module.
5. Validate the installed desktop file and package paths, then run the existing daemon and CLI test suite with each client module absent in turn.

Complete acceptance on the Arch/Niri target:

1. Restart Nautilus after installation. Start it once with `NAUTILUS_PYTHON_DEBUG=misc` and confirm that the script loads without tracebacks.
2. Confirm Refresh and Settings on the configured mount background, applicable actions on mounted selections, and no Immich items or emblems in its parent, a similarly named sibling, `$HOME`, and another mount.
3. Navigate a large directory and open context menus while recording control traffic and FUSE operations. Nautilus must remain responsive, queries must be bounded batches, and listing or menu construction must not open original asset bytes.
4. Exercise success, stopped-daemon, timeout, malformed-response, and rejected-operation cases. The desktop client must show a useful sanitized error while Nautilus remains responsive.
5. Trigger each state transition and confirm the relevant emblem changes without restarting Nautilus. Navigate away during a pending batch to verify cancellation does not leave a stuck request.
6. Under Niri, save the single profile, replace each key through Secret Service, query status, and restart the user service. Confirm no credential appears in argv, logs, socket frames, or error text.
7. Remove or disable the Nautilus script, then the GUI entry point, separately. In both cases the user service and CLI must still pass status, refresh, and configuration checks.

The only source-level blocker is the ticket's per-mount loading language. After changing it to provider scoping, the design uses public APIs available in the target Arch packages.
