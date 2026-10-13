"""Authenticated optional CD upload; queued never means installed."""
from __future__ import annotations
import hashlib
import json
import os
from pathlib import Path
from urllib.parse import urlsplit
from urllib.request import Request, build_opener, HTTPRedirectHandler


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def publish_package(zip_path, url, token=None):
    from package_validation import validate_package
    validate_package(zip_path, require_cd_only=True, audio_probe=True)
    endpoint = urlsplit(url)
    if endpoint.scheme != "https" or not endpoint.hostname or endpoint.username or endpoint.password or endpoint.query or endpoint.fragment:
        raise ValueError("publish_url_requires_https_without_credentials")
    secret = token if token is not None else os.getenv("OSAKA_CD_PUBLISH_TOKEN")
    if not secret or len(secret) < 32 or any(c.isspace() for c in secret):
        raise ValueError("publish_token_missing_or_invalid")
    data = Path(zip_path).read_bytes()
    request = Request(url, data=data, method="POST", headers={
        "Content-Type": "application/zip", "Authorization": "Bearer " + secret,
        "X-CD-Package-SHA256": hashlib.sha256(data).hexdigest()})
    try:
        with build_opener(_NoRedirect()).open(request, timeout=120) as response:
            raw = response.read(1024 * 1024 + 1)
            if response.status != 202 or len(raw) > 1024 * 1024:
                raise ValueError("publish_unexpected_response")
        result = json.loads(raw)
        state = result.get("state")
        if not result.get("ok") or state not in {"queued", "unchanged"} or (state == "queued" and not result.get("candidate_id")):
            raise ValueError("publish_not_queued")
        return {"state": state, "installed": False, "published_to_vps": True,
                "candidate_id": str(result["candidate_id"]) if result.get("candidate_id") else None,
                "zip_sha256": hashlib.sha256(data).hexdigest()}
    except Exception as exc:
        raise ValueError("publish_failed_" + type(exc).__name__) from None
