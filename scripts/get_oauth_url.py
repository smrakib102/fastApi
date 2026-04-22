import os

import httpx
from dotenv import load_dotenv


def main() -> None:
    load_dotenv()
    admin_token = os.environ.get("ADMIN_TOKEN")
    if not admin_token:
        raise SystemExit("ADMIN_TOKEN missing")

    response = httpx.get(
        "http://127.0.0.1:8000/google/oauth/start",
        headers={"X-Admin-Token": admin_token},
        timeout=30,
    )
    response.raise_for_status()
    print(response.json().get("url"))


if __name__ == "__main__":
    main()
