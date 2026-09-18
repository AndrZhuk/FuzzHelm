"""Ієрархія винятків FuzzHelm.

Найменування: core/errors.py
Автор: Андрій Жук, 2026.
"""

from __future__ import annotations


class FuzzHelmError(Exception):
    """Базовий виняток домену."""


class NormalizationError(FuzzHelmError):
    """Сире повідомлення біржі не відповідає очікуваній схемі (невідоме поле, неможливе значення)."""

    def __init__(self, message: str, *, field: str | None = None, venue: str | None = None) -> None:
        super().__init__(message)
        self.field = field
        self.venue = venue


class LookaheadError(FuzzHelmError):
    """Спроба прочитати бар/ознаку з майбутнього відносно курсора вікна."""


class MainnetHostRejected(FuzzHelmError):
    """Хост виконання не входить до allowlist testnet-хостів (жорстка заборона mainnet)."""


class JournalIntegrityError(FuzzHelmError):
    """Ланцюг хешів журналу розірвано; `seq` — перший зіпсований запис."""

    def __init__(self, seq: int, message: str = "") -> None:
        super().__init__(message or f"hash chain broken at seq={seq}")
        self.seq = seq


class ConfigValidationError(FuzzHelmError):
    """Невалідна конфігурація (YAML правил, МФ, лімітів). `path` — шлях до поля, напр. `rules[3].if.T`."""

    def __init__(self, message: str, *, path: str = "$") -> None:
        super().__init__(f"{path}: {message}")
        self.path = path
        self.detail = message


class RiskHaltedError(FuzzHelmError):
    """Операцію заборонено: ризик-автомат у засувному стані HALTED."""


class PermissionDeniedError(FuzzHelmError):
    """Дія вимагає вищої ролі (напр. зняття HALTED — лише admin)."""


class OrderRejectedError(FuzzHelmError):
    def __init__(self, code: str, message: str = "") -> None:
        super().__init__(message or code)
        self.code = code
