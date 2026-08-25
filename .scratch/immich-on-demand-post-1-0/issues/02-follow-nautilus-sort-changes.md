# Follow Nautilus sort changes without restart

Type: feature
Status: resolved
Target: 1.0.x
Blocked by: none

## Scope

Reorder only pending Preview jobs when Nautilus changes the folder's saved sort. Keep installed Previews and active requests untouched. Use the saved folder metadata because Nautilus exposes no supported viewport queue.

## Acceptance

- Change the Reference system from modified-date descending to another supported sort without restarting the service.
- The next not-yet-started Preview requests follow the new order.
- Browsing still performs zero original downloads.

## Answer

Poll the saved folder sort at most once per second between bounded Preview request batches. When the metadata changes, reorder only pending jobs. Keep active requests and installed Previews unchanged. The regression changes from modified-date ascending to descending after the first request and proves that the remaining requests reverse without restarting the service.
