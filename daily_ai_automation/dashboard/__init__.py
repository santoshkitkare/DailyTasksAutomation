"""Local web dashboard: report browsing, open-action tracking, analytics.

Launched via `daily-automation dashboard`. Localhost only by default - see
app.py's module docstring for why there is no authentication layer.
"""

from .app import create_app

__all__ = ["create_app"]
