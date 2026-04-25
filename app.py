"""SofaScore Basket Analiz Paneli - Flask uygulaması."""

from __future__ import annotations

import logging
import os
from typing import Any, Optional

from dotenv import load_dotenv
from flask import Flask, jsonify, render_template, request

import analyzer
import sofascore_client as sofa

load_dotenv()

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)

app = Flask(__name__)


# ---------------------------------------------------------------------------
# Canlı maç listesi için sade projeksiyon
# ---------------------------------------------------------------------------

def _shape_event(event: dict) -> dict:
    home = (event.get("homeTeam") or {})
    away = (event.get("awayTeam") or {})
    home_score = event.get("homeScore") or {}
    away_score = event.get("awayScore") or {}
    tournament = event.get("tournament") or {}
    status = event.get("status") or {}

    period_scores: list[dict] = []
    for i in range(1, 8):
        hkey = f"period{i}"
        if hkey in home_score or hkey in away_score:
            period_scores.append({
                "period": i,
                "home": home_score.get(hkey),
                "away": away_score.get(hkey),
            })

    return {
        "id": event.get("id"),
        "custom_id": event.get("customId"),
        "status_type": status.get("type"),
        "status_description": status.get("description"),
        "home": {
            "id": home.get("id"),
            "name": home.get("name"),
            "short": home.get("shortName"),
        },
        "away": {
            "id": away.get("id"),
            "name": away.get("name"),
            "short": away.get("shortName"),
        },
        "home_score": home_score.get("current"),
        "away_score": away_score.get("current"),
        "period_scores": period_scores,
        "tournament": tournament.get("name"),
        "category": ((tournament.get("category") or {}).get("name")),
        "start_timestamp": event.get("startTimestamp"),
    }


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@app.route("/")
def index() -> str:
    return render_template("index.html")


@app.route("/api/live-events")
def api_live_events():
    payload = sofa.get_live_events()
    if not payload:
        return jsonify({"events": [], "error": "Canlı maç verisi alınamadı."}), 200

    events = payload.get("events") or []
    # Sadece inprogress olanları tut
    live = [
        _shape_event(e)
        for e in events
        if (e.get("status") or {}).get("type") == "inprogress"
    ]

    # İlgi çekici sırayla: ligin adı + skor toplamı yüksekten başlayarak
    def sort_key(e: dict) -> tuple:
        total = (e.get("home_score") or 0) + (e.get("away_score") or 0)
        return (-total, e.get("tournament") or "", e.get("id") or 0)

    live.sort(key=sort_key)

    return jsonify({
        "events": live,
        "count": len(live),
    })


@app.route("/api/event/<int:event_id>/analysis")
def api_event_analysis(event_id: int):
    raw_line = request.args.get("line", "").strip()
    live_line: Optional[float] = None
    if raw_line:
        try:
            live_line = float(raw_line.replace(",", "."))
        except ValueError:
            live_line = None

    event_payload = sofa.get_event(event_id)
    event = (event_payload or {}).get("event") if event_payload else None
    if not event:
        return jsonify({
            "error": "Maç detayı alınamadı.",
            "event": None,
        }), 200

    stats_payload = sofa.get_statistics(event_id)
    incidents_payload = sofa.get_incidents(event_id)
    lineups_payload = sofa.get_lineups(event_id)

    analysis = analyzer.analyze(
        event=event,
        statistics_payload=stats_payload,
        incidents_payload=incidents_payload,
        lineups_payload=lineups_payload,
        live_line=live_line,
    )

    return jsonify({
        "event": _shape_event(event),
        "analysis": analysis,
        "has_statistics": bool(stats_payload),
        "has_incidents": bool(incidents_payload),
        "has_lineups": bool(lineups_payload),
        "live_line": live_line,
    })


@app.errorhandler(404)
def _not_found(_err: Any):
    return jsonify({"error": "Bulunamadı."}), 404


@app.errorhandler(500)
def _server_error(_err: Any):
    return jsonify({"error": "Sunucu hatası."}), 500


if __name__ == "__main__":
    host = os.getenv("HOST", "127.0.0.1")
    port = int(os.getenv("PORT", "5050"))
    debug = os.getenv("FLASK_DEBUG", "1") == "1"
    app.run(host=host, port=port, debug=debug)
