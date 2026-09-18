"""База правил Мамдані: модель, завантаження `config/rules_mamdani.yaml` і валідація.

Найменування: fuzzy/rules.py
Призначення: правила «ЯКЩО T є A І R є B І V є C ТО U є D» як дані (брифінг §5.6, §7).
    Валідація при завантаженні: рівно |T|·|R|·|V| = 5·3·3 = 45 правил, кожна комбінація
    антецедента рівно раз, без дублікатів, усі терми існують у membership.yaml, у робочому
    режимі (production=True) усі w == 1.0. Кожна помилка — ConfigValidationError з точним
    шляхом до поля (напр. "rules[3].if.T"), який API повертає як 422.
Автор: Андрій Жук, 2026.
"""

from __future__ import annotations

import itertools
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from fuzzhelm.core.errors import ConfigValidationError
from fuzzhelm.fuzzy.membership import (
    ANY_TERM,
    INPUT_VARIABLES,
    MembershipConfig,
    raise_config_error,
    read_yaml_source,
)


@dataclass(frozen=True, slots=True, eq=True)
class Rule:
    """Одне правило. `antecedent` — {"T": терм | "any", "R": ..., "V": ...}; w ∈ (0; 1]."""

    id: str
    antecedent: Mapping[str, str]
    consequent: str
    w: float = 1.0

    def to_dict(self) -> dict[str, Any]:
        return {"id": self.id, "if": dict(self.antecedent), "then": self.consequent, "w": self.w}


@dataclass(frozen=True, slots=True)
class RuleBase:
    """Впорядкована база правил. Конструктор НЕ перевіряє повноту — це робить load_rulebase /
    validate_rulebase (часткові бази потрібні тестам, напр. для терму "any")."""

    rules: tuple[Rule, ...]
    version: int = 3
    meta: Mapping[str, Any] = field(default_factory=dict)

    def __len__(self) -> int:
        return len(self.rules)

    def __iter__(self) -> Iterator[Rule]:
        return iter(self.rules)

    def by_id(self, rule_id: str) -> Rule:
        for r in self.rules:
            if r.id == rule_id:
                return r
        raise KeyError(rule_id)

    def lookup(self, T: str, R: str, V: str) -> Rule:
        """Правило з точним антецедентом (T, R, V) — для повної бази без "any"."""
        for r in self.rules:
            a = r.antecedent
            if a["T"] == T and a["R"] == R and a["V"] == V:
                return r
        raise KeyError((T, R, V))

    def with_weights(self, weights: Mapping[str, float]) -> RuleBase:
        """Копія з іншими w для вказаних id (для тесту-контрприкладу; у роботу — лише w ≡ 1)."""
        unknown = set(weights) - {r.id for r in self.rules}
        if unknown:
            raise KeyError(f"unknown rule ids {sorted(unknown)}")
        for rid, w in weights.items():
            if not 0.0 < w <= 1.0:
                raise ValueError(f"{rid}: w={w} outside (0, 1]")
        return RuleBase(tuple(replace(r, w=float(weights.get(r.id, r.w))) for r in self.rules),
                        self.version, self.meta)

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {"version": self.version}
        out.update(self.meta)
        out["rules"] = [r.to_dict() for r in self.rules]
        return out


# ------------------------------------------------------------------ pydantic-схема (форма YAML)


class _RuleSpec(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True, populate_by_name=True)

    id: str = Field(min_length=1, max_length=32)
    if_: dict[str, str] = Field(alias="if")
    then: str = Field(min_length=1)
    w: float = Field(default=1.0, gt=0.0, le=1.0)


class _RuleBaseSpec(BaseModel):
    model_config = ConfigDict(extra="allow", strict=True, frozen=True)

    version: int = 3
    rules: list[_RuleSpec]


# ------------------------------------------------------------------ семантична валідація


def validate_rulebase(rb: RuleBase, membership: MembershipConfig, *, production: bool = True,
                      require_complete: bool = True) -> None:
    """Перевірити базу проти МФ. Помилка → ConfigValidationError(path="rules[i]...").

    require_complete: рівно |T|·|R|·|V| правил і кожна комбінація термів рівно раз
    (терм "any" розгортається в усі терми змінної, тож у повній базі "any" неможливий).
    """
    seen_ids: dict[str, int] = {}
    covered: dict[tuple[str, str, str], int] = {}
    u_terms = membership.U.term_names
    for i, rule in enumerate(rb.rules):
        if rule.id in seen_ids:
            first = seen_ids[rule.id]
            raise ConfigValidationError(f"duplicate rule id {rule.id!r} (first at rules[{first}])",
                                        path=f"rules[{i}].id")
        seen_ids[rule.id] = i
        extra_vars = sorted(set(rule.antecedent) - set(INPUT_VARIABLES))
        if extra_vars:
            raise ConfigValidationError(f"unknown input variable {extra_vars[0]!r} (expected T, R, V)",
                                        path=f"rules[{i}].if.{extra_vars[0]}")
        expanded: list[tuple[str, ...]] = []
        for var in INPUT_VARIABLES:
            if var not in rule.antecedent:
                raise ConfigValidationError(f"missing variable {var} (use '{ANY_TERM}' for don't-care)",
                                            path=f"rules[{i}].if.{var}")
            term = rule.antecedent[var]
            names = membership[var].term_names
            if term == ANY_TERM:
                expanded.append(names)
            elif term in names:
                expanded.append((term,))
            else:
                raise ConfigValidationError(f"term {term!r} does not exist in variable {var} {list(names)}",
                                            path=f"rules[{i}].if.{var}")
        if rule.consequent not in u_terms:
            raise ConfigValidationError(f"term {rule.consequent!r} does not exist in U {list(u_terms)}",
                                        path=f"rules[{i}].then")
        if not 0.0 < rule.w <= 1.0:
            raise ConfigValidationError(f"w={rule.w} outside (0, 1]", path=f"rules[{i}].w")
        if production and rule.w != 1.0:
            raise ConfigValidationError(f"w={rule.w}: production rule base requires w == 1.0 "
                                        "(w != 1 breaks monotonicity, see test counterexample)",
                                        path=f"rules[{i}].w")
        for combo in itertools.product(*expanded):
            key = (combo[0], combo[1], combo[2])
            if key in covered:
                j = covered[key]
                raise ConfigValidationError(
                    f"duplicate antecedent {dict(zip(INPUT_VARIABLES, key, strict=True))} "
                    f"(already covered by rules[{j}] {rb.rules[j].id})", path=f"rules[{i}].if")
            covered[key] = i
    if require_complete:
        # без дублікатів і рівно N правил ⇒ покриття повне (принцип Діріхле), тож досить рахунку;
        # непокриті комбінації додаються до повідомлення як підказка для RulesEditor
        full = list(itertools.product(*(membership[v].term_names for v in INPUT_VARIABLES)))
        if len(rb.rules) != len(full):
            dims = "x".join(str(len(membership[v].term_names)) for v in INPUT_VARIABLES)
            missing = [c for c in full if c not in covered]
            hint = f"; not covered: {', '.join('/'.join(c) for c in missing[:5])}" if missing else ""
            raise ConfigValidationError(
                f"expected exactly {len(full)} rules ({dims}), got {len(rb.rules)}{hint}", path="rules")


def rulebase_from_dict(data: Mapping[str, Any], membership: MembershipConfig, *, production: bool = True,
                       require_complete: bool = True) -> RuleBase:
    try:
        spec = _RuleBaseSpec.model_validate(dict(data))
    except ValidationError as e:
        raise_config_error(e)
    rules = tuple(Rule(id=r.id, antecedent=dict(r.if_), consequent=r.then, w=r.w) for r in spec.rules)
    rb = RuleBase(rules=rules, version=spec.version, meta=dict(spec.model_extra or {}))
    validate_rulebase(rb, membership, production=production, require_complete=require_complete)
    return rb


def load_rulebase(src: Path | str | Mapping[str, Any] | None, membership: MembershipConfig, *,
                  production: bool = True, require_complete: bool = True) -> RuleBase:
    """Завантажити і перевірити базу правил.

    src: None → `config/rules_mamdani.yaml`; Path → файл; Mapping → розібраний YAML;
    str → текст YAML (рядок без переводу рядка, що закінчується на .yaml/.yml, — шлях до файлу).
    production=True відкидає будь-яке w != 1.0; production=False дозволяє w ∈ (0; 1]
    (лише для тесту-контрприкладу немонотонності).
    """
    return rulebase_from_dict(read_yaml_source(src, default="rules_mamdani"), membership,
                              production=production, require_complete=require_complete)


def rule_table(rb: RuleBase, membership: MembershipConfig) -> dict[str, list[list[str]]]:
    """Таблиця наслідків для Додатка А: {V-терм: рядки R (як у конфігурації) × стовпці T}."""
    out: dict[str, list[list[str]]] = {}
    for v in membership.V.term_names:
        out[v] = [[rb.lookup(t, r, v).consequent for t in membership.T.term_names]
                  for r in membership.R.term_names]
    return out


def format_rule_table_md(rb: RuleBase, membership: MembershipConfig,
                         header: Sequence[str] | None = None) -> str:
    """Markdown-таблиці бази правил (по одній на кожен терм V)."""
    lines: list[str] = []
    t_names = membership.T.term_names
    for v, rows in rule_table(rb, membership).items():
        lines.append(f"**V = {v}**\n")
        lines.append("| R \\ T | " + " | ".join(header or t_names) + " |")
        lines.append("|---" * (len(t_names) + 1) + "|")
        for r, row in zip(membership.R.term_names, rows, strict=True):
            ids = [rb.lookup(t, r, v).id for t in t_names]
            cells = [f"{c} ({rid})" for c, rid in zip(row, ids, strict=True)]
            lines.append(f"| {r} | " + " | ".join(cells) + " |")
        lines.append("")
    return "\n".join(lines)
