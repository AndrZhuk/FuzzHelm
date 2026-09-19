"""Валідація вхідних даних як межа довіри: безпечний розбір YAML стратегій і точний шлях до помилки.

Найменування: api/validation.py
Призначення: текст правил/МФ від користувача проходить чотири фільтри до того, як потрапить у БД:
(1) розмір (схема запиту), (2) безпечний YAML — лише SafeLoader, без якорів/псевдонімів (захист від
«billion laughs») і лише мапінг на верхньому рівні, (3) семантика — ті самі завантажувачі, що й у
рушія (fuzzy.membership.load_membership, fuzzy.rules.load_rulebase, production=True), (4) межі обсягу
обчислень (вузли сітки дефазифікації, кількість термів). Помилка → StrategyValidationError з полем
запиту і шляхом усередині документа (напр. `rules[3].if.T`) → HTTP 422.
Автор: Андрій Жук, 2026.

Текст завжди розбирається тут у Mapping і лише потім передається завантажувачам: fuzzy.read_yaml_source
трактує однорядковий рядок, що закінчується на `.yaml`, як ШЛЯХ до файлу — для даних із мережі це було б
читанням довільного файлу сервера.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

import yaml

from fuzzhelm.core.errors import ConfigValidationError, FuzzHelmError
from fuzzhelm.fuzzy.membership import MembershipConfig, load_membership
from fuzzhelm.fuzzy.rules import RuleBase, load_rulebase

MAX_DEFUZZ_NODES = 2001  # вузлів сітки дефазифікації (обґрунтування — check_resource_limits)
MAX_TERMS_PER_VARIABLE = 15  # T/R/V і так фіксує база 5×3×3; межа потрібна насамперед для виходу U


class StrategyValidationError(FuzzHelmError):
    """Невалідний текст стратегії: `field` — поле запиту, `path` — шлях у документі."""

    def __init__(
        self,
        field: str,
        path: str,
        message: str,
        *,
        kind: str = "config_validation",
        line: int | None = None,
        column: int | None = None,
    ) -> None:
        super().__init__(f"{field}: {path}: {message}")
        self.field = field
        self.path = path
        self.message = message
        self.kind = kind
        self.line = line
        self.column = column

    def as_item(self) -> dict[str, Any]:
        item: dict[str, Any] = {
            "loc": ["body", self.field],
            "msg": self.message,
            "type": self.kind,
            "path": self.path,
        }
        if self.line is not None:
            item["line"] = self.line
            item["column"] = self.column
        return item


class _NoAliasSafeLoader(yaml.SafeLoader):
    """SafeLoader, що відкидає якорі/псевдоніми: YAML-«бомба» з вкладених псевдонімів не пройде."""

    def compose_node(self, parent: Any, index: Any) -> Any:  # type: ignore[override]
        if self.check_event(yaml.AliasEvent):
            event = self.peek_event()
            raise yaml.composer.ComposerError(None, None, "YAML aliases are not allowed", event.start_mark)
        return super().compose_node(parent, index)


def safe_yaml_mapping(text: str, field: str) -> Mapping[str, Any]:
    try:
        data = yaml.load(text, Loader=_NoAliasSafeLoader)  # підклас SafeLoader: лише базові типи
    except yaml.MarkedYAMLError as e:
        mark = e.problem_mark or e.context_mark
        line = None if mark is None else mark.line + 1
        column = None if mark is None else mark.column + 1
        problem = e.problem or "YAML syntax error"
        raise StrategyValidationError(
            field, "$", problem, kind="yaml_syntax", line=line, column=column
        ) from e
    except yaml.YAMLError as e:
        raise StrategyValidationError(field, "$", "YAML syntax error", kind="yaml_syntax") from e
    if not isinstance(data, Mapping):
        raise StrategyValidationError(field, "$", "top-level YAML must be a mapping")
    return data


@dataclass(frozen=True, slots=True)
class ValidatedStrategy:
    membership: MembershipConfig
    rulebase: RuleBase
    rules: Mapping[str, Any]
    membership_tree: Mapping[str, Any]


def check_resource_limits(membership: MembershipConfig) -> None:
    """Межі обсягу обчислень для стратегії з мережі (STRIDE: DoS).

    Завантажувач fuzzy обмежує grid_nodes лише знизу (≥ 3), а MamdaniEngine тримає матрицю
    μ_k(u_j) розміром (терми U × вузли) і рахує її на кожен бар: `grid_nodes: 1000000000` пройшов би
    валідацію і вичерпав пам'ять на першому /explain чи бектесті. 2001 вузол — найдрібніша сітка
    дослідження збіжності дефазифікації (Δ = 0.001 на [−1; 1], fuzzy.defuzz.DEFAULT_DELTAS).
    """
    nodes = membership.defuzz.grid_nodes
    if nodes > MAX_DEFUZZ_NODES:
        raise StrategyValidationError(
            "membership_yaml", "defuzz.grid_nodes", f"grid_nodes must be <= {MAX_DEFUZZ_NODES}, got {nodes}"
        )
    for name, var in membership.variables.items():
        if len(var.term_names) > MAX_TERMS_PER_VARIABLE:
            raise StrategyValidationError(
                "membership_yaml",
                f"variables.{name}.terms",
                f"at most {MAX_TERMS_PER_VARIABLE} terms per variable, got {len(var.term_names)}",
            )


def validate_strategy_texts(rules_yaml: str, membership_yaml: str) -> ValidatedStrategy:
    """Розібрати і перевірити пару текстів тими самими завантажувачами, що й робочий рушій."""
    m_tree = safe_yaml_mapping(membership_yaml, "membership_yaml")
    r_tree = safe_yaml_mapping(rules_yaml, "rules_yaml")
    try:
        membership = load_membership(m_tree)
    except ConfigValidationError as e:
        raise StrategyValidationError("membership_yaml", e.path, e.detail) from e
    check_resource_limits(membership)
    try:
        rulebase = load_rulebase(r_tree, membership, production=True)
    except ConfigValidationError as e:
        raise StrategyValidationError("rules_yaml", e.path, e.detail) from e
    return ValidatedStrategy(membership=membership, rulebase=rulebase, rules=r_tree, membership_tree=m_tree)
