#!/usr/bin/env python3
"""Génère un rapport graphique des performances par marché."""

from __future__ import annotations

import csv
import sys
from collections import defaultdict
from datetime import date
from pathlib import Path

import matplotlib.pyplot as plt
from matplotlib.ticker import PercentFormatter

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from performance_report import parse_odds, report_group

INPUT = ROOT / "data" / "predictions.csv"
OUT_DIR = ROOT / "reports"
PNG = OUT_DIR / "performances_par_marche.png"
SUMMARY = OUT_DIR / "performances_par_marche.md"


def load_stats() -> list[dict]:
    groups = defaultdict(lambda: {"settled": 0, "wins": 0, "prob_sum": 0.0, "odds_n": 0, "profit": 0.0})
    with INPUT.open(newline="", encoding="utf-8") as handle:
        for row in csv.DictReader(handle):
            kind = report_group(row)
            if kind is None or row.get("result") not in {"gagne", "perdu", "won", "lost", "win", "lose"}:
                continue
            try:
                probability = float(row.get("probability") or "")
            except ValueError:
                continue
            if not 0 < probability < 1:
                continue
            market = row.get("market") or "unknown"
            key = (kind, market)
            stats = groups[key]
            won = row.get("result") in {"gagne", "won", "win"}
            stats["settled"] += 1
            stats["wins"] += int(won)
            stats["prob_sum"] += probability
            odds = parse_odds(row.get("odds_taken")) or parse_odds(row.get("odds_market"))
            if odds is not None:
                stats["odds_n"] += 1
                stats["profit"] += odds - 1 if won else -1

    rows = []
    for (kind, market), s in groups.items():
        s = dict(s)
        s.update(kind=kind, market=market)
        s["hit"] = s["wins"] / s["settled"]
        s["pred"] = s["prob_sum"] / s["settled"]
        s["roi"] = s["profit"] / s["odds_n"] if s["odds_n"] else None
        rows.append(s)
    return sorted(rows, key=lambda x: (x["kind"], -x["settled"], x["market"]))


def label(row: dict) -> str:
    return f"{row['market']} ({'prono' if row['kind'] == 'published' else 'ticket'})"


def make_chart(rows: list[dict]) -> None:
    labels = [label(r) for r in rows]
    y = list(range(len(rows)))
    fig, axes = plt.subplots(1, 3, figsize=(19, 10), gridspec_kw={"width_ratios": [1.35, 1.0, 1.0]})
    fig.patch.set_facecolor("#f7f8fa")
    for ax in axes:
        ax.set_facecolor("#ffffff")
        ax.grid(axis="x", color="#e5e7eb", linewidth=0.8)
        ax.set_axisbelow(True)
        for spine in ax.spines.values():
            spine.set_visible(False)

    # Taux de réussite versus probabilité annoncée.
    ax = axes[0]
    ax.barh(y, [r["hit"] for r in rows], color=["#2563eb" if r["kind"] == "published" else "#93c5fd" for r in rows], label="Réussite réelle")
    ax.scatter([r["pred"] for r in rows], y, color="#111827", s=32, zorder=3, label="Probabilité annoncée")
    ax.set_yticks(y, labels)
    ax.invert_yaxis()
    ax.set_xlim(0, 1)
    ax.xaxis.set_major_formatter(PercentFormatter(1))
    ax.set_title("Réussite réelle vs probabilité annoncée", fontweight="bold")
    ax.legend(loc="lower right", frameon=False)
    for i, r in enumerate(rows):
        ax.text(min(r["hit"] + 0.015, 0.97), i, f"n={r['settled']}", va="center", fontsize=8, color="#374151")

    # ROI réel.
    ax = axes[1]
    roi_rows = [r for r in rows if r["roi"] is not None]
    roi_values = [r["roi"] if r["roi"] is not None else 0 for r in rows]
    colors = ["#dc2626" if r["roi"] is not None and r["roi"] < 0 else "#16a34a" if r["roi"] is not None else "#d1d5db" for r in rows]
    ax.barh(y, roi_values, color=colors)
    ax.axvline(0, color="#111827", linewidth=1)
    ax.set_yticks(y, labels)
    ax.invert_yaxis()
    ax.xaxis.set_major_formatter(PercentFormatter(1))
    ax.set_title("ROI réel (cotes disponibles)", fontweight="bold")
    max_abs = max([abs(r["roi"]) for r in roi_rows] + [0.05])
    ax.set_xlim(-max_abs * 1.35, max_abs * 1.35)
    for i, r in enumerate(rows):
        text = "n/a" if r["roi"] is None else f"{r['roi']:+.1%} · {r['odds_n']} cotes"
        x = (r["roi"] or 0) + (max_abs * 0.04 if (r["roi"] or 0) >= 0 else -max_abs * 0.04)
        ax.text(x, i, text, va="center", ha="left" if (r["roi"] or 0) >= 0 else "right", fontsize=8)

    # Volume et couverture des cotes.
    ax = axes[2]
    ax.barh(y, [r["settled"] for r in rows], color="#64748b", alpha=0.85, label="Paris réglés")
    ax.barh(y, [r["odds_n"] for r in rows], color="#f59e0b", alpha=0.95, label="Avec cote réelle")
    ax.set_yticks(y, labels)
    ax.invert_yaxis()
    ax.set_title("Volume et couverture des cotes", fontweight="bold")
    ax.legend(loc="lower right", frameon=False)
    for i, r in enumerate(rows):
        coverage = r["odds_n"] / r["settled"] if r["settled"] else 0
        ax.text(r["settled"] + max(1, max(x["settled"] for x in rows) * 0.015), i, f"{coverage:.0%}", va="center", fontsize=8, color="#374151")

    fig.suptitle("bot-btts — performances par marché", fontsize=20, fontweight="bold", x=0.03, ha="left")
    fig.text(0.03, 0.935, "Historique journalisé · marchés actifs et tickets séparés · les ROI sans cote sont non mesurables", fontsize=10, color="#4b5563")
    fig.text(0.03, 0.02, f"Généré le {date.today().isoformat()} · Source : data/predictions.csv", fontsize=9, color="#6b7280")
    plt.tight_layout(rect=[0, 0.04, 1, 0.91])
    fig.savefig(PNG, dpi=180, bbox_inches="tight", facecolor=fig.get_facecolor())
    plt.close(fig)


def make_summary(rows: list[dict]) -> None:
    weakest_hit = min(rows, key=lambda r: r["hit"])
    measured = [r for r in rows if r["roi"] is not None]
    weakest_roi = min(measured, key=lambda r: r["roi"]) if measured else None
    lines = [
        "# Performances par marché — bot-btts",
        "",
        f"Rapport généré le **{date.today().isoformat()}** à partir de `data/predictions.csv`.",
        "Les lignes `prono` correspondent à `published=1`; les lignes `ticket` à `published=2`. Les marchés retirés sont exclus.",
        "",
        "## Lecture rapide",
        "",
        f"- Plus faible taux de réussite : **{label(weakest_hit)}**, {weakest_hit['hit']:.1%} sur {weakest_hit['settled']} lignes.",
    ]
    if weakest_roi:
        lines.append(f"- Plus mauvais ROI mesurable : **{label(weakest_roi)}**, {weakest_roi['roi']:+.1%} sur {weakest_roi['odds_n']} cotes réelles.")
    lines += ["- Attention : un ROI n’est calculé que lorsqu’une cote réelle (`odds_taken` ou `odds_market`) est disponible.", "", "## Détail", "", "| Groupe | Marché | Réglés | Gagnés | Réussite | Proba annoncée | Cotes réelles | ROI |", "|---|---|---:|---:|---:|---:|---:|---:|"]
    for r in rows:
        roi = "n/a" if r["roi"] is None else f"{r['roi']:+.1%}"
        lines.append(f"| {r['kind']} | {r['market']} | {r['settled']} | {r['wins']} | {r['hit']:.1%} | {r['pred']:.1%} | {r['odds_n']} | {roi} |")
    lines += ["", "## Interprétation", "", "Le volume de cotes réelles est encore faible sur plusieurs marchés. Il faut éviter de conclure à la rentabilité à partir du seul taux de réussite ; le seuil de rentabilité dépend de la cote effectivement disponible."]
    SUMMARY.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    rows = load_stats()
    if not rows:
        raise SystemExit("Aucune donnée exploitable dans data/predictions.csv")
    make_chart(rows)
    make_summary(rows)
    print(PNG)
    print(SUMMARY)


if __name__ == "__main__":
    main()
