"""Функції належності (МФ) і лінгвістичні змінні нечіткого ядра.

Найменування: fuzzy/membership.py
Призначення: трикутні, трапецієві й гаусові МФ (скалярні та векторизовані numpy), лінгвістичні
    змінні T, R, V, U і завантаження `config/membership.yaml` з валідацією (брифінг §5.5, §7).
Автор: Андрій Жук, 2026.

Семантика МФ:
  * trap(a,b,c,d): 0 поза [a; d], лінійний підйом на [a; b), плато 1 на [b; c], спад на (c; d].
    Вироджені ребра дозволені: a == b — вертикальний лівий край (μ(a) = 1), c == d — правий.
    tri(a,b,c) ≡ trap(a,b,b,c).
  * gauss(m,σ): μ(x) = exp(−(x − m)² / (2σ²)), σ > 0.
Скалярний і векторний шляхи виконують ті самі арифметичні операції в тому самому порядку, тому
для tri/trap результати побітово однакові (перевіряється тестом).

`LinguisticVariable.fuzzify` обрізає вхід до діапазону змінної: T, R ∈ [−1; 1] і V ∈ [0; 1] за
побудовою (консенсус і перцентильний ранг), а обрізання прибирає вплив похибки округлення на краях.
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, ClassVar, Literal, NoReturn, overload

import numpy as np
import numpy.typing as npt
from pydantic import BaseModel, ConfigDict, Field, ValidationError, ValidationInfo, field_validator

from fuzzhelm.config import load_yaml, parse_yaml_text
from fuzzhelm.core.errors import ConfigValidationError

FloatArray = npt.NDArray[np.float64]
DefuzzScheme = Literal["rect", "trapezoid", "simpson"]
DEFUZZ_SCHEMES: tuple[str, ...] = ("rect", "trapezoid", "simpson")
REQUIRED_VARIABLES: tuple[str, ...] = ("T", "R", "V", "U")
INPUT_VARIABLES: tuple[str, ...] = ("T", "R", "V")
ANY_TERM = "any"                     # «байдуже»: внесок у α дорівнює 1.0


class MembershipFunction:
    """Базовий клас МФ: `mf(x)` приймає float або np.ndarray і повертає той самий вид."""

    __slots__ = ()
    kind: ClassVar[str] = ""

    def scalar(self, x: float) -> float:
        raise NotImplementedError

    def array(self, x: npt.ArrayLike) -> FloatArray:
        raise NotImplementedError

    @overload
    def __call__(self, x: float) -> float: ...

    @overload
    def __call__(self, x: FloatArray) -> FloatArray: ...

    def __call__(self, x: float | FloatArray) -> float | FloatArray:
        if isinstance(x, np.ndarray):
            return self.array(x)
        return self.scalar(float(x))

    @property
    def core(self) -> tuple[float, float]:
        """Інтервал, де μ = 1 (для гаусіани — вироджений [m; m])."""
        raise NotImplementedError

    @property
    def support(self) -> tuple[float, float]:
        """Інтервал, де μ > 0 (для гаусіани — вся вісь)."""
        raise NotImplementedError

    @property
    def knots(self) -> tuple[float, ...] | None:
        """Точки зламу кусково-лінійної МФ; None — МФ не кусково-лінійна (гаусіана)."""
        return None

    def to_dict(self) -> dict[str, Any]:
        raise NotImplementedError


@dataclass(frozen=True, slots=True)
class Trapezoidal(MembershipFunction):
    """trap(a,b,c,d) — див. докстрінг модуля."""

    a: float
    b: float
    c: float
    d: float
    kind: ClassVar[str] = "trap"

    def __post_init__(self) -> None:
        pts = (self.a, self.b, self.c, self.d)
        if not all(math.isfinite(p) for p in pts):
            raise ValueError(f"{self.kind}: non-finite points {pts}")
        if not (self.a <= self.b <= self.c <= self.d) or self.a == self.d:
            raise ValueError(f"{self.kind}: points must satisfy a ≤ b ≤ c ≤ d, a < d; got {pts}")

    def scalar(self, x: float) -> float:
        a, b, c, d = self.a, self.b, self.c, self.d
        if x < a or x > d:
            return 0.0
        if x < b:
            return (x - a) / (b - a)
        if x <= c:
            return 1.0
        return (d - x) / (d - c)

    def array(self, x: npt.ArrayLike) -> FloatArray:
        xs = np.asarray(x, dtype=np.float64)
        a, b, c, d = self.a, self.b, self.c, self.d
        mu = np.zeros_like(xs)
        if b > a:
            m = (xs >= a) & (xs < b)
            mu[m] = (xs[m] - a) / (b - a)
        mu[(xs >= b) & (xs <= c)] = 1.0
        if d > c:
            m = (xs > c) & (xs <= d)
            mu[m] = (d - xs[m]) / (d - c)
        return mu

    @property
    def core(self) -> tuple[float, float]:
        return (self.b, self.c)

    @property
    def support(self) -> tuple[float, float]:
        return (self.a, self.d)

    @property
    def knots(self) -> tuple[float, ...]:
        return (self.a, self.b, self.c, self.d)

    def to_dict(self) -> dict[str, Any]:
        return {"type": "trap", "points": [self.a, self.b, self.c, self.d]}


@dataclass(frozen=True, slots=True)
class Triangular(Trapezoidal):
    """tri(a,b,c) ≡ trap(a,b,b,c); зберігає власний `kind` для серіалізації."""

    kind: ClassVar[str] = "tri"

    def __post_init__(self) -> None:
        Trapezoidal.__post_init__(self)
        if self.b != self.c:
            raise ValueError("tri: expected b == c (construct via tri(a, b, c))")

    def to_dict(self) -> dict[str, Any]:
        return {"type": "tri", "points": [self.a, self.b, self.d]}


@dataclass(frozen=True, slots=True)
class Gaussian(MembershipFunction):
    """gauss(m, σ): μ(x) = exp(−(x − m)² / (2σ²))."""

    m: float
    sigma: float
    kind: ClassVar[str] = "gauss"

    def __post_init__(self) -> None:
        if not (math.isfinite(self.m) and math.isfinite(self.sigma)) or self.sigma <= 0.0:
            raise ValueError(f"gauss: need finite m and sigma > 0; got m={self.m}, sigma={self.sigma}")

    def scalar(self, x: float) -> float:
        z = (x - self.m) / self.sigma
        return math.exp(-0.5 * z * z)

    def array(self, x: npt.ArrayLike) -> FloatArray:
        z = (np.asarray(x, dtype=np.float64) - self.m) / self.sigma
        return np.exp(-0.5 * z * z)

    @property
    def core(self) -> tuple[float, float]:
        return (self.m, self.m)

    @property
    def support(self) -> tuple[float, float]:
        return (-math.inf, math.inf)

    def to_dict(self) -> dict[str, Any]:
        return {"type": "gauss", "m": self.m, "sigma": self.sigma}


def tri(a: float, b: float, c: float) -> Triangular:
    return Triangular(float(a), float(b), float(b), float(c))


def trap(a: float, b: float, c: float, d: float) -> Trapezoidal:
    return Trapezoidal(float(a), float(b), float(c), float(d))


def gauss(m: float, sigma: float) -> Gaussian:
    return Gaussian(float(m), float(sigma))


@dataclass(frozen=True, slots=True)
class LinguisticVariable:
    """Лінгвістична змінна: діапазон і впорядковані терми (порядок — як у конфігурації)."""

    name: str
    range: tuple[float, float]
    terms: Mapping[str, MembershipFunction]
    meta: Mapping[str, Any] = field(default_factory=dict)   # source, provisional, silhouette, ...

    def __post_init__(self) -> None:
        lo, hi = self.range
        if not (math.isfinite(lo) and math.isfinite(hi) and lo < hi):
            raise ValueError(f"{self.name}: invalid range {self.range}")
        if not self.terms:
            raise ValueError(f"{self.name}: no terms")

    @property
    def term_names(self) -> tuple[str, ...]:
        return tuple(self.terms)

    def clip(self, x: float) -> float:
        lo, hi = self.range
        return lo if x < lo else hi if x > hi else x

    def fuzzify(self, x: float) -> dict[str, float]:
        """{терм: μ(x)} у порядку термів; x попередньо обрізається до діапазону. NaN/±inf → ValueError."""
        xf = float(x)
        if not math.isfinite(xf):
            # ±inf не обрізаємо мовчки до краю: нескінченний вхід — ознака помилки вище за потоком
            # (та сама політика, що й у MamdaniEngine._fuzzify_all)
            raise ValueError(f"{self.name}: input {x!r} is not finite")
        xc = self.clip(xf)
        return {name: mf.scalar(xc) for name, mf in self.terms.items()}

    def evaluate(self, xs: npt.ArrayLike) -> FloatArray:
        """Матриця μ форми (n_terms, len(xs)) для вже обрізаних/довільних точок (без обрізання)."""
        arr = np.asarray(xs, dtype=np.float64)
        return np.stack([mf.array(arr) for mf in self.terms.values()])

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {"range": [self.range[0], self.range[1]]}
        out.update(self.meta)
        out["terms"] = {name: mf.to_dict() for name, mf in self.terms.items()}
        return out


@dataclass(frozen=True, slots=True)
class DefuzzConfig:
    scheme: DefuzzScheme = "trapezoid"
    grid_nodes: int = 201


@dataclass(frozen=True, slots=True)
class MembershipConfig:
    """Повна конфігурація МФ: змінні T, R, V (входи), U (вихід) і параметри дефазифікації."""

    variables: Mapping[str, LinguisticVariable]
    defuzz: DefuzzConfig = DefuzzConfig()
    version: int = 3
    meta: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        missing = [v for v in REQUIRED_VARIABLES if v not in self.variables]
        if missing:
            raise ConfigValidationError(f"missing variables {missing}", path="variables")

    def __getitem__(self, name: str) -> LinguisticVariable:
        return self.variables[name]

    @property
    def T(self) -> LinguisticVariable:
        return self.variables["T"]

    @property
    def R(self) -> LinguisticVariable:
        return self.variables["R"]

    @property
    def V(self) -> LinguisticVariable:
        return self.variables["V"]

    @property
    def U(self) -> LinguisticVariable:
        return self.variables["U"]

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {"version": self.version}
        out.update(self.meta)
        out["variables"] = {name: var.to_dict() for name, var in self.variables.items()}
        out["defuzz"] = {"scheme": self.defuzz.scheme, "grid_nodes": self.defuzz.grid_nodes}
        return out


# ------------------------------------------------------------------ pydantic-схема і завантаження


def pydantic_path(loc: tuple[int | str, ...], prefix: str = "") -> str:
    """('rules', 3, 'if', 'T') → 'rules[3].if.T' (формат шляху ConfigValidationError)."""
    out = prefix
    for part in loc:
        if isinstance(part, int):
            out += f"[{part}]"
        else:
            out += f".{part}" if out else str(part)
    return out or "$"


def raise_config_error(err: ValidationError, prefix: str = "") -> NoReturn:
    """Перша помилка pydantic → ConfigValidationError з точним шляхом до поля."""
    first = err.errors()[0]
    raise ConfigValidationError(first["msg"], path=pydantic_path(tuple(first["loc"]), prefix)) from err


class _TermSpec(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)

    type: Literal["tri", "trap", "gauss"]
    points: list[float] | None = Field(default=None, validate_default=True)
    m: float | None = Field(default=None, validate_default=True)
    sigma: float | None = Field(default=None, validate_default=True)

    @field_validator("points")
    @classmethod
    def _points(cls, v: list[float] | None, info: ValidationInfo) -> list[float] | None:
        kind = info.data.get("type")
        if kind == "gauss":
            if v is not None:
                raise ValueError("gauss term takes m and sigma, not points")
            return v
        need = 3 if kind == "tri" else 4
        if v is None or len(v) != need:
            raise ValueError(f"{kind} needs exactly {need} points")
        if not all(math.isfinite(x) for x in v):
            raise ValueError("points must be finite")
        pts = [v[0], v[1], v[1], v[2]] if kind == "tri" else list(v)
        if not (pts[0] <= pts[1] <= pts[2] <= pts[3]) or pts[0] == pts[3]:
            raise ValueError("points must be non-decreasing with non-empty support")
        return v

    @field_validator("m", "sigma")
    @classmethod
    def _gauss_params(cls, v: float | None, info: ValidationInfo) -> float | None:
        kind = info.data.get("type")
        if kind != "gauss":
            if v is not None:
                raise ValueError(f"{kind} term takes points, not {info.field_name}")
            return v
        if v is None or not math.isfinite(v):
            raise ValueError(f"gauss needs a finite {info.field_name}")
        if info.field_name == "sigma" and v <= 0.0:
            raise ValueError("sigma must be > 0")
        return v

    def build(self) -> MembershipFunction:
        if self.type == "gauss":
            assert self.m is not None and self.sigma is not None
            return gauss(self.m, self.sigma)
        assert self.points is not None
        return tri(*self.points) if self.type == "tri" else trap(*self.points)


class _VariableSpec(BaseModel):
    model_config = ConfigDict(extra="allow", strict=True, frozen=True)

    range: list[float] = Field(min_length=2, max_length=2)
    terms: dict[str, _TermSpec] = Field(min_length=1)
    source: str | None = None                 # percentile | kmeans | expert
    source_run_id: str | None = None
    provisional: bool = False                 # тимчасові значення до прогону калібрування
    silhouette: float | None = None

    @field_validator("range")
    @classmethod
    def _range(cls, v: list[float]) -> list[float]:
        if not (math.isfinite(v[0]) and math.isfinite(v[1]) and v[0] < v[1]):
            raise ValueError("range must be finite [lo, hi] with lo < hi")
        return v

    @field_validator("terms")
    @classmethod
    def _term_names(cls, v: dict[str, _TermSpec]) -> dict[str, _TermSpec]:
        for name in v:
            if not name or name == ANY_TERM:
                raise ValueError(f"invalid term name {name!r}")
        return v


class _DefuzzSpec(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)

    scheme: Literal["rect", "trapezoid", "simpson"] = "trapezoid"
    grid_nodes: int = Field(default=201, ge=3)

    @field_validator("grid_nodes")
    @classmethod
    def _simpson_even(cls, v: int, info: ValidationInfo) -> int:
        if info.data.get("scheme") == "simpson" and (v - 1) % 2:
            raise ValueError("simpson needs an even number of intervals (odd grid_nodes)")
        return v


class _MembershipSpec(BaseModel):
    model_config = ConfigDict(extra="allow", strict=True, frozen=True)

    version: int = 3
    variables: dict[str, _VariableSpec]
    defuzz: _DefuzzSpec = _DefuzzSpec()

    @field_validator("variables")
    @classmethod
    def _required(cls, v: dict[str, _VariableSpec]) -> dict[str, _VariableSpec]:
        missing = [name for name in REQUIRED_VARIABLES if name not in v]
        if missing:
            raise ValueError(f"missing variables {missing}")
        return v


def membership_from_dict(data: Mapping[str, Any]) -> MembershipConfig:
    """Побудувати MembershipConfig зі словника (розібраного YAML). Помилки → ConfigValidationError."""
    try:
        spec = _MembershipSpec.model_validate(dict(data))
    except ValidationError as e:
        raise_config_error(e)
    variables: dict[str, LinguisticVariable] = {}
    for name, vs in spec.variables.items():
        meta: dict[str, Any] = {"source": vs.source, "source_run_id": vs.source_run_id,
                                "provisional": vs.provisional, "silhouette": vs.silhouette}
        meta = {k: v for k, v in meta.items() if k in vs.model_fields_set}
        meta.update(vs.model_extra or {})
        terms = {t: ts.build() for t, ts in vs.terms.items()}
        rng = (vs.range[0], vs.range[1])
        variables[name] = LinguisticVariable(name=name, range=rng, terms=terms, meta=meta)
    defuzz = DefuzzConfig(scheme=spec.defuzz.scheme, grid_nodes=spec.defuzz.grid_nodes)
    return MembershipConfig(variables=variables, defuzz=defuzz, version=spec.version,
                            meta=dict(spec.model_extra or {}))


def load_membership(src: Path | str | Mapping[str, Any] | None = None) -> MembershipConfig:
    """Завантажити МФ.

    src: None → `config/membership.yaml`; Path → файл; Mapping → уже розібраний YAML;
    str → текст YAML (рядок без переводу рядка, що закінчується на .yaml/.yml, — шлях до файлу).
    """
    return membership_from_dict(read_yaml_source(src, default="membership"))


def read_yaml_source(src: Path | str | Mapping[str, Any] | None, *, default: str) -> Mapping[str, Any]:
    """Спільний розбір джерела конфігурації для load_membership / load_rulebase."""
    if src is None:
        return load_yaml(default)
    if isinstance(src, Path):
        return load_yaml(src.name, src.parent)
    if isinstance(src, str):
        if "\n" not in src and src.endswith((".yaml", ".yml")):
            p = Path(src)
            return load_yaml(p.name, p.parent)
        return parse_yaml_text(src, name=default)
    if isinstance(src, Mapping):
        return src
    raise TypeError(f"unsupported config source {type(src).__name__}")
