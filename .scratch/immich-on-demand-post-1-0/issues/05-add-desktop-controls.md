# Add Nautilus actions and a settings GUI

Type: feature
Status: implemented
Target: 1.1
Blocked by: none
Target acceptance: pending

## Scope

Extend the private control protocol only for operations the clients need. Add a globally loaded Nautilus provider that returns actions and emblems only within the configured mount. Add a GUI over the existing settings and Secret Service code. Keep credentials and remote policy out of the control protocol.

## Acceptance

- The loaded Nautilus provider returns no actions or emblems outside the configured Immich mount.
- The GUI configures one Profile, stores keys in Secret Service, and controls the running service without importing FUSE code.
- Both clients use bounded local requests and display service errors without secrets.
- Removing either client leaves the daemon and CLI functional.

## Answer

The Nautilus 50 adapter caches the configured mount and uses `Gio.File` identity and prefix checks for scoping. Folder menus open Refresh and Settings. A one-file menu exposes Pin, Unpin, retry, and Evict according to cached daemon state. The info provider batches bounded state requests and adds cached, pinned, and busy emblems without importing the service, FUSE, or Immich client.

The GTK 4 and libadwaita application edits one Profile through the existing settings API. It writes replacement keys to Secret Service before it saves non-secret settings. Blank key fields leave stored keys unchanged. Settings work uses one bounded worker. Short-lived action processes use the private Unix control socket and report fixed results to the unique GUI application.

The source tree also contains the desktop entry, Nautilus loader, and application and emblem icons. The released Arch recipe does not install these files. The `immich-on-demand-git` development recipe installs them with GTK 4, libadwaita, and nautilus-python while leaving the released 1.0 recipe unchanged.

## Remaining acceptance

- Build the development Arch package and test install, upgrade, restart, disable, and uninstall on the target system.
- Load the adapter in Nautilus 50 on the target system. Verify that menus and emblems appear only inside the configured mount and update after each action.
- Save settings and replacement keys through the GUI. Restart the service and verify the saved configuration without exposing either key.
- Temporarily disable the loader, launcher, and GUI executable separately, prove that the daemon and CLI still work, then restore package integrity by reinstalling.
