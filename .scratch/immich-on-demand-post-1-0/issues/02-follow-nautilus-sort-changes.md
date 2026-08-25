# Follow Nautilus sort changes without restart

Type: feature
Status: open
Target: 1.0.x
Blocked by: none

## Scope

Reorder only pending Preview jobs when Nautilus changes the folder's saved sort. Keep installed Previews and active requests untouched. Use the saved folder metadata because Nautilus exposes no supported viewport queue.

## Acceptance

- Change the Reference system from modified-date descending to another supported sort without restarting the service.
- The next not-yet-started Preview requests follow the new order.
- Browsing still performs zero original downloads.
