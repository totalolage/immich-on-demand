# Provision read-only live-test access

Type: task
Status: resolved
Blocked by:

## Question

Create least-privilege read access to the Protected library without placing credentials in the repository or issue files. Mutation credentials and Test assets wait until the deletion guard exists.

## Answer

The user stored a dedicated key in Secret Service under `application=immich-on-demand`, `server=photos.nas.kalny.net`, `purpose=read-only`. Live validation confirmed Immich 3.0.3, exactly `user.read`, `asset.read`, `asset.view`, and `asset.download`, and successful user authentication without exposing the secret.
