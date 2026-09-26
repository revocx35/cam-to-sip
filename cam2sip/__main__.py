"""Entry point: python -m cam2sip"""

import uvicorn

from .settings import Settings
from .web.app import create_app


def main() -> None:
    settings = Settings()
    app = create_app(settings)
    uvicorn.run(app, host=settings.web_host, port=settings.web_port, log_config=None,
                proxy_headers=True, forwarded_allow_ips="*", access_log=False)


if __name__ == "__main__":
    main()
