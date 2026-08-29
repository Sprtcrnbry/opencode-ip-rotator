def classify_upstream_429(response) -> tuple[str, int | None, object]:
    """Preserve upstream limit semantics; a 429 is not evidence of an IP block."""
    retry_after = response.headers.get("retry-after")
    try:
        retry_seconds = max(0, int(float(retry_after))) if retry_after else None
    except (TypeError, ValueError):
        retry_seconds = None

    try:
        payload = response.json()
    except Exception:
        payload = {"error": {"type": "upstream_rate_limit", "message": response.text}}

    error = payload.get("error", {}) if isinstance(payload, dict) else {}
    # Upstream varies: type vs code vs message text
    raw_type = str(error.get("type") or error.get("code") or "upstream_rate_limit")
    # message fallback for providers that only put quota text in message
    raw_msg = str(error.get("message") or payload.get("message") or "")
    combined = f"{raw_type} {raw_msg}"
    if any(k in raw_type for k in ("FreeUsageLimit", "GoUsageLimit", "BlackUsageLimit")) or any(k in combined for k in ("FreeUsageLimit", "GoUsageLimit", "BlackUsageLimit", "quota exceeded", "billing")):
        category = "quota"
    elif raw_type == "RateLimitError" or "rate limit" in combined.lower():
        category = "rate_limit"
    else:
        category = "upstream_rate_limit"
    return category, retry_seconds, payload
