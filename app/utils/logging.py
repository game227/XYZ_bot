import logging
import re

from app.config import Settings

# Telegram bot tokens, bearer-ish secrets and key=value secrets.
_PATTERNS = [
    re.compile(r"\d{6,12}:[A-Za-z0-9_-]{30,}"),
    re.compile(r"(?i)(password|passwd|token|secret|api_key|access_key)(['\"]?\s*[:=]\s*['\"]?)[^\s'\",}]+"),
]


class RedactingFilter(logging.Filter):
    """Masks secrets that slip into log messages (TZ §15.3, §15.18)."""

    def __init__(self, secrets: list[str]) -> None:
        super().__init__()
        self._secrets = [s for s in secrets if s and len(s) >= 6]

    def _redact(self, text: str) -> str:
        for secret in self._secrets:
            text = text.replace(secret, "***")
        text = _PATTERNS[0].sub("***", text)
        return _PATTERNS[1].sub(lambda m: f"{m.group(1)}{m.group(2)}***", text)

    def filter(self, record: logging.LogRecord) -> bool:
        message = record.getMessage()
        redacted = self._redact(message)
        if redacted != message:
            record.msg = redacted
            record.args = None
        return True


def setup_logging(settings: Settings) -> None:
    handler = logging.StreamHandler()
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    handler.addFilter(RedactingFilter([settings.bot_token.get_secret_value()]))
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(settings.log_level.upper())
    logging.getLogger("aiogram.event").setLevel(logging.WARNING)
