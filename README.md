# Immich On-Demand

Immich On-Demand mounts one user's Immich library as a flat Linux directory. Nautilus reads server-generated thumbnails, and applications download original media only when they read file content.

The project currently targets Arch Linux, Nautilus 50, FUSE 3, and Immich 3.0.3. See [PROBLEM_STATEMENT.md](PROBLEM_STATEMENT.md) for the product contract and [.scratch/immich-on-demand-1-0/map.md](.scratch/immich-on-demand-1-0/map.md) for the implementation map.

Development checks run with:

```bash
scripts/check
```
