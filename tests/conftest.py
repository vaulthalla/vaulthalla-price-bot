"""Test-session guards.

``load_settings()`` calls ``load_dotenv()``, which would fill DATABASE_URL/DB_* from a checkout's
``.env`` -- on a deployed host that is the shared service database, and the DB-backed tests run
``db init``/``refresh``/``publish`` against it. Under pytest, settings come only from the real
environment: DB tests run when DATABASE_URL is exported explicitly and skip otherwise.
"""

from __future__ import annotations

import price_bot.config as config

config.load_dotenv = lambda *args, **kwargs: False
