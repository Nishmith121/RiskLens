"""RiskLens Monte Carlo engine.

Simulates many possible years of cyberattacks for one company and reports the
expected yearly loss, the 95th-percentile (worst-case planning) loss and the
chance of any loss. Attack counts are Poisson, attack costs are lognormal.

The engine is deterministic for a given seed, so every result is reproducible.
The AI layer never computes numbers itself; it calls these functions as tools.

Run the Sharma Logistics example:
    python risklens_engine.py
"""
from __future__ import annotations

import itertools
from pathlib import Path
import sys

# Ensure UTF-8 console output for currency symbol (₹) on Windows
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

import numpy as np
import yaml

HERE = Path(__file__).parent


def load_assumptions(path: Path = HERE / "assumptions.yaml") -> dict:
    with open(path, encoding="utf-8") as f:
        return yaml.safe_load(f)


def _adjusted(assumptions: dict, controls: list[str]) -> dict:
    """Apply each active control's multipliers to the scenarios."""
    out = {}
    for name, s in assumptions["scenarios"].items():
        freq, med = s["frequency"], s["severity_median"]
        for c in controls:
            eff = assumptions["controls"][c]["effects"].get(name, {})
            freq *= eff.get("frequency", 1.0)
            med *= eff.get("severity", 1.0)
        out[name] = (freq, med, s["severity_sigma"])
    return out


def simulate(assumptions: dict, controls: list[str] | None = None,
             n_years: int = 100_000, seed: int = 42) -> dict:
    """Run the Monte Carlo simulation. Money values are in ₹ lakh."""
    controls = controls or []
    rng = np.random.default_rng(seed)
    total = np.zeros(n_years)
    by_scenario = {}
    for name, (freq, med, sigma) in _adjusted(assumptions, controls).items():
        counts = rng.poisson(freq, n_years)
        loss = np.zeros(n_years)
        for k in range(1, counts.max() + 1 if counts.size else 1):
            hit = counts >= k
            loss[hit] += rng.lognormal(np.log(med), sigma, hit.sum())
        by_scenario[name] = float(loss.mean())
        total += loss
    return {
        "controls": controls,
        "expected_loss": float(total.mean()),
        "p95_loss": float(np.percentile(total, 95)),
        "chance_of_loss": float((total > 0).mean()),
        "by_scenario": by_scenario,
    }


def rank_fixes(assumptions: dict, current: list[str] | None = None, **kw) -> list[dict]:
    """Rank each missing control by expected loss saved per rupee spent."""
    current = current or []
    base = simulate(assumptions, current, **kw)["expected_loss"]
    rows = []
    for c, spec in assumptions["controls"].items():
        if c in current:
            continue
        saved = base - simulate(assumptions, current + [c], **kw)["expected_loss"]
        rows.append({"control": c, "label": spec["label"], "saved": saved,
                     "cost": spec["annual_cost"], "return": saved / spec["annual_cost"]})
    return sorted(rows, key=lambda r: r["return"], reverse=True)


def best_for_budget(assumptions: dict, budget: float, current: list[str] | None = None,
                    target: str = "expected_loss", **kw) -> dict:
    """Try every affordable combination of controls and return the best one.
    Savings overlap (two fixes can cut the same risk), so combinations are
    re-simulated rather than adding single-fix savings together."""
    current = current or []
    options = [c for c in assumptions["controls"] if c not in current]
    best = None
    for r in range(len(options) + 1):
        for combo in itertools.combinations(options, r):
            cost = sum(assumptions["controls"][c]["annual_cost"] for c in combo)
            if cost > budget:
                continue
            res = simulate(assumptions, current + list(combo), **kw)
            if best is None or res[target] < best["result"][target]:
                best = {"controls": list(combo), "cost": cost, "result": res}
    return best


def cheapest_for_target(assumptions: dict, max_p95: float,
                        current: list[str] | None = None, **kw) -> dict | None:
    """Cheapest combination of controls that brings the 95th-percentile loss
    under max_p95 (₹ lakh). Answers "what's the cheapest way to get my
    worst case under ₹1 crore?"."""
    current = current or []
    options = [c for c in assumptions["controls"] if c not in current]
    best = None
    for r in range(len(options) + 1):
        for combo in itertools.combinations(options, r):
            cost = sum(assumptions["controls"][c]["annual_cost"] for c in combo)
            if best is not None and cost >= best["cost"]:
                continue
            res = simulate(assumptions, current + list(combo), **kw)
            if res["p95_loss"] < max_p95:
                best = {"controls": list(combo), "cost": cost, "result": res}
    return best


def fmt(lakh: float) -> str:
    if lakh >= 100:
        return f"₹{lakh / 100:.1f} crore"
    return f"₹{lakh:.1f} lakh" if lakh < 10 else f"₹{lakh:.0f} lakh"


if __name__ == "__main__":
    a = load_assumptions()
    before = simulate(a)
    after = simulate(a, ["mfa", "offline_backups"])

    print("Sharma Logistics: 150 employees, ₹80 crore revenue, no controls\n")
    print(f"{'':28}{'Before':>14}{'MFA + backups':>16}{'Change':>10}")
    for key, label in [("expected_loss", "Expected yearly loss"),
                       ("p95_loss", "95th-percentile loss")]:
        change = after[key] / before[key] - 1
        print(f"{label:28}{fmt(before[key]):>14}{fmt(after[key]):>16}{change:>10.0%}")
    pts = round((after["chance_of_loss"] - before["chance_of_loss"]) * 100)
    print(f"{'Chance of any loss':28}{before['chance_of_loss']:>14.0%}"
          f"{after['chance_of_loss']:>16.0%}{pts:>7} pts")
    saved = before["expected_loss"] - after["expected_loss"]
    print(f"\nLoss avoided: {fmt(saved)} a year for ₹4.5 lakh a year spent")

    share = before["by_scenario"]["ransomware"] / before["expected_loss"]
    print(f"\nRansomware share of expected loss: {share:.0%}\n")

    print("Fixes ranked by ₹ saved per ₹ spent:")
    for r in rank_fixes(a):
        print(f"  {r['label']:40} saves {fmt(r['saved']):>10}/yr  "
              f"costs {fmt(r['cost']):>11}/yr  return {r['return']:.1f}x")

    b = best_for_budget(a, budget=5.0)
    print(f"\nBest use of a ₹5 lakh budget: {' + '.join(b['controls'])} "
          f"({fmt(b['cost'])}/yr) -> expected loss {fmt(b['result']['expected_loss'])}")

    g = cheapest_for_target(a, max_p95=100)
    print(f"Cheapest way to get the worst case under ₹1 crore: {' + '.join(g['controls'])} "
          f"({fmt(g['cost'])}/yr) -> 95th percentile {fmt(g['result']['p95_loss'])}")
