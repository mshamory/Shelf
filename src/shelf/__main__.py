import logging

import uvicorn

from .config import settings


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    uvicorn.run("shelf.main:app", host=settings.host, port=settings.port,
                # X-Forwarded-* is applied by the app, from SHELF_TRUSTED_PROXIES (see auth.py).
                proxy_headers=False)


if __name__ == "__main__":
    main()
