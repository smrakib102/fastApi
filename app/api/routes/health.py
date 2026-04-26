from fastapi import APIRouter

from app.core.config import settings

router = APIRouter()


@router.get("/health")
def health_check():
    """Liveness + v4 governance visibility.

    Always returns ``{"status": "ok"}`` for backwards-compatibility. Adds
    build identity and a flag/kernel snapshot so operators can verify which
    version + governance mode is live without shelling into the container.
    Snapshot lookup is best-effort: any error degrades to an empty dict so
    health checks never fail because of a flag-store outage.
    """
    payload: dict = {
        "status": "ok",
        "environment": settings.environment,
        "build": {
            "sha": settings.build_sha,
            "tag": settings.build_tag,
        },
    }

    try:
        from app.services.feature_flags import snapshot

        payload.update(snapshot())
    except Exception:
        payload["kernels"] = {}
        payload["flags"] = {}

    return payload
