# Performances par marché — bot-btts

Rapport généré le **2026-11-28** à partir de `data/predictions.csv`.
Les lignes `prono` correspondent à `published=1`; les lignes `ticket` à `published=2`. Les marchés retirés sont exclus.

## Lecture rapide

- Plus faible taux de réussite : **win_home (ticket)**, 33.3% sur 3 lignes.
- Plus mauvais ROI mesurable : **win_away (ticket)**, -100.0% sur 1 cotes réelles.
- Attention : un ROI n’est calculé que lorsqu’une cote réelle (`odds_taken` ou `odds_market`) est disponible.

## Détail

| Groupe | Marché | Réglés | Gagnés | Réussite | Proba annoncée | Cotes réelles | ROI |
|---|---|---:|---:|---:|---:|---:|---:|
| published | over15 | 413 | 323 | 78.2% | 80.2% | 0 | n/a |
| published | home | 344 | 294 | 85.5% | 80.5% | 0 | n/a |
| published | away | 262 | 206 | 78.6% | 79.4% | 0 | n/a |
| published | over25 | 93 | 62 | 66.7% | 64.5% | 53 | -4.8% |
| ticket | over25 | 17 | 7 | 41.2% | 56.0% | 7 | -19.7% |
| ticket | home | 7 | 5 | 71.4% | 67.9% | 0 | n/a |
| ticket | away | 6 | 3 | 50.0% | 69.8% | 0 | n/a |
| ticket | btts | 5 | 2 | 40.0% | 58.3% | 0 | n/a |
| ticket | win_away | 4 | 3 | 75.0% | 53.1% | 1 | -100.0% |
| ticket | win_home | 3 | 1 | 33.3% | 57.2% | 1 | -100.0% |
| ticket | over15 | 1 | 1 | 100.0% | 69.0% | 0 | n/a |

## Interprétation

Le volume de cotes réelles est encore faible sur plusieurs marchés. Il faut éviter de conclure à la rentabilité à partir du seul taux de réussite ; le seuil de rentabilité dépend de la cote effectivement disponible.
