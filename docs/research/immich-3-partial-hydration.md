# Immich 3.0.3 partial hydration

This report is pinned to Immich `v3.0.3`, commit `cd308ad93093735135f99d85ce6980c8e93df231`. [Release](https://github.com/immich-app/immich/releases/tag/v3.0.3)

## Decision

Keep the complete-file cache. Do not add a sparse production cache for 1.4.

Immich 3.0.3 does answer single byte-range requests for originals, but this is an inherited Express behavior rather than a documented contract of the original-download endpoint. The response validator is a weak tag made from file size and modification time. It is not a content digest and a standards-compliant client cannot send it in `If-Range`. Immich exposes one whole-file SHA-1 in asset metadata and no chunk hashes, so a persisted range cannot be authenticated against that checksum until every byte has arrived.

The Reference inventory has only 17 assets of at least 100 MiB. There is no trace yet showing how much of those files local players read before close, or how many complete downloads partial hydration would avoid. Range support is real. The safety and workload evidence needed to justify sparse state is not.

Revisit this decision only when all of these gates pass:

1. A seven-day Reference trace shows at least 10 GiB of avoidable whole-file traffic and leaves at least 75 percent of the affected bytes unread. Ten GiB is the current default maximum cache budget: savings below one cache budget per week do not justify a second cache representation and its recovery rules.
2. The public Immich origin supplies a strong validator bound to the original bytes, or Immich supplies authenticated chunk hashes. A weak stat tag and `Last-Modified` do not pass this gate.
3. The probe below passes through the real proxy before and after an Immich service restart, then again after every proxy or Immich upgrade.
4. A prototype falls back to the existing complete-file download on any `200`, missing validator, encoded response, malformed `Content-Range`, overlap conflict, or capability change. It must never publish a sparse file as a complete cache entry.

These are decision thresholds, not an implementation specification. If the workload gate never passes, the current cache remains the smaller and safer design.

## What serves an original

`GET /assets/{id}/original` checks `asset.download`, resolves the original or edited path, and returns an `ImmichFileResponse`. The controller passes that response to the shared `sendFile` helper. The video playback route uses the same helper. Its controller description explicitly says it supports byte ranges; the original-download description does not. [Original controller](https://github.com/immich-app/immich/blob/v3.0.3/server/src/controllers/asset-media.controller.ts#L89-L108) [playback controller](https://github.com/immich-app/immich/blob/v3.0.3/server/src/controllers/asset-media.controller.ts#L163-L177) [service paths](https://github.com/immich-app/immich/blob/v3.0.3/server/src/services/asset-media.service.ts#L225-L311)

The shared helper sets private cache control, content type, and an inline filename, then calls Express `res.sendFile(path, { dotfiles: 'allow' })`. It does not implement ranges or add a checksum header itself. [Immich file helper](https://github.com/immich-app/immich/blob/v3.0.3/server/src/utils/file.ts#L23-L82)

The v3.0.3 lockfile resolves Express 5.2.1, `send` 1.2.1, `serve-static` 2.2.1, `etag` 1.8.1, and compression 1.8.1. Express `res.sendFile` constructs a `send` stream and copies only the enabled state of the Express `etag` option into it. `serve-static` is installed through Express, but it does not serve the controller's original route. [Immich dependency resolutions](https://github.com/immich-app/immich/blob/v3.0.3/pnpm-lock.yaml#L480-L501) [Express dependency graph](https://github.com/immich-app/immich/blob/v3.0.3/pnpm-lock.yaml#L21154-L21183) [`serve-static` resolution](https://github.com/immich-app/immich/blob/v3.0.3/pnpm-lock.yaml#L25339-L25345) [Express `sendFile`](https://github.com/expressjs/express/blob/v5.2.1/lib/response.js#L378-L420)

Immich sets the Express `etag` option to `strong`, but `res.sendFile` reduces that setting to the Boolean returned by `app.enabled('etag')`. `send` then calls `etag(stat)`. The `etag` package defaults a filesystem stat tag to weak and encodes only hexadecimal size and modification time. The resulting original response tag has the form `W/"<size>-<mtime>"`; it is not the SHA-1 in asset metadata. [Immich Express setup](https://github.com/immich-app/immich/blob/v3.0.3/server/src/app.common.ts#L42-L65) [`send` header generation](https://github.com/pillarjs/send/blob/1.2.1/index.js#L736-L768) [`etag` stat algorithm](https://github.com/jshttp/etag/blob/v1.8.1/index.js#L70-L93) [`etag` size and mtime fields](https://github.com/jshttp/etag/blob/v1.8.1/index.js#L118-L131)

Immich's private cache policy includes `no-transform`. Its compression middleware therefore leaves file responses unencoded. That preserves byte offsets at the server boundary. A custom intermediary can still violate or replace this behavior, which is why the probe requests `Accept-Encoding: identity` and checks the public response. [Immich cache policy](https://github.com/immich-app/immich/blob/v3.0.3/server/src/utils/file.ts#L36-L40) [compression installation](https://github.com/immich-app/immich/blob/v3.0.3/server/src/app.common.ts#L70-L94) [compression `no-transform` handling](https://github.com/expressjs/compression/blob/v1.8.1/index.js#L161-L180)

## Exact range behavior

`send` enables byte ranges by default and adds `Accept-Ranges: bytes`. It parses the request after it has statted the file and set validators. It creates a Node file stream with inclusive `start` and `end` offsets. [Default options](https://github.com/pillarjs/send/blob/1.2.1/index.js#L96-L136) [range and stream selection](https://github.com/pillarjs/send/blob/1.2.1/index.js#L490-L592) [Node range reads](https://nodejs.org/docs/latest-v24.x/api/fs.html#fscreatereadstreampath-options)

| Request at the Immich application | `send` response | Client rule |
| --- | --- | --- |
| No `Range` | `200` with full `Content-Length` | Existing whole-file path |
| One satisfiable `bytes` range | `206`, one `Content-Range`, range `Content-Length` | The only form a prototype should send |
| One unsatisfiable range | `send` raises `416` with `Content-Range: bytes */<size>`, but Immich maps a pre-send `sendFile` error to `404` | Treat either status as a failed range and fall back |
| Malformed range, another range unit, or several non-combinable ranges | `200` full response | Abort the response body and fall back |
| Satisfiable range with a stale `If-Range` | `200` full response | Abort the response body and fall back |

The implementation accepts only one combined range. It does not produce a multipart response for several separated ranges. A stale `If-Range` changes the internal parse result to the same full-response path used for an ignored range. An unsatisfiable interval becomes a `sendFile` error before headers are sent, and Immich replaces such errors with `NotFoundException`; no sparse client should depend on the exact error status. [Range branches](https://github.com/pillarjs/send/blob/1.2.1/index.js#L533-L591) [Immich error mapping](https://github.com/immich-app/immich/blob/v3.0.3/server/src/utils/file.ts#L69-L82)

RFC 9110 requires a client to send a strong entity tag in `If-Range`, and the recipient must use strong comparison. It explicitly forbids a client from generating `If-Range` with a weak tag. `send` nevertheless accepts its own weak stat tag by substring comparison. Depending on that implementation quirk would make persisted ranges fragile across dependency changes. An HTTP date is allowed, but `Last-Modified` is still file metadata rather than proof of content identity. [RFC 9110 `If-Range`](https://www.rfc-editor.org/rfc/rfc9110.html#field.if-range) [`send` `If-Range` check](https://github.com/pillarjs/send/blob/1.2.1/index.js#L337-L360)

`Accept-Ranges: bytes` advertises support, while each `206` must still be checked against its requested interval and known total size. A capability flag alone cannot make a sparse cache safe. [RFC 9110 range requests](https://www.rfc-editor.org/rfc/rfc9110.html#name-range-requests) [RFC 9110 `Accept-Ranges`](https://www.rfc-editor.org/rfc/rfc9110.html#field.accept-ranges) [RFC 9110 `Content-Range`](https://www.rfc-editor.org/rfc/rfc9110.html#field.content-range)

## Integrity boundary

The normal asset response calls `checksum` a Base64-encoded SHA-1. Immich stores that checksum with the asset and returns it in the mapper. The original response has no `Digest`, `Content-Digest`, chunk checksum, or checksum-bound ETag. [Asset checksum schema and mapper](https://github.com/immich-app/immich/blob/v3.0.3/server/src/dtos/asset-response.dto.ts#L99-L119) [checksum mapping](https://github.com/immich-app/immich/blob/v3.0.3/server/src/dtos/asset-response.dto.ts#L212-L241) [stored checksum](https://github.com/immich-app/immich/blob/v3.0.3/server/src/schema/tables/asset.table.ts#L84-L96)

Managed uploads store a SHA-1 of the file bytes. External-library assets instead store a SHA-1 of `path:<normalized path>` with the `sha1Path` algorithm, so their advertised checksum is not an original-content digest. The current cache consequently checks byte count for every download and the whole-file SHA-1 only for managed assets before atomic publication. [Managed checksum](https://github.com/immich-app/immich/blob/v3.0.3/server/src/services/asset-media.service.ts#L150-L168) [external path checksum](https://github.com/immich-app/immich/blob/v3.0.3/server/src/services/library.service.ts#L400-L418) [current complete-file checks](../../src/immich_on_demand/content_cache.py)

One range cannot be checked against a whole-file SHA-1. TLS, exact `Content-Range`, and a stable validator detect several failures, but they do not prove the bytes against catalog metadata. Persisting fetched extents would therefore add an extent map, crash recovery, overlap conflict handling, validator state, and a final whole-file hash before the cache could become complete. None of that is justified without the measured workload gate.

## Video access is workload-dependent

Immich's web viewer gives a native HTML `video` element either the original URL or the playback URL. The normal path uses `/assets/{id}/video/playback`; playing the original switches to the original endpoint. The browser, not Immich's Svelte component, chooses the byte intervals. [Viewer URL choice](https://github.com/immich-app/immich/blob/v3.0.3/web/src/lib/components/asset-viewer/VideoNativeViewer.svelte#L77-L89) [native video element](https://github.com/immich-app/immich/blob/v3.0.3/web/src/lib/components/asset-viewer/VideoNativeViewer.svelte#L385-L422) [URL construction](https://github.com/immich-app/immich/blob/v3.0.3/web/src/lib/utils.ts#L235-L245)

The HTML standard leaves the needed media byte range implementation-defined. A user agent may fetch the entire resource, a bounded interval, or an offset through end based on codec, network state, and heuristics. Seeking can create several buffered ranges. The `preload` value is only a hint and may be ignored. [HTML media loading](https://html.spec.whatwg.org/multipage/media.html#loading-the-media-resource) [HTML buffered ranges](https://html.spec.whatwg.org/multipage/media.html#dom-media-buffered)

Mounted desktop players have the same practical variability through filesystem reads. Container layout matters. Metadata can be near the beginning or end, players can probe both, and seeking can jump. Source code cannot predict the user's player, codec, file layout, or close point. Trace FUSE read offsets and bytes returned on the Reference system. Do not use the browser's request pattern as a substitute for that measurement.

## Reverse-proxy boundary

The official Immich guide says every reverse proxy must forward all headers. Its nginx example adds no special range rule. That means Range and `If-Range` should reach Immich in the documented setup, but the application source cannot prove what the deployed TLS proxy, CDN, cache, or security middleware does. [Immich reverse-proxy requirements and nginx example](https://github.com/immich-app/immich/blob/v3.0.3/docs/docs/administration/reverse-proxy.md#L1-L56)

Capability detection must target the configured public HTTPS origin, not container port 2283. A proxy that strips `Range` produces a `200`; one that strips `If-Range` can turn a stale conditional request into `206`; one that transforms content invalidates original offsets. Every such result must select the complete-file path.

## Smallest safe Reference probe

Paste this block into a terminal on the configured Reference machine after installing the current development package and exporting the read-only API key as `IMMICH_KEY`. It selects Profile `default`; set `PROFILE` to choose another Profile. It requires the key's permissions to match `READ_PERMISSIONS` exactly. It lists metadata, chooses the smallest active managed video with a known size of at least two bytes, and probes both its original and playback representations. Each endpoint receives a two-byte range, an overlapping one-byte range, a final-byte range, and a stale `If-Range` request. Including overflow detection and the optional strong-validator check, the probe retains at most nine response bytes per endpoint and never reads an unexpected `200` body.

The output is one compact JSON object. It contains aggregate counts and booleans only. It prints no URL, path, filename, UUID, header value, media byte, exception, or API key. The block has no shell `exit`, so a failed probe does not close the calling terminal.

This is deliberately a throwaway probe pinned to the installed development client. Its use of `client._http` and `client._api_root` is not a proposed production seam. Before making a network request, its local self-test proves that a dishonest one-MiB `206` body claiming two bytes is consumed only through byte three and that weak or multiple entity tags cannot pass the strong-tag parser.

```bash
app="$(readlink -f "${APP:-$(command -v immich-on-demand)}")"
py="${app%/*}/python"
PROFILE="${PROFILE:-default}" "$py" - <<'PY'
from __future__ import annotations

import json
import os
import re
from urllib.parse import urljoin

import trio

from immich_on_demand.immich import ImmichClient, READ_PERMISSIONS
from immich_on_demand.profiles import select_profile
from immich_on_demand.settings import load


STRONG_ETAG = re.compile(r'"[\x21\x23-\x7e\x80-\xff]*"')


def is_strong_etag(value: str | None) -> bool:
    return value is not None and STRONG_ETAG.fullmatch(value) is not None


async def request(
    client: ImmichClient,
    url: str,
    *,
    byte_range: str,
    if_range: str | None = None,
    expected_bytes: int | None = None,
    original: bool = False,
) -> tuple[int, dict[str, str], bytes]:
    headers = {"accept-encoding": "identity", "range": byte_range}
    if if_range is not None:
        headers["if-range"] = if_range
    async with client._http.stream(
        "GET",
        url,
        params={"edited": "false"} if original else None,
        headers=headers,
    ) as response:
        response_headers = {key.lower(): value for key, value in response.headers.items()}
        body = b""
        if (
            expected_bytes is not None
            and response.status_code == 206
            and response_headers.get("content-length") == str(expected_bytes)
        ):
            limit = expected_bytes + 1
            received = bytearray()
            async for chunk in response.aiter_raw(chunk_size=limit):
                received.extend(chunk[: limit - len(received)])
                if len(received) == limit:
                    break
            body = bytes(received)
        return response.status_code, response_headers, body


def exact_content_range(
    value: str | None,
    start: int,
    end: int,
    total: int,
) -> bool:
    return value == f"bytes {start}-{end}/{total}"


async def endpoint_probe(
    client: ImmichClient,
    url: str,
    *,
    expected_total: int | None,
    original: bool,
) -> dict[str, object]:
    first_status, first, first_body = await request(
        client,
        url,
        byte_range="bytes=0-1",
        expected_bytes=2,
        original=original,
    )
    match = re.fullmatch(r"bytes 0-1/([1-9][0-9]*)", first.get("content-range", ""))
    total = int(match.group(1)) if match else 0
    if total < 2:
        return {
            "probe_completed": False,
            "transport_range_mechanics": False,
            "strong_validator_present": False,
            "safe_sparse_prerequisites": False,
        }

    overlap_status, overlap, overlap_body = await request(
        client,
        url,
        byte_range="bytes=1-1",
        expected_bytes=1,
        original=original,
    )
    last = total - 1
    tail_status, tail, tail_body = await request(
        client,
        url,
        byte_range=f"bytes={last}-{last}",
        expected_bytes=1,
        original=original,
    )
    stale_status, stale, _ = await request(
        client,
        url,
        byte_range="bytes=0-0",
        if_range='"immich-on-demand-stale"',
        original=original,
    )

    etag = first.get("etag")
    strong = is_strong_etag(etag)
    conditional_status = 0
    conditional: dict[str, str] = {}
    conditional_body = b""
    if strong:
        conditional_status, conditional, conditional_body = await request(
            client,
            url,
            byte_range="bytes=0-0",
            if_range=etag,
            expected_bytes=1,
            original=original,
        )

    range_headers = (first, overlap, tail)
    validators = {
        (headers.get("etag"), headers.get("last-modified"))
        for headers in range_headers
    }
    checks = {
        "accept_ranges_bytes": all(
            headers.get("accept-ranges") == "bytes" for headers in range_headers
        ),
        "cache_private": "private" in first.get("cache-control", ""),
        "cache_no_transform": "no-transform" in first.get("cache-control", ""),
        "content_encoding_identity": all(
            headers.get("content-encoding") in {None, "identity"}
            for headers in (*range_headers, stale)
        ),
        "digest_header_present": any(
            name in first for name in ("digest", "content-digest", "repr-digest")
        ),
        "etag_is_weak": bool(etag and etag.startswith('W/"')),
        "strong_validator_present": strong,
        "first_range_206": first_status == 206
        and len(first_body) == 2
        and exact_content_range(first.get("content-range"), 0, 1, total),
        "overlap_range_206": overlap_status == 206
        and len(overlap_body) == 1
        and exact_content_range(overlap.get("content-range"), 1, 1, total),
        "overlap_bytes_consistent": len(first_body) == 2
        and overlap_body == first_body[1:2],
        "tail_range_206": tail_status == 206
        and len(tail_body) == 1
        and exact_content_range(tail.get("content-range"), last, last, total),
        "catalog_size_matches": expected_total is None or total == expected_total,
        "stale_if_range_falls_back_200": stale_status == 200
        and stale.get("content-length") == str(total),
        "validators_consistent": etag is not None and len(validators) == 1,
        "strong_if_range_206": strong
        and conditional_status == 206
        and len(conditional_body) == 1
        and exact_content_range(conditional.get("content-range"), 0, 0, total),
    }
    mechanical = all(
        value
        for key, value in checks.items()
        if key
        not in {
            "digest_header_present",
            "etag_is_weak",
            "strong_validator_present",
            "strong_if_range_206",
        }
    )
    return {
        "probe_completed": True,
        "checks": checks,
        "transport_range_mechanics": mechanical,
        "strong_validator_present": strong,
        "safe_sparse_prerequisites": mechanical
        and strong
        and checks["strong_if_range_206"],
    }


async def self_test() -> None:
    class DishonestResponse:
        status_code = 206
        headers = {"content-length": "2"}

        def __init__(self) -> None:
            self.remaining = 1024 * 1024
            self.consumed = 0

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_: object) -> None:
            pass

        async def aiter_raw(self, *, chunk_size: int):
            while self.remaining:
                size = min(chunk_size, self.remaining)
                self.remaining -= size
                self.consumed += size
                yield b"x" * size

    dishonest = DishonestResponse()

    class FakeHttp:
        def stream(self, *_args: object, **_kwargs: object):
            return dishonest

    class FakeClient:
        _http = FakeHttp()

    status, _, body = await request(
        FakeClient(),
        "https://invalid.example",
        byte_range="bytes=0-1",
        expected_bytes=2,
    )
    assert status == 206 and body == b"xxx"
    assert dishonest.consumed == 3
    assert is_strong_etag('"one"')
    assert not is_strong_etag('W/"one"')
    assert not is_strong_etag('"one", "two"')


async def main() -> None:
    stage = "self_test"
    try:
        await self_test()
        stage = "settings"
        profile = select_profile(os.environ["PROFILE"])
        settings = load(profile.config / "config.json")
        stage = "environment"
        key = os.environ["IMMICH_KEY"]
        stage = "key_validation"
        async with ImmichClient(settings.server_url, key) as client:
            session = await client.validate(READ_PERMISSIONS, exact_permissions=True)
            stage = "inventory"
            candidate = None
            eligible = 0
            async for page in client.asset_pages(session.owner_id, page_limit=10_000):
                for asset in page:
                    if (
                        asset.visible
                        and asset.library_id is None
                        and asset.size is not None
                        and asset.size >= 2
                        and asset.mime_type.lower().startswith("video/")
                    ):
                        eligible += 1
                        if candidate is None or asset.size < candidate.size:
                            candidate = asset
            if candidate is None or candidate.size is None:
                raise RuntimeError("no candidate")

            stage = "range_requests"
            original = await endpoint_probe(
                client,
                urljoin(client._api_root, f"assets/{candidate.id}/original"),
                expected_total=candidate.size,
                original=True,
            )
            playback = await endpoint_probe(
                client,
                urljoin(client._api_root, f"assets/{candidate.id}/video/playback"),
                expected_total=None,
                original=False,
            )

        report = {
            "probe_version": 2,
            "probe_status": "ok",
            "server_contract": session.version,
            "exact_read_key": True,
            "eligible_video_candidates": eligible,
            "endpoints": {"original": original, "playback": playback},
            "public_transport_passed": all(
                endpoint["transport_range_mechanics"]
                for endpoint in (original, playback)
            ),
            "safe_sparse_prerequisites": all(
                endpoint["safe_sparse_prerequisites"]
                for endpoint in (original, playback)
            ),
        }
    except Exception:
        report = {
            "probe_version": 2,
            "probe_status": "failed",
            "failure_stage": stage,
        }
    print(json.dumps(report, sort_keys=True, separators=(",", ":")))
    if report["probe_status"] != "ok":
        raise SystemExit(1)


trio.run(main)
PY
```

Run it once in the normal public configuration. Run the same block after restarting Immich. Run it again after a proxy or Immich upgrade. `public_transport_passed` must remain true. `etag_is_weak` is expected to be true and `safe_sparse_prerequisites` false on the pinned stack. A future strong tag is checked separately; it no longer makes a mechanical transport check fail. `digest_header_present` remains observational because a generic whole-representation digest would not by itself authenticate each range.

The probe confirms end-to-end mechanics without downloading a complete original or playback file. It cannot establish content integrity for a persisted range and it does not measure mounted-player read coverage. Those are separate gates, and both remain unmet.
