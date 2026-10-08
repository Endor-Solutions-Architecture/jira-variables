"""Start the Jira variables proxy."""

from __future__ import annotations

import logging
import sys

from jira_variables_proxy.config import Config
from jira_variables_proxy.endor import EndorClient
from jira_variables_proxy.errors import ShimError
from jira_variables_proxy.proxy import Proxy
from jira_variables_proxy.server import make_server


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        stream=sys.stderr,
        force=True,
    )
    try:
        config = Config.from_dotenv()
    except ValueError as exc:
        logging.error("%s", exc)
        sys.exit(1)
    client = EndorClient(
        config.endor_api_url,
        config.endor_api_key,
        config.endor_api_secret,
        config.timeout,
    )
    try:
        client.check_credentials()
    except ShimError as exc:
        logging.error("Endor credentials check failed: %s", exc.message)
        sys.exit(1)
    logging.info("Endor credentials accepted")
    server = make_server(config, Proxy(config, client))
    host = config.listen_host or "0.0.0.0"
    logging.info("listening on %s:%s", host, config.listen_port)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        server.shutdown()


if __name__ == "__main__":
    main()
