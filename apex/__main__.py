"""Point d'entrée : python -m apex <service>

Services : ingestor | features | labeler | learner | supervisor | notifier | dashboard
Commandes : migrate | backfill
"""
from __future__ import annotations

import asyncio
import logging
import re
import sys

SERVICES = ["ingestor", "features", "labeler", "learner", "supervisor", "notifier", "dashboard", "trader", "migrate", "backfill"]


class RedactSecrets(logging.Filter):
    """Aucun secret dans les logs : masque api-key / tokens de bot dans les messages."""
    PATTERNS = [re.compile(r"(api[-_]key=)[^&\s\"']+", re.I), re.compile(r"(bot)\d+:[\w-]+"),
                re.compile(r"(sk-ant-)[\w-]+")]

    def filter(self, record: logging.LogRecord) -> bool:
        msg = record.getMessage()
        for p in self.PATTERNS:
            msg = p.sub(r"\1***", msg)
        record.msg, record.args = msg, ()
        return True


def setup_logging() -> None:
    h = logging.StreamHandler(sys.stdout)
    h.addFilter(RedactSecrets())
    logging.basicConfig(level=logging.INFO, handlers=[h], format="%(asctime)s %(levelname)s %(name)s : %(message)s")
    for noisy in ("httpx", "httpcore", "websockets", "anthropic", "asyncio"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


async def _migrate() -> None:
    from .config import secrets
    from .db import DB

    db = await DB.connect(secrets().database_url)
    await db.migrate()


def main() -> None:
    setup_logging()
    if len(sys.argv) < 2 or sys.argv[1] not in SERVICES:
        print(__doc__)
        sys.exit(2)
    name = sys.argv[1]
    if name == "dashboard":
        from .dashboard.app import main as dash
        dash()
        return
    if name == "migrate":
        asyncio.run(_migrate())
        return
    mod = {
        "ingestor": "apex.ingestor.service", "features": "apex.features.service", "labeler": "apex.labeler.service",
        "learner": "apex.learning.service", "supervisor": "apex.supervision.service", "notifier": "apex.reporting.telegram",
        "backfill": "apex.backfill.run", "trader": "apex.trading.service",
    }[name]
    import importlib

    asyncio.run(importlib.import_module(mod).main())


if __name__ == "__main__":
    main()
