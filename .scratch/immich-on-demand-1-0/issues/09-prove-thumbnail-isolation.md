# Prove thumbnail isolation in Nautilus 50

Type: prototype
Status: resolved
Blocked by: 01, 02, 04, 06

## Question

Which minimal integration makes Nautilus 50 show Immich Previews for representative JPEG, PNG, GIF, and common video assets while request logs prove that browsing performs zero original downloads?

## Answer

Before mounting, install a URI-, mtime-, and size-keyed GNOME failure PNG for every visible entry and reconcile its success records. Keep only a current `large` success PNG for supported media. Remove stale or competing success records from every standard size directory so GLib cannot prefer an obsolete larger thumbnail. Fetch missing JPEG, PNG, GIF, MP4, MOV, and M4V Previews while startup continues. Validate, resize, and atomically install each bounded Immich Preview as a `large` PNG while retaining the failure entry. GLib prefers the successful PNG, and the retained failure entry protects against later cache eviction. The service and Preview tests prove this ordering and keep original Hydration on the FUSE read path. Live Nautilus observation remains part of Reference-system release acceptance.
