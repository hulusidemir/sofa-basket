"""Canlı basket maçı analiz motoru.

Profesyonel basket analisti yaklaşımıyla yapılandırıldı:

* Lig bazlı baseline'lar (NBA / WNBA / NCAA / FIBA için tipik ppm + ORtg).
* Possession-bazlı ileri metrikler (TS%, eFG%, TOV%, ORB%, FTr, ORtg).
* Çift-sayım temizliği: FT ve hücum ribaundu zaten possessions/score üzerinden
  tempoya yansıdığı için ayrı puan eklenmez; sadece SÜRDÜRÜLEBİLİRLİK
  modülatörü olarak kullanılır.
* Zaman-duyarlı script: Q4 son 5 dakikada yakın maç → kasıtlı faul kapısı,
  Q4 son 5 dakikada büyük fark → garbage time.
* Adaptif belirsizlik: projeksiyonun standart sapması maç ilerledikçe daralır;
  barem ± eşikleri sigma cinsinden hesaplanır (Q1'de gevşek, Q4'te sıkı).
* Karar z-score modeline dayanır; ÜST/ALT/Pas için tek bir kompozit sinyal.

Veri eksikse sahte sayı üretilmez; ilgili alanlar ``None`` döner ve
``warnings`` listesine eklenir.
"""

from __future__ import annotations

import re
from typing import Any, Optional


# ---------------------------------------------------------------------------
# Lig baseline'ları (toplam pts/min ve takım başına ORtg)
# ---------------------------------------------------------------------------
#
# ppm = iki takımın toplam puanı / dakika, sezonluk ortalama
# ortg = puan / 100 possession (takım başına; iki takım yakın)
#
# Kaynaklar: NBA 2023-25 ortalamaları, EuroLeague/WNBA/NCAA resmi sezon
# özetleri. Sayılar ±%2 içinde varsayım kabul edilebilir.

LEAGUE_BASELINES: dict[str, dict[str, float]] = {
    "NBA":                   {"ppm": 4.65, "ortg": 115.0},
    "WNBA":                  {"ppm": 3.95, "ortg": 102.0},
    "NCAA Erkek":            {"ppm": 3.55, "ortg": 105.0},
    "NCAA Kadın":            {"ppm": 3.45, "ortg": 95.0},
    "Uluslararası / FIBA":   {"ppm": 4.00, "ortg": 108.0},
}


def detect_league_meta(event: dict) -> dict:
    """Lig tipini, maç süresini ve baseline'larını tahmin et."""

    tournament = (event or {}).get("tournament") or {}
    unique_t = tournament.get("uniqueTournament") or {}
    category = tournament.get("category") or {}
    home = (event or {}).get("homeTeam") or {}
    away = (event or {}).get("awayTeam") or {}

    corpus = " ".join(
        str(x)
        for x in [
            tournament.get("name"),
            unique_t.get("name"),
            category.get("name"),
            category.get("slug"),
            home.get("name"),
            away.get("name"),
        ]
        if x
    ).lower()

    is_wnba = "wnba" in corpus
    is_nba = (not is_wnba) and (
        re.search(r"\bnba\b", corpus) is not None
        or corpus.startswith("nba ")
        or " nba " in corpus
    )
    is_ncaa = "ncaa" in corpus or "college basketball" in corpus
    is_ncaa_women = is_ncaa and ("women" in corpus or "kadın" in corpus)

    league_type = "Uluslararası / FIBA"
    total_minutes, period_count, period_length = 40, 4, 10
    time_certainty = "assumed"

    if is_nba:
        league_type = "NBA"
        total_minutes, period_count, period_length = 48, 4, 12
        time_certainty = "high"
    elif is_wnba:
        league_type = "WNBA"
        total_minutes, period_count, period_length = 40, 4, 10
        time_certainty = "high"
    elif is_ncaa and not is_ncaa_women:
        league_type = "NCAA Erkek"
        total_minutes, period_count, period_length = 40, 2, 20
        time_certainty = "medium"
    elif is_ncaa_women:
        league_type = "NCAA Kadın"
        total_minutes, period_count, period_length = 40, 4, 10
        time_certainty = "medium"

    baseline = LEAGUE_BASELINES.get(league_type, LEAGUE_BASELINES["Uluslararası / FIBA"])

    return {
        "total_minutes": total_minutes,
        "period_count": period_count,
        "period_length": period_length,
        "league_type": league_type,
        "time_certainty": time_certainty,
        "baseline_ppm": baseline["ppm"],
        "baseline_ortg": baseline["ortg"],
    }


# ---------------------------------------------------------------------------
# Süre parse
# ---------------------------------------------------------------------------

def _sum_period_seconds(completed: int, regular_count: int, period_sec: int, ot_sec: int) -> int:
    if completed <= 0:
        return 0
    if completed <= regular_count:
        return completed * period_sec
    return regular_count * period_sec + (completed - regular_count) * ot_sec


def parse_time(event: dict, meta: dict) -> Optional[dict]:
    """Geçen / kalan dakika ve mevcut periyot. Okunamıyorsa None.

    SofaScore basket eventlerinde ``time.played`` maç boyunca kümülatif
    geçen saniyedir (periyot içi değil).
    """

    status = (event or {}).get("status") or {}
    if status.get("type") != "inprogress":
        return None

    time_obj = (event or {}).get("time") or {}
    home_score = (event or {}).get("homeScore") or {}
    away_score = (event or {}).get("awayScore") or {}

    played_periods = 0
    for i in range(1, 12):
        if f"period{i}" in home_score or f"period{i}" in away_score:
            played_periods = i
    if played_periods == 0:
        return None

    api_period_len = time_obj.get("periodLength")
    period_sec = int(api_period_len) if api_period_len and api_period_len > 0 else meta["period_length"] * 60
    ot_sec = int(time_obj.get("overtimeLength") or 300)
    total_period_count = time_obj.get("totalPeriodCount") or meta["period_count"]

    played_total = time_obj.get("played")
    try:
        played_total = int(played_total) if played_total is not None else None
    except (TypeError, ValueError):
        played_total = None

    description = (status.get("description") or "").lower()
    between_keywords = ("halftime", "half time", "end of", "after ", "break", "pause")
    is_between = any(k in description for k in between_keywords)

    current_period = played_periods
    in_overtime = current_period > total_period_count
    regulation_total_sec = total_period_count * period_sec

    if played_total is not None and played_total > 0:
        elapsed_sec = played_total
    elif is_between:
        elapsed_sec = _sum_period_seconds(current_period, total_period_count, period_sec, ot_sec)
    else:
        elapsed_sec = _sum_period_seconds(current_period - 1, total_period_count, period_sec, ot_sec)

    if in_overtime:
        played_in_current_ot = elapsed_sec - regulation_total_sec - max(0, current_period - total_period_count - 1) * ot_sec
        played_in_current_ot = max(0, min(ot_sec, played_in_current_ot))
        remaining_sec = ot_sec - played_in_current_ot
        return {
            "elapsed_min": round(elapsed_sec / 60.0, 2),
            "remaining_min": round(remaining_sec / 60.0, 2),
            "total_min": round((elapsed_sec + remaining_sec) / 60.0, 2),
            "current_period": current_period,
            "in_overtime": True,
            "is_between": is_between,
        }

    remaining_sec = max(0, regulation_total_sec - elapsed_sec)
    return {
        "elapsed_min": round(elapsed_sec / 60.0, 2),
        "remaining_min": round(remaining_sec / 60.0, 2),
        "total_min": round(regulation_total_sec / 60.0, 2),
        "current_period": current_period,
        "in_overtime": False,
        "is_between": is_between,
    }


# ---------------------------------------------------------------------------
# İstatistik parse
# ---------------------------------------------------------------------------

def _to_int(value: Any) -> Optional[int]:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return int(value)
    if isinstance(value, str):
        m = re.search(r"-?\d+", value)
        if m:
            try:
                return int(m.group())
            except ValueError:
                return None
    return None


def _split_made_attempted(value: Any) -> tuple[Optional[int], Optional[int]]:
    if not isinstance(value, str):
        return (None, None)
    m = re.search(r"(\d+)\s*/\s*(\d+)", value)
    if not m:
        return (None, None)
    return (int(m.group(1)), int(m.group(2)))


def _parse_time_str(value: Any) -> Optional[int]:
    if not isinstance(value, str):
        return None
    m = re.match(r"\s*(\d+):(\d{1,2})\s*$", value)
    if not m:
        return None
    return int(m.group(1)) * 60 + int(m.group(2))


def parse_statistics(stats_payload: Optional[dict]) -> dict:
    canonical: dict[str, dict[str, Optional[int]]] = {}
    if not stats_payload:
        return canonical

    periods = stats_payload.get("statistics") or []
    all_period = next(
        (p for p in periods if (p.get("period") or "").upper() == "ALL"),
        None,
    ) or (periods[0] if periods else None)
    if not all_period:
        return canonical

    flat: dict[str, dict[str, Any]] = {}
    for group in all_period.get("groups") or []:
        for item in group.get("statisticsItems") or []:
            name = (item.get("name") or "").strip().lower()
            if not name:
                continue
            flat[name] = {"home": item.get("home"), "away": item.get("away")}

    def find(keys: list[str]) -> Optional[dict]:
        for key_substr in keys:
            for name, val in flat.items():
                if key_substr in name:
                    return val
        return None

    def set_int(cname: str, src: Optional[dict]) -> None:
        if not src:
            canonical[cname] = {"home": None, "away": None}
            return
        canonical[cname] = {
            "home": _to_int(src.get("home")),
            "away": _to_int(src.get("away")),
        }

    def set_ma(cname_made: str, cname_att: str, src: Optional[dict]) -> None:
        if not src:
            canonical[cname_made] = {"home": None, "away": None}
            canonical[cname_att] = {"home": None, "away": None}
            return
        hm, ha = _split_made_attempted(src.get("home"))
        am, aa = _split_made_attempted(src.get("away"))
        canonical[cname_made] = {"home": hm, "away": am}
        canonical[cname_att] = {"home": ha, "away": aa}

    set_ma("two_made", "two_att", find(["2-pointers", "2 pointers", "two pointer"]))
    set_ma("three_made", "three_att", find(["3-pointers", "3 pointers", "three pointer"]))
    set_ma("fg_made", "fg_att", find(["field goal"]))
    set_ma("ft_made", "ft_att", find(["free throw"]))

    set_int("rebounds_total", find(["total rebound", "rebounds"]))
    set_int("offensive_rebounds", find(["offensive rebound"]))
    set_int("defensive_rebounds", find(["defensive rebound"]))
    set_int("assists", find(["assist"]))
    set_int("turnovers", find(["turnover"]))
    set_int("steals", find(["steal"]))
    set_int("blocks", find(["block"]))
    set_int("fouls", find(["personal foul", "fouls"]))
    set_int("biggest_lead", find(["biggest lead"]))
    set_int("biggest_run", find(["biggest run"]))

    lead_time = find(["time spent in lead", "time in lead"])
    if lead_time:
        canonical["time_in_lead_sec"] = {
            "home": _parse_time_str(lead_time.get("home")),
            "away": _parse_time_str(lead_time.get("away")),
        }
    else:
        canonical["time_in_lead_sec"] = {"home": None, "away": None}

    return canonical


# ---------------------------------------------------------------------------
# İleri metrikler
# ---------------------------------------------------------------------------

def _sum_hw(stat: Optional[dict]) -> Optional[int]:
    if not stat:
        return None
    h, a = stat.get("home"), stat.get("away")
    if h is None and a is None:
        return None
    return (h or 0) + (a or 0)


def _safe_div(a: Optional[float], b: Optional[float]) -> Optional[float]:
    if a is None or b is None or b == 0:
        return None
    return a / b


def compute_advanced(stats: dict, current_total: Optional[int]) -> dict:
    """Possession-bazlı ileri metrikler. Her biri yoksa None döner."""

    fga = _sum_hw(stats.get("fg_att"))
    fgm = _sum_hw(stats.get("fg_made"))
    fta = _sum_hw(stats.get("ft_att"))
    three_made = _sum_hw(stats.get("three_made"))
    three_att = _sum_hw(stats.get("three_att"))
    turnovers = _sum_hw(stats.get("turnovers"))
    oreb = _sum_hw(stats.get("offensive_rebounds"))
    dreb = _sum_hw(stats.get("defensive_rebounds"))

    out = {
        "possessions": None,
        "ortg": None,
        "efg_pct": None,
        "ts_pct": None,
        "ftr": None,
        "orb_pct": None,
        "tov_pct": None,
        "three_rate": None,
    }

    # Possessions: Hollinger formülü, iki takım toplamı (lig ortalamasında her
    # iki takım benzer possession sayısına sahiptir).
    if fga is not None and fta is not None and turnovers is not None and oreb is not None:
        poss = fga + 0.44 * fta + turnovers - oreb
        if poss > 0:
            out["possessions"] = round(poss, 1)
            if current_total is not None:
                out["ortg"] = round(current_total / poss * 100, 1)
            if turnovers is not None and poss > 0:
                out["tov_pct"] = round(turnovers / poss, 3)

    if fga and fga > 0:
        if fgm is not None:
            out["efg_pct"] = round((fgm + 0.5 * (three_made or 0)) / fga, 3)
        if fta is not None:
            out["ftr"] = round(fta / fga, 2)
        if three_att is not None:
            out["three_rate"] = round(three_att / fga, 2)

    if current_total is not None and fga is not None and (fga + 0.44 * (fta or 0)) > 0:
        out["ts_pct"] = round(current_total / (2 * (fga + 0.44 * (fta or 0))), 3)

    if oreb is not None and dreb is not None and (oreb + dreb) > 0:
        out["orb_pct"] = round(oreb / (oreb + dreb), 3)

    return out


# ---------------------------------------------------------------------------
# Belirsizlik (sigma) modeli
# ---------------------------------------------------------------------------

def _projection_sigma(projected: float, progress: float) -> float:
    """Final toplam projeksiyonunun standart sapması (puan).

    Q1 erken başlangıçta %10-12 (~17 puan), Q4 başında ~%3-4 (~6 puan).
    """
    progress = max(0.0, min(1.0, progress))
    sigma_pct = 0.115 * (1.0 - progress) ** 0.6 + 0.018
    return max(2.5, projected * sigma_pct)


# ---------------------------------------------------------------------------
# Ana analiz
# ---------------------------------------------------------------------------

def analyze(
    event: Optional[dict],
    statistics_payload: Optional[dict] = None,
    incidents_payload: Optional[dict] = None,
    lineups_payload: Optional[dict] = None,
    live_line: Optional[float] = None,
) -> dict:
    """Z-score temelli karar üretir. Her zaman dolu sözlük döndürür."""

    result: dict[str, Any] = {
        "league_type": None,
        "baseline_ppm": None,
        "baseline_ortg": None,
        "total_minutes": None,
        "elapsed_minutes": None,
        "remaining_minutes": None,
        "current_period": None,
        "in_overtime": False,
        "current_total": None,
        "raw_pace": None,
        "pace_index": None,
        "efficiency_index": None,
        "league_expected_total": None,
        "projected_total": None,
        "sigma": None,
        "z_score": None,
        # Etiketler
        "pace_label": None,
        "shot_quality_label": None,
        "foul_pressure_label": None,
        "turnover_label": None,
        "rebound_label": None,
        "script_label": None,
        # İleri metrikler
        "advanced": {},
        # Karar
        "recommendation_side": "Veri yetersiz",
        "confidence_score": 0,
        "confidence_label": "Pas",
        "reasons": [],
        "warnings": [],
        "value_vs_line": None,
        "stats": {},
        "player_notes": [],
    }

    if not event:
        result["warnings"].append("Maç bilgisi alınamadı.")
        return result

    meta = detect_league_meta(event)
    result["league_type"] = meta["league_type"]
    result["total_minutes"] = meta["total_minutes"]
    result["baseline_ppm"] = meta["baseline_ppm"]
    result["baseline_ortg"] = meta["baseline_ortg"]
    if meta["time_certainty"] != "high":
        result["warnings"].append(
            f"Lig kesin tespit edilemedi (varsayım: {meta['league_type']}, {meta['total_minutes']} dk)."
        )

    home_score_obj = (event.get("homeScore") or {})
    away_score_obj = (event.get("awayScore") or {})
    home_pts = home_score_obj.get("current")
    away_pts = away_score_obj.get("current")
    if home_pts is not None and away_pts is not None:
        result["current_total"] = (home_pts or 0) + (away_pts or 0)
        diff = abs((home_pts or 0) - (away_pts or 0))
    else:
        diff = None

    time_info = parse_time(event, meta)
    if not time_info:
        result["warnings"].append("Süre okunamadı; projeksiyon yapılmıyor.")
    else:
        result["elapsed_minutes"] = time_info["elapsed_min"]
        result["remaining_minutes"] = time_info["remaining_min"]
        result["current_period"] = time_info["current_period"]
        result["in_overtime"] = time_info["in_overtime"]

    stats = parse_statistics(statistics_payload)
    result["stats"] = stats

    advanced = compute_advanced(stats, result["current_total"])
    result["advanced"] = advanced

    # ---------------- Tempo & projeksiyon (lig kalibrasyonlu) ----------------
    if (
        time_info
        and result["current_total"] is not None
        and time_info["elapsed_min"] > 0
    ):
        elapsed = time_info["elapsed_min"]
        remaining = time_info["remaining_min"]
        total_min = time_info["total_min"]
        progress = elapsed / total_min if total_min else 0

        raw_pace = result["current_total"] / elapsed
        # Projeksiyon: kalan süreye lig ortalamasına doğru hafif Bayesian shrinkage
        # uygulanır (erken bölümde lig ortalamasına %30, geç bölümde %5 ağırlık).
        shrink = max(0.05, 0.30 * (1.0 - progress) ** 1.5)
        future_pace = raw_pace * (1.0 - shrink) + meta["baseline_ppm"] * shrink
        projected = result["current_total"] + future_pace * remaining

        result["raw_pace"] = round(raw_pace, 2)
        result["projected_total"] = round(projected, 1)
        result["pace_index"] = round(raw_pace / meta["baseline_ppm"], 2)
        result["league_expected_total"] = round(meta["baseline_ppm"] * total_min, 1)
        result["sigma"] = round(_projection_sigma(projected, progress), 1)

        pi = result["pace_index"]
        if pi >= 1.10:
            result["pace_label"] = "Yüksek tempo (lig ort. üstü)"
        elif pi >= 1.03:
            result["pace_label"] = "Normal-üstü tempo"
        elif pi >= 0.93:
            result["pace_label"] = "Normal tempo"
        elif pi >= 0.85:
            result["pace_label"] = "Düşük tempo"
        else:
            result["pace_label"] = "Çok düşük tempo"

    # Verim endeksi
    if advanced.get("ortg") and meta["baseline_ortg"]:
        result["efficiency_index"] = round(advanced["ortg"] / meta["baseline_ortg"], 2)

    # ---------------- Şut kalitesi (TS%) ----------------
    ts = advanced.get("ts_pct")
    efg = advanced.get("efg_pct")
    fga = _sum_hw(stats.get("fg_att"))
    if ts is not None:
        if ts >= 0.620:
            result["shot_quality_label"] = f"TS%% çok yüksek ({ts*100:.1f}) - regresyon riski"
        elif ts >= 0.560:
            result["shot_quality_label"] = f"TS%% yüksek ({ts*100:.1f})"
        elif ts >= 0.500:
            result["shot_quality_label"] = f"TS%% normal ({ts*100:.1f})"
        elif ts >= 0.450:
            result["shot_quality_label"] = f"TS%% düşük ({ts*100:.1f}) - hacim varsa toparlama payı"
        else:
            result["shot_quality_label"] = f"TS%% çok düşük ({ts*100:.1f})"
    elif efg is not None:
        result["shot_quality_label"] = f"eFG%% {efg*100:.1f}"
    else:
        result["shot_quality_label"] = "Hesaplanamıyor"

    # ---------------- Faul / FT (FTr) ----------------
    ftr = advanced.get("ftr")
    if ftr is not None:
        if ftr >= 0.32:
            result["foul_pressure_label"] = f"Faul/FT baskısı yüksek (FTr {ftr:.2f})"
        elif ftr >= 0.22:
            result["foul_pressure_label"] = f"Faul/FT normal (FTr {ftr:.2f})"
        else:
            result["foul_pressure_label"] = f"Faul/FT düşük (FTr {ftr:.2f})"
    else:
        result["foul_pressure_label"] = "Hesaplanamıyor"

    # ---------------- Top kaybı (TOV%) ----------------
    tov_pct = advanced.get("tov_pct")
    if tov_pct is not None:
        if tov_pct >= 0.18:
            result["turnover_label"] = f"TOV%% yüksek ({tov_pct*100:.1f})"
        elif tov_pct >= 0.13:
            result["turnover_label"] = f"TOV%% normal ({tov_pct*100:.1f})"
        else:
            result["turnover_label"] = f"TOV%% düşük ({tov_pct*100:.1f})"
    else:
        result["turnover_label"] = "Hesaplanamıyor"

    # ---------------- Ribaund (ORB%) ----------------
    orb_pct = advanced.get("orb_pct")
    if orb_pct is not None:
        if orb_pct >= 0.30:
            result["rebound_label"] = f"ORB%% yüksek ({orb_pct*100:.1f})"
        elif orb_pct >= 0.20:
            result["rebound_label"] = f"ORB%% normal ({orb_pct*100:.1f})"
        else:
            result["rebound_label"] = f"ORB%% düşük ({orb_pct*100:.1f})"
    else:
        result["rebound_label"] = "Hesaplanamıyor"

    # ---------------- Maç scripti (zaman duyarlı) ----------------
    script_text, script_mod = _script_assessment(time_info, meta, diff)
    result["script_label"] = script_text

    # ---------------- Modülatörler (z-score üzerinde küçük kaymalar) ----------------
    z_modifiers: list[tuple[float, str]] = []

    if ts is not None and fga is not None:
        if ts >= 0.620:
            z_modifiers.append((-0.40, f"TS%% çok yüksek ({ts*100:.1f}) - sıcak şut, regresyon riski"))
        elif ts <= 0.460 and fga >= 30:
            z_modifiers.append((+0.25, f"TS%% düşük ({ts*100:.1f}) ama hacim yüksek - toparlama payı"))

    three_pct_team = _safe_div(_sum_hw(stats.get("three_made")), _sum_hw(stats.get("three_att")))
    three_att = _sum_hw(stats.get("three_att"))
    if three_pct_team is not None and three_att and three_att >= 14:
        if three_pct_team >= 0.50:
            z_modifiers.append((-0.30, f"3P%% çok yüksek ({three_pct_team*100:.1f}) - sıcak şut riski"))
        elif three_pct_team <= 0.22:
            z_modifiers.append((+0.20, f"3P%% çok düşük ({three_pct_team*100:.1f}) - ortalamaya dönüş payı"))

    if orb_pct is not None:
        if orb_pct >= 0.30:
            z_modifiers.append((+0.25, "Hücum ribaundu yüksek - ekstra possession sürdürülebilir"))
        elif orb_pct <= 0.18:
            z_modifiers.append((-0.15, "Savunma ribaundu baskın - tek atışta biten hücumlar"))

    if tov_pct is not None:
        if tov_pct >= 0.18:
            z_modifiers.append((-0.30, f"TOV%% yüksek ({tov_pct*100:.1f}) - hücum verimsiz"))
        elif tov_pct <= 0.11:
            z_modifiers.append((+0.15, "TOV%% düşük - temiz hücumlar"))

    # Pace, ÜST/ALT'ın asıl belirleyicilerinden biri ama zaten projeksiyona
    # gömüldü; burada sadece sürdürülebilirliği yorumluyoruz.
    if result["pace_index"]:
        if result["pace_index"] >= 1.15:
            z_modifiers.append((+0.15, f"Tempo lig ort. {(result['pace_index']-1)*100:.0f}%% üstünde"))
        elif result["pace_index"] <= 0.85:
            z_modifiers.append((-0.15, f"Tempo lig ort. {(1-result['pace_index'])*100:.0f}%% altında"))

    if script_mod:
        z_modifiers.append(script_mod)

    # ---------------- Z-score: anchor bul, sapmayı sigmaya böl ----------------
    # Anchor sadece barem girildiyse aktif. Barem yoksa karar tamamen maçın iç
    # dinamiklerinden (modifier'lardan) gelir — projeksiyonu lig ortalamasına
    # zorla kıyaslamayız çünkü amaç bu maçın kendi gidişatına göre eğilim
    # belirlemektir.
    z_anchor = None
    anchor_label = None
    if live_line is not None and result["projected_total"] is not None:
        z_anchor = live_line
        anchor_label = "barem"

    base_z: Optional[float] = None
    if z_anchor is not None and result["sigma"]:
        base_z = (result["projected_total"] - z_anchor) / result["sigma"]

    final_z = base_z if base_z is not None else 0.0
    for mod_value, _ in z_modifiers:
        final_z += mod_value
    if base_z is None and not z_modifiers:
        final_z = None
    result["z_score"] = round(final_z, 2) if final_z is not None else None

    # ---------------- Canlı barem değer notu (sigma cinsinden) ----------------
    if live_line is not None and result["projected_total"] is not None and result["sigma"]:
        diff_pts = result["projected_total"] - live_line
        z_line = diff_pts / result["sigma"]
        if z_line >= 1.5:
            value_label = "ÜST güçlü değerli"
        elif z_line >= 0.7:
            value_label = "ÜST hafif değerli"
        elif z_line <= -1.5:
            value_label = "ALT güçlü değerli"
        elif z_line <= -0.7:
            value_label = "ALT hafif değerli"
        else:
            value_label = "Net avantaj yok"
        result["value_vs_line"] = {
            "diff": round(diff_pts, 1),
            "z": round(z_line, 2),
            "label": value_label,
        }
    elif live_line is None:
        result["warnings"].append(
            "Barem yok: ÜST/ALT eğilimi maçın iç dinamiklerinden çıkarıldı. "
            "Barem girersen değer analizi (sigma cinsinden ÜST/ALT mesafesi) eklenir."
        )

    if result["in_overtime"] and live_line is not None:
        result["warnings"].append("Maç uzatmaya gitti; baremin uzatmayı kapsayıp kapsamadığı belirsiz.")

    # ---------------- Karar ----------------
    progress = (
        time_info["elapsed_min"] / time_info["total_min"]
        if time_info and time_info.get("total_min")
        else 0.0
    )
    side, confidence, conf_label, top_reasons = _decide(
        final_z=final_z,
        base_z=base_z,
        modifiers=z_modifiers,
        anchor_label=anchor_label,
        time_info=time_info,
        advanced=advanced,
        projected=result["projected_total"],
        progress=progress,
        league_certainty=meta["time_certainty"],
    )
    result["recommendation_side"] = side
    result["confidence_score"] = confidence
    result["confidence_label"] = conf_label
    result["reasons"] = top_reasons

    # ---------------- Oyuncu notları ----------------
    notes = _player_notes(lineups_payload)
    result["player_notes"] = notes
    for n in notes:
        if n.get("type") == "foul_trouble":
            result["warnings"].append(f"Faul problemi: {n['name']} ({n['fouls']} faul).")
        elif n.get("type") == "single_carry":
            result["warnings"].append(
                f"Skor yükü tek oyuncuda: {n['name']} (%{int(n['share']*100)}) - sürdürülebilirlik riski."
            )

    return result


# ---------------------------------------------------------------------------
# Yardımcı: maç scripti
# ---------------------------------------------------------------------------

def _script_assessment(
    time_info: Optional[dict],
    meta: dict,
    diff: Optional[int],
) -> tuple[str, Optional[tuple[float, str]]]:
    if not time_info or diff is None:
        return ("Script okunamadı", None)

    period = time_info["current_period"]
    is_final = period >= meta["period_count"]
    remaining = time_info["remaining_min"]
    last_5 = is_final and remaining <= 5.0
    last_2 = is_final and remaining <= 2.0

    if last_5 and diff <= 6:
        return (
            f"Q4 son {remaining:.1f} dk, fark {diff} - kasıtlı faul kapısı",
            (+0.55 if last_2 else +0.40, "Yakın final periyot: faul oyunu + 3'lükler ÜST'e çekiyor"),
        )
    if last_5 and diff >= 12:
        return (
            f"Q4 son {remaining:.1f} dk, fark {diff} - garbage time",
            (-0.45, "Q4 büyük fark: tempo düşer, takımlar yedek çıkarır"),
        )
    if diff >= 20 and remaining <= 8 and is_final:
        return (
            f"Fark {diff} ve süre az - garbage time olası",
            (-0.35, "Çok büyük fark + az süre: tempo düşme riski"),
        )
    if is_final and diff <= 8:
        return (f"Çekişmeli final periyodu (fark {diff})", (+0.10, "Çekişmeli final periyodu"))
    if diff >= 15:
        return (f"Fark açık ({diff})", (-0.10, "Geniş fark tempoyu düşürür"))
    return (f"Fark {diff}, periyot {period}", None)


# ---------------------------------------------------------------------------
# Yardımcı: karar
# ---------------------------------------------------------------------------

def _decide(
    final_z: Optional[float],
    base_z: Optional[float],
    modifiers: list[tuple[float, str]],
    anchor_label: Optional[str],
    time_info: Optional[dict],
    advanced: dict,
    projected: Optional[float],
    progress: float = 0.0,
    league_certainty: str = "high",
) -> tuple[str, int, str, list[str]]:
    if final_z is None or not time_info or projected is None:
        return ("Veri yetersiz", 0, "Pas", ["Yeterli istatistik veya süre verisi yok."])

    # Veri kalitesi: anchor + advanced metrikler oranı
    quality = 0.0
    if base_z is not None:
        quality += 0.45
    if advanced.get("ts_pct") is not None:
        quality += 0.20
    if advanced.get("possessions") is not None:
        quality += 0.20
    if advanced.get("orb_pct") is not None:
        quality += 0.15
    quality = min(1.0, quality)

    # Karar yönü — barem varsa daha yüksek eşik (ÜST/ALT destekli),
    # yoksa iç dinamiklerin net yönü için daha düşük eşik (eğilim).
    if base_z is not None:
        over_thr, under_thr, mixed_thr = 0.7, -0.7, 0.3
    else:
        over_thr, under_thr, mixed_thr = 0.40, -0.40, 0.20

    if final_z >= over_thr:
        side = "ÜST eğilimli"
    elif final_z <= under_thr:
        side = "ALT eğilimli"
    elif abs(final_z) < mixed_thr:
        side = "Karışık / izle"
    else:
        side = "Pas"

    # İstatistik vs barem çelişkisi (sadece anchor=barem ise)
    contradicts = False
    if base_z is not None and modifiers:
        modifier_sum = sum(m[0] for m in modifiers)
        if (base_z > 0.5 and modifier_sum < -0.6) or (base_z < -0.5 and modifier_sum > 0.6):
            contradicts = True

    # Confidence: data quality + |final_z| sinyali
    base_conf = 25 + quality * 35
    signal = min(50, abs(final_z) * 28)
    confidence = int(max(0, min(100, base_conf + signal)))
    if contradicts:
        confidence = max(15, confidence - 25)
    # Lig varsayımsa baseline güvenilmez -> güveni düşür
    if league_certainty != "high":
        confidence = max(0, confidence - 12)
    # Erken bölümde örneklem küçük; sigma zaten genişledi ama yine de tavanla
    if progress < 0.10:
        confidence = min(confidence, 50)
    elif progress < 0.25:
        confidence = min(confidence, 70)
    elif progress < 0.40:
        confidence = min(confidence, 85)

    # Barem yoksa karar tamamen iç dinamiklerden geliyor; "Güçlü" tier'ı zorlaştır.
    if base_z is None:
        confidence = min(confidence, 78)

    if confidence >= 75:
        conf_label = "Güçlü"
    elif confidence >= 60:
        conf_label = "Orta"
    elif confidence >= 45:
        conf_label = "Zayıf"
    else:
        conf_label = "Pas"

    if side == "Karışık / izle" and confidence < 50:
        conf_label = "Pas"

    # En önemli 3 neden: önce sapmanın yönü ile uyumlu modifier'lar, sonra anchor mesajı
    sign = 1 if final_z > 0 else -1
    aligned = [m for m in modifiers if (m[0] >= 0) == (sign > 0) and abs(m[0]) >= 0.10]
    aligned.sort(key=lambda m: abs(m[0]), reverse=True)
    reasons = [m[1] for m in aligned[:3]]

    if base_z is not None:
        anchor_msg = (
            f"Projeksiyon ({projected}) {anchor_label} göre "
            f"{('üstte' if base_z > 0 else 'altta')} (z={base_z:+.2f})"
        )
        reasons.insert(0, anchor_msg)
    elif side in ("ÜST eğilimli", "ALT eğilimli"):
        direction = "ÜST" if final_z > 0 else "ALT"
        reasons.insert(
            0,
            f"Maçın iç dinamikleri {direction} yönünde baskın (z={final_z:+.2f}, barem yok)",
        )

    if contradicts:
        reasons.append("Uyarı: barem ile istatistik sinyalleri çelişiyor.")

    if side in ("Pas", "Karışık / izle") and not reasons:
        reasons = ["Sinyaller yön ayrımı yapacak kadar güçlü değil."]

    return (side, confidence, conf_label, reasons[:4])


# ---------------------------------------------------------------------------
# Oyuncu notları
# ---------------------------------------------------------------------------

def _player_notes(lineups: Optional[dict]) -> list[dict]:
    if not lineups:
        return []

    notes: list[dict] = []
    team_points: dict[str, int] = {"home": 0, "away": 0}
    team_top_scorer: dict[str, tuple[str, int]] = {}

    for side_key in ("home", "away"):
        team = (lineups or {}).get(side_key) or {}
        players = team.get("players") or []
        scorers: list[tuple[str, int]] = []
        for p in players:
            stats = (p.get("statistics") or {})
            name = (
                (p.get("player") or {}).get("shortName")
                or (p.get("player") or {}).get("name")
                or "?"
            )
            points = stats.get("points")
            fouls = stats.get("personalFouls") or stats.get("fouls")
            if isinstance(points, (int, float)):
                scorers.append((name, int(points)))
                team_points[side_key] += int(points)
            if isinstance(fouls, (int, float)) and fouls >= 4:
                notes.append({
                    "type": "foul_trouble",
                    "name": name,
                    "fouls": int(fouls),
                })
        scorers.sort(key=lambda t: t[1], reverse=True)
        if scorers:
            team_top_scorer[side_key] = scorers[0]

    for side_key, (name, pts) in team_top_scorer.items():
        total = team_points.get(side_key, 0)
        if total >= 30 and pts / total >= 0.40:
            notes.append({
                "type": "single_carry",
                "name": name,
                "points": pts,
                "team_total": total,
                "share": round(pts / total, 2),
            })

    return notes
