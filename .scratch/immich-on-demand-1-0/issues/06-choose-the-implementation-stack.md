# Choose the implementation stack

Type: grilling
Status: resolved
Blocked by: 03

## Question

Which researched implementation stack should 1.0 adopt, given the Reference system, the required desktop integration, packaging cost, runtime correctness, and Ponytail's preference for the least code?

## Answer

Adopt the recommended single-package Python stack. It uses the platform's existing Python, FUSE, SQLite, Secret Service, GNOME, systemd, and Arch packaging facilities, and introduces no second runtime, ORM, web server, D-Bus interface, plugin system, or GUI toolkit.
