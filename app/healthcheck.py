"""Docker healthcheck: python -m app.healthcheck bot|worker

Healthy iff the component refreshed its Redis heartbeat recently (see services/monitoring.py).
"""

import asyncio
import sys

from app.services import monitoring

MAX_AGE_SECONDS = 120


async def _check(component: str) -> bool:
    age = await monitoring.last_beat(component)
    return age is not None and age <= MAX_AGE_SECONDS


if __name__ == "__main__":
    component = sys.argv[1] if len(sys.argv) > 1 else "bot"
    sys.exit(0 if asyncio.run(_check(component)) else 1)
