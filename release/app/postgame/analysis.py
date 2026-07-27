"""Delta-Engine, Lobby-Ranking, Item-Sanity und Narrativ des Post-Game-Reports.

Reine Logik ohne IO/Netz (offline testbar). Grundprinzip (s.
docu/plan_postgame.md 2.1/2.4): Massstab ist die Lobby, nicht High-Elo. Jeder
Team-Spieler wird mit seinem Rollen-Gegenpart (gleiche teamPosition im anderen
Team) verglichen; Deltas werden ueber Spielphasen Early 0-10 / Mid 10-20 /
Late 20+ auf den rollen-relevanten Metriken gebildet, ergaenzt um Kontext-Signale
(Kill-Participation, Assists) - roh UND Kontext, kein Blame-Tool.
"""

from . import series

# Spielphasen (Redesign 2026-07-24): (key, Label, Start-Minute, End-Minute|None).
# Durchgaengig Early 0-10 / Mid 10-20 / Late 20+ (ersetzt 0-15/15-25/25+).
PHASES = (("early", "Early (0–10)", 0, 10),
          ("mid", "Mid (10–20)", 10, 20),
          ("late", "Late (20+)", 20, None))

# Rollen-Fokusmetrik je teamPosition (welcher Delta traegt die Impact-Deutung).
ROLE_FOCUS = {"TOP": "dmg", "JUNGLE": "cs", "MIDDLE": "dmg",
              "BOTTOM": "dmg", "UTILITY": "vision"}

METRIC_LABEL = {"gold": "Gold", "cs": "CS", "dmg": "Schaden",
                "vision": "Vision"}


# --- Gegenpart-Paarung ------------------------------------------------------

def counterpart_map(participants: list) -> dict:
    """{pid -> pid des Rollen-Gegenparts (gleiche Rolle, anderes Team)}.

    `participants`: Liste von Dicts mit pid, team, role. Fehlt ein Gegenpart
    (unvollstaendige Rollen), ist der Wert None."""
    by_key: dict[tuple, int] = {}
    for p in participants:
        by_key[(p["role"], p["team"])] = p["pid"]
    out: dict[int, int | None] = {}
    for p in participants:
        other = 200 if p["team"] == 100 else 100
        out[p["pid"]] = by_key.get((p["role"], other))
    return out


# --- Phasen-Deltas ----------------------------------------------------------

def _cum_gain(seq: list, a: int, b, n: int) -> int:
    """Zuwachs einer kumulativen Serie im Frame-Fenster [a, b] (b None = Ende).

    Grenzen werden auf [0, n-1] geklemmt. Liegt der Phasenstart jenseits des
    Spielendes (Serie kuerzer als a), ist der Zuwachs 0."""
    if n <= 0 or not seq:
        return 0
    last = n - 1
    if a > last:
        return 0
    a = max(0, min(a, last))
    b = last if b is None else max(a, min(b, last))
    return (seq[b] or 0) - (seq[a] or 0)


def _vision_actions(events: list, pid: int, a: int, b, n: int) -> int:
    """Ward-Aktionen (gelegt + zerstoert) eines Spielers im Minutenfenster [a, b).

    Zaehlt nur echte Ward-Typen (series.is_real_ward) - das UNDEFINED-Rauschen der
    Timeline (Runen-/Effekt-Wards) bleibt aussen vor, damit der Vision-Delta nicht
    verzerrt (gleiche Filterung wie die Vision-Graphen)."""
    last = n - 1
    hi = (last + 1) if b is None else b
    cnt = 0
    for w in events:
        if not series.is_real_ward(w.get("ward_type")):
            continue
        actor = w["creator"] if w["kind"] == "WARD_PLACED" else w["killer"]
        if actor != pid:
            continue
        m = int(w["minute"])
        if a <= m < hi:
            cnt += 1
    return cnt


def phase_deltas(series: dict, pid: int, opp: int | None, role: str) -> list:
    """Phasen-Deltas Spieler vs. Gegenpart (roh) je Metrik.

    Rueckgabe: Liste je Phase mit {key, label, focus, metrics:{gold,cs,dmg,
    vision}}. Jede Metrik ist ein Dict {me, opp, delta} des Phasen-Zuwachses
    (Delta = me - opp). Ohne Gegenpart bleibt opp/delta None (nur eigene Werte).
    `focus` markiert die rollen-relevante Metrik (ROLE_FOCUS)."""
    players = series["players"]
    n = series["n_frames"]
    wards = series["events"]["wards"]
    me = players.get(pid, {})
    op = players.get(opp, {}) if opp is not None else {}
    focus = ROLE_FOCUS.get(role, "gold")

    out = []
    for key, label, a, b in PHASES:
        metrics: dict[str, dict] = {}
        for metric in ("gold", "cs", "dmg"):
            mine = _cum_gain(me.get(metric, []), a, b, n)
            if opp is None:
                metrics[metric] = {"me": mine, "opp": None, "delta": None}
            else:
                other = _cum_gain(op.get(metric, []), a, b, n)
                metrics[metric] = {"me": mine, "opp": other,
                                   "delta": mine - other}
        mine_v = _vision_actions(wards, pid, a, b, n)
        if opp is None:
            metrics["vision"] = {"me": mine_v, "opp": None, "delta": None}
        else:
            opp_v = _vision_actions(wards, opp, a, b, n)
            metrics["vision"] = {"me": mine_v, "opp": opp_v,
                                 "delta": mine_v - opp_v}
        out.append({"key": key, "label": label, "focus": focus,
                    "metrics": metrics})
    return out


# --- Kill-Kontext -----------------------------------------------------------

def kill_context(kills: list, pid: int, team_pids: set, team_total_kills: int) -> dict:
    """Kills/Assists/Tode eines Spielers + Kill-Participation am Team.

    `team_total_kills` = Summe der Kills des eigenen Teams (aus Match-Stats);
    0 -> KP 0. KP = (kills + assists) / team_total_kills, geklemmt auf <=1."""
    k = sum(1 for e in kills if e["killer"] == pid)
    a = sum(1 for e in kills if pid in (e["assists"] or []))
    d = sum(1 for e in kills if e["victim"] == pid)
    kp = 0.0
    if team_total_kills > 0:
        kp = min(1.0, (k + a) / team_total_kills)
    return {"kills": k, "assists": a, "deaths": d, "kp": round(kp, 3)}


def death_phases(kills: list, pid: int) -> dict:
    """Tode eines Spielers, aufgeteilt nach Phase (early/mid/late) + Zeitpunkte.

    Phasen-Grenzen wie PHASES (Redesign 2026-07-24): Early 0-10 / Mid 10-20 /
    Late 20+."""
    times = sorted(round(e["minute"], 1) for e in kills if e["victim"] == pid)
    return {
        "times": times,
        "early": sum(1 for t in times if t < 10),
        "mid": sum(1 for t in times if 10 <= t < 20),
        "late": sum(1 for t in times if t >= 20),
    }


# --- Lobby-Ranking ----------------------------------------------------------

def _percentile(value, values: list) -> float:
    """Perzentil (0..1) eines Werts unter allen `values` (Mittelrang-Methode:
    strikt kleinere + halbe gleiche). Bei einem einzigen Wert -> 0.5."""
    n = len(values)
    if n == 0:
        return 0.0
    less = sum(1 for v in values if v < value)
    equal = sum(1 for v in values if v == value)
    return (less + 0.5 * equal) / n


def lobby_ranking(participants: list) -> dict:
    """Perzentil je Metrik fuer jeden der 10 Spieler.

    `participants`: Dicts mit pid + Endwerten gold/cs/dmg/vision (aus Match-
    Stats). Rueckgabe {pid: {metric: {value, pct}}}. Perzentil ist die relative
    Position unter allen Spielern der Lobby (fairer Selbst-Massstab)."""
    metrics = ("gold", "cs", "dmg", "vision")
    cols = {m: [p.get(m, 0) or 0 for p in participants] for m in metrics}
    out: dict[int, dict] = {}
    for p in participants:
        row = {}
        for m in metrics:
            val = p.get(m, 0) or 0
            row[m] = {"value": val, "pct": round(_percentile(val, cols[m]), 3)}
        out[p["pid"]] = row
    return out


# --- Side-by-side-Scoreboard (Redesign 2026-07-24) --------------------------

# Rollen-Reihenfolge des Reports (Riot-Standard) - gilt fuer Team-Karten UND
# Scoreboard.
ROLE_ORDER = {"TOP": 0, "JUNGLE": 1, "MIDDLE": 2, "BOTTOM": 3, "UTILITY": 4}

# Scoreboard-Metriken (key, Label, higher_is_better). 'dmg' nur bei has_damage.
SCOREBOARD_METRICS = (("gold", "Item-Gold", True), ("cs", "CS", True),
                      ("vision", "Vision", True), ("kda", "KDA", True),
                      ("level", "Level", True), ("dmg", "Schaden", True))


def build_scoreboard(ranked_names: dict, finals: dict, my_team: int) -> list:
    """Side-by-side-Scoreboard: je Rolle ein Paar (eigenes Team vs. Gegenpart).

    `ranked_names`: {pid: {champ, role, team, name}} - liefert Rolle/Team/Champ.
    `finals`: {pid: {gold, cs, vision, dmg, level, kda:(k,d,a)}} - die Endwerte
    aller 10 (Item-Gold = gehaltenes Item-Gold am Spielende). Rueckgabe: Liste
    je auf dem eigenen Team belegter Rolle in ROLE_ORDER mit
    {role, me:{pid,champ,vals}, opp:{pid,champ,vals}|None}; `vals` traegt alle
    Scoreboard-Metriken (dmg auch key-frei vorhanden, im Renderer nur bei
    has_damage gezeigt). So bleiben Einzelwerte direkt vergleichbar."""
    # pid je (Rolle, Team) aufloesen.
    by_role_team: dict[tuple, int] = {}
    for pid, info in ranked_names.items():
        by_role_team[(info["role"], info["team"])] = pid
    other_team = 200 if my_team == 100 else 100

    def _side(pid):
        if pid is None:
            return None
        info = ranked_names[pid]
        f = finals.get(pid, {})
        return {"pid": pid, "champ": info["champ"], "vals": {
            "gold": f.get("gold", 0), "cs": f.get("cs", 0),
            "vision": f.get("vision", 0), "dmg": f.get("dmg", 0),
            "level": f.get("level", 0), "kda": tuple(f.get("kda", (0, 0, 0)))}}

    rows = []
    my_roles = sorted(
        {info["role"] for info in ranked_names.values()
         if info["team"] == my_team and info["role"]},
        key=lambda r: ROLE_ORDER.get(r, 9))
    for role in my_roles:
        me_pid = by_role_team.get((role, my_team))
        opp_pid = by_role_team.get((role, other_team))
        rows.append({"role": role, "me": _side(me_pid), "opp": _side(opp_pid)})
    return rows


# --- Item-Sanity ------------------------------------------------------------

def item_sanity(item_ids: list, core_names: list, id_to_name) -> dict:
    """Zugehoerigkeits-Check der finalen Items gegen das builds.yaml-Core-*Set*.

    KEIN Timing/keine Benchmarks (builds.yaml ist elo-abhaengig) - nur, ob der
    Champion seine Core-Items grundsaetzlich gebaut hat. `id_to_name` bildet eine
    Item-ID auf ihren Namen ab (None -> nicht aufloesbar). Rueckgabe {core,
    built, present, missing}."""
    built_names = set()
    for iid in item_ids:
        if not iid:
            continue
        name = id_to_name(iid)
        if name:
            built_names.add(name)
    core = list(core_names)
    present = [c for c in core if c in built_names]
    missing = [c for c in core if c not in built_names]
    return {"core": core, "built": sorted(built_names),
            "present": present, "missing": missing}


# --- Narrativ / Auto-Verdikt ------------------------------------------------
#
# Das Auto-Verdikt (Redesign 2026-07-24) ist eine Liste kurzer, eigenstaendiger
# Befund-Zeilen (je 1 Satz, wichtigster zuerst), die der Renderer je auf eigener
# Zeile ausgibt - kein Fliesstext mehr. Alle Schwellen liegen als Konstanten
# vor (tunebar). §2.4-konform: KEIN Blame und - verbindlich - NIRGENDS ein
# rollen-uebergreifender Absolut-Vergleich ("geringster Beitrag" nach Impact-
# Total/Perzentil). Beitraege werden ausschliesslich RELATIV zum jeweiligen
# Rollen-Gegenpart bewertet (Impact-Quote), damit z. B. ein Zilean nicht gegen
# einen Carry gemessen wird.

# Cap (Team-Diagnose-Ausbau 2026-07-26): von 6 auf 8 angehoben. Das Verdikt
# traegt seit dem Ausbau vier zusaetzliche TEAM-Befunde (Rollen-Differentiale,
# nicht umgesetzte Buffs, Antiheal, Elder-getrennte Objective-Bilanz); mit 6
# Zeilen waeren die spieler-bezogenen Befunde (Lane/Tode) komplett verdraengt
# worden. 8 Zeilen bleiben ueberschaubar und lassen beide Ebenen zu.
VERDICT_MAX_LINES = 8           # Cap: hoechstens so viele Befund-Zeilen

# Slot-Reservierung (2026-07-27). Der Cap schnitt bisher stumpf hinten ab -
# und seit dem Team-Diagnose-Ausbau gibt es so viele TEAM-Kandidaten (Kipp-
# Punkt, Teamfight-Warum, Rollen-Differentiale, Objective-Bilanz, Unconverted,
# Antiheal), dass sie die SPIELER-Befunde (Lane, Tode, Impact-Rueckstand, Build,
# Vision) vollstaendig verdraengen konnten. Der Report soll aber primaer zeigen,
# woran DER SPIELER arbeiten muss. Gibt es mindestens so viele Spieler-Befunde,
# bekommt der Spieler-Block darum garantiert so viele der Slots; der Team-Block
# wird dafuer hinten gekappt (Reihenfolge bleibt). Weniger Spieler-Befunde ->
# der Team-Block fuellt wieder auf (kein Slot verfaellt).
VERDICT_MIN_PLAYER_LINES = 2
EARLY_DEATH_MIN = 3             # >= so viele Tode vor Min 10 -> eigener Befund
DEATH_SKEW_MIN = 2              # ab so vielen Toden einer Art zaehlt der Skew
DEATH_SKEW_SHARE = 0.5          # Anteil (Teamfight/Pick), ab dem wir ihn nennen
DEATH_KILLER_MIN = 4            # ab so vielen Toden durch DENSELBEN Gegner ...
DEATH_KILLER_SHARE = 0.5        # ... und diesem Anteil wird der Killer benannt
                                # (4 von 8 ist ein Muster, 2 von 3 Zufall)
QUOTE_LOW = 0.8                 # Impact-Quote < diesem Wert = spuerbarer Rueckstand
QUOTE_HIGH = 1.2                # Impact-Quote > diesem Wert = spuerbarer Vorsprung
TEAMFIGHT_MIN = 2               # ab so vielen entschiedenen Fights zaehlt die Bilanz
                                # (Kipp-Punkt-Schwellen: TIP_* in Sektion 4)
OBJECTIVE_DIFF_MIN = 2          # Elite-Differenz, ab der Objectives "praegend" sind
TOWER_DIFF_MIN = 3              # Turm-Differenz, ab der Objectives "praegend" sind
BUILD_LATE_GAP = 2.0            # Min hinter Gegenpart bei einem Kern-Item -> Befund
BUILD_HIT_LOW = 0.34           # Engine-Konformitaet <= 34 % -> Build-Ausreisser
VISION_DEFICIT_MIN = 8         # Ward-Aktionen-Rueckstand (Summe), ab dem markant

# Antiheal-Befund (Team-Diagnose 2026-07-26, Gating verschaerft 2026-07-26b).
#
# Nutzer-Feedback: die Zeile war zu trigger-freudig - sie erschien auch dort, wo
# Heilung nachweislich kein Problem war. Ein Befund ist sie nur, wenn ALLE DREI
# Bedingungen gelten (s. `_verdict_antiheal_line`):
#   (a) Heil-Signal   - Team-Heilung ueber ANTIHEAL_HEAL_PER_MIN_HARD ODER ein
#                       EINZELNER Gegner ueber ANTIHEAL_SOLO_PER_MIN,
#   (b) Versaeumnis   - kein eigenes Antiheal oder erst nach ANTIHEAL_LATE_MIN,
#   (c) Wirkung       - die Teamfight-Bilanz ist negativ (mehr verlorene als
#                       gewonnene Fights); sonst war die Heilung offenbar
#                       beherrschbar und es gibt nichts zu lernen.
#
# Bezug ist immer die SPIELDAUER, nicht ein Absolutwert - ein 20-Minuten-Spiel
# heilt naturgemaess weniger als ein 50-Minuten-Spiel.
#
# **Kalibrierung (2026-07-26, lokaler Match-Cache 16.13 + 16.14, 36.497 Matches
# mit vollstaendiger Match-Summary = 72.994 Team- und 364.970 Spieler-Werte;
# Heilung = totalHeal + totalHealsOnTeammates):**
#   Team je Minute:    p50 1.442 | p75 1.861 | p90 2.337 | p95 2.700
#   Spieler je Minute: p50   227 | p75   407 | p90   640 | p95   830
# Daraus:
ANTIHEAL_HEAL_PER_MIN_HARD = 2350   # ~p90 der Team-Verteilung (9,6 % der Teams).
                                    # Bewusst ueber dem alten Wert 1.900 (~p77):
                                    # jedes vierte Spiel ist kein "Befund".
ANTIHEAL_SOLO_PER_MIN = 850         # ~p95 der Spieler-Verteilung (4,7 % der
                                    # Spieler). Ein einzelner, unkuerzbar
                                    # heilender Gegner ist auch dann ein Problem,
                                    # wenn die TEAM-Summe unauffaellig bleibt.
                                    # Belegt an beiden Referenz-Spielen:
                                    # Nasus 27.556/30,7 min = 898/min
                                    # (EUW1_7929799918, Team-Summe nur ~1.750/min)
                                    # und XinZhao 57.187/57,7 min = 991/min
                                    # (EUW1_7929841225, Team ~2.122/min - beide
                                    # unter der Team-Schwelle). Tiefer als 850
                                    # (p95) zu gehen wuerde jeden zweiten Tank/
                                    # Lifesteal-Bruiser melden.
ANTIHEAL_LATE_MIN = 25.0            # ein erstes Antiheal ab dieser Minute kam zu
                                    # spaet, um die Mid-Game-Fights zu drehen
                                    # (dokumentierte Wahl: die Mid-Phase der
                                    # Delta-Engine endet bei Min 20, ein Kauf
                                    # braucht danach noch Wirkzeit)

# Rollen-Kurzlabel fuer die Team-Zeilen (Rollen-Differentiale).
ROLE_LABEL = {"TOP": "Top", "JUNGLE": "Jungle", "MIDDLE": "Mid",
              "BOTTOM": "Bot", "UTILITY": "Support"}

# Riot-monsterType -> deutsches Label (Objective-Bilanz). Der Elder-Drache ist
# in den Daten ein DRAGON mit monsterSubType ELDER_DRAGON; er wird in der Bilanz
# SEPARAT gefuehrt (Nutzer-Befund 2026-07-26: 'Drachen 5:4' verschweigt, dass
# zwei davon Elder waren - eine voellig andere Aussage).
_MONSTER_LABEL = {"DRAGON": "Drachen", "ELDER": "Elder", "HORDE": "Grubs",
                  "RIFTHERALD": "Herold", "BARON_NASHOR": "Baron",
                  "ATAKHAN": "Atakhan"}

ELDER_SUBTYPE = "ELDER_DRAGON"


def _fmt_int(n) -> str:
    """Ganzzahl mit Tausenderpunkt (7.550) - _fmt-Konvention des Renderers, hier
    lokal, damit die Verdikt-Logik netz-/renderer-frei testbar bleibt."""
    try:
        return f"{int(round(n)):,}".replace(",", ".")
    except (TypeError, ValueError):
        return str(n)


def _phase_extreme(deltas: list, metric: str, sign: int):
    """Groesster (sign=+1) bzw. kleinster (sign=-1) Delta EINER Metrik ueber alle
    Phasen. Rueckgabe (phase_label, delta) oder None (kein Gegenpart-Delta)."""
    best = None
    for ph in deltas:
        d = ph["metrics"].get(metric, {}).get("delta")
        if d is None:
            continue
        if sign > 0 and d > 0 and (best is None or d > best[1]):
            best = (ph["label"], d)
        if sign < 0 and d < 0 and (best is None or d < best[1]):
            best = (ph["label"], d)
    return best


# --- Einzelne Befund-Zeilen (jede reine Logik, offline testbar) -------------

def _teamfight_balance(teamfights: list) -> tuple:
    """(won, lost, decisive) aus den erkannten Teamfights.

    KEIN Kipp-Punkt mehr (Korrektur 2026-07-25): der wird ausschliesslich von
    `teamfight_tipping_minute` bestimmt, das dafuer die Team-Serien braucht."""
    won = sum(1 for f in teamfights if f.get("result") == "gewonnen")
    lost = sum(1 for f in teamfights if f.get("result") == "verloren")
    decisive = [f for f in teamfights
                if f.get("result") in ("gewonnen", "verloren")]
    return won, lost, len(decisive)


def _defining_factor(teamfights: list, objectives: dict | None) -> str | None:
    """Praegendster Faktor als kurze Nominalphrase - oder None.

    **Nutzer-Feedback 2026-07-26b (verbindliches Design-Prinzip): keine
    tautologischen Aussagen.** "Niederlage. Praegendster Faktor: Verlorene
    Teamfights." sagt nichts - dass ein verlorenes Spiel verlorene Fights hatte,
    ist keine Erkenntnis; die interessante Frage ist, WARUM die Fights nicht
    liefen (das beantwortet `_verdict_teamfight_reason_line`). Der Teamfight-
    Zweig ist darum ersatzlos entfallen; es bleibt der nicht-tautologische
    Objective-Befund bei markanter Differenz. Ohne Faktor traegt die Kopfzeile
    nur noch das Ergebnis.

    **Komponenten-Schaerfung 2026-07-27:** vorher genuegte EINE markante
    Komponente, um pauschal "Objective-Kontrolle des Gegners" zu behaupten - bei
    gemischter Bilanz (Referenz-Marathon: Elder 2:0 fuer uns, Tuerme 7:11 fuer
    den Gegner) war das schlicht falsch. Jetzt entscheidet die Kombination:

      * beide Komponenten markant und in DERSELBEN Richtung -> die pauschale
        Aussage ist gedeckt,
      * nur EINE markant (die andere unter ihrer Schwelle) -> die Zeile nennt
        genau diese Komponente mit ihren Zahlen,
      * beide markant, aber WIDERSPRUECHLICH -> gar kein Faktor. Eine gemischte
        Bilanz ist nicht "praegend"; lieber keine Zeile als eine irrefuehrende."""
    if not objectives:
        return None
    me_el = len((objectives.get("elites") or {}).get("me") or [])
    opp_el = len((objectives.get("elites") or {}).get("opp") or [])
    tw = objectives.get("towers") or {}
    mt, ot = tw.get("me", 0), tw.get("opp", 0)
    el_diff, tw_diff = me_el - opp_el, mt - ot
    el_marked = abs(el_diff) >= OBJECTIVE_DIFF_MIN
    tw_marked = abs(tw_diff) >= TOWER_DIFF_MIN
    if el_marked and tw_marked:
        if el_diff > 0 and tw_diff > 0:
            return "Eigene Objective-Kontrolle"
        if el_diff < 0 and tw_diff < 0:
            return "Objective-Kontrolle des Gegners"
        return None                      # gemischt -> kein praegender Faktor
    if el_marked:
        return (f"Eigene Elite-Objectives ({me_el}:{opp_el})" if el_diff > 0
                else f"Elite-Objectives des Gegners ({me_el}:{opp_el})")
    if tw_marked:
        return (f"Eigener Turm-Vorteil ({mt}:{ot})" if tw_diff > 0
                else f"Turm-Vorteil des Gegners ({mt}:{ot})")
    return None


def _verdict_outcome_line(win, outcome_known: bool, teamfights: list,
                          objectives: dict | None) -> str | None:
    """Zeile 1: Spiel-Ausgang + praegendster Faktor - Letzterer nur, wenn er
    nicht-tautologisch ist (s. `_defining_factor`). Ohne bekanntes Endergebnis
    (Live-/Dump-Pfad) bleibt nur der Faktor."""
    factor = _defining_factor(teamfights, objectives)
    if outcome_known:
        res = "Sieg" if win else "Niederlage"
        return f"{res}. Prägendster Faktor: {factor}." if factor else f"{res}."
    return f"Prägendster Faktor: {factor}." if factor else None


def _verdict_lane_line(me_player: dict, has_damage: bool) -> str | None:
    """Auffaelligste Phase auf der Rollen-Fokusmetrik gegen den Gegenpart
    (Vorsprung ODER Rueckstand, je nach Betrag). Key-frei faellt die Schaden-
    Fokusrolle auf Gold zurueck (kein Schaden -> Gold traegt die Deutung)."""
    deltas = me_player.get("deltas") or []
    role = me_player.get("role", "")
    focus = ROLE_FOCUS.get(role, "gold")
    if not has_damage and focus == "dmg":
        focus = "gold"
    strong = _phase_extreme(deltas, focus, +1)
    weak = _phase_extreme(deltas, focus, -1)
    if strong and weak:
        pick = strong if abs(strong[1]) >= abs(weak[1]) else weak
    else:
        pick = strong or weak
    if not pick:
        return None
    label, d = pick
    mlabel = METRIC_LABEL.get(focus, focus)
    # Subjekt = eigener Champ (nie implizit "du"); Bezug = benannter Gegenpart.
    me_champ = me_player.get("champ") or "Du"
    opp = (me_player.get("counterpart") or {}).get("champ")
    vs = f"gegen {opp}" if opp else "gegen den Rollen-Gegenpart"
    if d > 0:
        return (f"{me_champ}: stärkste Phase {vs} — "
                f"{label}, +{_fmt_int(d)} {mlabel}.")
    return (f"{me_champ}: größter Lane-Rückstand {vs} — "
            f"{label}, −{_fmt_int(abs(d))} {mlabel}.")


def _death_killer_part(me_player: dict, total: int) -> str | None:
    """Zusatz-Satz zur Killer-Verteilung: haeufigster Toeter der eigenen Tode.

    Nur wenn EIN Gegner mindestens DEATH_KILLER_SHARE der Tode verursacht hat und
    das absolut mindestens DEATH_KILLER_MIN Mal - sonst ist die Verteilung Zufall
    und keine Aussage. `death_by` (aus `death_killers`) ist eine absteigend
    sortierte Liste [(champ, anzahl), ...]. Neutral: nennt nur die Verteilung."""
    by = me_player.get("death_by") or []
    if not by or total <= 0:
        return None
    champ, cnt = by[0][0], by[0][1]
    if not champ or cnt < DEATH_KILLER_MIN or cnt / total < DEATH_KILLER_SHARE:
        return None
    return f"{cnt} von {total} Toden durch {champ}."


def _verdict_death_line(me_player: dict) -> str | None:
    """Todes-Muster: viele fruehe Tode ODER klare Teamfight-/Pick-Haeufung,
    ergaenzt um die Killer-Verteilung (ein Gegner traegt die Mehrheit der Tode).
    §2.4-konform (nur Muster, kein 'gefeedet')."""
    dp = me_player.get("deaths") or {}
    total = len(dp.get("times") or [])
    if total == 0:
        return None
    # Subjekt = eigener Champ (nicht implizit "du").
    me_champ = me_player.get("champ") or "Du"
    early = dp.get("early", 0)
    kind = me_player.get("death_kind") or {}
    tf = kind.get("teamfight", 0)
    pick = kind.get("pick", 0)
    pattern = None
    if early >= EARLY_DEATH_MIN:
        pattern = (f"{me_champ}: Todes-Muster — {early} der {total} Tode "
                   f"fielen vor Min 10.")
    elif tf >= DEATH_SKEW_MIN and tf / total >= DEATH_SKEW_SHARE:
        pattern = (f"{me_champ}: Todes-Muster — {tf} von {total} Toden "
                   f"in Teamfights.")
    elif pick >= DEATH_SKEW_MIN and pick / total >= DEATH_SKEW_SHARE:
        pattern = (f"{me_champ}: Todes-Muster — {pick} von {total} Toden "
                   f"abseits der Kämpfe (Picks).")
    killer = _death_killer_part(me_player, total)
    if pattern and killer:
        return f"{pattern} {killer}"
    if pattern:
        return pattern
    # Kein Muster, aber eine klare Killer-Verteilung -> die traegt die Zeile
    # allein (mit Subjekt, wie alle spieler-bezogenen Zeilen).
    return f"{me_champ}: {killer}" if killer else None


def role_quote_rows(impact: dict | None, scoreboard: list | None) -> list:
    """Je Rolle eine Zeile {role, pid, champ, opp_champ, quote} der Impact-Quote.

    EINE Quelle fuer alle quoten-basierten Verdikt-Zeilen (Rueckstand, Beitrag,
    Rollen-Differentiale): der Composite-Impact aus `impact_scores` (gespeist von
    den `impact_raw`-Rohwerten aus enrich/build_report) wird ueber
    `impact_quotes` **je Rollen-Paar** ins Verhaeltnis gesetzt - nichts wird
    doppelt gerechnet. Zeilen ohne bewertbare Quote (fehlender Gegenpart oder
    Gegenpart-Total 0) haben `quote=None`. Reihenfolge = Scoreboard-Reihenfolge
    (ROLE_ORDER)."""
    if not impact or not scoreboard:
        return []
    scores = impact.get("scores") or {}
    pairs = [(r["me"]["pid"] if r["me"] else None,
              r["opp"]["pid"] if r["opp"] else None) for r in scoreboard]
    quotes = impact_quotes(scores, pairs)
    rows = []
    for r in scoreboard:
        if not r.get("me"):
            continue
        pid = r["me"]["pid"]
        rows.append({"role": r.get("role") or "", "pid": pid,
                     "champ": r["me"].get("champ"),
                     "opp_champ": (r.get("opp") or {}).get("champ"),
                     "quote": quotes.get(pid)})
    return rows


def _impact_quote_lines(impact: dict | None, scoreboard: list | None,
                        me_pid) -> tuple:
    """(deficit_line, contribution_line, worst_pid) aus den Impact-Quoten.

    Quote = eigener Impact / Gegenpart-Impact (niedrigste Quote = groesster
    Rueckstand). VERBINDLICH nur relativ zum Rollen-Gegenpart - kein absoluter
    Impact-Total-Vergleich ueber Rollen hinweg. Self-bezogen, wenn der groesste
    Rueckstand der eigene Spieler ist. `worst_pid` (pid des groessten
    Rueckstands, sonst None) laesst den Aufrufer erkennen, ob die Rueckstand-
    Zeile den eigenen Spieler meint - nur dann bleibt sie neben der
    Rollen-Differential-Zeile stehen (sonst waere sie ein Duplikat)."""
    rows = role_quote_rows(impact, scoreboard)
    if not rows:
        return (None, None, None)
    champ_by_pid = {r["pid"]: r["champ"] for r in rows}
    # Gegenpart-Champ je eigenem Spieler, damit jede Quote-Zeile beide Namen
    # nennt (Quote = eigener Impact / Gegenpart-Impact).
    opp_champ_by_pid = {r["pid"]: r["opp_champ"] for r in rows if r["opp_champ"]}
    rated = [(r["pid"], r["quote"]) for r in rows if r["quote"] is not None]

    deficit, worst_pid = None, None
    below = [(pid, q) for pid, q in rated if q < QUOTE_LOW]
    if below:
        pid, q = min(below, key=lambda t: t[1])
        worst_pid = pid
        champ, pct = champ_by_pid.get(pid, "?"), round(q * 100)
        opp_champ = opp_champ_by_pid.get(pid)
        of = f" von {opp_champ}" if opp_champ else ""
        if pid == me_pid:
            gp = opp_champ or "den Gegenpart"
            deficit = f"{champ}: Impact-Rückstand zum Gegenpart {gp} — {pct} %."
        else:
            deficit = f"Größter Impact-Rückstand im Team: {champ} ({pct} %{of})."

    contribution = None
    above = [(pid, q) for pid, q in rated if q >= QUOTE_HIGH]
    if above:
        pid, q = max(above, key=lambda t: t[1])
        champ, pct = champ_by_pid.get(pid, "?"), round(q * 100)
        opp_champ = opp_champ_by_pid.get(pid)
        of = f" von {opp_champ}" if opp_champ else ""
        contribution = f"Stärkster Beitrag: {champ} ({pct} %{of})."
    return (deficit, contribution, worst_pid)


def _quote_part(row: dict) -> str:
    """'Jungle (Khazix 39 % von XinZhao)' - ein Rollen-Eintrag der
    Differential-Zeile. Ohne Gegenpart-Namen bleibt der 'von ...'-Teil weg."""
    label = ROLE_LABEL.get(row["role"], row["role"] or "?")
    pct = round((row["quote"] or 0) * 100)
    of = f" von {row['opp_champ']}" if row.get("opp_champ") else ""
    return f"{label} ({row['champ']} {pct} %{of})"


def _join_de(parts: list) -> str:
    """'A', 'A und B', 'A, B und C' - deutsche Aufzaehlung."""
    if len(parts) <= 1:
        return parts[0] if parts else ""
    return ", ".join(parts[:-1]) + " und " + parts[-1]


def _verdict_role_diff_line(rows: list) -> str | None:
    """Rollen-Differentiale: ALLE Rollen im Rueckstand + das groesste Plus.

    Team-Diagnose 2026-07-26 (Nutzer-Feedback: das Verdikt war zu stark auf den
    eigenen Spieler bezogen): stellt JEDE Rolle des eigenen Teams ihrem
    Rollen-Gegenpart gegenueber (Impact-Quote, s. `role_quote_rows`). Rein
    deskriptiv - Prozent des Gegenparts, keine Wertung.

    **Erweiterung 2026-07-26b:** vorher nannte die Zeile nur Minimum und
    Maximum. Das verschwieg, wenn MEHRERE Rollen hinten lagen - genau die
    Information, aus der ein Team etwas lernt. Jetzt werden alle Rollen unter
    QUOTE_LOW aufsteigend (schlechteste zuerst) als 'Minus' aufgezaehlt; das
    'Plus' bleibt bewusst der EINE Spitzenwert ab QUOTE_HIGH (das Verdikt soll
    Befunde nennen, keine Lobeslisten). Ohne Ausreisser nach unten oder oben
    entfaellt die Zeile - lieber nichts als Rauschen. Braucht mindestens zwei
    bewertbare Rollen (bei einer einzigen waere die Aussage ein Einzelvergleich,
    den die Rueckstand-Zeile schon traegt)."""
    rated = [r for r in rows if r.get("quote") is not None and r.get("champ")]
    if len(rated) < 2:
        return None
    lows = sorted((r for r in rated if r["quote"] < QUOTE_LOW),
                  key=lambda r: r["quote"])
    high = max(rated, key=lambda r: r["quote"])
    if high["quote"] < QUOTE_HIGH:
        high = None
    if not lows and high is None:
        return None
    parts = []
    if lows:
        parts.append("Minus " + _join_de([_quote_part(r) for r in lows]))
    if high is not None and all(high["pid"] != r["pid"] for r in lows):
        parts.append(f"größtes Plus {_quote_part(high)}")
    return "Rollen-Differentiale: " + ", ".join(parts) + "."


def _verdict_antiheal_line(antiheal: dict | None,
                           teamfights: list | None = None) -> str | None:
    """Antiheal-Befund: markante Gegner-Heilung + eigene Grievous-Wounds-Kaeufe.

    `antiheal` (vom Report-Pfad gebaut, s. postgame._antiheal_summary):
    {opp_heal, opp_top:{champ,heal}, duration_min, buys:[{minute,champ,item}]}.

    **Gating (Verschaerfung 2026-07-26b, Nutzer-Feedback "die Zeile ist zu
    trigger-freudig"):** ein Befund ist das nur, wenn ALLE DREI Bedingungen
    zutreffen (Schwellen + Kalibrierung s. Konstanten-Block oben):

      (a) Heil-Signal: Team-Heilung >= ANTIHEAL_HEAL_PER_MIN_HARD (p90) ODER ein
          EINZELNER Gegner >= ANTIHEAL_SOLO_PER_MIN (p95). Der Solo-Zweig faengt
          den haeufigen Fall ab, dass EIN unkuerzbarer Champion das Spiel
          traegt, waehrend die Team-Summe unauffaellig bleibt.
      (b) Versaeumnis: das eigene Team hat gar kein Antiheal gekauft oder erst
          nach ANTIHEAL_LATE_MIN. Wer frueh gekauft hat, hat die Lehre bereits
          gezogen - dann ist die Zeile nur Rauschen.
      (c) Wirkung: die Teamfight-Bilanz ist negativ (verlorene > gewonnene
          Fights). War sie es nicht, war die Heilung offenbar beherrschbar.

    Sie nennt die Summe, den staerksten Heiler und den ERSTEN eigenen
    Antiheal-Kauf - neutral, ohne Bewertung."""
    if not antiheal:
        return None
    total = antiheal.get("opp_heal", 0) or 0
    dur = float(antiheal.get("duration_min", 0) or 0)
    if total <= 0 or dur <= 0:
        return None
    top = antiheal.get("opp_top") or {}
    # (a) Heil-Signal - Team-Summe ODER ein einzelner Gegner.
    solo = float(top.get("heal", 0) or 0) / dur
    if ((total / dur) < ANTIHEAL_HEAL_PER_MIN_HARD
            and solo < ANTIHEAL_SOLO_PER_MIN):
        return None
    # (b) Versaeumnis - kein eigenes Antiheal oder erst spaet.
    buys = antiheal.get("buys") or []
    if buys and float(buys[0].get("minute", 0) or 0) < ANTIHEAL_LATE_MIN:
        return None
    # (c) Wirkung - ohne negative Fight-Bilanz war die Heilung beherrschbar.
    won, lost, decisive = _teamfight_balance(teamfights or [])
    if decisive < TEAMFIGHT_MIN or lost <= won:
        return None
    head = f"Gegner-Heilung {_fmt_int(total)}"
    if top.get("champ"):
        head += f" (Spitze {top['champ']} {_fmt_int(top.get('heal', 0))})"
    if not buys:
        return head + " — kein Antiheal im eigenen Team."
    first = buys[0]
    buyers = {b.get("champ") for b in buys if b.get("champ")}
    tail = ("sonst kein weiterer Käufer" if len(buyers) <= 1
            else f"{len(buyers)} Käufer insgesamt")
    return (f"{head} — erstes Antiheal Min {float(first['minute']):.0f} "
            f"({first.get('champ') or '?'}), {tail}.")


def _verdict_teamfight_line(teamfights: list,
                            team_series: dict | None = None) -> str | None:
    """Teamfight-Bilanz mit Kipp-Punkt (nur bei entschiedener Uebermacht).

    Der Kipp-Punkt kommt aus `teamfight_tipping_minute` - derselben Quelle, die
    auch das `tip`-Flag der Fight-Karten setzt (kein Drift). Ohne Kipp-Punkt
    bleibt die Bilanz-Zeile fuer sich stehen; die Ersatz-Aussage liefert
    `_verdict_oneside_line`."""
    if not teamfights:
        return None
    won, lost, decisive = _teamfight_balance(teamfights)
    if decisive < TEAMFIGHT_MIN or won == lost:
        return None
    if lost > won:
        line = f"{lost} von {decisive} großen Teamfights verloren"
    else:
        line = f"{won} von {decisive} großen Teamfights gewonnen"
    dec = tipping_decision(teamfights, team_series)
    if dec and dec["kind"] == "tip":
        line += f"; Kipp-Punkt bei Min {dec['minute']:.0f}"
    elif dec and dec["kind"] == "swing":
        # Kein tragfaehiger Kipp-Punkt: die Fuehrung wechselte danach noch
        # mehrfach (s. tipping_decision) - dann nennen wir den letzten
        # entscheidenden Fight statt einer erfundenen Wende.
        line += (f"; mehrere Führungswechsel — entscheidender Fight bei "
                 f"Min {dec['minute']:.0f}")
    return line + "."


def _verdict_oneside_line(teamfights: list,
                          team_series: dict | None = None) -> str | None:
    """Ersatz-Aussage fuer Spiele OHNE Kipp-Punkt: ab wann war es einseitig?

    Greift in zwei Faellen:

    1. Kein einziger Fight erfuellt die Kipp-Punkt-Regel (`_tipping_candidate` -
       bewusst ohne die Verdikt-Kopplung, sonst behauptete die Zeile bei
       ausgeglichener Fight-Bilanz faelschlich 'kein Kipp-Punkt').
    2. **Neu 2026-07-26b:** `tipping_decision` hat den Kandidaten selbst als
       "durchgehend" entwertet (`kind="oneside"`), weil das am Ende unterlegene
       Team vorher nie gefuehrt hat - dann waere "Kipp-Punkt bei Min X" falsch
       erzaehlt (s. dort).

    In beiden Faellen zusaetzlich noetig: das Spiel war dauerhaft einseitig
    (`oneside_minute`)."""
    dec = tipping_decision(teamfights or [], team_series)
    if dec is not None:
        if dec["kind"] != "oneside":
            return None                      # tip/swing tragen die Aussage schon
    elif _tipping_candidate(teamfights or [], team_series) is not None:
        return None
    one = oneside_minute(team_series)
    if not one:
        return None
    where = "vorn" if one["ahead"] else "hinten"
    what = "Vorsprung" if one["ahead"] else "Rückstand"
    label = _TIP_METRIC_LABEL.get(one["metric"], "Team-Differenz")
    # 'Kein Kipp-Punkt' nur, wenn es ueberhaupt entschiedene Fights gab, die
    # einer haette sein koennen.
    decisive = _teamfight_balance(teamfights or [])[2]
    head = "Kein Kipp-Punkt — durchgehend" if decisive else "Durchgehend"
    return (f"{head} {where} ab Min {one['minute']:.0f} "
            f"({label}-{what} nie wieder unter {_fmt_int(one['thr'])}).")


def _elite_key(e: dict) -> str:
    """Zaehl-Schluessel eines Elite-Monsters fuer die Bilanz.

    Wie `monsterType`, mit EINER Ausnahme: der Elder-Drache (DRAGON mit
    monsterSubType ELDER_DRAGON) bekommt den eigenen Schluessel 'ELDER'. Ein
    Elder ist spielentscheidend, ein Wasserdrache nicht - sie in einer Zahl zu
    mischen verschweigt genau das (Nutzer-Befund 2026-07-26)."""
    mon = e.get("monster")
    if mon == "DRAGON" and e.get("subtype") == ELDER_SUBTYPE:
        return "ELDER"
    return mon


def _verdict_objective_line(objectives: dict | None) -> str | None:
    """Objective-Bilanz (Drachen/Elder/Grubs/Herold/Baron + Tuerme) eigenes vs.
    Gegner-Team - nur bei markanter Differenz."""
    if not objectives:
        return None
    elites = objectives.get("elites") or {}
    me_el, opp_el = elites.get("me") or [], elites.get("opp") or []
    towers = objectives.get("towers") or {}
    mt, ot = towers.get("me", 0), towers.get("opp", 0)
    if (abs(len(me_el) - len(opp_el)) < OBJECTIVE_DIFF_MIN
            and abs(mt - ot) < TOWER_DIFF_MIN):
        return None
    counts: dict = {}
    for side, lst in (("me", me_el), ("opp", opp_el)):
        for e in lst:
            counts.setdefault(_elite_key(e), {"me": 0, "opp": 0})[side] += 1
    parts = []
    # Bekannte Monster in fester Reihenfolge, danach unbekannte (future-proof).
    order = list(_MONSTER_LABEL) + [m for m in counts if m not in _MONSTER_LABEL]
    for mon in order:
        c = counts.get(mon)
        if c and (c["me"] or c["opp"]):
            lab = _MONSTER_LABEL.get(mon, (mon or "?").title())
            parts.append(f"{lab} {c['me']}:{c['opp']}")
    parts.append(f"Türme {mt}:{ot}")
    return "Objective-Bilanz: " + ", ".join(parts) + "."


def _verdict_unconverted_line(objectives: dict | None) -> str | None:
    """Barone/Elder, die binnen BUFF_CONVERT_S folgenlos blieben (beide Teams).

    Team-Diagnose 2026-07-26: 'Baron 2:3' sagt nichts darueber, was aus den
    Buffs wurde. `objectives['unconverted']` (s. `unconverted_buffs`) liefert je
    Team, wie viele der genommenen Baron-/Elder-Buffs weder in Turm/Inhibitor
    noch in ein Kill-Plus muendeten. Die Zeile erscheint nur, wenn ueberhaupt
    Buffs fielen UND mindestens einer folgenlos blieb - sonst gibt es nichts zu
    berichten."""
    unc = (objectives or {}).get("unconverted") or {}
    me, opp = unc.get("me") or {}, unc.get("opp") or {}
    m_tot, o_tot = me.get("total", 0), opp.get("total", 0)
    m_unc, o_unc = me.get("unconverted", 0), opp.get("unconverted", 0)
    if (m_tot + o_tot) <= 0 or (m_unc + o_unc) <= 0:
        return None
    return (f"Baron/Elder ohne Folge: eigenes Team {m_unc} von {m_tot}, "
            f"Gegner {o_unc} von {o_tot} (binnen "
            f"{int(BUFF_CONVERT_S // 60)} min kein Turm/Inhibitor und kein "
            f"Kill-Plus).")


def _verdict_build_line(me_player: dict) -> str | None:
    """Auffaelligster Build-Befund: markant verspaetetes Kern-Item (Timing vs.
    Gegenpart) ODER deutlich niedrige Engine-Konformitaet (Engine-Check)."""
    me_champ = me_player.get("champ") or "Du"
    opp_champ = (me_player.get("counterpart") or {}).get("champ")
    gp = opp_champ or "dem Gegenpart"
    be = me_player.get("build_eval") or {}
    behind = [t for t in (be.get("timing") or [])
              if t.get("behind") and t.get("opp") is not None]
    if behind:
        worst = max(behind, key=lambda t: t["mine"] - t["opp"])
        gap = worst["mine"] - worst["opp"]
        return (f"{me_champ}: Build-Timing — {worst['n']}. Kern-Item "
                f"{_fmt_int(gap)} min nach {gp} fertig.")
    br = me_player.get("build_replay") or {}
    if br.get("evaluable"):
        sc = br.get("score") or {}
        total, hits = sc.get("total", 0), sc.get("hits", 0)
        if total >= 3 and hits / total <= BUILD_HIT_LOW:
            return (f"{me_champ}: Build-Check — nur {hits} von {total} "
                    f"Käufen engine-konform.")
    return None


def _verdict_vision_line(me_player: dict) -> str | None:
    """Vision-Befund nur, wenn der Ward-Aktions-Rueckstand ueber alle Phasen
    markant ist (deutlich unter dem Gegenpart)."""
    total, seen = 0, False
    for ph in me_player.get("deltas") or []:
        d = ph["metrics"].get("vision", {}).get("delta")
        if d is None:
            continue
        seen = True
        total += d
    if not seen or total > -VISION_DEFICIT_MIN:
        return None
    me_champ = me_player.get("champ") or "Du"
    opp_champ = (me_player.get("counterpart") or {}).get("champ")
    gp = opp_champ or "dem Gegenpart"
    return (f"{me_champ}: Vision klar unter {gp} "
            f"({_fmt_int(abs(total))} Ward-Aktionen weniger).")


def verdict(me_player: dict, team: list, *, win=None, outcome_known: bool = True,
            objectives: dict | None = None, teamfights: list | None = None,
            impact: dict | None = None, scoreboard: list | None = None,
            has_damage: bool = True, team_series: dict | None = None,
            antiheal: dict | None = None) -> dict:
    """Regelbasiertes Auto-Verdikt als Liste kurzer, eigenstaendiger Befund-Zeilen
    (wichtigster zuerst), gerendert je auf eigener Zeile.

    Kandidaten (nur aufgenommen, was die Daten hergeben; Schwellen als Konstanten
    oben): Spiel-Ausgang + praegendster Faktor (nur wenn nicht-tautologisch),
    Teamfight-Bilanz (mit Kipp-Punkt bzw. Fuehrungswechsel-Befund), die
    Warum-Zeile zu den verlorenen Fights, Rollen-Differentiale, Objective-Bilanz,
    folgenlose Baron-/Elder-Buffs, Antiheal-Befund, eigene Lane-Phase vs.
    Gegenpart, Todes-Muster + Killer-Verteilung, Impact-Quote-Rueckstand,
    Build-Befund, Vision-Befund. `has_damage=False` (key-frei) laesst die
    schaden-/impact-basierten Zeilen ersatzlos weg (kein Platzhalter).
    §2.4-konform - Beitraege NUR relativ zum Rollen-Gegenpart (Quote), nie als
    absoluter Rollen-uebergreifender Vergleich.

    Reihenfolge (Team-Diagnose-Ausbau 2026-07-26): erst die TEAM-Befunde
    (warum lief das Spiel so?), dann die spieler-bezogenen. Nutzer-Feedback: bei
    langen Spielen mit Fuehrungswechseln verfehlte das stark me-zentrierte
    Verdikt die Team-Story.

    `win`/`outcome_known`: Endergebnis; der Live-/Dump-Pfad kennt keins ->
    `outcome_known=False`. `objectives`/`teamfights`/`impact`/`scoreboard`
    stammen aus dem fertigen Report-Modell. `team_series` (Modell-Feld
    `team_series`, key-frei) traegt die Kipp-Punkt-Pruefung: ohne sie gibt es
    keinen Kipp-Punkt und keine Einseitig-Zeile. `antiheal` (nur der
    Timeline-Pfad hat die Heilungs-Endwerte) traegt den Antiheal-Befund.
    Rueckgabe {lines: [...]}."""
    teamfights = teamfights or []
    me_pid = me_player.get("pid")
    deficit, contribution, role_diff = (None, None, None)
    if has_damage:
        deficit, contribution, worst_pid = _impact_quote_lines(
            impact, scoreboard, me_pid)
        role_diff = _verdict_role_diff_line(role_quote_rows(impact, scoreboard))
        if role_diff:
            # Die Differential-Zeile nennt Minus UND Plus bereits mit Rolle,
            # Champ und Gegenpart - der separate Beitrags-Satz waere wortgleich.
            # Die Rueckstand-Zeile bleibt nur, wenn sie den EIGENEN Spieler
            # meint (andere Formulierung, eigener Erkenntniswert).
            contribution = None
            if worst_pid != me_pid:
                deficit = None

    # Reihenfolge = Prioritaet (wichtigster Befund zuerst): Kopf, dann
    # Team-Befunde, dann Spieler-Befunde. Die Dreiteilung traegt die
    # Slot-Reservierung (s. VERDICT_MIN_PLAYER_LINES) - ohne sie schnitt der Cap
    # die Spieler-Befunde bei team-lastigen Spielen komplett weg.
    head = [
        _verdict_outcome_line(win, outcome_known, teamfights, objectives),
        # Fight-Bilanz + Kipp-Punkt: die eine Zeile, die das Spiel einordnet.
        _verdict_teamfight_line(teamfights, team_series),
    ]
    team_block = [
        # Ersatz fuer den Kipp-Punkt-Teil bei einseitigen Spielen (Stomp).
        _verdict_oneside_line(teamfights, team_series),
        # WARUM liefen die Fights nicht? (ersetzt den tautologischen Faktor)
        _verdict_teamfight_reason_line(teamfights),
        role_diff,
        _verdict_objective_line(objectives),
        _verdict_unconverted_line(objectives),
        _verdict_antiheal_line(antiheal, teamfights),
    ]
    player_block = [
        _verdict_lane_line(me_player, has_damage),
        _verdict_death_line(me_player),
        deficit,
        contribution,
        _verdict_build_line(me_player),
        _verdict_vision_line(me_player),
    ]
    head = [c for c in head if c]
    team_block = [c for c in team_block if c]
    player_block = [c for c in player_block if c]

    # Der Kopf hat Vorrang, danach wird der Team-Block auf das gekappt, was nach
    # der Spieler-Reservierung uebrig bleibt; die Spieler-Zeilen fuellen den Rest
    # (und mehr, wenn der Team-Block kuerzer ist als sein Budget).
    budget = max(0, VERDICT_MAX_LINES - len(head))
    reserved = min(len(player_block), VERDICT_MIN_PLAYER_LINES)
    lines = head + team_block[:max(0, budget - reserved)]
    lines += player_block[:max(0, VERDICT_MAX_LINES - len(lines))]
    if not lines:
        lines = ["Zu wenige Signale für ein belastbares Verdikt."]
    return {"lines": lines}


# ============================================================================
# Phase 4b — zusaetzliche Statistik-Sektionen (s. plan_postgame.md §8/§8b).
# Alle Funktionen bleiben reine Logik (offline testbar); die DD-/KB-abhaengigen
# Bausteine (Comp-Priors, Item-Namen/-Gold) werden vom Aufrufer vorbereitet und
# hier nur noch verrechnet - so laufen die Unit-Tests ohne Netz/Static-Cache.
# ============================================================================

def _ts_ms(ev) -> float:
    """Zeitstempel eines Kill-/Objective-Events in Millisekunden.
    Timeline traegt `ts` (ms) direkt, der Live-Dump minute·60000 - beide Pfade
    liefern `minute`, daher der Fallback."""
    ts = ev.get("ts")
    return float(ts) if ts is not None else float(ev.get("minute", 0.0)) * 60000.0


# --- Sektion 1: Schaden-Vergleich je Phase (nur has_damage) -----------------

def phase_gain_pairs(me_seq: list, opp_seq: list | None, n: int) -> list:
    """Phasen-Zuwaechse einer kumulativen Serie (z. B. Schaden) me vs. opp.

    Fuer jede PHASE (Early/Mid/Late) der Zuwachs `seq[ende]-seq[start]` (ueber
    `_cum_gain`, gleiche Fensterlogik wie die Delta-Engine). `opp_seq` None/leer
    -> nur eigene Werte. Rueckgabe je Phase {key, label, me, opp, delta}. Basis
    fuer den Early/Mid/End-Schaden-Vergleich (Ich vs. Gegenpart UND Team vs.
    Team)."""
    out = []
    for key, label, a, b in PHASES:
        mine = _cum_gain(me_seq, a, b, n)
        if not opp_seq:
            out.append({"key": key, "label": label, "me": mine, "opp": None,
                        "delta": None})
        else:
            other = _cum_gain(opp_seq, a, b, n)
            out.append({"key": key, "label": label, "me": mine, "opp": other,
                        "delta": mine - other})
    return out


# --- Sektion 2: Composite-Impact-Score (nur mit impact_raw) ------------------

# Gewichtung der drei "grossen" Impact-Komponenten (Startwert 1:1:1, als
# Konstante aenderbar - s. plan_postgame.md §8): Impact = Schaden + Heilung/Shield
# + getankter (abgefangener) Schaden. Loest die Support/Tank-Unfairness
# (Brand-vs-Soraka), weil Heilen/Tanken gleichwertig zaehlt.
IMPACT_WEIGHTS = {"damage": 1.0, "healShield": 1.0, "tanked": 1.0}

# Utility-Komponenten (Erweiterung 2026-07-24): Kits wie Zilean (Stasis/Revive/
# CC/Utility) tauchen in KEINER der drei grossen Komponenten auf und landeten
# trotz Top-Vision/hoher KP auf dem letzten Impact-Platz. Darum zwei zusaetzliche,
# key-gebundene (Match-Summary) Bonusse - beide tunebar:
#
#   * IMPACT_SAVE_BONUS: Gold-Aequivalent je `challenges.saveAllyFromDeath`.
#     ~2000 = typische Champion-HP im Midgame. Die ECHTEN HP des Geretteten
#     stehen NICHT in den Daten - der Bonus ist bewusst eine Pauschale.
#   * IMPACT_CC_PER_SECOND: Gewicht je Sekunde `timeCCingOthers`. 150 gibt einem
#     40-60s-CC-Tank/Support ~6-9k - spuerbar, aber nicht dominierend gegenueber
#     den 25-60k-Gesamtwerten der Carrys.
IMPACT_SAVE_BONUS = 2000
IMPACT_CC_PER_SECOND = 150


def impact_scores(impact_raw: dict, weights: dict | None = None) -> dict:
    """Composite-Impact je pid aus den Rohwerten.

    `impact_raw`: {pid: {damage, healShield, tanked, saves, cc_s}} (aus der
    Match-Summary, s. enrich/build_report; `saves`/`cc_s` fehlen bei aelteren
    Rohwerten -> 0). Rueckgabe {pid: {damage, healShield, tanked, utility, saves,
    cc_s, total}} mit `total` = Σ Gewicht·grosse-Komponente + Utility-Bonus. Die
    Einzelkomponenten bleiben erhalten, damit der Renderer sie als Segmente im
    Balken (inkl. Utility) und die Save-Zahl als Chip zeigen kann."""
    w = weights or IMPACT_WEIGHTS
    out: dict = {}
    for pid, c in impact_raw.items():
        d = c.get("damage", 0) or 0
        h = c.get("healShield", 0) or 0
        t = c.get("tanked", 0) or 0
        saves = c.get("saves", 0) or 0
        cc_s = c.get("cc_s", 0) or 0
        # Utility als EIN Segment (Saves + CC zusammengefasst); die Einzelwerte
        # bleiben fuer den Save-Chip erhalten.
        utility = round(saves * IMPACT_SAVE_BONUS + cc_s * IMPACT_CC_PER_SECOND)
        out[pid] = {"damage": d, "healShield": h, "tanked": t,
                    "utility": utility, "saves": saves, "cc_s": cc_s,
                    "total": round(w["damage"] * d + w["healShield"] * h
                                   + w["tanked"] * t + utility)}
    return out


# Impact-Serien mit MINUTEN-Aufloesung (Erweiterung 2026-07-25): nur diese drei
# liegen in den Timeline-`participantFrames` je Minute vor. Heilung/Shield und
# Saves stehen ausschliesslich als Match-Endwert zur Verfuegung und fliessen
# darum NICHT in die Phasen-Balken ein (Fussnote im Renderer). `dmg_taken` ist
# der ROHE erlittene Schaden (nicht damageSelfMitigated).
#
# Das Gewicht je Serie entspricht dem Gesamt-Score (`impact_scores`): Schaden und
# Erlitten 1:1, CC-Sekunden mit IMPACT_CC_PER_SECOND. So ist der Phasenwert
# dieselbe Groesse wie der Gesamt-Balken darueber, nur auf ein Zeitfenster
# eingeschraenkt (Nutzer-Feedback 2026-07-25: EINE gemergte Phasen-Gruppe statt
# drei Komponenten-Gruppen).
IMPACT_PHASE_SERIES = (("dmg", 1.0), ("dmg_taken", 1.0),
                       ("cc_s", float(IMPACT_CC_PER_SECOND)))

# Label der gemergten Phasen-Gruppe (Renderer-Kopfzeile).
IMPACT_PHASE_LABEL = "Impact-Phasen (Schaden+Erlitten+CC)"


def _has_signal(seq: list) -> bool:
    """True, wenn eine kumulative Serie ueberhaupt etwas gemessen hat (letzter
    Wert > 0). Faengt Timelines ohne das jeweilige Feld ab (Serie voller Nullen)."""
    return bool(seq) and (seq[-1] or 0) > 0


def _impact_combined_series(player: dict) -> list:
    """Gewichtete Summe der minuten-aufgeloesten Impact-Serien eines Spielers.

    Alle Quellserien sind kumulativ -> die Summe ist es auch. Fehlende/leere
    Serien zaehlen als 0, kuerzere werden mit ihrem letzten Wert fortgeschrieben
    (kumulativ = konstant nach Spielende). Rueckgabe [] wenn KEINE Serie ein
    Signal traegt - dann gibt es fuer diesen Spieler nichts zu vergleichen."""
    seqs = [(player.get(key) or [], w) for key, w in IMPACT_PHASE_SERIES]
    if not any(_has_signal(s) for s, _w in seqs):
        return []
    length = max((len(s) for s, _w in seqs), default=0)
    out = []
    for i in range(length):
        total = 0.0
        for s, w in seqs:
            if not s:
                continue
            total += w * ((s[i] if i < len(s) else s[-1]) or 0)
        # Ganzzahlig wie der Gesamt-Impact (CC-Gewicht macht sonst Floats).
        out.append(round(total))
    return out


def impact_phase_rows(ser: dict, pid: int, opp: int | None) -> list | None:
    """Early/Mid/Late-Zuwaechse des KOMBINIERTEN Impacts (Sup vs. Sup).

    Je Phase EIN Wert pro Spieler = Schaden + erlittener Schaden + CC-Sekunden ·
    IMPACT_CC_PER_SECOND (s. IMPACT_PHASE_SERIES) - dieselbe Merge-Logik wie im
    Gesamt-Balken der Kachel, nur je Zeitfenster. Fensterlogik ueber
    `phase_gain_pairs` (identisch zur Delta-Engine).

    Rueckgabe: Zeilenliste {key, label, me, opp, delta} (early/mid/late) oder
    **None**, wenn keine der drei Serien Daten hergibt (key-freier Pfad ohne
    Anreicherung -> Kachel zeigt nur die Gesamt-Balken). Ohne Gegenpart-Serie
    bleiben `opp`/`delta` None."""
    players = ser["players"]
    n = ser["n_frames"]
    me_seq = _impact_combined_series(players.get(pid, {}))
    op = players.get(opp, {}) if opp is not None else {}
    opp_seq = _impact_combined_series(op)
    if not (me_seq or opp_seq):
        return None
    return phase_gain_pairs(me_seq, opp_seq, n)


def impact_quotes(scores: dict, pairs: list) -> dict:
    """Rollen-faire Impact-Quote je Paar: eigener Total / Gegenpart-Total.

    Absolute Impact-Punkte sind quer ueber Rollen NICHT vergleichbar (ein
    Support erreicht nie Carry-Punkte) - die Quote setzt jeden Spieler nur zu
    seinem gleichrolligen Gegenpart ins Verhaeltnis (Feedback 2026-07-24). Sie
    ist eine reine Darstellungs-/Bewertungsschicht ueber `impact_scores`, ohne
    die Formel/Konstanten zu beruehren.

    `scores`: {pid: {..., total}} aus `impact_scores`. `pairs`: Liste von
    (me_pid, opp_pid) (i. d. R. aus dem Scoreboard; opp_pid darf None sein).
    Rueckgabe {me_pid: quote|None} - `None`, wenn der Gegenpart fehlt, ein
    Score-Eintrag fehlt oder der Gegenpart-Total 0 ist (Division nicht
    definiert). Paare ohne me_pid werden uebersprungen."""
    out: dict = {}
    for me_pid, opp_pid in pairs:
        if me_pid is None:
            continue
        me_s = scores.get(me_pid)
        opp_s = scores.get(opp_pid) if opp_pid is not None else None
        if me_s is None or opp_s is None:
            out[me_pid] = None
            continue
        opp_total = opp_s.get("total", 0) or 0
        me_total = me_s.get("total", 0) or 0
        out[me_pid] = (me_total / opp_total) if opp_total else None
    return out


# --- Sektion 3: Comp-Diagnose beider Teams (key-frei, Schicht-0-Referenz) ----

def comp_diagnosis(sides: dict, champ_info: dict) -> dict:
    """AD/AP-Split, Frontline-Zaehler und Team-CC-Score beider Comps + Befund.

    `sides`: {"me": [champ, ...], "opp": [champ, ...]} (Champion-Namen je Team).
    `champ_info`: {champ: {"ad_share": float|None, "frontline": bool,
    "cc": float|None}} - vom Aufrufer aus `engine/champions.py` (prior/bucket/cc)
    vorbereitet, damit diese Funktion netz-/static-frei bleibt. Champions ohne
    ad_share/cc-Prior fliessen (wie `damage_bucket`/`team_cc_score`) NICHT ins
    jeweilige Mittel ein. Rueckgabe {me, opp, verdict}."""
    def _side(champs):
        infos = [champ_info.get(c, {}) for c in champs]
        ad_shares = [i["ad_share"] for i in infos if i.get("ad_share") is not None]
        ccs = [i["cc"] for i in infos if i.get("cc") is not None]
        front = sum(1 for i in infos if i.get("frontline"))
        ad = (sum(ad_shares) / len(ad_shares)) if ad_shares else None
        cc = (sum(ccs) / len(ccs)) if ccs else None
        if ad is None:
            label = "unbekannt"
        elif ad >= 0.6:
            label = "AD-lastig"
        elif ad <= 0.4:
            label = "AP-lastig"
        else:
            label = "gemischt"
        return {"ad_pct": round(ad * 100) if ad is not None else None,
                "ap_pct": round((1 - ad) * 100) if ad is not None else None,
                "dmg_label": label, "frontline": front,
                "cc": round(cc, 2) if cc is not None else None}

    me, opp = _side(sides.get("me", [])), _side(sides.get("opp", []))
    return {"me": me, "opp": opp, "verdict": _comp_verdict(me, opp)}


def _comp_verdict(me: dict, opp: dict) -> str:
    """Kurzer regelbasierter Befund-Satz zur Comp-Diagnose (Frontline zuerst,
    dann Schadenstyp, dann CC)."""
    parts = []
    mf, of = me["frontline"], opp["frontline"]
    if mf == 0 and of > 0:
        parts.append(f"Eure Comp hatte keine Frontline; Gegner {of} Frontliner.")
    elif of == 0 and mf > 0:
        parts.append(f"Ihr hattet {mf} Frontliner, der Gegner keine.")
    elif mf != of:
        who = "Ihr" if mf > of else "Der Gegner"
        parts.append(f"{who} mit mehr Frontline ({mf} zu {of}).")
    else:
        parts.append(f"Beide Comps mit {mf} Frontliner(n).")
    if me["dmg_label"] in ("AD-lastig", "AP-lastig"):
        if me["dmg_label"] == opp["dmg_label"]:
            parts.append(f"Beide Teams {me['dmg_label']} — einseitige "
                         f"Resistenzen greifen.")
        else:
            parts.append(f"Euer Schaden {me['dmg_label']} — leichter zu kontern.")
    if me["cc"] is not None and opp["cc"] is not None and abs(me["cc"] - opp["cc"]) >= 0.3:
        who = "Ihr" if me["cc"] > opp["cc"] else "Der Gegner"
        parts.append(f"{who} mit spuerbar mehr CC.")
    return " ".join(parts)


# --- Sektion 4: Teamfight-Erkennung (key-frei) ------------------------------

def detect_teamfights(kills: list, pid_team: dict, my_team: int, *,
                      gap_s: float = 20.0, min_kills: int = 3) -> list:
    """Kill-Cluster (Teamfights) im Event-Strom.

    Zusammenhaengende Kills mit <= `gap_s` Sekunden Abstand bilden ein Cluster;
    ein Cluster mit >= `min_kills` Kills gilt als Teamfight. Je Fight das
    Ergebnis aus Sicht des eigenen Teams (Kills eigenes vs. gegnerisches Team ->
    gewonnen/verloren/neutral), Startminute, beteiligte pids (Killer+Opfer+
    Assists) und das Zeitfenster (ts_start/ts_end in ms fuer `classify_deaths`).
    Key-frei: der Kill-Strom liegt in beiden Datenpfaden vor."""
    evs = sorted(kills, key=_ts_ms)
    other = 200 if my_team == 100 else 100
    clusters, cur = [], []
    for k in evs:
        if cur and _ts_ms(k) - _ts_ms(cur[-1]) > gap_s * 1000:
            clusters.append(cur)
            cur = []
        cur.append(k)
    if cur:
        clusters.append(cur)

    out = []
    for cl in clusters:
        if len(cl) < min_kills:
            continue
        my_k = sum(1 for k in cl if pid_team.get(k.get("killer")) == my_team)
        opp_k = sum(1 for k in cl if pid_team.get(k.get("killer")) == other)
        result = ("gewonnen" if my_k > opp_k
                  else "verloren" if my_k < opp_k else "neutral")
        pids = set()
        for k in cl:
            for p in [k.get("killer"), k.get("victim"), *(k.get("assists") or [])]:
                if p is not None:
                    pids.add(p)
        # Gefallene des Fights (Opfer der Cluster-Kills) - fuer die Durchstreich-
        # Markierung im Renderer.
        victims = sorted({k.get("victim") for k in cl
                          if k.get("victim") is not None})
        ts_start = min(_ts_ms(k) for k in cl)
        # Opfer des ERSTEN Kills im Cluster - wer den Fight eroeffnet hat, indem
        # er als Erster fiel (Basis des Faktors "Eroeffnung verloren",
        # s. `teamfight_reasons`). `cl` ist bereits zeitlich sortiert.
        first_victim = cl[0].get("victim")
        out.append({"minute": round(ts_start / 60000.0, 1),
                    "my_kills": my_k, "opp_kills": opp_k, "result": result,
                    "pids": sorted(pids), "victims": victims,
                    "first_victim": first_victim,
                    "ts_start": ts_start,
                    "ts_end": max(_ts_ms(k) for k in cl)})
    return out


# Isolations-Fenster der Pick-Erkennung (Schaerfung 2026-07-26): ein Solo-Kill
# ohne Assists, um den herum in +/- diesem Fenster KEIN weiterer Champion-Kill
# faellt, ist ein Pick - auch wenn er in ein Cluster-/Objective-Fenster faellt.
#
# **Wirkungs-Analyse (wichtig, bevor jemand daran dreht):** mit den DEFAULTS von
# `detect_teamfights` (gap_s=20, min_kills=3) kann diese Ausnahme nicht greifen -
# jeder Tod INNERHALB eines Cluster-Fensters ist per Konstruktion selbst ein
# Cluster-Kill und hat damit einen Nachbar-Kill <= gap_s (20 s) neben sich, also
# weit innerhalb von 60 s. Nachgemessen an 40 zufaelligen 16.14-Timelines
# (400 Spieler): 0 Umklassifizierungen; auch im Referenz-Spiel EUW1_7929841225
# bleibt es bei 7 Teamfight-/2 Pick-Toden fuer Ezreal. Die Regel ist damit
# ausdruecklich eine Absicherung fuer LOSERE Cluster-Parameter (gap_s > 60 oder
# ein grob gezogenes Objective-Fenster), nicht die Korrektur einzelner
# Solo-Tode mitten im Fight. Wer Letzteres will, braucht ein anderes Kriterium
# (z. B. "Solo-Kill ohne Assists zaehlt immer als Pick") - das ist eine
# fachliche Entscheidung, keine Feinjustage dieser Konstante.
PICK_ISOLATION_S = 60.0


def _is_isolated_solo_death(kill: dict, all_kills: list) -> bool:
    """True, wenn `kill` ein Solo-Kill (0 Assists) ohne weitere Champion-Kills
    in +/- PICK_ISOLATION_S ist - also unstrittig ein Pick."""
    if kill.get("assists"):
        return False
    ts = _ts_ms(kill)
    for other in all_kills:
        if other is kill:
            continue
        if abs(_ts_ms(other) - ts) <= PICK_ISOLATION_S * 1000:
            return False
    return True


def classify_deaths(kills: list, pid: int, clusters: list) -> dict:
    """Tode eines Spielers in Teamfight- vs. Pick-Tode aufteilen.

    Ein Tod zaehlt als Teamfight, wenn sein Zeitpunkt in eines der Cluster-
    Zeitfenster (`ts_start..ts_end` aus `detect_teamfights`) faellt, sonst als
    Pick. **Ausnahme (2026-07-26):** ein isolierter Solo-Kill (0 Assists, kein
    weiterer Champion-Kill in +/- PICK_ISOLATION_S) zaehlt IMMER als Pick, auch
    innerhalb eines Cluster-Fensters - er ist keine Teamfight-Beteiligung.
    Rueckgabe {teamfight, pick}."""
    windows = [(c["ts_start"], c["ts_end"]) for c in clusters]
    tf = pick = 0
    for k in kills:
        if k.get("victim") != pid:
            continue
        ts = _ts_ms(k)
        in_fight = any(a <= ts <= b for a, b in windows)
        if in_fight and not _is_isolated_solo_death(k, kills):
            tf += 1
        else:
            pick += 1
    return {"teamfight": tf, "pick": pick}


def death_killers(kills: list, pid: int, champ_by_pid: dict) -> list:
    """Verteilung der Toeter eines Spielers: [(champ, anzahl), ...] absteigend.

    Nur der KILLER zaehlt (Assists nicht) - die Frage ist "wer hat mich
    wiederholt erwischt?". Bei Gleichstand entscheidet der Champion-Name, damit
    die Ausgabe deterministisch bleibt. Unbekannte/fehlende Killer-pids
    (Exekutionen durch Tuerme/Monster haben killerId 0) bleiben aussen vor."""
    counts: dict = {}
    for k in kills:
        if k.get("victim") != pid:
            continue
        champ = champ_by_pid.get(k.get("killer"))
        if not champ:
            continue
        counts[champ] = counts.get(champ, 0) + 1
    return sorted(counts.items(), key=lambda t: (-t[1], t[0]))


# --- Nicht umgesetzte Baron-/Elder-Buffs (Team-Diagnose 2026-07-26) ----------
#
# Ein Baron/Elder ist nur so viel wert wie das, was daraus wird. Als "umgesetzt"
# gilt ein Buff, wenn das Team binnen BUFF_CONVERT_S danach mindestens EINEN
# gegnerischen Turm/Inhibitor zerstoert ODER in dem Fenster einen positiven
# Kill-Saldo hat. Beides sind bewusst grobe, key-freie Proxys (Gebaeude- und
# Kill-Events liegen in allen Datenpfaden vor) - der Report behauptet damit
# keine Taktik, sondern nur: es folgte nichts Messbares.

BUFF_CONVERT_S = 120.0   # Umsetzungs-Fenster nach dem Buff (2 min ~ ein Drittel
                         # der Baron-Buffdauer bzw. die halbe Elder-Wirkzeit -
                         # laenger gemessen wird jede Entscheidung dem Buff
                         # zugeschrieben, kuerzer verpasst den Recall/Reset)


def _is_convert_buff(e: dict) -> bool:
    """True fuer Baron Nashor und Elder-Drache (die beiden Buffs, aus denen ein
    Team eine Entscheidung machen soll). Normale Drachen/Grubs/Herold sind
    Dauervorteile ohne Umsetzungs-Fenster."""
    return (e.get("monster") == "BARON_NASHOR"
            or _elite_key(e) == "ELDER")


def unconverted_buffs(elites: list, buildings: list, kills: list,
                      pid_team: dict, my_team: int, *,
                      window_s: float = BUFF_CONVERT_S) -> dict:
    """Baron-/Elder-Buffs je Team und wie viele davon folgenlos blieben.

    Rueckgabe {"me"|"opp": {total, unconverted, minutes}} - `minutes` sind die
    Minuten der folgenlosen Buffs (aufsteigend). `pid_team` mappt Kill-Killer auf
    ihr Team, `buildings[*]['team']` ist wie ueberall das Team des ZERSTOERTEN
    Baus (der Gegner bekommt ihn gutgeschrieben). Elite-Events fremder Teams
    (Riot vergibt fuer den Herold teils killerTeamId 300) fallen heraus."""
    other = 200 if my_team == 100 else 100
    out = {"me": {"total": 0, "unconverted": 0, "minutes": []},
           "opp": {"total": 0, "unconverted": 0, "minutes": []}}
    for e in elites or []:
        team = e.get("team")
        if team not in (my_team, other) or not _is_convert_buff(e):
            continue
        side = "me" if team == my_team else "opp"
        start = _ts_ms(e)
        end = start + window_s * 1000.0
        took_building = any(
            b.get("building") in ("TOWER_BUILDING", "INHIBITOR_BUILDING")
            and b.get("team") not in (None, team)
            and start < _ts_ms(b) <= end
            for b in buildings or [])
        saldo = 0
        for k in kills or []:
            if not (start < _ts_ms(k) <= end):
                continue
            kt = pid_team.get(k.get("killer"))
            if kt == team:
                saldo += 1
            elif kt is not None:
                saldo -= 1
        out[side]["total"] += 1
        if not took_building and saldo <= 0:
            out[side]["unconverted"] += 1
            out[side]["minutes"].append(round(e.get("minute", 0.0), 1))
    return out


# --- Kipp-Punkt-Erkennung (Korrektur 2026-07-25) ----------------------------
#
# Nutzer-Befund an einem realen Spiel: ein gnadenlos verlorenes Spiel (Gegner
# laut Team-Graphen DURCHGEHEND vorn, kein Schnittpunkt) bekam trotzdem einen
# "Kipp-Punkt" - weil der schlicht als LETZTER entschiedener Teamfight definiert
# war. Ein Stomp hat aber keinen Kipp-Punkt.
#
# Neue, ehrliche Regel: ein entschiedener Fight bei Minute M ist NUR dann
# Kipp-Punkt, wenn beides gilt:
#   (1) Vorher offen - unmittelbar vor M lag die Team-Differenz innerhalb der
#       Schwelle ODER das am Ende unterlegene Team lag vorn.
#   (2) Nachher dauerhaft gekippt - der Vorsprung des am Ende fuehrenden Teams
#       wird innerhalb des Bestaetigungsfensters dauerhaft (erreicht die Schwelle
#       und faellt bis Spielende nicht mehr darunter zurueck); Fights NACH diesem
#       Moment kippen nichts mehr (das Spiel war da schon entschieden).
# Mehrere Kandidaten -> der FRUEHESTE. Kein Kandidat -> KEIN Kipp-Punkt (weder
# `tf-tip`-Tag noch Erwaehnung im Verdikt); stattdessen greift bei einseitigen
# Spielen die Ersatz-Aussage aus `oneside_minute`.
#
# Datenbasis sind die key-freien Team-Serien des Report-Modells (`team_series`):
# "spent" = Item-Gold je Team; zeigt das am Spielende kein fuehrendes Team
# (Late-Game-Konvergenz), traegt "kills" die Aussage (s. `_tip_basis`).

TIP_OPEN_GOLD = 2000    # Team-Item-Gold-Differenz, bis zu der ein Spiel "offen" ist
TIP_OPEN_KILLS = 5      # dieselbe Rolle fuer die Kill-Differenz (Ersatz-Datenbasis)
TIP_MIN_MINUTE = 10     # vor dieser Minute gibt es keinen Kipp-Punkt: Item-Gold
                        # startet bei 0, dort ist JEDES Spiel per Definition
                        # "offen" - eine so fruehe Entscheidung ist ein Stomp,
                        # kein Kippen (dokumentierte Wahl). Zugleich die
                        # Mindestlaenge der Serie fuer beide Aussagen: ein
                        # 3-Minuten-Capture belegt kein "dauerhaft".
TIP_CONFIRM_MIN = 5     # Minuten nach dem Fight, in denen der Vorsprung
                        # dauerhaft werden muss (sonst kippte es woanders)
TIP_BELOW_TOL = 2       # ab so vielen AUFEINANDERFOLGENDEN Minuten unter der
                        # Schwelle gilt der Vorsprung als zurueckgefallen -
                        # einzelne Ausreisser sind ok (dokumentierte Wahl)

# Haertung 2026-07-26 (Nutzer-Befund am realen Spiel EUW1_7929841225): das
# Verdikt nannte "Kipp-Punkt Min 11" fuer ein 58-Minuten-Spiel, in dem die
# Fuehrung danach noch mehrfach wechselte und das "gekippte" Team bei Min 56 mit
# Doppel-Elder und Doppel-Baron vorn lag. Getragen wurde die Aussage vom
# Kill-Fallback in `_tip_basis` (die Kill-Differenz fiel ab Min 12 nie wieder
# unter die Schwelle) - die GOLD-Serie erzaehlte eine ganz andere Geschichte.
#
# Regel: bevor ein Kipp-Punkt behauptet wird, wird die Team-Item-Gold-Serie auf
# spaetere Fuehrungswechsel geprueft. Ab TIP_FLIP_MIN Wechseln nach dem
# Kandidaten ist "hier kippte das Spiel" nicht haltbar; stattdessen nennt das
# Verdikt den letzten entscheidenden Fight (s. `tipping_decision`).
TIP_FLIP_GOLD = 3000    # Item-Gold-Differenz, ab der eine Fuehrung als
                        # gewechselt gilt. BEWUSST ueber TIP_OPEN_GOLD (2.000):
                        # im Late-Game schwankt das gehaltene Item-Gold um ein
                        # paar tausend Gold, ohne dass die Fuehrung wirklich
                        # wechselt (beide Teams item-capped, ein Tod/ein Kauf
                        # dreht die Differenz). Erst ein Vorsprung von 3.000
                        # Gold ist eine echte Fuehrung. Geprueft an den beiden
                        # Referenz-Spielen: EUW1_7928614824 (klar gekippt) hat
                        # danach 1 Wechsel, EUW1_7929841225 (Hin und Her) 2.
TIP_FLIP_MIN = 2        # ab so vielen Wechseln NACH dem Kandidaten gilt der
                        # Kipp-Punkt als nicht belastbar ("mehrere
                        # Fuehrungswechsel"); ein einzelner Wechsel ist der
                        # normale Verlauf eines gekippten Spiels.

# Metrik-Label der Einseitig-Zeile (welche Serie die Aussage traegt).
_TIP_METRIC_LABEL = {"spent": "Team-Gold", "kills": "Team-Kills"}


def _team_diff(pair: dict | None) -> list:
    """Differenz-Serie 'eigenes Team - Gegner' aus einem `team_series`-Eintrag
    ({me: [...], opp: [...]}). Laenge = kuerzere der beiden Serien."""
    if not pair:
        return []
    me, opp = pair.get("me") or [], pair.get("opp") or []
    n = min(len(me), len(opp))
    return [(me[i] or 0) - (opp[i] or 0) for i in range(n)]


def _final_lead(diff: list, thr: float) -> int | None:
    """Richtung des Vorsprungs am Spielende (+1 eigenes / -1 gegnerisches Team)
    oder None, wenn die Differenz dort UNTER der Schwelle bleibt - dann hat kein
    Team das Spiel dauerhaft an sich gerissen und nichts ist 'gekippt'."""
    if not diff:
        return None
    s = 1 if diff[-1] > 0 else -1
    return s if s * diff[-1] >= thr else None


def _lead_holds(diff: list, s: int, start: int, thr: float) -> bool:
    """Haelt der Vorsprung in Richtung `s` ab Index `start` bis Spielende?

    Toleranz: einzelne Minuten unter der Schwelle sind ok, ab `TIP_BELOW_TOL`
    aufeinanderfolgenden gilt er als zurueckgefallen (Comeback). Am Spielende
    muss die Differenz die Schwelle in jedem Fall halten."""
    if not diff:
        return False
    run = 0
    for j in range(start, len(diff)):
        if s * diff[j] >= thr:
            run = 0
            continue
        run += 1
        if run >= TIP_BELOW_TOL:
            return False
    return s * diff[-1] >= thr


def _hold_start(diff: list, s: int, thr: float) -> int | None:
    """Fruehester Frame-Index, ab dem der Vorsprung in Richtung `s` die Schwelle
    erreicht und sie bis Spielende HAELT - der Moment, in dem das Spiel dauerhaft
    entschieden war. None, wenn es nie dauerhaft entschieden war."""
    for j in range(len(diff)):
        if s * diff[j] >= thr and _lead_holds(diff, s, j, thr):
            return j
    return None


def _tip_basis(team_series: dict | None):
    """(diff, thr, richtung, hold_start, metrik) der ENTSCHEIDENDEN Team-Serie.

    Bevorzugt das Team-Item-Gold ("spent"). Ist dessen Differenz am Spielende
    unter der Schwelle, traegt die Team-Kill-Differenz die Aussage: Item-Gold
    konvergiert im Late-Game (beide Teams item-capped) - der reale Fall
    EUW1_7928614824 endete bei -250 Gold, obwohl das Spiel klar gekippt war
    (Kill-Differenz +11 -> -12). Beide Serien liegen key-frei in allen
    Datenpfaden vor. Rueckgabe None, wenn KEINE Serie ein dauerhaft fuehrendes
    Team zeigt - dann gibt es weder Kipp-Punkt noch Einseitig-Aussage."""
    if not team_series:
        return None
    for metric, thr in (("spent", TIP_OPEN_GOLD), ("kills", TIP_OPEN_KILLS)):
        diff = _team_diff(team_series.get(metric))
        # Kurze Serien (Remake/Live-Capture-Schnipsel) tragen keine Aussage
        # ueber 'dauerhaft' - dieselbe Grenze wie fuer den Kipp-Punkt selbst.
        if len(diff) <= TIP_MIN_MINUTE:
            continue
        s = _final_lead(diff, thr)
        if s is None:
            continue
        hold = _hold_start(diff, s, thr)
        if hold is not None:
            return (diff, thr, s, hold, metric)
    return None


def _tipping_candidate(teamfights: list,
                       team_series: dict | None) -> float | None:
    """Die REINE Kipp-Punkt-Regel (s. Block oben): frueheste Minute eines
    entschiedenen Fights, der das Spiel nachweislich gekippt hat - sonst None.

    `hold` ist der Moment, ab dem der Vorsprung des am Ende fuehrenden Teams
    dauerhaft haelt. Ein Fight bei Minute M qualifiziert nur, wenn
    (1) er VOR/BEI diesem Moment liegt (danach war das Spiel laengst
        entschieden - ein Fight im gewonnenen Spiel kippt nichts mehr),
    (2) der Moment hoechstens TIP_CONFIRM_MIN Minuten spaeter eintritt (sonst
        kippte das Spiel woanders) und
    (3) es unmittelbar vor M noch offen war bzw. der am Ende Unterlegene vorn lag.
    Ohne Team-Serien gibt es keinen belastbaren Kipp-Punkt (lieber keine Aussage
    als eine erfundene)."""
    basis = _tip_basis(team_series)
    if basis is None:
        return None
    diff, thr, s, hold, _metric = basis
    n = len(diff)
    minutes = sorted(f["minute"] for f in (teamfights or [])
                     if f.get("result") in ("gewonnen", "verloren"))
    for m in minutes:
        if m < TIP_MIN_MINUTE:
            continue
        i = max(0, min(int(m), n - 1))
        if i > hold or (hold - i) > TIP_CONFIRM_MIN:
            continue
        if abs(diff[i]) <= thr or s * diff[i] < 0:
            return m
    return None


def _lead_flips_after(team_series: dict | None, minute: float) -> tuple:
    """(Anzahl Fuehrungswechsel nach `minute`, Index des letzten Wechsels).

    Datenbasis ist IMMER das Team-Item-Gold ("spent") - die Serie, die abbildet,
    wer gerade das staerkere Team auf der Karte hat. Eine Fuehrung gilt ab
    TIP_FLIP_GOLD als etabliert; ein Wechsel ist der Uebergang von einer
    etablierten Fuehrung zur entgegengesetzten (Zwischenwerte innerhalb der
    Schwelle aendern nichts). Der ERSTE Fuehrungsaufbau aus dem neutralen
    Zustand heraus zaehlt nicht als Wechsel. Ohne Gold-Serie -> (0, None):
    keine Grundlage, den Kandidaten zu entwerten."""
    diff = _team_diff((team_series or {}).get("spent"))
    if not diff:
        return (0, None)
    start = int(minute)
    lead, flips, last = 0, 0, None
    for i, d in enumerate(diff):
        s = 1 if d >= TIP_FLIP_GOLD else (-1 if d <= -TIP_FLIP_GOLD else 0)
        if s == 0 or s == lead:
            continue
        if i > start and lead != 0:
            flips += 1
            last = i
        lead = s
    return (flips, last)


def _decisive_swing_minute(teamfights: list, after_index) -> float | None:
    """Minute des LETZTEN entschiedenen Fights ab dem letzten Fuehrungswechsel.

    Ersatz-Aussage fuer Spiele ohne haltbaren Kipp-Punkt: wenn die Fuehrung
    mehrfach wechselte, ist nicht der erste, sondern der letzte entscheidende
    Fight die Wende, nach der es nicht mehr zurueckging. Gibt es ab dem letzten
    Wechsel keinen entschiedenen Fight mehr, faellt die Wahl auf den letzten
    entschiedenen Fight des Spiels."""
    decisive = sorted(f["minute"] for f in (teamfights or [])
                      if f.get("result") in ("gewonnen", "verloren"))
    if not decisive:
        return None
    if after_index is not None:
        late = [m for m in decisive if m >= after_index]
        if late:
            return late[-1]
    return decisive[-1]


def _underdog_ever_led(basis, minute: float) -> bool:
    """Hat das am Ende UNTERLEGENE Team vor `minute` je selbst gefuehrt?

    `basis` ist das Tupel aus `_tip_basis` (diff, thr, richtung, hold, metrik);
    `richtung` s zeigt auf das am Ende fuehrende Team, das unterlegene ist also
    -s. Gefuehrt heisst: die Differenz lag in Richtung -s mindestens einmal ueber
    der Fuehrungs-Schwelle.

    **Schwelle bewusst TIP_FLIP_GOLD (3.000) statt TIP_OPEN_GOLD (2.000), wenn
    das Item-Gold die Basis ist:** dieselbe Begruendung wie bei
    `_lead_flips_after` - unter 3.000 Gold schwankt die Differenz, ohne dass
    jemand wirklich vorn liegt. Ein 2.000er Zwischenhoch fuer eine Minute ist
    keine Fuehrung, die ein Spiel spaeter "kippen" koennte. Bei der
    Kill-Differenz als Basis bleibt es bei deren eigener Schwelle."""
    diff, thr, s, _hold, metric = basis
    lead_thr = TIP_FLIP_GOLD if metric == "spent" else thr
    end = max(0, min(int(minute), len(diff)))
    return any(-s * d >= lead_thr for d in diff[:end])


def tipping_decision(teamfights: list,
                     team_series: dict | None = None) -> dict | None:
    """Kipp-Punkt-Befund des Spiels: {kind, minute, flips} oder None.

    EINE Quelle der Wahrheit fuer das `tip`-Flag der Fight-Karten UND die
    Verdikt-Teamfight-Zeile. Ablauf:

      1. Die Fight-Bilanz muss ueberhaupt eine Aussage hergeben
         (>= TEAMFIGHT_MIN entschiedene Fights UND won != lost).
      2. Ein Fight muss die Kipp-Punkt-Regel erfuellen (`_tipping_candidate`).
      3. **Gegenprobe A (Haertung 2026-07-26):** wechselte die Fuehrung laut
         Team-Item-Gold NACH dem Kandidaten noch mindestens TIP_FLIP_MIN Mal,
         ist "hier kippte das Spiel" nicht haltbar -> `kind="swing"` mit der
         Minute des letzten entscheidenden Fights statt eines Kipp-Punkts.
      4. **Gegenprobe B (Nutzer-Befund 2026-07-26b an EUW1_7929799918):** ein
         Spiel kann nur kippen, wenn es vorher in die andere Richtung stand.
         Hat das am Ende unterlegene Team VOR dem Kandidaten nie selbst gefuehrt
         (`_underdog_ever_led`), war es durchgehend einseitig - "Kipp-Punkt bei
         Min X" waere falsch erzaehlt. Dann `kind="oneside"`, und die
         Durchgehend-Formulierung aus `_verdict_oneside_line` uebernimmt.
         Die Reihenfolge ist Absicht: bei mehreren Fuehrungswechseln (A) hat das
         unterlegene Team per Definition zwischendurch gefuehrt - dort greift B
         gar nicht.

    `kind="tip"` = belastbarer Kipp-Punkt, `kind="swing"` = mehrere
    Fuehrungswechsel, `kind="oneside"` = durchgehend einseitig (kein Kippen).
    None = kein Befund (u. a. zu duenne Fight-Bilanz)."""
    won, lost, decisive = _teamfight_balance(teamfights or [])
    if decisive < TEAMFIGHT_MIN or won == lost:
        return None
    cand = _tipping_candidate(teamfights or [], team_series)
    if cand is None:
        return None
    flips, last = _lead_flips_after(team_series, cand)
    if flips < TIP_FLIP_MIN:
        basis = _tip_basis(team_series)
        if basis is not None and not _underdog_ever_led(basis, cand):
            return {"kind": "oneside", "minute": cand, "flips": flips}
        return {"kind": "tip", "minute": cand, "flips": flips}
    swing = _decisive_swing_minute(teamfights or [], last)
    if swing is None:
        return None
    return {"kind": "swing", "minute": swing, "flips": flips}


def teamfight_tipping_minute(teamfights: list,
                             team_series: dict | None = None) -> float | None:
    """Minute des Kipp-Punkt-Fights - nur bei belastbarem Kipp-Punkt.

    Duenne Huelle um `tipping_decision`: liefert die Minute ausschliesslich fuer
    `kind="tip"`. Ein Spiel mit mehreren Fuehrungswechseln (`kind="swing"`)
    bekommt bewusst KEINE Kipp-Punkt-Markierung auf der Fight-Karte - dort war
    nichts der Kipp-Punkt."""
    dec = tipping_decision(teamfights, team_series)
    return dec["minute"] if dec and dec["kind"] == "tip" else None


def oneside_minute(team_series: dict | None) -> dict | None:
    """Minute, ab der das Spiel DAUERHAFT entschieden war - sonst None.

    Das ist `_hold_start` der entscheidenden Serie: die frueheste Minute, ab der
    die Team-Differenz in Richtung des am Ende fuehrenden Teams die Schwelle
    erreicht und sie bis Spielende haelt. Ersatz-Aussage fuer Spiele ohne
    Kipp-Punkt ('durchgehend hinten ab Min X'). Rueckgabe {minute, ahead, thr,
    metric}; `ahead` True = das EIGENE Team lag durchgehend vorn. Frame-Index ==
    Minute (Frames liegen minuetlich vor, wie in der ganzen Delta-Engine)."""
    basis = _tip_basis(team_series)
    if basis is None:
        return None
    _diff, thr, s, hold, metric = basis
    return {"minute": float(hold), "ahead": s > 0, "thr": thr, "metric": metric}


# --- Warum gingen die Fights verloren? (key-frei, 2026-07-26b) --------------
#
# Nutzer-Feedback (verbindliches Design-Prinzip): "Praegendster Faktor:
# Verlorene Teamfights" ist wertlos - die Frage ist, WARUM die Fights nicht
# liefen. Diese Sektion beantwortet sie mit vier Faktoren, die sich key-frei aus
# Kill-Strom, Team-Serien und Elite-Monster-Events bestimmen lassen (also in
# BEIDEN Datenpfaden: Timeline und Live-Dump/Capture):
#
#   undermanned  Der Fight begann in Unterzahl - ein eigener Tod fiel kurz vor
#                dem ersten Kill des Clusters.
#   behind       Der Fight wurde aus einem Gold-Rueckstand heraus angenommen.
#   enemy_buff   Der Gegner hatte einen frischen Baron-/Elder-Buff.
#   opened       Der erste Tod des Clusters fiel im eigenen Team.
#
# Bewusst KEINE Positionsdaten/Taktik-Behauptungen - alles Proxys aus Ereignis-
# Zeitpunkten. Ein Faktor ist immer nur ein Umstand, nie ein Vorwurf; die
# Formulierung im Verdikt bleibt entsprechend neutral.

# Unterzahl-Fenster: ein eigener Tod in diesem Fenster VOR dem Fight-Start
# heisst, der Gefallene stand beim Fight nicht mehr zur Verfuegung.
#
# **Staffelung 2026-07-27.** Vorher war das EIN fester Wert (30 s) - und damit
# praktisch wirkungslos: `detect_teamfights` clustert mit `gap_s=20`, also
# gehoert jeder Tod <= 20 s vor dem ersten Kill schon SELBST zum Cluster. Das
# effektive Erkennungsfenster war das schmale Band (20 s, 30 s]. Die Untergrenze
# bleibt implizit `gap_s`; wirksam wird die Regel erst mit einer Obergrenze
# deutlich darueber.
#
# Die Staffelung ist an die real WACHSENDEN Respawn-Timer angelehnt (frueh ein
# paar Sekunden, spaet weit ueber eine halbe Minute plus Rueckweg): (bis Minute
# exklusiv, Fenster in s). Ehrlich als HEURISTIK zu lesen - die echten Timer
# haengen an Champion-Level und Patch und sind key-frei nicht verfuegbar; hier
# wird nur die Groessenordnung nachgebildet, nicht der Timer selbst.
TF_UNDERMANNED_BY_MIN = ((15.0, 30.0), (25.0, 45.0), (float("inf"), 60.0))

TF_UNDERMANNED_S = TF_UNDERMANNED_BY_MIN[0][1]   # Basiswert der Staffel (Early)


def _undermanned_window_s(minute: float) -> float:
    """Unterzahl-Fenster (s) fuer einen Fight zur Spielminute `minute`."""
    for upto, window in TF_UNDERMANNED_BY_MIN:
        if minute < upto:
            return window
    return TF_UNDERMANNED_BY_MIN[-1][1]



TF_BEHIND_GOLD = TIP_OPEN_GOLD   # Gold-Rueckstand, ab dem ein Fight "aus dem
                          # Rueckstand heraus" angenommen wurde. BEWUSST
                          # dieselbe Schwelle wie beim Kipp-Punkt ("offen" bis
                          # 2.000 Item-Gold Differenz) - eine zweite, eigene
                          # Zahl fuer denselben Begriff waere Drift.
TF_BUFF_S = 180.0         # Baron-Buff-Dauer (3 min). Faellt ein gegnerischer
                          # Baron/Elder in diesem Fenster VOR dem Fight, war der
                          # Buff beim Fight noch aktiv.

TF_REASON_MIN = 2         # Ein Faktor wird nur genannt, wenn er bei mindestens
                          # so vielen verlorenen Fights auftrat UND (s. u.) bei
                          # mindestens der Haelfte. Einmal ist Zufall.
TF_OPENER_MIN = 2         # ab so vielen Eroeffnungen wird der haeufigste eigene
                          # Champion dazu benannt (darunter ist es kein Muster)

# Reihenfolge = Nenn-Reihenfolge in der Verdikt-Zeile. Je Faktor zwei
# Formulierungen: die erste (mit "X von N") traegt den Bezug, die folgenden
# kuerzen auf "X×" ab, damit die Zeile lesbar bleibt.
_TF_REASON_TEXT = [
    ("undermanned", "{c} von {n} mit Unterzahl gestartet",
                    "{c}× in Unterzahl gestartet"),
    ("behind",      "{c} von {n} aus einem Gold-Rückstand angenommen",
                    "{c}× aus einem Gold-Rückstand angenommen"),
    ("enemy_buff",  "{c} von {n} gegen einen aktiven Baron-/Elder-Buff",
                    "{c}× gegen einen aktiven Baron-/Elder-Buff"),
    ("opened",      "{c} von {n} mit dem ersten Tod auf unserer Seite",
                    "{c}× eröffnete der erste Tod bei uns"),
]


def teamfight_reasons(clusters: list, kills: list, pid_team: dict,
                      my_team: int, *, elites: list | None = None,
                      team_series: dict | None = None) -> list:
    """Haengt jedem VERLORENEN Fight seine Ursachen-Flags an (`reasons`).

    Arbeitet auf den Clustern aus `detect_teamfights` und ergaenzt sie in place
    (Rueckgabe = dieselbe Liste, damit sich der Aufruf verketten laesst) - so
    bleibt die Zuordnung Fight <-> Flags ohne fehleranfaellige Index-Kopplung.
    Gewonnene/neutrale Fights bekommen bewusst KEINE Flags: die Frage lautet
    "warum ging es schief?", nicht "wie stand es bei jedem Fight?".

    `reasons` je verlorenem Fight:
      {undermanned, behind, enemy_buff, opened: bool, opener_pid: int|None}
    `opener_pid` ist nur gesetzt, wenn `opened` gilt (eigener Spieler fiel
    zuerst) - der Aufrufer loest ihn ueber das Roster in einen Champion auf
    (s. `teamfight_cards`)."""
    other = 200 if my_team == 100 else 100
    # Eigene Tode (Zeitpunkte) - Basis des Unterzahl-Faktors.
    my_deaths = sorted(_ts_ms(k) for k in kills or []
                       if pid_team.get(k.get("victim")) == my_team)
    # Gegnerische Baron-/Elder-Kills - Basis des Buff-Faktors.
    opp_buffs = sorted(_ts_ms(e) for e in elites or []
                       if e.get("team") == other and _is_convert_buff(e))
    gold = _team_diff((team_series or {}).get("spent"))

    for c in clusters or []:
        if c.get("result") != "verloren":
            continue
        start = c.get("ts_start")
        if start is None:
            start = float(c.get("minute", 0.0)) * 60000.0
        # Fenster spielzeitabhaengig (s. TF_UNDERMANNED_BY_MIN) - die Minute
        # kommt aus dem Cluster-Start, nicht aus dem gerundeten `minute`-Feld.
        window = _undermanned_window_s(start / 60000.0)
        undermanned = any(start - window * 1000 <= t < start
                          for t in my_deaths)
        enemy_buff = any(start - TF_BUFF_S * 1000 <= t < start
                         for t in opp_buffs)
        behind = False
        if gold:
            i = max(0, min(int(c.get("minute", 0.0)), len(gold) - 1))
            behind = gold[i] <= -TF_BEHIND_GOLD
        first_victim = c.get("first_victim")
        opened = pid_team.get(first_victim) == my_team
        c["reasons"] = {"undermanned": undermanned, "behind": behind,
                        "enemy_buff": enemy_buff, "opened": opened,
                        "opener_pid": first_victim if opened else None}
    return clusters


def teamfight_reason_summary(teamfights: list | None) -> dict | None:
    """Aggregat der Fight-Ursachen ueber alle verlorenen Fights.

    Zaehlt je Faktor, in wie vielen verlorenen Fights er auftrat, und ermittelt
    den eigenen Champion, der am oeftesten als Erster fiel. Rueckgabe
    {lost, counts:{...}, opener_top:(champ, anzahl)|None} oder None, wenn es gar
    keine verlorenen Fights mit Ursachen-Daten gab. Arbeitet auf den
    FIGHT-KARTEN (`teamfight_cards`), damit das Verdikt keine zweite Datenquelle
    braucht."""
    lost = [f for f in teamfights or []
            if f.get("result") == "verloren" and f.get("reasons")]
    if not lost:
        return None
    counts = {key: sum(1 for f in lost if f["reasons"].get(key))
              for key, _first, _more in _TF_REASON_TEXT}
    openers: dict = {}
    for f in lost:
        champ = f["reasons"].get("opener_champ")
        if f["reasons"].get("opened") and champ:
            openers[champ] = openers.get(champ, 0) + 1
    top = max(openers.items(), key=lambda t: (t[1], t[0])) if openers else None
    return {"lost": len(lost), "counts": counts, "opener_top": top}


def _verdict_teamfight_reason_line(teamfights: list | None) -> str | None:
    """Die "Warum"-Zeile zur Fight-Bilanz - oder None.

    Genannt wird ein Faktor nur, wenn er in mindestens TF_REASON_MIN verlorenen
    Fights UND in mindestens der HAELFTE davon auftrat. Beides zusammen: unter
    zwei Vorkommen ist es Zufall, unter der Haelfte ist es kein Muster des
    Spiels. Erreicht kein Faktor die Schwelle, entfaellt die Zeile ersatzlos -
    lieber nichts als Rauschen (Design-Prinzip 2026-07-26b).

    Der Ton ist bewusst deskriptiv ("mit Unterzahl gestartet", nicht "schlecht
    positioniert") - das Verdikt beschreibt Umstaende, es verteilt keine
    Schuld."""
    summary = teamfight_reason_summary(teamfights)
    if not summary:
        return None
    n, counts = summary["lost"], summary["counts"]
    parts = []
    for key, first, more in _TF_REASON_TEXT:
        c = counts.get(key, 0)
        if c < TF_REASON_MIN or c * 2 < n:
            continue
        text = (first if not parts else more).format(c=c, n=n)
        if key == "opened":
            top = summary.get("opener_top")
            if top and top[1] >= TF_OPENER_MIN:
                text += f" ({top[1]}× {top[0]})"
        parts.append(text)
    if not parts:
        return None
    return "Verlorene Fights: " + ", ".join(parts) + "."


# --- Gewinnchance ueber Zeit (heuristisch, key-frei) ------------------------
#
# Nutzer-Wunsch 2026-07-26: eine Kurve "wie stand das Spiel zu Minute t?" ans
# Ende des Verlauf-Blocks. Bewusst KEIN trainiertes Modell (dafuer fehlt uns ein
# gelabelter Datensatz) - sondern eine logistische Heuristik ueber genau die
# Team-Signale, die in ALLEN Datenpfaden key-frei vorliegen:
#
#   P(win, t) = sigmoid( w_gold·goldDiff_norm(t) + w_kill·killDiff(t)
#                        + w_level·levelDiff(t) + w_dragon·dracheDiff(t)
#                        + w_small·grubsHeroldDiff(t) + w_baron·baronAktiv(t)
#                        + w_tower·turmDiff(t) )
#
# Alle Diffs sind "eigenes Team - Gegner". Ohne Bias-Term gilt automatisch
# P(win, t)=0.5, solange alle Differenzen 0 sind (neutraler Start).
#
# **Normierung der Gold-Differenz (dokumentierte Wahl):** ein 2.000er Vorsprung
# in Minute 8 ist ein anderes Spiel als derselbe Vorsprung in Minute 30 (dort
# sind beide Teams laengst item-satt). Darum wird die Item-Gold-Differenz auf ein
# mit der Spielzeit WACHSENDES Bezugsgold normiert:
#     goldDiff_norm(t) = goldDiff(t) / (WINPROB_GOLD_BASE + t · WINPROB_GOLD_PER_MIN)
# Mit 3000 + 200·t ist der Nenner in Min 10 = 5.000, in Min 30 = 9.000 - derselbe
# absolute Vorsprung wiegt spaet also knapp halb so viel. Das faengt zugleich die
# Late-Game-Konvergenz des Item-Golds ab, die schon dem Kipp-Punkt Probleme
# machte (s. `_tip_basis`). Der vergleichsweise hohe Sockel (3.000) verhindert,
# dass die ersten Minuten - wo das Item-Gold noch bei ~0 startet - aus einem
# Erstblut-Vorsprung sofort 95 % machen.
#
# **Kalibrierung der Gewichte** (an den beiden Referenz-Spielen EUW1_7927767894
# (Sieg) und EUW1_7928614824 (Niederlage) geprueft, s. test_postgame_phase4b.py):
#   * ein einzelner Drache bringt um die 50-%-Marke ~4-5 Prozentpunkte
#     (sigmoid'(0)=0.25 -> Δp ≈ 0.25·w),
#   * ein 5.000er Gold-Lead im Midgame (t=20 -> Nenner 7.000, also 0.71 Einheiten)
#     kommt mit w=1.7 auf z≈1.2 = ~77 % - deutlich, aber eben nicht 99 %,
#     derselbe Lead in Min 8 (Nenner 4.600) auf ~86 %,
#   * und erst die SUMME vieler Signale (Gold + Kills + Level + Objectives) landet
#     in den 90ern, wie es ein durchgespielter Stomp verdient.
# Die Gewichte sind bewusst Konstanten und keine Fit-Parameter - wer sie aendert,
# aendert eine Darstellungs-Heuristik, kein gemessenes Modell.
WINPROB_WEIGHTS = {
    "gold": 1.7,     # je Einheit normierter Item-Gold-Differenz
    "kill": 0.06,    # je Kill Team-Differenz
    "level": 0.05,   # je Level Team-Differenz (Summe aller fuenf Champions)
    "dragon": 0.18,  # je Drache (bzw. Atakhan) Differenz
    "small": 0.06,   # je Grub/Herold Differenz (kleine Epic-Monster)
    "baron": 0.45,   # aktiver Baron-Buff (abklingend, Vorzeichen je Team)
    "tower": 0.08,   # je Turm Differenz
}

WINPROB_GOLD_BASE = 3000.0      # Bezugsgold in Minute 0
WINPROB_GOLD_PER_MIN = 200.0    # Zuwachs des Bezugsgolds je Minute
WINPROB_BARON_MIN = 3.0         # Wirkdauer des Baron-Buffs (Minuten, linear abklingend)

# Mindestlaenge der Serie: unter so vielen Frames zeigt die Kurve nichts als
# Rauschen (ein 4-Frame-Live-Schnipsel wie tests/fixtures/dump_min hat weder
# Objectives noch nennenswerte Gold-Differenzen) - dann lieber gar keine Kurve
# als eine erfundene. Gleiche Haltung wie TIP_MIN_MINUTE beim Kipp-Punkt.
WINPROB_MIN_FRAMES = 8

# Epic-Monster -> Gewichts-Kategorie. Unbekannte Monster zaehlen als "klein"
# (lieber unterschaetzen als eine erfundene Grosswirkung).
_WINPROB_MONSTER_CLASS = {"DRAGON": "dragon", "ATAKHAN": "dragon",
                          "HORDE": "small", "RIFTHERALD": "small",
                          "BARON_NASHOR": "baron"}


def _sigmoid(z: float) -> float:
    """Logistische Funktion, ueberlauf-sicher (grosse |z| saettigen sauber)."""
    if z >= 0:
        return 1.0 / (1.0 + 2.718281828459045 ** (-z))
    e = 2.718281828459045 ** z
    return e / (1.0 + e)


def _diff_at(diff: list, i: int) -> float:
    """Wert einer Differenz-Serie in Minute `i`; laeuft die Serie vorher aus,
    wird ihr letzter Wert fortgeschrieben (Zustandsgroesse, kein Zuwachs)."""
    if not diff:
        return 0.0
    return float(diff[i] if i < len(diff) else diff[-1])


def _cum_event_diff(events: list, n: int, my_team, sign_of) -> list:
    """Kumulative Ereignis-Differenz 'eigenes Team - Gegner' je Minute.

    `sign_of(ev)` liefert +1/-1/0 fuer ein Ereignis (die Team-Semantik ist je
    Event-Strom verschieden: beim Elite-Monster ist `team` der KILLER, beim
    Gebaeude das Team des ZERSTOERTEN Baus). Ereignisse jenseits des letzten
    Frames werden auf ihn geklemmt."""
    out = [0.0] * n
    if not events or n <= 0 or my_team is None:
        return out
    for ev in events:
        s = sign_of(ev)
        if not s:
            continue
        idx = int(ev.get("minute", 0.0) or 0.0)
        idx = max(0, min(idx, n - 1))
        out[idx] += s
    run = 0.0
    for i in range(n):
        run += out[i]
        out[i] = run
    return out


def _baron_activity(events: list, n: int, my_team) -> list:
    """Signierte Baron-Aktivitaet je Minute: +1 direkt nach eigenem Baron, -1 nach
    gegnerischem, linear abklingend ueber WINPROB_BARON_MIN Minuten.

    Anders als Drachen/Tuerme ist der Baron KEIN dauerhafter Vorteil, sondern ein
    Zeitfenster - genau so modelliert. Ueberlappende Barone beider Teams heben
    sich anteilig auf (Summe der signierten Beitraege)."""
    out = [0.0] * n
    if not events or n <= 0 or my_team is None:
        return out
    for ev in events:
        if _WINPROB_MONSTER_CLASS.get(ev.get("monster")) != "baron":
            continue
        s = 1.0 if ev.get("team") == my_team else -1.0
        start = float(ev.get("minute", 0.0) or 0.0)
        for i in range(n):
            age = i - start
            if 0.0 <= age < WINPROB_BARON_MIN:
                out[i] += s * (1.0 - age / WINPROB_BARON_MIN)
    return out


def winprob_series(team_series: dict | None, *, elites: list | None = None,
                   buildings: list | None = None, my_team: int | None = None,
                   weights: dict | None = None) -> list:
    """Heuristische Gewinnchance des EIGENEN Teams je Minute (Liste 0..1).

    Datenbasis sind ausschliesslich key-freie Team-Signale: `team_series`
    ("spent" = Team-Item-Gold, "kills", "level") plus die Event-Stroeme
    `elites`/`buildings` (beide in Timeline- UND Live-Dump-Pfad vorhanden, s.
    series/live_series). `my_team` entscheidet das Vorzeichen der Objective-
    Differenzen; ohne sie bleiben Objectives aussen vor (Gold/Kills/Level tragen
    die Kurve dann allein).

    Formel und Kalibrierung: s. Block-Kommentar oben (WINPROB_WEIGHTS). Die
    Serienlaenge folgt der laengsten vorliegenden Differenz-Serie; kuerzere
    werden mit ihrem letzten Wert fortgeschrieben. Serien unter
    WINPROB_MIN_FRAMES Frames ergeben **[]** (keine Kurve statt einer erfundenen).

    Bewusst KEIN trainiertes Modell - der Report weist das im Chart-Untertitel
    auch so aus."""
    w = weights or WINPROB_WEIGHTS
    ts = team_series or {}
    gold = _team_diff(ts.get("spent"))
    kills = _team_diff(ts.get("kills"))
    level = _team_diff(ts.get("level"))
    n = max(len(gold), len(kills), len(level))
    if n < WINPROB_MIN_FRAMES:
        return []

    def _elite_sign(cls):
        def _sign(ev):
            if _WINPROB_MONSTER_CLASS.get(ev.get("monster"), "small") != cls:
                return 0
            return 1 if ev.get("team") == my_team else -1
        return _sign

    def _tower_sign(ev):
        if ev.get("building") != "TOWER_BUILDING":
            return 0
        # `team` ist das Team des ZERSTOERTEN Turms -> der Gegner bekommt ihn
        # gutgeschrieben (gleiche Lesart wie `_objective_summary`).
        return -1 if ev.get("team") == my_team else 1

    elites = elites or []
    dragons = _cum_event_diff(elites, n, my_team, _elite_sign("dragon"))
    smalls = _cum_event_diff(elites, n, my_team, _elite_sign("small"))
    towers = _cum_event_diff(buildings or [], n, my_team, _tower_sign)
    baron = _baron_activity(elites, n, my_team)

    out = []
    for i in range(n):
        gnorm = _diff_at(gold, i) / (WINPROB_GOLD_BASE
                                     + i * WINPROB_GOLD_PER_MIN)
        z = (w["gold"] * gnorm
             + w["kill"] * _diff_at(kills, i)
             + w["level"] * _diff_at(level, i)
             + w["dragon"] * dragons[i]
             + w["small"] * smalls[i]
             + w["baron"] * baron[i]
             + w["tower"] * towers[i])
        out.append(round(_sigmoid(z), 4))
    return out


def teamfight_cards(clusters: list, roster: dict, my_team: int,
                    *, tip_minute: float | None = None) -> list:
    """Render-fertige Teamfight-Karten aus den Kill-Clustern.

    Je Fight werden die beteiligten Spieler nach Team getrennt (eigenes Team
    links, Gegner rechts), innerhalb jedes Teams nach ROLE_ORDER sortiert und als
    Champ-Eintrag `{champ, role, died}` aufbereitet. `died` = Opfer eines
    Cluster-Kills (Gefallene). `roster` = {pid: {champ, role, team}}. Nicht dem
    Roster zuordenbare oder team-fremde pids werden uebersprungen; ein Fight ohne
    beteiligte, zuordenbare Champs entfaellt (kein leeres Rendern). `tip_minute`
    markiert den Kipp-Punkt-Fight (`tip=True`). Rein/testbar - der Renderer
    konsumiert nur; `minute`/`result` bleiben fuer das Verdikt erhalten.

    Wurde der Cluster vorher mit `teamfight_reasons` annotiert, wandern dessen
    Ursachen-Flags als `reasons` mit auf die Karte - `opener_pid` dabei ueber das
    Roster zu `opener_champ` aufgeloest, damit weder Verdikt noch Renderer das
    Roster nochmal brauchen."""
    other = 200 if my_team == 100 else 100
    cards = []
    for c in clusters:
        victims = set(c.get("victims") or [])
        sides: dict[int, list] = {my_team: [], other: []}
        for pid in c.get("pids", []):
            info = roster.get(pid)
            if not info:
                continue
            team = info.get("team")
            if team not in sides:
                continue
            sides[team].append({
                "champ": info.get("champ"),
                "role": info.get("role") or "",
                "died": pid in victims,
            })
        if not sides[my_team] and not sides[other]:
            continue
        for side in sides.values():
            side.sort(key=lambda e: ROLE_ORDER.get(e["role"], 9))
        is_tip = (tip_minute is not None
                  and abs(c["minute"] - tip_minute) < 1e-6)
        card = {
            "minute": c["minute"],
            "my_kills": c["my_kills"],
            "opp_kills": c["opp_kills"],
            "result": c["result"],
            "me": sides[my_team],
            "opp": sides[other],
            "tip": is_tip,
        }
        reasons = c.get("reasons")
        if reasons:
            opener = reasons.get("opener_pid")
            card["reasons"] = {
                **{k: v for k, v in reasons.items() if k != "opener_pid"},
                "opener_champ": (roster.get(opener) or {}).get("champ")
                                if opener is not None else None,
            }
        cards.append(card)
    return cards


# --- Sektion 5: Objective-Beteiligung (key-frei, Proxy) ---------------------

def objective_participation(elites: list, kills: list, pid: int, my_team: int,
                            *, window_s: float = 60.0) -> dict:
    """Anteil der eigenen Elite-Objectives, an denen der Spieler beteiligt war.

    Beteiligt = direkt (Killer/Assist am ELITE_MONSTER_KILL, sofern der Strom
    Assists traegt - Timeline ja, Live-Events teils) ODER als Proxy ein
    Champion-Kill/-Assist innerhalb ±`window_s` um das Objective. Gezaehlt werden
    nur Objectives des EIGENEN Teams. Rueckgabe {present, total}."""
    own = [e for e in elites if e.get("team") == my_team]
    total = len(own)
    if total == 0:
        return {"present": 0, "total": 0}
    part_ts = [_ts_ms(k) for k in kills
               if k.get("killer") == pid or pid in (k.get("assists") or [])]
    present = 0
    for e in own:
        ets = _ts_ms(e)
        direct = e.get("killer") == pid or pid in (e.get("assists") or [])
        near = any(abs(t - ets) <= window_s * 1000 for t in part_ts)
        if direct or near:
            present += 1
    return {"present": present, "total": total}


# --- Sektion 6: Build-Eval Stufe 1 (Reihenfolge) + Stufe 2 (Timing) ----------

# Ein Item gilt als "fertig", wenn es im builds.yaml-Core steht ODER sein
# Gesamt-Gold diese Schwelle erreicht (grosse Fertig-Items; Komponenten liegen
# darunter). Kein Engine-Replay (das ist Phase 5).
FINISHED_GOLD = 2000


def build_order_check(kb_order: list, actual_seq: list) -> dict:
    """Kaufreihenfolge der Core-Items gegen die builds.yaml-Reihenfolge.

    `kb_order`: Core-Items in KB-Reihenfolge (nach avg_slot sortiert).
    `actual_seq`: dieselben Core-Items in der TATSAECHLICHEN Fertigstellungs-
    Reihenfolge des Spielers. Rueckgabe {ok, text}: erste Inversion gegen die
    KB-Ordnung -> 'Rabadon vor Dusk (KB: Dusk zuerst)', sonst 'Reihenfolge ok'.
    Weniger als zwei gemeinsame Items -> ok (nichts zu vergleichen)."""
    common = [x for x in actual_seq if x in kb_order]
    if len(common) < 2:
        return {"ok": True, "text": "Reihenfolge ok"}
    rank = {name: i for i, name in enumerate(kb_order)}
    for i in range(len(common)):
        for j in range(i + 1, len(common)):
            if rank[common[i]] > rank[common[j]]:
                return {"ok": False,
                        "text": f"{common[i]} vor {common[j]} "
                                f"(KB: {common[j]} zuerst)"}
    return {"ok": True, "text": "Reihenfolge ok"}


def build_timing_pairs(my_minutes: list, opp_minutes: list, *, n: int = 3,
                       behind_gap: float = 2.0) -> list:
    """Fertigstellungs-Minute des 1./2./3. Fertig-Items vs. Rollen-Gegenpart.

    `my_minutes`/`opp_minutes`: aufsteigende Fertigstellungs-Minuten der ersten
    Fertig-Items. Rueckgabe je vorhandenem eigenem Item {n, mine, opp, behind}
    (`behind` = mind. `behind_gap` min hinter dem Gegner)."""
    out = []
    for i in range(n):
        if i >= len(my_minutes):
            break
        mine = my_minutes[i]
        opp = opp_minutes[i] if i < len(opp_minutes) else None
        behind = opp is not None and (mine - opp) >= behind_gap
        out.append({"n": i + 1, "mine": mine, "opp": opp, "behind": behind})
    return out


# --- Sektion 7: Jungle-Gank-Proxy (key-frei) --------------------------------

def kill_participation_times(kills: list, pid: int) -> list:
    """Aufsteigende Minuten der Kill-Beteiligungen (Kills + Assists) eines
    Spielers - Proxy fuer 'wann war der Jungler/Roamer aktiv' (key-frei)."""
    ts = [round(k.get("minute", 0.0), 2) for k in kills
          if k.get("killer") == pid or pid in (k.get("assists") or [])]
    return sorted(ts)
