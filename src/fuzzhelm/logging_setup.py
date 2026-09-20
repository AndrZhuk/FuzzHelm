"""Єдине налаштування журналювання для всіх точок входу сервісу.

Найменування: src/fuzzhelm/logging_setup.py
Призначення: до цього модуля `logging.basicConfig` викликали три точки входу окремо
    (`workers/trading_worker.py`, `workers/ingest_worker.py`, `scheduler/jobs.py`), і формат
    із рівнем доводилося тримати синхронними вручну. Тут вони задані один раз.
Рівень береться зі змінної оточення `FUZZHELM_LOG` (типово INFO); виклик ідемпотентний,
    тож повторний запуск у тому самому процесі (наприклад, у тестах) нічого не ламає.
Автор: Андрій Жук, 2026.
"""

from __future__ import annotations

import logging
import os
import sys
from typing import Final

#: Формат навмисно однаковий для воркерів і планувальника: журнали читають поруч.
LOG_FORMAT: Final = "%(asctime)s %(levelname)s %(name)s: %(message)s"
DATE_FORMAT: Final = "%Y-%m-%d %H:%M:%S"
ENV_LEVEL: Final = "FUZZHELM_LOG"
DEFAULT_LEVEL: Final = "INFO"

#: Бібліотеки, чий DEBUG забиває журнал корисними лише їм подробицями.
NOISY_LOGGERS: Final = ("asyncio", "httpx", "httpcore", "websockets.client", "matplotlib")

#: Стан у словнику, а не в глобальній змінній: так не потрібен `global` у функції.
_STATE: dict[str, bool] = {"configured": False}


def resolve_level(level: str | int | None = None) -> int:
    """Рівень із аргументу, потім з `FUZZHELM_LOG`, потім типовий. Невідому назву зводимо до INFO."""
    raw = level if level is not None else os.environ.get(ENV_LEVEL, DEFAULT_LEVEL)
    if isinstance(raw, int):
        return raw
    resolved = logging.getLevelName(str(raw).strip().upper())
    return resolved if isinstance(resolved, int) else logging.INFO


def setup_logging(level: str | int | None = None, *, force: bool = False) -> int:
    """Налаштувати кореневий журнал один раз на процес; повертає застосований рівень.

    `force=True` перенастроює навіть після першого виклику — потрібно тестам, які
    перевіряють поведінку за різних рівнів.
    """
    resolved = resolve_level(level)
    if _STATE["configured"] and not force:
        return resolved

    logging.basicConfig(level=resolved, format=LOG_FORMAT, datefmt=DATE_FORMAT,
                        stream=sys.stderr, force=True)
    # Шумні бібліотеки тримаємо на щабель вище за наш рівень, але не нижче WARNING.
    noisy_level = max(resolved + 10, logging.WARNING)
    for name in NOISY_LOGGERS:
        logging.getLogger(name).setLevel(noisy_level)

    _STATE["configured"] = True
    return resolved
