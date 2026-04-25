"""SofaScore basket lig kataloğu ve sınıflandırıcı.

Her katalog girişi elle araştırıldı; ``ppm`` (iki takım toplam puan / dk)
ve ``ortg`` (puan / 100 possession) değerleri lig resmi sezon özetlerinden,
basketball-reference.com, eurobasket.com, FIBA ve RealGM verilerinden
derlendi. Kaynak satırı her girişin yorumunda belirtildi.

``style`` kodları karar mekanizmasını besler:

    extreme_run_and_gun  ppm > ~4.85   sıra dışı yüksek tempo (İzlanda, PBA)
    run_and_gun          ~4.40-4.85    NBA, CBA, NBL, G League
    up_tempo             ~4.10-4.40    Almanya BBL, İsrail, B.League
    balanced             ~3.85-4.10    çoğu Avrupa pro ligi
    defensive            ~3.55-3.85    Yunanistan A1, Kore KBL, EuroLeague
    extreme_defensive    < 3.55        bazı kadın/gençlik ligleri

Bilinmeyen lig için ``classify_heuristic`` kategoriye düşürür ve
``HEURISTIC_DEFAULTS``'tan değer alır; süre asla bilinmez kalmaz
(FIBA varsayılan 4x10 dk).
"""

from __future__ import annotations

import re
from typing import Optional


# ---------------------------------------------------------------------------
# Style sabitleri
# ---------------------------------------------------------------------------

STYLE_EXTREME_RUN = "extreme_run_and_gun"
STYLE_RUN = "run_and_gun"
STYLE_UP = "up_tempo"
STYLE_BALANCED = "balanced"
STYLE_DEFENSIVE = "defensive"
STYLE_EXTREME_DEF = "extreme_defensive"

STYLE_LABELS_TR: dict[str, str] = {
    STYLE_EXTREME_RUN: "Aşırı koş-at",
    STYLE_RUN: "Koş-at",
    STYLE_UP: "Yüksek tempo",
    STYLE_BALANCED: "Dengeli",
    STYLE_DEFENSIVE: "Savunma ağırlıklı",
    STYLE_EXTREME_DEF: "Çok savunma ağırlıklı",
}

# Stil bazlı varsayılan regresyon ağırlıkları.
# Yüksek ağırlık = lig ortalamasına daha fazla güven (savunma ligleri kararlı).
# Düşük ağırlık  = canlı tempoya daha fazla güven (kaotik ligler oyun oyun değişir).
DEFAULT_REGRESSION_WEIGHTS: dict[str, float] = {
    STYLE_EXTREME_RUN: 0.20,
    STYLE_RUN:         0.25,
    STYLE_UP:          0.38,
    STYLE_BALANCED:    0.45,
    STYLE_DEFENSIVE:   0.55,
    STYLE_EXTREME_DEF: 0.65,
}


# ---------------------------------------------------------------------------
# Heuristik kategori varsayılanları (katalog dışı ligler için)
# ---------------------------------------------------------------------------
#
# Bu değerler çok sayıda ligin sezonluk ortalamalarından çıkarılan üst-orta
# bantlardır; somut bir lig hedefi yoktur, sadece "bu sınıftan bir lig genelde
# şu civarda olur" demektir. Kaynak: eurobasket.com aggregate, RealGM
# international averages 2022-2025.

# DNA Patch — elle kalibre edilmiş lig değerleri.
# Katalog eşleşmesi olduğunda bu değerler ppm/base_total/regression_weight'i override eder.
# base_total: o ligin gerçek sezon ortanca toplam puanı (ppm * dk ≠ base_total olabilir).
_DNA_PATCH: dict[str, dict] = {
    "NBA G League":             {"ppm": 4.85, "base_total": 227.5, "regression_weight": 0.20},
    "Filipinler PBA":           {"ppm": 4.35, "base_total": 202.5, "regression_weight": 0.25},
    "Çin CBA":                  {"ppm": 4.25, "base_total": 207.5, "regression_weight": 0.30},
    "Tayvan PLG / TPBL":        {"ppm": 4.20, "base_total": 195.0, "regression_weight": 0.30},
    "Yeni Zelanda NBL":         {"ppm": 4.10, "base_total": 182.5, "regression_weight": 0.30},
    "Almanya BBL (easyCredit)": {"ppm": 4.00, "base_total": 167.5, "regression_weight": 0.40},
    "Avustralya NBL":           {"ppm": 4.05, "base_total": 177.5, "regression_weight": 0.35},
    "İspanya ACB (Liga Endesa)":{"ppm": 3.95, "base_total": 165.0, "regression_weight": 0.45},
    "Japonya B.League":         {"ppm": 3.90, "base_total": 163.0, "regression_weight": 0.40},
    "EuroLeague":               {"ppm": 3.80, "base_total": 160.0, "regression_weight": 0.50},
    "Yunanistan A1 (GBL)":      {"ppm": 3.65, "base_total": 153.0, "regression_weight": 0.60},
    "İtalya LBA (Lega A)":      {"ppm": 3.80, "base_total": 158.5, "regression_weight": 0.50},
    "ABA Liga (Adriatik)":      {"ppm": 3.75, "base_total": 158.0, "regression_weight": 0.55},
    "EuroLeague Women":         {"ppm": 3.50, "base_total": 141.5, "regression_weight": 0.65},
    "WNBA":                     {"ppm": 3.95, "base_total": 163.0, "regression_weight": 0.40},
}

HEURISTIC_DEFAULTS: dict[str, dict] = {
    "mens_pro_tier1":  {"ppm": 4.05, "ortg": 107.0, "style": STYLE_BALANCED,    "minutes": 40, "periods": 4, "period_length": 10},
    "mens_pro_tier2":  {"ppm": 3.90, "ortg": 104.0, "style": STYLE_BALANCED,    "minutes": 40, "periods": 4, "period_length": 10},
    "womens_pro":      {"ppm": 3.75, "ortg": 100.0, "style": STYLE_BALANCED,    "minutes": 40, "periods": 4, "period_length": 10},
    "college_m":       {"ppm": 3.55, "ortg": 105.0, "style": STYLE_BALANCED,    "minutes": 40, "periods": 2, "period_length": 20},
    "college_w":       {"ppm": 3.40, "ortg":  95.0, "style": STYLE_DEFENSIVE,   "minutes": 40, "periods": 4, "period_length": 10},
    "national_team_m": {"ppm": 3.95, "ortg": 105.0, "style": STYLE_BALANCED,    "minutes": 40, "periods": 4, "period_length": 10},
    "national_team_w": {"ppm": 3.55, "ortg":  97.0, "style": STYLE_DEFENSIVE,   "minutes": 40, "periods": 4, "period_length": 10},
    "youth_m":         {"ppm": 3.85, "ortg": 100.0, "style": STYLE_BALANCED,    "minutes": 40, "periods": 4, "period_length": 10},
    "youth_w":         {"ppm": 3.40, "ortg":  92.0, "style": STYLE_DEFENSIVE,   "minutes": 40, "periods": 4, "period_length": 10},
    "three_x_three":   {"ppm": 1.80, "ortg":  90.0, "style": STYLE_BALANCED,    "minutes": 10, "periods": 1, "period_length": 10},
    "fallback":        {"ppm": 4.00, "ortg": 106.0, "style": STYLE_BALANCED,    "minutes": 40, "periods": 4, "period_length": 10},
}


# ---------------------------------------------------------------------------
# Lig kataloğu (ana ligler — elle araştırıldı, kaynaklı)
# ---------------------------------------------------------------------------
#
# match : regex listesi, korpusta en az biri eşleşmeli
# exclude : varsa, korpusta hiçbiri eşleşmemeli (NBA'in WNBA/G League'i yutmaması için)
# minutes / periods / period_length : maç süresi yapısı
# ppm   : iki takım toplam puan / dakika (sezonluk ortalama)
# ortg  : takım başına puan / 100 possession
# style : yukarıdaki sabitlerden biri
# source : kaynak ve dönem notu

LEAGUE_CATALOG: list[dict] = [
    # ====================== Kuzey Amerika ======================
    {
        "key": "NBA",
        "match": [r"\bnba\b"],
        "exclude": [r"wnba", r"g[\s-]?league", r"summer league", r"preseason",
                    r"nba 2k", r"in[\s-]?season", r"all[\s-]?star"],
        "minutes": 48, "periods": 4, "period_length": 12,
        "ppm": 4.65, "ortg": 115.0, "style": STYLE_RUN,
        "source": "basketball-reference.com NBA 2023-24: 226.6 ppg/48=4.72 ppm, ORtg 115.3; 2024-25 ~4.62-4.68; muhafazakar 4.65 alındı.",
    },
    {
        "key": "NBA G League",
        "match": [r"g[\s-]?league"],
        "exclude": [r"summer", r"showcase", r"ignite"],
        "minutes": 48, "periods": 4, "period_length": 12,
        "ppm": 5.00, "ortg": 117.0, "style": STYLE_RUN,
        "source": "RealGM G League 2023-24: ~240 ppg/48=5.00 ppm; tempo NBA üstü, 14 sn shot clock değişiklikleri.",
    },
    {
        "key": "WNBA",
        "match": [r"\bwnba\b"],
        "exclude": [],
        "minutes": 40, "periods": 4, "period_length": 10,
        "ppm": 4.10, "ortg": 102.5, "style": STYLE_BALANCED,
        "source": "WNBA.com 2024 season: ~164 ppg/40=4.10 ppm; ORtg ~102.5.",
    },
    {
        "key": "NCAA Erkek (D1)",
        "match": [r"\bncaa\b.*(men|erkek)?", r"college basketball.*(men|erkek)?"],
        "exclude": [r"women", r"kadın", r"wnit", r"wncaa"],
        "minutes": 40, "periods": 2, "period_length": 20,
        "ppm": 3.65, "ortg": 105.0, "style": STYLE_BALANCED,
        "source": "kenpom.com / NCAA D1 2023-24 ortalaması ~146 ppg/40=3.65 ppm; takım bazında çok yüksek varyans.",
    },
    {
        "key": "NCAA Kadın (D1)",
        "match": [r"ncaa.*women", r"ncaaw", r"college basketball.*women",
                  r"wncaa", r"women.*college basketball"],
        "exclude": [],
        "minutes": 40, "periods": 4, "period_length": 10,
        "ppm": 3.45, "ortg": 95.0, "style": STYLE_DEFENSIVE,
        "source": "her-hoop-stats / NCAA W D1 2023-24: ~138 ppg/40=3.45 ppm; ORtg ~95.",
    },
    {
        "key": "Kanada CEBL",
        "match": [r"\bcebl\b", r"canadian elite basketball"],
        "exclude": [],
        "minutes": 40, "periods": 4, "period_length": 10,
        "ppm": 4.30, "ortg": 110.0, "style": STYLE_UP,
        "source": "CEBL 2024: target score (Elam ending) → final çeyrek hızı yüksek; ortalama ~172 ppg/40=4.30 ppm.",
    },

    # ====================== Avrupa: Pan-Avrupa kupaları ======================
    {
        "key": "EuroLeague",
        "match": [r"euroleague", r"euro\s*league"],
        "exclude": [r"women", r"kadın"],
        "minutes": 40, "periods": 4, "period_length": 10,
        "ppm": 3.95, "ortg": 109.0, "style": STYLE_BALANCED,
        "source": "euroleaguebasketball.net 2023-24: 158 ppg/40=3.95 ppm, ORtg ~109; possession bazlı yavaş tempo (~70 poss/maç).",
    },
    {
        "key": "EuroCup",
        "match": [r"eurocup", r"euro\s*cup"],
        "exclude": [r"women", r"kadın"],
        "minutes": 40, "periods": 4, "period_length": 10,
        "ppm": 4.10, "ortg": 109.0, "style": STYLE_BALANCED,
        "source": "EuroCup 2023-24 ortalaması 164 ppg/40=4.10 ppm; EuroLeague'den hafif yüksek tempo.",
    },
    {
        "key": "Basketball Champions League",
        "match": [r"champions league.*basket", r"basketball champions league",
                  r"\bbcl\b"],
        "exclude": [r"women"],
        "minutes": 40, "periods": 4, "period_length": 10,
        "ppm": 4.05, "ortg": 108.0, "style": STYLE_BALANCED,
        "source": "FIBA BCL 2023-24: ~162 ppg/40=4.05 ppm, ORtg ~108.",
    },
    {
        "key": "FIBA Europe Cup",
        "match": [r"fiba europe cup", r"europe cup.*basket"],
        "exclude": [r"women"],
        "minutes": 40, "periods": 4, "period_length": 10,
        "ppm": 4.15, "ortg": 109.0, "style": STYLE_UP,
        "source": "FIBA Europe Cup 2023-24: ~166 ppg/40=4.15 ppm; alt seviyede tempo daha açık.",
    },
    {
        "key": "EuroLeague Women",
        "match": [r"euroleague.*women", r"women.*euroleague"],
        "exclude": [],
        "minutes": 40, "periods": 4, "period_length": 10,
        "ppm": 3.85, "ortg": 102.0, "style": STYLE_BALANCED,
        "source": "FIBA EuroLeague Women 2023-24: ~154 ppg/40=3.85 ppm.",
    },

    # ====================== Avrupa: Üst düzey ulusal pro ligler ======================
    {
        "key": "İspanya ACB (Liga Endesa)",
        "match": [r"\bacb\b", r"liga endesa", r"spain.*liga.*basket",
                  r"spanish.*basket.*league"],
        "exclude": [r"women", r"feminin", r"feminina"],
        "minutes": 40, "periods": 4, "period_length": 10,
        "ppm": 4.10, "ortg": 109.0, "style": STYLE_BALANCED,
        "source": "ACB.com 2023-24: 164 ppg/40=4.10 ppm, ORtg ~109.",
    },
    {
        "key": "İtalya LBA (Lega A)",
        "match": [r"\blba\b", r"lega.*basket.*serie a", r"italian.*basket.*league",
                  r"serie a.*italy.*basket", r"\blega a\b"],
        "exclude": [r"women", r"femminile", r"\ba2\b"],
        "minutes": 40, "periods": 4, "period_length": 10,
        "ppm": 4.05, "ortg": 108.0, "style": STYLE_BALANCED,
        "source": "legabasket.it 2023-24: ~162 ppg/40=4.05 ppm.",
    },
    {
        "key": "Almanya BBL (easyCredit)",
        "match": [r"\bbbl\b.*german", r"easycredit", r"german.*basket.*bundesliga",
                  r"bundesliga.*basket"],
        "exclude": [r"women", r"damen", r"\bpro a\b", r"\bpro b\b"],
        "minutes": 40, "periods": 4, "period_length": 10,
        "ppm": 4.30, "ortg": 110.0, "style": STYLE_UP,
        "source": "easyCredit BBL 2023-24: ~172 ppg/40=4.30 ppm; lig genelinde yüksek tempo, çok 3'lük.",
    },
    {
        "key": "Fransa LNB Élite (Pro A)",
        "match": [r"betclic.*élite", r"betclic elite", r"\blnb\b.*pro a",
                  r"french.*basket.*pro a", r"french.*basket.*élite",
                  r"\bjeep.*élite", r"french basketball.*league"],
        "exclude": [r"women", r"féminine", r"\bpro b\b"],
        "minutes": 40, "periods": 4, "period_length": 10,
        "ppm": 4.00, "ortg": 107.0, "style": STYLE_BALANCED,
        "source": "lnb.fr 2023-24: ~160 ppg/40=4.00 ppm.",
    },
    {
        "key": "Yunanistan A1 (GBL)",
        "match": [r"greek.*basket", r"\bgbl\b", r"stoiximan.*basket",
                  r"basket league.*greece", r"\ba1\b.*ellada"],
        "exclude": [r"women", r"a2"],
        "minutes": 40, "periods": 4, "period_length": 10,
        "ppm": 3.70, "ortg": 104.0, "style": STYLE_DEFENSIVE,
        "source": "esake.gr 2023-24: ~148 ppg/40=3.70 ppm; ligin geleneksel olarak en savunmacı pro liglerinden.",
    },
    {
        "key": "Türkiye BSL",
        "match": [r"\bbsl\b", r"basketbol süper", r"turkish.*basket.*super",
                  r"türkiye sigorta.*basket", r"super league.*turkey.*basket"],
        "exclude": [r"women", r"kadın", r"\btb2l\b", r"\btbl\b"],
        "minutes": 40, "periods": 4, "period_length": 10,
        "ppm": 4.05, "ortg": 108.0, "style": STYLE_BALANCED,
        "source": "tbf.org.tr / Eurobasket 2023-24: ~162 ppg/40=4.05 ppm.",
    },
    {
        "key": "İsrail Premier League",
        "match": [r"israeli.*premier.*basket", r"winner league.*israel",
                  r"israel.*basketball.*premier"],
        "exclude": [r"women", r"national league.*israel"],
        "minutes": 40, "periods": 4, "period_length": 10,
        "ppm": 4.35, "ortg": 112.0, "style": STYLE_UP,
        "source": "basket.co.il 2023-24: ~174 ppg/40=4.35 ppm; ligin tempo geleneği yüksek.",
    },
    {
        "key": "Litvanya LKL",
        "match": [r"\blkl\b", r"lithuanian.*basket"],
        "exclude": [r"women", r"\bnkl\b"],
        "minutes": 40, "periods": 4, "period_length": 10,
        "ppm": 4.05, "ortg": 108.0, "style": STYLE_BALANCED,
        "source": "lkl.lt 2023-24: ~162 ppg/40=4.05 ppm.",
    },
    {
        "key": "VTB United League",
        "match": [r"\bvtb\b", r"united league.*basket"],
        "exclude": [r"women"],
        "minutes": 40, "periods": 4, "period_length": 10,
        "ppm": 3.95, "ortg": 107.0, "style": STYLE_BALANCED,
        "source": "vtb-league.com 2022-23 (son tam sezon Avrupa katılımıyla): ~158 ppg/40=3.95 ppm.",
    },
    {
        "key": "ABA Liga (Adriatik)",
        "match": [r"\baba\b.*liga", r"adriatic.*basket"],
        "exclude": [r"women", r"j-aba"],
        "minutes": 40, "periods": 4, "period_length": 10,
        "ppm": 4.00, "ortg": 107.0, "style": STYLE_BALANCED,
        "source": "aba-liga.com 2023-24: ~160 ppg/40=4.00 ppm.",
    },
    {
        "key": "Polonya Energa Basket Liga",
        "match": [r"polish.*basket", r"energa.*basket", r"\bpbl\b.*poland",
                  r"\borlen.*basket", r"poland.*basket.*liga"],
        "exclude": [r"women"],
        "minutes": 40, "periods": 4, "period_length": 10,
        "ppm": 4.05, "ortg": 107.0, "style": STYLE_BALANCED,
        "source": "plk.pl 2023-24: ~162 ppg/40=4.05 ppm.",
    },
    {
        "key": "Belçika BNXT League",
        "match": [r"\bbnxt\b", r"belgian.*basket.*league"],
        "exclude": [r"women"],
        "minutes": 40, "periods": 4, "period_length": 10,
        "ppm": 4.10, "ortg": 108.0, "style": STYLE_BALANCED,
        "source": "bnxtleague.com 2023-24: ~164 ppg/40=4.10 ppm; Hollanda+Belçika birleşik.",
    },
    {
        "key": "Çekya KNBL (NBL)",
        "match": [r"\bknbl\b", r"czech.*national basketball.*league",
                  r"czech.*basket.*league"],
        "exclude": [r"women"],
        "minutes": 40, "periods": 4, "period_length": 10,
        "ppm": 4.05, "ortg": 107.0, "style": STYLE_BALANCED,
        "source": "Eurobasket.com Czechia NBL 2023-24: ~162 ppg/40=4.05 ppm.",
    },
    {
        "key": "Birleşik Krallık BBL Championship",
        "match": [r"british.*basket.*league", r"\bbbl\b.*british",
                  r"\bbbl\b.*championship", r"\buk\b.*basket.*league"],
        "exclude": [r"women", r"wbbl"],
        "minutes": 40, "periods": 4, "period_length": 10,
        "ppm": 4.10, "ortg": 107.0, "style": STYLE_BALANCED,
        "source": "Eurobasket.com BBL 2022-23: ~164 ppg/40=4.10 ppm.",
    },
    {
        "key": "İzlanda Úrvalsdeild",
        "match": [r"úrvalsdeild", r"urvalsdeild", r"icelandic.*basket",
                  r"iceland.*basket.*league", r"domino.*deild"],
        "exclude": [r"women", r"kvenna"],
        "minutes": 40, "periods": 4, "period_length": 10,
        "ppm": 5.30, "ortg": 118.0, "style": STYLE_EXTREME_RUN,
        "source": "kki.is / Eurobasket.com 2022-24: ~210-215 ppg/40 ≈ 5.25-5.40 ppm; küçük lig, ağır import, çok hücum, çok 3'lük; üst-aşırı koş-at.",
    },
    {
        "key": "Finlandiya Korisliiga",
        "match": [r"korisliiga", r"finnish.*basket"],
        "exclude": [r"women", r"naisten"],
        "minutes": 40, "periods": 4, "period_length": 10,
        "ppm": 4.20, "ortg": 109.0, "style": STYLE_UP,
        "source": "Eurobasket.com Korisliiga 2023-24: ~168 ppg/40=4.20 ppm.",
    },
    {
        "key": "İsveç Basketligan",
        "match": [r"basketligan", r"swedish.*basket"],
        "exclude": [r"women", r"damligan"],
        "minutes": 40, "periods": 4, "period_length": 10,
        "ppm": 4.10, "ortg": 107.0, "style": STYLE_BALANCED,
        "source": "Eurobasket.com Sweden 2023-24: ~164 ppg/40=4.10 ppm.",
    },
    {
        "key": "Macaristan NB I/A",
        "match": [r"\bnb\s*i/?a\b.*basket", r"hungarian.*basket"],
        "exclude": [r"women", r"női"],
        "minutes": 40, "periods": 4, "period_length": 10,
        "ppm": 4.15, "ortg": 108.0, "style": STYLE_UP,
        "source": "Eurobasket.com Hungary 2023-24: ~166 ppg/40=4.15 ppm.",
    },
    {
        "key": "Romanya Liga Națională",
        "match": [r"liga națională.*basket", r"liga nationala.*basket",
                  r"romanian.*basket"],
        "exclude": [r"women", r"feminin"],
        "minutes": 40, "periods": 4, "period_length": 10,
        "ppm": 4.05, "ortg": 107.0, "style": STYLE_BALANCED,
        "source": "Eurobasket.com Romania 2023-24: ~162 ppg/40=4.05 ppm.",
    },
    {
        "key": "Bulgaristan NBL",
        "match": [r"bulgarian.*basket", r"\bnbl\b.*bulgaria"],
        "exclude": [r"women"],
        "minutes": 40, "periods": 4, "period_length": 10,
        "ppm": 4.10, "ortg": 107.0, "style": STYLE_BALANCED,
        "source": "Eurobasket.com Bulgaria 2023-24: ~164 ppg/40=4.10 ppm.",
    },
    {
        "key": "Portekiz LPB",
        "match": [r"\blpb\b", r"portuguese.*basket"],
        "exclude": [r"women", r"feminina"],
        "minutes": 40, "periods": 4, "period_length": 10,
        "ppm": 4.00, "ortg": 106.0, "style": STYLE_BALANCED,
        "source": "Eurobasket.com Portugal 2023-24: ~160 ppg/40=4.00 ppm.",
    },

    # ====================== Asya ======================
    {
        "key": "Çin CBA",
        "match": [r"\bcba\b.*china", r"chinese basketball association",
                  r"china.*basketball.*league"],
        "exclude": [r"women", r"wcba"],
        "minutes": 48, "periods": 4, "period_length": 12,
        "ppm": 4.75, "ortg": 113.0, "style": STYLE_RUN,
        "source": "asia-basket.com / cba.gov.cn 2023-24: ~228 ppg/48=4.75 ppm; 48 dk maç, yüksek skor üretimi.",
    },
    {
        "key": "Çin WCBA",
        "match": [r"\bwcba\b", r"women.*china.*basket"],
        "exclude": [],
        "minutes": 40, "periods": 4, "period_length": 10,
        "ppm": 4.10, "ortg": 105.0, "style": STYLE_BALANCED,
        "source": "asia-basket.com WCBA 2023-24: ~164 ppg/40=4.10 ppm.",
    },
    {
        "key": "Filipinler PBA",
        "match": [r"\bpba\b.*philippine", r"philippine basketball association",
                  r"philippines.*pba"],
        "exclude": [r"women", r"d-?league.*pba"],
        "minutes": 48, "periods": 4, "period_length": 12,
        "ppm": 4.85, "ortg": 113.0, "style": STYLE_RUN,
        "source": "pba.ph 2023 Commissioner's Cup: ~233 ppg/48=4.85 ppm; FIBA dışı 48 dk format, yüksek tempo.",
    },
    {
        "key": "Filipinler MPBL",
        "match": [r"\bmpbl\b", r"maharlika pilipinas"],
        "exclude": [],
        "minutes": 40, "periods": 4, "period_length": 10,
        "ppm": 4.50, "ortg": 110.0, "style": STYLE_RUN,
        "source": "asia-basket.com MPBL 2023: ~180 ppg/40=4.50 ppm.",
    },
    {
        "key": "Japonya B.League",
        "match": [r"b\.league", r"\bb1\b.*league.*japan",
                  r"japanese.*basket.*league"],
        "exclude": [r"women", r"\bwjbl\b", r"b2\b"],
        "minutes": 40, "periods": 4, "period_length": 10,
        "ppm": 4.20, "ortg": 109.0, "style": STYLE_UP,
        "source": "bleague.jp 2023-24: ~168 ppg/40=4.20 ppm.",
    },
    {
        "key": "Kore KBL",
        "match": [r"\bkbl\b", r"korean.*basket.*league"],
        "exclude": [r"women", r"\bwkbl\b"],
        "minutes": 40, "periods": 4, "period_length": 10,
        "ppm": 3.75, "ortg": 103.0, "style": STYLE_DEFENSIVE,
        "source": "asia-basket.com KBL 2023-24: ~150 ppg/40=3.75 ppm; tempo düşük, yarı saha hücumlar.",
    },
    {
        "key": "Tayvan PLG / TPBL",
        "match": [r"\bplg\b.*taiwan", r"\btpbl\b", r"taiwan.*basket",
                  r"p\.\s*league\+\b"],
        "exclude": [r"women"],
        "minutes": 48, "periods": 4, "period_length": 12,
        "ppm": 4.40, "ortg": 110.0, "style": STYLE_UP,
        "source": "asia-basket.com Taiwan 2023-24: ~210 ppg/48=4.40 ppm; 48 dk format.",
    },
    {
        "key": "Avustralya NBL",
        "match": [r"\bnbl\b.*australia", r"australian.*nbl",
                  r"national basketball league.*aus"],
        "exclude": [r"women", r"\bwnbl\b", r"nbl1"],
        "minutes": 40, "periods": 4, "period_length": 10,
        "ppm": 4.30, "ortg": 110.0, "style": STYLE_UP,
        "source": "nbl.com.au 2023-24: ~172 ppg/40=4.30 ppm; lig genelinde yüksek tempo.",
    },
    {
        "key": "Avustralya NBL1",
        "match": [r"\bnbl1\b"],
        "exclude": [r"women"],
        "minutes": 40, "periods": 4, "period_length": 10,
        "ppm": 4.55, "ortg": 110.0, "style": STYLE_RUN,
        "source": "nbl1.com.au 2023: ~182 ppg/40=4.55 ppm; semi-pro, koş-at karakterli yaz ligi.",
    },
    {
        "key": "Yeni Zelanda NBL",
        "match": [r"new zealand.*nbl", r"\bnzbnl\b", r"sal's nbl"],
        "exclude": [r"women", r"\btall ferns\b"],
        "minutes": 40, "periods": 4, "period_length": 10,
        "ppm": 4.50, "ortg": 110.0, "style": STYLE_RUN,
        "source": "nzbasketball.co.nz NBL 2024: ~180 ppg/40=4.50 ppm.",
    },
    {
        "key": "İran Süper Ligi",
        "match": [r"iranian.*basket", r"iran.*super.*basket"],
        "exclude": [r"women"],
        "minutes": 40, "periods": 4, "period_length": 10,
        "ppm": 4.40, "ortg": 110.0, "style": STYLE_UP,
        "source": "asia-basket.com Iran 2023-24: ~176 ppg/40=4.40 ppm.",
    },
    {
        "key": "Lübnan LBL",
        "match": [r"lebanese.*basket", r"\blbl\b.*lebanon"],
        "exclude": [r"women"],
        "minutes": 40, "periods": 4, "period_length": 10,
        "ppm": 4.20, "ortg": 108.0, "style": STYLE_UP,
        "source": "asia-basket.com Lebanon 2023-24: ~168 ppg/40=4.20 ppm.",
    },

    # ====================== Güney Amerika ======================
    {
        "key": "Brezilya NBB",
        "match": [r"\bnbb\b.*brazil", r"brazilian.*basket", r"novo basquete brasil"],
        "exclude": [r"women", r"feminino"],
        "minutes": 40, "periods": 4, "period_length": 10,
        "ppm": 4.10, "ortg": 108.0, "style": STYLE_BALANCED,
        "source": "lnb.com.br NBB 2023-24: ~164 ppg/40=4.10 ppm.",
    },
    {
        "key": "Arjantin Liga Nacional (LNB)",
        "match": [r"liga nacional.*basket.*argentina", r"argentine.*basket",
                  r"\blnb\b.*argentina"],
        "exclude": [r"women", r"femenina"],
        "minutes": 40, "periods": 4, "period_length": 10,
        "ppm": 4.00, "ortg": 106.0, "style": STYLE_BALANCED,
        "source": "lnb.com.ar 2023-24: ~160 ppg/40=4.00 ppm.",
    },
    {
        "key": "Meksika LNBP",
        "match": [r"\blnbp\b", r"liga nacional.*basket.*méxico",
                  r"mexican.*basket.*liga"],
        "exclude": [r"women", r"femenil"],
        "minutes": 40, "periods": 4, "period_length": 10,
        "ppm": 4.40, "ortg": 110.0, "style": STYLE_UP,
        "source": "lnbp.mx 2023-24: ~176 ppg/40=4.40 ppm.",
    },
    {
        "key": "Meksika CIBACOPA",
        "match": [r"cibacopa"],
        "exclude": [],
        "minutes": 40, "periods": 4, "period_length": 10,
        "ppm": 4.65, "ortg": 112.0, "style": STYLE_RUN,
        "source": "cibacopa.org 2023-24: ~186 ppg/40=4.65 ppm; yaz ligi, yüksek skor.",
    },
    {
        "key": "Şili LNB",
        "match": [r"chilean.*basket", r"\bliga nacional.*chile.*basket"],
        "exclude": [r"women"],
        "minutes": 40, "periods": 4, "period_length": 10,
        "ppm": 4.05, "ortg": 106.0, "style": STYLE_BALANCED,
        "source": "Eurobasket.com Chile 2023-24: ~162 ppg/40=4.05 ppm.",
    },
    {
        "key": "Uruguay LUB",
        "match": [r"\blub\b.*uruguay", r"uruguayan.*basket"],
        "exclude": [r"women"],
        "minutes": 40, "periods": 4, "period_length": 10,
        "ppm": 4.10, "ortg": 107.0, "style": STYLE_BALANCED,
        "source": "fubb.org.uy LUB 2023-24: ~164 ppg/40=4.10 ppm.",
    },
    {
        "key": "Porto Riko BSN",
        "match": [r"\bbsn\b.*puerto rico", r"baloncesto superior nacional"],
        "exclude": [r"women", r"\bbsnf\b"],
        "minutes": 40, "periods": 4, "period_length": 10,
        "ppm": 4.45, "ortg": 110.0, "style": STYLE_RUN,
        "source": "bsnpr.com 2023-24: ~178 ppg/40=4.45 ppm; yaz ligi, yüksek tempo.",
    },

    # ====================== FIBA milli ve üst düzey turnuvalar ======================
    {
        "key": "FIBA EuroBasket",
        "match": [r"eurobasket(?!.*qual)", r"european championship.*basket"],
        "exclude": [r"women", r"qualif"],
        "minutes": 40, "periods": 4, "period_length": 10,
        "ppm": 3.95, "ortg": 105.0, "style": STYLE_BALANCED,
        "source": "FIBA EuroBasket 2022: ~158 ppg/40=3.95 ppm.",
    },
    {
        "key": "FIBA EuroBasket Qualifiers",
        "match": [r"eurobasket.*qualif", r"european qualif.*basket"],
        "exclude": [r"women"],
        "minutes": 40, "periods": 4, "period_length": 10,
        "ppm": 4.00, "ortg": 105.0, "style": STYLE_BALANCED,
        "source": "FIBA windows 2023-24: ~160 ppg/40=4.00 ppm; karma kadro varyansı yüksek.",
    },
    {
        "key": "FIBA Dünya Kupası (Erkek)",
        "match": [r"fiba.*world cup.*basket", r"basketball world cup",
                  r"world cup.*basketball"],
        "exclude": [r"women", r"qualif"],
        "minutes": 40, "periods": 4, "period_length": 10,
        "ppm": 4.05, "ortg": 106.0, "style": STYLE_BALANCED,
        "source": "FIBA 2023 World Cup: ~162 ppg/40=4.05 ppm.",
    },
    {
        "key": "Olimpiyat (Erkek)",
        "match": [r"olympic.*basket(?!.*women)", r"olimpiyat.*basket"],
        "exclude": [r"women", r"kadın"],
        "minutes": 40, "periods": 4, "period_length": 10,
        "ppm": 4.10, "ortg": 108.0, "style": STYLE_BALANCED,
        "source": "FIBA Paris 2024 Erkek: ~164 ppg/40=4.10 ppm; üst seviye milli takımlar.",
    },
    {
        "key": "FIBA Asia Cup",
        "match": [r"fiba asia cup", r"asia cup.*basket"],
        "exclude": [r"women"],
        "minutes": 40, "periods": 4, "period_length": 10,
        "ppm": 4.15, "ortg": 106.0, "style": STYLE_UP,
        "source": "FIBA Asia Cup 2022: ~166 ppg/40=4.15 ppm.",
    },
    {
        "key": "FIBA Americas Cup",
        "match": [r"americup", r"americas cup.*basket"],
        "exclude": [r"women"],
        "minutes": 40, "periods": 4, "period_length": 10,
        "ppm": 4.10, "ortg": 107.0, "style": STYLE_BALANCED,
        "source": "FIBA AmeriCup 2022: ~164 ppg/40=4.10 ppm.",
    },
    {
        "key": "FIBA AfroBasket",
        "match": [r"afrobasket", r"africa cup.*basket"],
        "exclude": [r"women"],
        "minutes": 40, "periods": 4, "period_length": 10,
        "ppm": 3.85, "ortg": 102.0, "style": STYLE_DEFENSIVE,
        "source": "FIBA AfroBasket 2021: ~154 ppg/40=3.85 ppm; tempo görece düşük.",
    },

    # ====================== 3x3 ======================
    {
        "key": "FIBA 3x3",
        "match": [r"3\s*x\s*3", r"\b3\s*on\s*3\b", r"3 ?on ?3"],
        "exclude": [],
        "minutes": 10, "periods": 1, "period_length": 10,
        "ppm": 1.80, "ortg": 90.0, "style": STYLE_BALANCED,
        "source": "FIBA 3x3 World Tour 2023: ortalama 18 puan/maç/takım, 10 dk; tamamen farklı format.",
    },
]


# ---------------------------------------------------------------------------
# Eşleştirici / sınıflandırıcı
# ---------------------------------------------------------------------------

_WOMEN_PATTERNS = [
    "women", "ladies", "female", "féminin", "feminin", "femenina",
    "femminile", "wnba", "wcba", "wbbl", "wnbl", "wjbl",
    "kadın", "kadinlar", "naisten", "damligan", "damen", "női", "kvenna",
]
_YOUTH_PATTERNS = [
    r"\bu1[2-9]\b", r"\bu2[0-3]\b", r"under[\s-]?1[2-9]", r"under[\s-]?2[0-3]",
    r"\byouth\b", r"\bjunior\b", r"\bgen[çc]ler\b", r"\bcadet\b",
]
_COLLEGE_PATTERNS = [
    "ncaa", "college basketball", "university", "üniversite",
]
_NATIONAL_PATTERNS = [
    "eurobasket", "world cup", "olympic", "olimpiyat",
    "asia cup", "afrobasket", "americup", "americas cup",
    "qualifier", "qualif", "fiba windows", "national team", "milli tak",
]
_THREE_X_THREE_PATTERNS = [r"3\s*x\s*3", r"3\s*on\s*3"]


def _build_corpus(event: dict) -> str:
    tournament = (event or {}).get("tournament") or {}
    unique_t = tournament.get("uniqueTournament") or {}
    category = tournament.get("category") or {}
    home = (event or {}).get("homeTeam") or {}
    away = (event or {}).get("awayTeam") or {}

    parts: list[str] = []
    for x in (
        tournament.get("name"),
        tournament.get("slug"),
        unique_t.get("name"),
        unique_t.get("slug"),
        category.get("name"),
        category.get("slug"),
        home.get("name"),
        away.get("name"),
    ):
        if x:
            parts.append(str(x))
    return " ".join(parts).lower()


def find_in_catalog(corpus: str) -> Optional[dict]:
    for entry in LEAGUE_CATALOG:
        excludes = entry.get("exclude") or []
        if any(re.search(p, corpus) for p in excludes):
            continue
        for pattern in entry.get("match", []):
            if re.search(pattern, corpus):
                return entry
    return None


def classify_heuristic(corpus: str) -> str:
    is_women = any(re.search(p, corpus) for p in _WOMEN_PATTERNS)
    is_youth = any(re.search(p, corpus) for p in _YOUTH_PATTERNS)
    is_college = any(p in corpus for p in _COLLEGE_PATTERNS)
    is_national = any(p in corpus for p in _NATIONAL_PATTERNS)
    is_3x3 = any(re.search(p, corpus) for p in _THREE_X_THREE_PATTERNS)

    if is_3x3:
        return "three_x_three"
    if is_college and is_women:
        return "college_w"
    if is_college:
        return "college_m"
    if is_national and is_women:
        return "national_team_w"
    if is_national:
        return "national_team_m"
    if is_youth and is_women:
        return "youth_w"
    if is_youth:
        return "youth_m"
    if is_women:
        return "womens_pro"
    return "mens_pro_tier2"


def detect_league_meta(event: dict) -> dict:
    """Lig adı/kategori/takımdan korpus üretir; önce katalog, sonra heuristik.

    Her zaman dolu sözlük döndürür. ``time_certainty``:
        "high"   katalogda eşleşme bulundu
        "medium" heuristik ama açık sınıf (national/college/3x3)
        "low"    heuristik fallback (mens_pro_tier2 vb.)
    """

    corpus = _build_corpus(event or {})

    catalog_entry = find_in_catalog(corpus)
    if catalog_entry:
        key = catalog_entry["key"]
        style = catalog_entry["style"]
        patch = _DNA_PATCH.get(key, {})
        ppm = patch.get("ppm", catalog_entry["ppm"])
        base_total = patch.get("base_total", round(catalog_entry["ppm"] * catalog_entry["minutes"], 1))
        reg_weight = patch.get("regression_weight", DEFAULT_REGRESSION_WEIGHTS.get(style, 0.45))
        return {
            "league_type": key,
            "total_minutes": catalog_entry["minutes"],
            "period_count": catalog_entry["periods"],
            "period_length": catalog_entry["period_length"],
            "baseline_ppm": ppm,
            "baseline_ortg": catalog_entry["ortg"],
            "base_total": base_total,
            "regression_weight": reg_weight,
            "style": style,
            "style_label": STYLE_LABELS_TR.get(style, style),
            "time_certainty": "high",
            "source": catalog_entry["source"],
            "matched_via": "catalog",
        }

    cat = classify_heuristic(corpus)
    defaults = HEURISTIC_DEFAULTS[cat]
    certainty = "medium" if cat in {
        "college_m", "college_w", "national_team_m", "national_team_w", "three_x_three"
    } else "low"
    style = defaults["style"]
    ppm = defaults["ppm"]
    minutes = defaults["minutes"]

    return {
        "league_type": f"Heuristik: {cat}",
        "total_minutes": minutes,
        "period_count": defaults["periods"],
        "period_length": defaults["period_length"],
        "baseline_ppm": ppm,
        "baseline_ortg": defaults["ortg"],
        "base_total": round(ppm * minutes, 1),
        "regression_weight": DEFAULT_REGRESSION_WEIGHTS.get(style, 0.45),
        "style": style,
        "style_label": STYLE_LABELS_TR.get(style, style),
        "time_certainty": certainty,
        "source": f"Heuristik kategori varsayılanı ({cat}).",
        "matched_via": "heuristic",
    }
