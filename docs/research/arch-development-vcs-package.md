# Arch development VCS package

Status: implementation-ready research
Date: 2026-08-26
Examined baseline: Immich On-Demand 1.4.0.dev0, Arch Linux, Nautilus 50

## Decision

Keep `packaging/PKGBUILD` as the checksum-pinned version 1.0.0 release recipe. Add the moving development recipe only at `packaging/development/PKGBUILD`, named `immich-on-demand-git`. This keeps release builds reproducible while providing one Reference-system package for the current daemon, CLI, GTK application, Nautilus adapter, and icons.

Use a Git source named `immich-on-demand`, fetched from the HTTPS `main` branch, and derive `pkgver` as `1.4.0.dev0.r<commit-count>.g<short-hash>`. The repository's last tag is still 1.0.0, so reading the development version from `pyproject.toml` and appending a monotonic revision identifies the current source more accurately than `git describe` alone. Arch's VCS guidance permits parsing project files for the release component and recommends the `RELEASE.rREVISION` form, a `-git` package name, the VCS tool in `makedepends`, and `SKIP` for a moving VCS checksum. It also recommends versioned `provides`, `conflicts`, and no `replaces`. See the official [VCS package guidelines](https://wiki.archlinux.org/title/VCS_package_guidelines) and [PKGBUILD relations](https://wiki.archlinux.org/title/PKGBUILD#Package_relations).

Use:

```bash
pkgname=immich-on-demand-git
provides=("immich-on-demand=$pkgver")
conflicts=('immich-on-demand')
source=('immich-on-demand::git+https://github.com/totalolage/immich-on-demand.git#branch=main')
b2sums=('SKIP')
```

Do not add `replaces`. Installing the VCS package should prompt to remove the conflicting release package, but repository metadata should not cause an automatic replacement.

## Dependencies and build

Retain the released runtime dependencies and add the direct desktop dependencies:

- `gtk4`, `libadwaita`, and `python-gobject` for the modules imported by the settings application;
- `nautilus-python` for the Nautilus 4.1 typelib and Python loader;
- `hicolor-icon-theme` because this package installs application and emblem icons into that theme.

Arch says packages must list direct dependencies rather than rely on transitive installation. The official package metadata confirms that [libadwaita depends on GTK 4](https://archlinux.org/packages/extra/x86_64/libadwaita/), [nautilus-python depends on Nautilus and python-gobject](https://archlinux.org/packages/extra/x86_64/nautilus-python/), and [hicolor-icon-theme supplies the fallback theme](https://archlinux.org/packages/extra/any/hicolor-icon-theme/). They remain direct here because the installed application, extension, and icons each use them directly. See the [Arch dependency rule](https://wiki.archlinux.org/title/Arch_package_guidelines#Package_dependencies).

The complete additions to the released recipe are therefore `gtk4`, `hicolor-icon-theme`, `libadwaita`, and `nautilus-python`; keep `python-gobject`. Keep `git`, `python-build`, `python-flit-core`, and `python-installer` in `makedepends`. Build the wheel with `python -m build --wheel --no-isolation`, run the standard-library test suite in `check()`, and install through `python -m installer --destdir="$pkgdir"`. Arch documents `build()`, `check()`, and `package()` as running in `$srcdir`, with `package()` writing only under `$pkgdir`; no extra build directory is required for a VCS source. See [PKGBUILD(5)](https://man.archlinux.org/man/PKGBUILD.5.en.html) and the [Arch Python package guidelines](https://wiki.archlinux.org/title/Python_package_guidelines).

Use `desktop-file-utils` only in `checkdepends` so `check()` can run `desktop-file-validate`; it is not a runtime dependency. Do not add `gtk-update-icon-cache` directly. GTK 4 already supplies the cache updater on Arch, and that package installs an ALPM path hook that runs when icon directories change. The systemd package likewise installs a user-manager reload hook. See the [GTK 4 package dependencies](https://archlinux.org/packages/extra/x86_64/gtk4/), [icon-cache hook contents](https://archlinux.org/packages/extra/x86_64/gtk-update-icon-cache/files/), [systemd hook contents](https://archlinux.org/packages/core/x86_64/systemd/files/), and [ALPM hook contract](https://man.archlinux.org/man/alpm-hooks.5).

## Installed files

Let the wheel install the Python package and both executables. Install the remaining package data with mode `0644`:

| Source | Destination |
| --- | --- |
| `packaging/immich-on-demand.service` | `/usr/lib/systemd/user/immich-on-demand.service` |
| `packaging/net.kalny.ImmichOnDemand.desktop` | `/usr/share/applications/net.kalny.ImmichOnDemand.desktop` |
| `packaging/immich-on-demand-nautilus.py` | `/usr/share/nautilus-python/extensions/immich-on-demand.py` |
| application SVG | `/usr/share/icons/hicolor/scalable/apps/immich-on-demand.svg` |
| four emblem SVGs | `/usr/share/icons/hicolor/scalable/emblems/` |
| `README.md` | `/usr/share/doc/immich-on-demand/README.md` |

These locations match the supported platform contracts: installed user units belong under `/usr/lib/systemd/user` ([Arch systemd/User](https://wiki.archlinux.org/title/Systemd/User#How_it_works)); nautilus-python loads scripts from the `nautilus-python/extensions` subdirectory of system data directories and requires a Nautilus restart ([official loader overview](https://nautilus-python-d06d4b.pages.gitlab.gnome.org/nautilus-python-overview.html)); system desktop entries live in `/usr/share/applications` ([Arch desktop entries](https://wiki.archlinux.org/title/Desktop_entries)); and scalable application icons belong in `hicolor/scalable/apps` ([Icon Theme Specification](https://specifications.freedesktop.org/icon-theme/latest/index.html#install_icons)). The emblem context is the standard context for file-manager property markers ([Icon Naming Specification](https://specifications.freedesktop.org/icon-naming/latest/)).

Do not run `sudo`, `systemctl`, Nautilus, or cache-update commands from `package()`. Package construction must be side-effect free outside `$pkgdir`; user-service restart and Nautilus reload are target acceptance operations.

## Target commands

After the implementation commit is pushed, build and install the remote `main` branch on the Arch target:

```bash
sudo pacman -S --needed base-devel git
cd "$HOME/Projects/immich-on-demand"
git pull --ff-only
cd packaging/development
makepkg -sCfi
```

`-s` installs missing dependencies, `-C` removes the previous `$srcdir`, `-f` permits replacing an earlier local package archive, and `-i` installs the completed package. See [makepkg(8)](https://man.archlinux.org/man/makepkg.8).

Verify the installed artifact before exercising data paths:

```bash
pacman -Q immich-on-demand-git
pacman -Ql immich-on-demand-git
immich-on-demand --version
desktop-file-validate /usr/share/applications/net.kalny.ImmichOnDemand.desktop
systemd-analyze --user verify /usr/lib/systemd/user/immich-on-demand.service
systemctl --user daemon-reload
systemctl --user restart immich-on-demand.service
systemctl --user is-active immich-on-demand.service
nautilus -q
NAUTILUS_PYTHON_DEBUG=misc nautilus
```

Confirm that the printed Arch version ends in the source commit hash, the Python version is `1.4.0.dev0`, every table destination is owned by the package, the service is active, and Nautilus reports no loader traceback. Then perform ticket 05's mount-scope, action, emblem, settings, replacement-key, and daemon/CLI independence checks.

For the uninstall check, stop and disable the user service first, remove the package, and restart Nautilus:

```bash
systemctl --user disable --now immich-on-demand.service
sudo pacman -Rns immich-on-demand-git
nautilus -q
```

Configuration, Secret Service items, catalog, cache, and Pending uploads live under the user's home or Secret Service and are not package-owned. Package replacement or removal must not delete them.

## Limit

This VCS package deliberately follows a moving branch and uses `SKIP`, so it is a development acceptance artifact, not a reproducible release. Its generated version suffix records the fetched revision. Continue to use the tagged archive and fixed BLAKE2 checksum in `packaging/PKGBUILD` for releases. Generate AUR metadata only when publication work begins; it is not required for local target acceptance.
