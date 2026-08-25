from __future__ import annotations

import gi

from .settings import load

gi.require_version("Gio", "2.0")
gi.require_version("Nautilus", "4.1")

from gi.repository import Gio, GObject, Nautilus


_DESKTOP_CLIENT = "immich-on-demand-desktop"


def _load_mount():
    try:
        return Gio.File.new_for_path(str(load().mount_path))
    except Exception:
        return None


class NautilusExtension(GObject.GObject, Nautilus.MenuProvider):
    def __init__(self) -> None:
        super().__init__()
        self._mount = _load_mount()

    def _in_mount(self, candidate) -> bool:
        return self._mount is not None and (
            candidate.equal(self._mount) or candidate.has_prefix(self._mount)
        )

    @staticmethod
    def _launch(*arguments: str) -> None:
        Gio.Subprocess.new(
            [_DESKTOP_CLIENT, *arguments],
            Gio.SubprocessFlags.NONE,
        )

    def _activate_refresh(self, _item) -> None:
        self._launch("--action", "refresh")

    def _activate_settings(self, _item) -> None:
        self._launch()

    def _activate_evict(self, _item, uri: str) -> None:
        self._launch("--action", "evict", "--uri", uri)

    @staticmethod
    def _menu_item(name: str, label: str, callback):
        item = Nautilus.MenuItem(name=f"ImmichOnDemand::{name}", label=label)
        item.connect("activate", callback)
        return item

    def get_background_items(self, current_folder) -> list:
        if not self._in_mount(current_folder.get_location()):
            return []
        return [
            self._menu_item("refresh", "Refresh Immich", self._activate_refresh),
            self._menu_item(
                "settings", "Immich On-Demand Settings", self._activate_settings
            ),
        ]

    def get_file_items(self, files) -> list:
        if len(files) != 1:
            return []
        file = files[0]
        if file.is_directory() or not self._in_mount(file.get_location()):
            return []
        item = Nautilus.MenuItem(
            name="ImmichOnDemand::evict",
            label="Evict Local Copy",
        )
        item.connect("activate", self._activate_evict, file.get_uri())
        return [item]
