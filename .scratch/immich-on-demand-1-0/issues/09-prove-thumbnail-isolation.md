# Prove thumbnail isolation in Nautilus 50

Type: prototype
Status: resolved
Blocked by: 01, 02, 04, 06

## Question

Which minimal integration makes Nautilus 50 show Immich Previews for representative JPEG, PNG, GIF, and common video assets while request logs prove that browsing performs zero original downloads?

## Answer

Populate the global FreeDesktop cache before mounting. Install a URI-, mtime-, and size-keyed GNOME failure PNG for every visible entry first, so an unsupported or failed Preview cannot fall through to a thumbnailer that opens the original. For JPEG, PNG, GIF, MP4, MOV, and M4V, fetch only the bounded Immich Preview, validate and resize it, atomically install a standard PNG, then remove the failure entry. The service and Preview tests prove this ordering and keep original Hydration on the FUSE read path. Live Nautilus observation remains part of Reference-system release acceptance.
