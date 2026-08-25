from __future__ import annotations

from .immich import (
    ImmichClient,
    MUTATION_PERMISSIONS,
    READ_PERMISSIONS,
    ServerSession,
    UPLOAD_PERMISSIONS,
)
from .settings import Settings


def api_key_permissions(settings: Settings, purpose: str) -> frozenset[str]:
    if purpose == "read-only":
        return READ_PERMISSIONS
    if purpose == "mutation":
        return MUTATION_PERMISSIONS if settings.remote_delete else UPLOAD_PERMISSIONS
    raise ValueError("API key purpose must be read-only or mutation")


async def validate_api_key(
    settings: Settings, purpose: str, secret: str
) -> ServerSession:
    async with ImmichClient(settings.server_url, secret) as client:
        return await client.validate(api_key_permissions(settings, purpose))
