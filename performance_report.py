#!/usr/bin/env python3
"""Rapport ROI des pronostics publiés, sans cote estimée.

Le taux de réussite peut être calculé sans cote. Le ROI n'est calculé que
pour les lignes résolues qui possèdent odds_taken ou odds_market.
"""

from __future__ import annotations

import argparse
import csv
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path


WINS = {"gagne", "gagné", "win", "won"}
LOSSES = {"perdu", "loss", "lost", "lose"}


@dataclass
class Stats:
    settled: int = 0
    wins: int = 0
    real_odds: int = 0
    profit: float = 0.0

    @property
    def hit_rate(self) -> float | None:
        return self.wins / self.settled if self.settled else None

    @property
    def roi(self) -> float | None:
        return self.profit / self.real_odds if self.real_odds else None


def parse_odds(value: str | None) -> float | None:
    try:
        odds = float(str(value or "").replace(",", "."))
    except ValueError:
        return None
    return odds if odds > 1.0 else None


def is_published(value: str | None) -> bool:
    return str(value or "").strip() in {"1", "2"}


def add_row(stats: Stats, row: dict[str, str]) -> None:
    result = str(row.get("result") or "").strip().lower()
    if result not in WINS | LOSSES:
        return
    stats.settled += 1
    won = result in WINS
    stats.wins += int(won)

    # odds_taken est prioritaire : c'est la cote effectivement jouée.
    odds = parse_odds(row.get("odds_taken")) or parse_odds(row.get("odds_market"))
    if odds is not None:
        stats.real_odds += 1
        stats.profit += odds - 1.0 if won else -1.0


def pct(value: float | None) -> str:
    return "n/a" if value is None else f"{value:.1%}"


def load_rows(path: Path) -> list[dict[str, str]]:
    with path.open(newline="", encoding="utf-8") as handle:
        return list(csv.DictReader(handle))


def write_report(path: Path, rows: list[dict[str, str]]) -> None:
    groups: dict[tuple[str, str], Stats] = defaultdict(Stats)
    for row in rows:
        if not is_published(row.get("published")):
            continue
        add_row(groups[("published", row.get("market") or "unknown")], row)

    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle, delimiter=";", lineterminator="\n")
        writer.writerow([
            "type", "market", "settled", "wins", "hit_rate",
            "real_odds_n", "profit_1u", "roi_real", "warning",
        ])
        for (kind, market), stats in sorted(groups.items()):
            warning = ""
            if stats.real_odds < 30:
                warning = "echantillon_cotes_reelles_insuffisant"
            elif stats.roi is not None and stats.roi < 0:
                warning = "ROI_negatif"
            writer.writerow([
                kind,
                market,
                stats.settled,
                stats.wins,
                "" if stats.hit_rate is None else f"{stats.hit_rate:.6f}",
                stats.real_odds,
                f"{stats.profit:.4f}",
                "" if stats.roi is None else f"{stats.roi:.6f}",
                warning,
            ])


def print_report(rows: list[dict[str, str]]) -> None:
    groups: dict[str, Stats] = defaultdict(Stats)
    published = [row for row in rows if is_published(row.get("published"))]
    for row in published:
        add_row(groups[row.get("market") or "unknown"], row)

    print("=== ROI réel par marché (pronostics publiés) ===")
    print("ROI = mise fixe de 1 unité, uniquement quand odds_taken ou odds_market est présent.")
    for market, stats in sorted(groups.items(), key=lambda item: (-item[1].settled, item[0])):
        roi = "n/a" if stats.roi is None else f"{stats.roi:+.1%}"
        print(
            f"{market:12} résolus={stats.settled:4d} "
            f"gagnés={stats.wins:4d} hit={pct(stats.hit_rate):>6} "
            f"cotes_réelles={stats.real_odds:4d} ROI={roi:>7}"
        )
    missing = sum(1 for row in published if not (parse_odds(row.get("odds_taken")) or parse_odds(row.get("odds_market"))))
    print(f"Lignes publiées: {len(published)} | sans cote réelle: {missing}")
    if missing:
        print("AVERTISSEMENT: aucun ROI estimé n'est affiché pour ces lignes.")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", default="data/predictions.csv")
    parser.add_argument("--output", default="data/performance_report.csv")
    args = parser.parse_args()
    rows = load_rows(Path(args.input))
    write_report(Path(args.output), rows)
    print_report(rows)


if __name__ == "__main__":
    main()
