# Compare the minimal implementation stacks

Type: research
Status: resolved
Blocked by:

## Question

Which language and already-available libraries give the smallest maintainable implementation of FUSE 3, concurrent HTTP transfers, SQLite state, Secret Service access, a local control API, Nautilus integration, and Arch packaging on the Reference system?

## Answer

[Minimal implementation stack](../../../docs/research/minimal-implementation-stack.md) recommends one Python package using pyfuse3 with Trio, HTTPX, stdlib SQLite, SecretStorage, newline-delimited JSON over a Unix socket, and a thin nautilus-python adapter.
