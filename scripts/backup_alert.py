"""Send a backup alert to the superadmins (TZ §19). Used by scripts/backup*.sh inside the bot container:
python scripts/backup_alert.py "text"
"""

import asyncio
import sys

from app.bot import create_bot
from app.config import get_settings


async def main(text: str) -> None:
    settings = get_settings()
    bot = create_bot(settings)
    try:
        for chat_id in settings.superadmin_ids:
            try:
                await bot.send_message(chat_id, text, parse_mode=None)
            except Exception:  # noqa: BLE001 - best effort, one admin failing must not stop the rest
                pass
    finally:
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main(" ".join(sys.argv[1:]) or "Backup alert"))
