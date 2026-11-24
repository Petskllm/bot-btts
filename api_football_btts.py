"""Contrôle secondaire API-Football pour les matchs BTTS.

Le contrôle vérifie les correspondances de matchs et la couverture disponible.
Il ne modifie pas les probabilités SportAPI.ai tant que la concordance n'est
pas mesurée sur un historique suffisant.
"""
from __future__ import annotations

import os
import re
import unicodedata
from datetime import date

import requests

BASE_URL = "https://v3.football.api-sports.io"


def _norm(value):
    text = unicodedata.normalize("NFKD", str(value or ""))
    text = "".join(c for c in text if not unicodedata.combining(c))
    return re.sub(r"[^a-z0-9]+", " ", text.lower()).strip()


def _get(path, params, key):
    response = requests.get(
        f"{BASE_URL}/{path}",
        params=params,
        headers={"x-apisports-key": key, "Accept": "application/json"},
        timeout=15,
    )
    response.raise_for_status()
    payload = response.json()
    if payload.get("errors"):
        raise RuntimeError(str(payload["errors"]))
    return payload


def validate_fixtures(fixtures, day: date):
    """Mappe les matchs SportAPI.ai du jour aux fixtures API-Football."""
    key = os.getenv("API_FOOTBALL_KEY", "").strip()
    if not key:
        return {"enabled": False, "matched": 0, "total": len(fixtures), "items": []}
    try:
        payload = _get("fixtures", {"date": day.isoformat()}, key)
    except (requests.RequestException, ValueError, RuntimeError) as exc:
        return {"enabled": True, "error": str(exc), "matched": 0, "total": len(fixtures), "items": []}

    by_pair = {}
    for item in payload.get("response", []):
        teams = item.get("teams", {})
        home = teams.get("home", {}).get("name")
        away = teams.get("away", {}).get("name")
        by_pair[(_norm(home), _norm(away))] = item

    items = []
    for fixture in fixtures:
        key_pair = (_norm(fixture.get("home_team")), _norm(fixture.get("away_team")))
        item = by_pair.get(key_pair)
        entry = {
            "fixture_id": fixture.get("id"),
            "match": f"{fixture.get('home_team')} — {fixture.get('away_team')}",
            "matched": bool(item),
        }
        if item:
            league = item.get("league", {})
            entry.update({
                "api_fixture_id": item.get("fixture", {}).get("id"),
                "api_status": item.get("fixture", {}).get("status", {}).get("short"),
                "api_league": league.get("name"),
                "api_season": league.get("season"),
            })
        items.append(entry)

    return {
        "enabled": True,
        "api_fixtures": len(payload.get("response", [])),
        "matched": sum(item["matched"] for item in items),
        "total": len(items),
        "items": items,
    }


def format_report(report):
    if not report.get("enabled"):
        return "API-Football BTTS : clé absente"
    if report.get("error"):
        return f"API-Football BTTS : erreur non bloquante ({report['error']})"
    return (
        "API-Football BTTS : "
        f"{report.get('matched', 0)}/{report.get('total', 0)} matchs correspondants "
        f"({report.get('api_fixtures', 0)} fixtures du jour)"
    )
