# Abstract

**FuzzHelm — an exchange data aggregation service with a fuzzy-logic decision core and a hierarchical
automatic risk and limit control subsystem**

Andrii Zhuk. Project-technological practice, Department of Automated Control Systems,
Institute of Computer Science and Information Technologies, Lviv Polytechnic National University, 2026.
Programme F3 "Computer Science (Computational Intelligence of Smart Systems)".

---

**Problem.** The share of automated systems in exchange trading keeps growing, and with it the cost of an
opaque decision. When a system is built on gradient neural models, the question "why exactly this position
size, right now" can only be answered approximately and after the fact. In margin trading, where an error
leads to forced liquidation rather than to a merely smaller profit, this opacity is unacceptable: the
decision must be reproducible and explainable, and the risk bound must be provable rather than statistical.

**Object of research** — the process of automated trading decision-making under incomplete, noisy and
untimely market data. **Subject of research** — methods and tools of fuzzy inference and automatic risk and
limit control within an exchange data aggregation service.

**Aim.** To design, implement and experimentally study a service in which the trading decision is made by a
Mamdani fuzzy core, while the position size is bounded by a hierarchical risk control subsystem with a
provable non-increasing-exposure property.

**Methods.** Fuzzy set theory and Mamdani inference; k-means clustering for data-driven calibration of
membership functions; Saaty's analytic hierarchy process for weighting data-quality components; control
theory (hysteresis, a dwell-time state machine, gain scheduling); numerical integration; statistical
evaluation (probabilistic and deflated Sharpe ratios, Kupiec test); property-based verification.

**Results.** A working service was built with a full "data → decision → risk control → accounting" cycle:
165 Python modules (34 242 lines), a three-screen Vue web panel (3 963 lines), 986 tests with 89.38 % line
coverage (98.7–100 % on the fuzzy, risk, decision and sizing packages). The rule space was reduced from
3⁶ = 729 to 45 rules by hierarchical aggregation. Membership functions were calibrated from 21 078 training
observations, with the number of volatility clusters chosen by the silhouette coefficient (k = 3, s = 0.3354).
Defuzzification is treated as a numerical integration problem on a 201-node grid; the trapezoidal scheme was
selected because the aggregated set is piecewise-defined with break points, where Simpson's rule loses its
advantage. Every run carries a reproducibility passport (configuration, dataset, commit, seed and equity
hashes); a rerun on a later commit reproduced the equity curve bit-for-bit.

**Main experimental finding, stated as measured.** Over a 45-day window of 64 800 one-minute bars per symbol,
the signal shows a weak positive gross edge (out-of-sample Sharpe 6.66 and PSR 0.9727 on BTCUSDT without
execution costs), but that edge is destroyed by execution costs. With spread, square-root market impact and
taker fees, **all** 216 grid cells, 24 out-of-sample folds and 20 ablation variants are unprofitable, and
PSR = DSR = 0. No advantage of the method over random trading is claimed. Sensitivity analysis over eight
parameters shows that the two most influential ones both govern trading *frequency*, and a linear voting
baseline loses less precisely because it trades 4.8–5.9 times less often.

**Risk control, by contrast, behaves exactly as designed.** All 216 grid cells reached the HALTED latch at a
drawdown of 0.1200–0.1204 against a 0.12 limit — an overshoot of at most 0.04 percentage points — after which
trading stops until an administrator releases it explicitly, with the reason recorded in the audit log. The
core invariant "the risk chain never increases exposure" is not demonstrated by examples but proved by a
property-based test over generated verdict combinations.

**Findings that contradicted the specification.** Machine checking refuted the assumption of unconditional
monotonicity of the fuzzy output: it holds only for equal rule weights, and a counterexample was constructed
otherwise. Data-driven calibration showed that the "medium volatility" cluster is in fact a group of 6.6 % of
bars with a volume spike. A literal reading of the state machine made the COOLDOWN state absorbing. Each case
is documented among 235 recorded specification-versus-reality deviations.

**Practical value.** A reproducible research stand in which each decision carries a formal derivation
available through the API and the web panel, and the risk bound is an architectural property verified
automatically. The service has no technical path to real funds: the exchange host allowlist is restricted to
testnet and public read-only endpoints, and a mainnet configuration aborts startup.

**Keywords:** fuzzy inference, Mamdani, defuzzification, computational intelligence, risk management,
walk-forward analysis, deflated Sharpe ratio, property-based testing, reproducibility.
