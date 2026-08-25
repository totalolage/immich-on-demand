from __future__ import annotations

from collections import OrderedDict
from dataclasses import dataclass
import gi
import json
import threading
import time

import trio

from .desktop import run_action
from .settings import load

gi.require_version("Gio", "2.0")
gi.require_version("Nautilus", "4.1")

from gi.repository import Gio, GLib, GObject, Nautilus


_DESKTOP_CLIENT = "immich-on-demand-desktop"
_MAX_BATCH_URIS = 64
_MAX_BATCH_BYTES = 48 * 1024
_CACHE_SECONDS = 2.0
_MAX_CACHE_ITEMS = 256
_EMBLEMS = (
    ("cached", "immich-on-demand-cached"),
    ("pinned", "immich-on-demand-pinned"),
    ("busy", "immich-on-demand-busy"),
    ("recoverable", "immich-on-demand-recoverable"),
)


@dataclass(slots=True)
class _Update:
    provider: object
    handle: object
    closure: object
    file: object
    uri: str
    completed: bool = False


def _load_mount():
    try:
        return Gio.File.new_for_path(str(load().mount_path))
    except Exception:
        return None


def _describe_request_size(uris: list[str]) -> int:
    return len(
        json.dumps(
            {
                "id": (1 << 63) - 1,
                "method": "describe",
                "params": {"uris": uris},
            },
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
    ) + 1


class NautilusExtension(
    GObject.GObject,
    Nautilus.MenuProvider,
    Nautilus.InfoProvider,
):
    def __init__(self) -> None:
        super().__init__()
        self._mount = _load_mount()
        self._pending: list[_Update] = []
        self._inflight: list[_Update] = []
        self._idle_source = None
        # ponytail: one worker preserves request order; add a pool only if UI latency demands it.
        self._worker_active = False
        self._cache: OrderedDict[
            str, tuple[float, dict[str, bool]]
        ] = OrderedDict()

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

    def update_file_info_full(self, provider, handle, closure, file):
        if not self._in_mount(file.get_location()):
            return Nautilus.OperationResult.COMPLETE
        uri = file.get_uri()
        if not isinstance(uri, str):
            return Nautilus.OperationResult.COMPLETE
        cached = self._cache.get(uri)
        if cached is not None:
            expires, state = cached
            if expires > time.monotonic():
                self._cache.move_to_end(uri)
                try:
                    self._apply_state(file, state)
                except Exception:
                    pass
                return Nautilus.OperationResult.COMPLETE
            del self._cache[uri]
        self._pending.append(_Update(provider, handle, closure, file, uri))
        if self._idle_source is None and not self._worker_active:
            self._idle_source = GLib.idle_add(self._start_batch)
        return Nautilus.OperationResult.IN_PROGRESS

    def cancel_update(self, provider, handle) -> None:
        for updates in (self._pending, self._inflight):
            for update in updates:
                if update.provider == provider and update.handle == handle:
                    if updates is self._pending:
                        updates.remove(update)
                    self._complete(update)
                    return

    def _start_batch(self) -> bool:
        self._idle_source = None
        if not self._pending or self._worker_active:
            return False
        uris: list[str] = []
        for update in self._pending:
            if update.uri in uris:
                continue
            candidate = [*uris, update.uri]
            if (
                len(candidate) > _MAX_BATCH_URIS
                or _describe_request_size(candidate) >= _MAX_BATCH_BYTES
            ):
                break
            uris = candidate
        if not uris:
            oversized_uri = self._pending[0].uri
            oversized = [
                update for update in self._pending if update.uri == oversized_uri
            ]
            self._pending = [
                update for update in self._pending if update.uri != oversized_uri
            ]
            for update in oversized:
                self._complete(update)
            if self._pending:
                self._idle_source = GLib.idle_add(self._start_batch)
            return False
        selected = set(uris)
        batch = [update for update in self._pending if update.uri in selected]
        self._pending = [update for update in self._pending if update.uri not in selected]
        self._inflight = batch
        self._worker_active = True
        try:
            threading.Thread(
                target=self._describe,
                args=(batch, uris),
                daemon=True,
            ).start()
        except Exception:
            return self._finish_batch(batch, None)
        return False

    def _describe(self, batch: list[_Update], uris: list[str]) -> None:
        try:
            response = trio.run(run_action, "describe", uris)
        except Exception:
            response = None
        GLib.idle_add(self._finish_batch, batch, response)

    @staticmethod
    def _states(response: object, uris: list[str]) -> dict[str, dict[str, bool]]:
        if not isinstance(response, dict) or set(response) != {"items"}:
            return {}
        items = response["items"]
        if not isinstance(items, list):
            return {}
        expected = {"uri", *(name for name, _emblem in _EMBLEMS)}
        allowed = set(uris)
        result: dict[str, dict[str, bool]] = {}
        for item in items:
            if (
                not isinstance(item, dict)
                or set(item) != expected
                or not isinstance(item["uri"], str)
                or item["uri"] not in allowed
                or item["uri"] in result
                or any(type(item[name]) is not bool for name, _emblem in _EMBLEMS)
            ):
                return {}
            result[item["uri"]] = {
                name: item[name] for name, _emblem in _EMBLEMS
            }
        return result

    @staticmethod
    def _complete(update: _Update) -> None:
        if update.completed:
            return
        update.completed = True
        try:
            Nautilus.info_provider_update_complete_invoke(
                update.closure,
                update.provider,
                update.handle,
                Nautilus.OperationResult.COMPLETE,
            )
        except Exception:
            pass

    @staticmethod
    def _apply_state(file, state: dict[str, bool]) -> None:
        for name, emblem in _EMBLEMS:
            if state[name]:
                file.add_emblem(emblem)

    def _finish_batch(self, batch: list[_Update], response: object) -> bool:
        states = self._states(response, [update.uri for update in batch])
        expires = time.monotonic() + _CACHE_SECONDS
        for uri, state in states.items():
            self._cache[uri] = (expires, state)
            self._cache.move_to_end(uri)
        while len(self._cache) > _MAX_CACHE_ITEMS:
            self._cache.popitem(last=False)
        for update in batch:
            try:
                state = states.get(update.uri)
                if state is not None and not update.completed:
                    self._apply_state(update.file, state)
            except Exception:
                pass
            finally:
                self._complete(update)
        self._inflight = []
        self._worker_active = False
        if self._pending and self._idle_source is None:
            self._idle_source = GLib.idle_add(self._start_batch)
        return False
