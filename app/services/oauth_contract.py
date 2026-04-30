import json
from pathlib import Path
from typing import Any


_CONTRACT_PATH = Path(__file__).resolve().parents[2] / "contract" / "oauth_contract.json"
_CONTRACT_CACHE: dict[str, Any] | None = None


def _load_contract() -> dict[str, Any]:
    global _CONTRACT_CACHE
    if _CONTRACT_CACHE is None:
        with _CONTRACT_PATH.open("r", encoding="utf-8") as handle:
            _CONTRACT_CACHE = json.load(handle)
    return _CONTRACT_CACHE


def get_oauth_request_id_regex() -> str:
    return _load_contract()["oauth_request_id"]["regex"]


def get_oauth_error_code(code: str) -> str:
    return _load_contract()["oauth_request_id"]["error_codes"][code]


def get_oauth_metric(metric: str) -> str:
    return _load_contract()["metrics"][metric]
