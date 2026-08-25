# Minimal implementation stack

- Status: implemented; the Nautilus adapter recommendation was superseded by [ADR 0002](../adr/0002-populate-the-freedesktop-thumbnail-cache.md)
- Researched: 2026-08-25
- Scope: the Arch Linux, FUSE 3, Nautilus 50 target in the [problem statement](../../PROBLEM_STATEMENT.md)

## Recommendation

Build 1.0 as one Python 3 package:

| Concern | Choice |
| --- | --- |
| Runtime | Python 3.14 |
| FUSE | pyfuse3 on Trio |
| Immich HTTP | one long-lived HTTPX `AsyncClient` |
| Catalog | Python's `sqlite3` module and the system SQLite library |
| Secrets | SecretStorage against the user's Secret Service provider |
| Local IPC | newline-delimited JSON over an `AF_UNIX` socket in `$XDG_RUNTIME_DIR` |
| Nautilus | direct FreeDesktop thumbnail-cache integration using PyGObject |
| Service | one systemd user service |
| Package | a Flit Core wheel plus one Arch `PKGBUILD` |

This is the least-code credible stack. The FUSE daemon, settings service, CLI,
and Preview integration share Python domain code. Arch supplies pyfuse3, HTTPX,
Pillow, PyGObject, SecretStorage, and Trio.

Do not add an ORM, a web server, a D-Bus service, a plugin framework, a second
daemon language, or a GUI toolkit for 1.0. The CLI calls the settings service as
Python code. Only operations against the running mount need the private
Unix-socket control channel. A future GUI can call the same service code or the
same small control protocol.

## Why this stack holds

### FUSE and concurrency

[`pyfuse3`](https://github.com/libfuse/pyfuse3) is maintained by the libfuse
organization, binds libfuse 3, and provides an asynchronous API. Its maintainers
call the Trio path stable and explicitly say its asyncio path is less tested.
Its handlers already model inode lookup, attributes, open, read, write, create,
release, and directory iteration. The project does not need to bind libfuse or
write a request loop itself. The current Arch
[`python-pyfuse3`](https://archlinux.org/packages/extra/x86_64/python-pyfuse3/)
package is version 3.5.0 and directly depends on `fuse3` and `python-trio`. Its
[file list](https://archlinux.org/packages/extra/x86_64/python-pyfuse3/files/)
shows that Arch builds it for Python 3.14.

Use Trio throughout the daemon rather than adding asyncio compatibility glue.
Trio nurseries give downloads and the FUSE loop one cancellation tree, and
[`trio.to_thread.run_sync`](https://trio.readthedocs.io/en/stable/reference-core.html#trio.to_thread.run_sync)
is available for the few blocking calls that must not stall that loop. No
separate job framework or thread-pool wrapper is warranted.

### HTTP

Use one HTTPX `AsyncClient`, created at daemon startup and closed at shutdown.
HTTPX clients reuse pooled connections, while its top-level functions open a
new connection for each call
([client documentation](https://www.python-httpx.org/advanced/clients/)). Its
async interface supports streamed response bodies and streamed request bodies
([async documentation](https://www.python-httpx.org/async/)). Its normal request
API already handles custom headers, JSON, multipart file uploads, status errors,
and binary response streaming
([quick start](https://www.python-httpx.org/quickstart/)). It also distinguishes
connect, read, write, and pool timeouts
([timeout documentation](https://www.python-httpx.org/advanced/timeouts/)).
HTTPX runs on Trio through AnyIO, whose documented backends include Trio
([AnyIO basics](https://anyio.readthedocs.io/en/stable/basics.html)).

This dependency earns its place. Implementing multipart upload, connection
reuse, streaming, timeout classification, and TLS error handling on top of
`urllib.request` would create more application code. Arch ships
[`python-httpx`](https://archlinux.org/packages/extra/any/python-httpx/) in
`extra`.

### Catalog

Use Python's standard-library [`sqlite3`](https://docs.python.org/3/library/sqlite3.html)
module directly. SQLite is an embedded, disk-backed database with no server
process, and the Python module is already a DB-API 2.0 interface. Arch ships
[`sqlite`](https://archlinux.org/packages/core/x86_64/sqlite/) in `core`, and the
Python package's
[file list](https://archlinux.org/packages/core/x86_64/python/files/) includes
both `sqlite3` and `_sqlite3`. An ORM would add types and migrations without
removing the SQL needed for inode, path, cache-state, and pending-operation
invariants.

Start with one connection and short transactions. Ordinary indexed catalog
queries may run synchronously in the Trio thread. This choice has a known
ceiling. If measurement on the real library shows visible event-loop stalls,
move the same database calls behind one dedicated worker thread. Do not add an
asynchronous SQLite wrapper before that evidence exists.

### Secrets

Use [`SecretStorage`](https://secretstorage.readthedocs.io/en/latest/) directly.
It implements the FreeDesktop Secret Service protocol, can use the default
collection, and supports lookup, create, replace, delete, lock, and unlock. The
Secret Service specification recommends locating items by attributes rather
than persisting D-Bus object paths
([collections and items](https://specifications.freedesktop.org/secret-service/latest/ch03.html),
[lookup attributes](https://specifications.freedesktop.org/secret-service/latest/lookup-attributes.html)).
Store the API key under stable application, server, and account attributes.
Keep those non-secret identifiers in normal configuration.

SecretStorage's unlock path is synchronous and may block for a user prompt.
Read the key before starting the FUSE request loop and fail clearly if the
service is absent, locked, or the prompt is dismissed. Do not put a Secret
Service call inside a FUSE handler. Arch ships
[`python-secretstorage`](https://archlinux.org/packages/extra/any/python-secretstorage/)
in `extra`. Make the application package require the virtual
`org.freedesktop.secrets` provider so a real backend such as GNOME Keyring,
KeePassXC, KWallet, or oo7 is present.

### Settings API and local IPC

Keep settings operations as ordinary importable Python functions or classes in
the core package. The CLI is an adapter over that API, not its owner. This
satisfies the future-GUI requirement without creating a network service.

For live commands such as status, refresh, or cache eviction, use a private
`AF_UNIX` stream socket with one JSON request and response per line. Both Python
and Trio support Unix sockets
([Python `socket`](https://docs.python.org/3/library/socket.html),
[Trio I/O](https://trio.readthedocs.io/en/stable/reference-io.html)). Put the
socket under `$XDG_RUNTIME_DIR`. The XDG specification reserves that directory
for runtime IPC objects and requires it to be user-owned with mode 0700
([XDG Base Directory Specification](https://specifications.freedesktop.org/basedir/0.8/)).
Set the socket itself to 0600 and reject unknown methods and malformed or
oversized messages.

D-Bus would add interface description, name ownership, and another event-loop
integration problem without helping the two 1.0 clients. Reconsider it only if
independently installed third-party clients need discovery or bus activation.

### Nautilus

Write Preview and failure records directly to the FreeDesktop thumbnail cache.
Nautilus-python exposes info, menu, column, and properties providers, but it has
no thumbnail-provider interface. Keep nautilus-python for later mount-scoped
actions and emblems. The [thumbnail-route research](nautilus-50-thumbnail-route.md)
and [ADR 0002](../adr/0002-populate-the-freedesktop-thumbnail-cache.md) record the final decision.

### Arch packaging and service lifecycle

Use `flit_core` as the PEP 517 backend. It builds one importable package from a
standard `pyproject.toml` and supports console scripts without build code
([Flit configuration](https://flit.pypa.io/en/stable/pyproject_toml.html)). Arch
ships
[`python-flit-core`](https://archlinux.org/packages/extra/any/python-flit-core/)
in `extra`. Follow Arch's
[`python-build` and `python-installer` wheel pattern](https://wiki.archlinux.org/title/Python_package_guidelines).
Set the project to `arch=('any')`. Arch owns the architecture-specific pyfuse3
extension. Install the systemd user unit as a plain data file from the
`PKGBUILD`. The Python build backend does not need to know about system paths.

Run the foreground process under the user manager. A systemd
service unit directly supervises a process, and systemd recommends `Type=exec`
for long-running services so an `execve` failure is reported
([systemd.service](https://man.archlinux.org/man/systemd.service.5.en)). No
forking, PID file, root helper, container, or socket-activation unit is needed.

Expected direct runtime dependencies are:

```bash
depends=(
	python
	python-pyfuse3
	python-httpx
	python-pillow
	python-gobject
	python-secretstorage
	python-trio
	org.freedesktop.secrets
)
```

Expected build dependencies are:

```bash
makedepends=(python-build python-installer python-flit-core)
```

`fuse3` and SecretStorage's D-Bus and cryptographic libraries arrive through
those packages. Pin application compatibility in project tests, but let pacman
resolve the distribution packages. Do not vendor them into the release.

## Alternatives considered

| Stack | What it saves | What it adds here | Verdict |
| --- | --- | --- | --- |
| Go + go-fuse | A single daemon binary. HTTP and Unix sockets are in the standard library | A SQLite driver, a Secret Service client, and a Python or C Nautilus extension. The result has two application languages | Fallback if Python fails measured filesystem workloads |
| Rust + fuser | Compile-time ownership and a single daemon binary | An async runtime plus FUSE and runtime bridging, SQLite and Secret Service crates, and a Python or C Nautilus extension | More glue and more dependency and API churn than this personal 1.0 needs |
| C + libfuse + libcurl + SQLite + libsecret | Direct use of mature platform libraries. A C Nautilus extension can share the language | Manual ownership, cleanup, error translation, and asynchronous state across four callback APIs | Credible, but the most application code and maintenance burden |

Go is the closest alternative. [`go-fuse`](https://github.com/hanwen/go-fuse)
is active and offers node- and path-based APIs. Go's
[`net/http`](https://pkg.go.dev/net/http) and [`net`](https://pkg.go.dev/net)
cover HTTP and Unix sockets. However, `database/sql` requires an external driver
([Go documentation](https://pkg.go.dev/database/sql)). The common
[`go-sqlite3`](https://github.com/mattn/go-sqlite3) driver requires CGO and GCC.
The compact [`go-keyring`](https://github.com/zalando/go-keyring) client handles
Secret Service, but it is another external module. Nautilus integration still
needs Python or C. The binary is simpler to deploy, but the implementation is
not smaller.

Rust's [`fuser`](https://github.com/cberner/fuser) and
[`rusqlite`](https://docs.rs/rusqlite/latest/rusqlite/) are credible. The current
fuser changelog nevertheless labels its async API experimental and shows major
public-API changes in recent releases
([changelog](https://docs.rs/crate/fuser/latest/source/CHANGELOG.md)). Its
maintainer also states that pull requests are no longer accepted. Joining its
default synchronous callback API to async HTTP and Secret Service work creates
coordination code that pyfuse3 already avoids.

C uses only first-party platform APIs. Libfuse offers high-level synchronous and
low-level asynchronous APIs
([libfuse documentation](https://libfuse.github.io/doxygen/)). Libcurl's multi
API supports concurrent transfers in one thread
([libcurl overview](https://curl.se/libcurl/c/libcurl.html)). SQLite has a small
but manual `prepare`, `step`, and `finalize` lifecycle
([SQLite C introduction](https://www.sqlite.org/cintro.html)). Libsecret has
synchronous and asynchronous lookup and store calls
([libsecret simple API](https://gnome.pages.gitlab.gnome.org/libsecret/libsecret-simple-api.html)).
Those are sound building blocks, but composing their lifecycles safely is code
the Python libraries already contain.

## Operational and maintenance risks

1. **Python and libfuse compatibility on rolling Arch.** pyfuse3 contains a
   compiled extension, so Python upgrades require a coordinated rebuild. This is
   a distribution risk, not application code to solve. Use Arch's package,
   which is currently built for Python 3.14. Smoke-test the package after system
   upgrades.
2. **pyfuse3 is in maintenance mode.** Its maintainers promise fixes and support
   for new Python and libfuse versions but no planned new features. That is
   acceptable because 1.0 needs existing low-level filesystem operations, not a
   new binding feature. Re-evaluate only if a required kernel notification or
   operation is absent.
3. **Blocking local libraries.** SQLite and SecretStorage are synchronous.
   Keep secret access outside the request loop and keep SQL transactions short.
   Add one worker boundary only when a timing test demonstrates an event-loop
   stall.
4. **Python throughput.** The workload is HTTP and disk I/O, not transcoding.
   Still, the actual library may expose large-directory or concurrent-read
   pressure. Treat measured latency and correctness, not language preference,
   as the switch criterion.
5. **Nautilus process stability.** Extension exceptions run inside Nautilus.
   Keep the provider tiny, perform no network or database I/O there, bound its
   socket call, and return no additions on failure.

## Required proof before committing the implementation

Run one throwaway smoke prototype on the actual target Arch desktop:

1. Mount a pyfuse3 filesystem with one generated entry under the systemd user
   service and verify create, write, release, read, unlink, unmount, and restart.
2. Stream one HTTP response under the same Trio loop while a second FUSE request
   completes, proving that hydration does not serialize unrelated metadata work.
3. Store and retrieve a disposable credential through SecretStorage in the
   service's desktop session.
4. Load a minimal nautilus-python provider in Nautilus 50 and round-trip a status
   request through the Unix socket.

Switch the daemon to Go only if that prototype shows a pyfuse3 correctness or
throughput failure that cannot be fixed with indexing or one bounded worker.
Do not switch merely to obtain a single binary.

## Local inspection note

The accessible execution namespace was inspected read-only with `uname`,
`/etc/os-release`, `command -v`, the distro package database, `pkg-config`, and
Python module discovery. It is **not the stated Reference system**: it reports
Ubuntu 26.04 rather than Arch and does not expose Nautilus. It has Python 3.14.4
with SQLite 3.46.1, Go 1.26, FUSE 3.18.2, systemd 259, libsecret,
`secret-tool`, and PyGObject. It lacks Rust, pyfuse3, Trio, HTTPX, and
SecretStorage. The inventory only detected the environment mismatch. Current
Arch package pages, cited above, are the source of truth for target availability.
The four-step prototype must run on the real Arch, Niri, and Nautilus machine.
