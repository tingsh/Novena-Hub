"""Stable identity for one physical Gateway ownership claim."""


def gateway_claim_id(gateway) -> str:
    # A release and subsequent claim creates a new Gateway row. Activation
    # retries on the same row retain this identity and its offline buffer.
    return f"{gateway.pk}:{gateway.created_at.isoformat()}"
