# Start from trusted cached state while Immich is offline

Type: feature
Status: open
Target: 1.2
Blocked by: 04

## Scope

Persist the validated server identity, user identity, Immich version, and exact key scopes. If online validation is unreachable, mount a nonempty valid catalog in degraded read-only mode. Retry validation and refresh in the background. Fail closed when trusted state is absent or inconsistent.

## Acceptance

- With Immich unreachable, a previously validated Profile mounts its catalog and reads pinned or cached originals.
- Uncached reads and every remote mutation fail without changing local or remote state.
- Reconnection revalidates identity and scopes before refresh or mutation resumes.
- A new Profile, changed server, changed user, or changed key scope cannot use stale trust data.
