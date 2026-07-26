"""HTML-Renderer des Post-Game-Reports: self-contained, theme-aware, CSP-safe.

Uebernimmt das CSS-Token-System des Mavaluz-Prototyps (Light/Dark via
prefers-color-scheme + :root[data-theme=...], Gold-Akzent, Mono fuer Zahlen).
Verlaufsgraphen sind inline-SVG (keine externen Libs, keine Skripte noetig).
Der Renderer bekommt ausschliesslich das Report-Modell (Dict) aus
`build_report` - keine IO, kein Netz.
"""

import html

from . import analysis

# Farb-Tokens fuer die SVG-Linien (nutzen die CSS-Variablen des Reports).
_C_ME = "var(--accent)"
_C_OPP = "var(--even)"
_C_ME_STROKE = "#C79A34"     # Fallback fuer SVG (CSS-Var im stroke ok, aber
_C_OPP_STROKE = "#868D9A"    # explizite Hex sind robuster in <polyline>)


def _esc(s) -> str:
    return html.escape(str(s if s is not None else ""))


def _fmt(n) -> str:
    """Ganzzahl mit Tausenderpunkt (7.550 statt 7 550) - liest sich leichter
    und zeigt, dass die Zahl zusammengehoert (Nutzer-Entscheidung 2026-07-24)."""
    try:
        return f"{int(round(n)):,}".replace(",", ".")
    except (TypeError, ValueError):
        return _esc(n)


def _disp_name(s) -> str:
    """Anzeige-Name ohne Riot-ID-Tagline: 'Nexxy#1337' -> 'Nexxy' (Nutzer-
    Entscheidung 2026-07-24). Nur Darstellung - intern (me-Aufloesung,
    Namen->pid-Mapping, Roster-Zuordnung) bleibt der volle Name erhalten."""
    return str(s or "").split("#", 1)[0]


def _signed(n) -> str:
    if n is None:
        return "–"
    n = int(round(n))
    return f"+{_fmt(n)}" if n >= 0 else f"−{_fmt(abs(n))}"


def _verdict_items(v: dict) -> str:
    """Auto-Verdikt-Zeilen als <li>-Liste (je Befund eine eigene Zeile, s.
    analysis.verdict). Die erste (wichtigste) Zeile wird hervorgehoben."""
    lines = (v or {}).get("lines") or []
    out = []
    for i, text in enumerate(lines):
        cls = ' class="lead"' if i == 0 else ""
        out.append(f"<li{cls}>{_esc(text)}</li>")
    return "".join(out)


def _axis_label(v) -> str:
    """Kompaktes Y-Achsen-Label: grosse Werte als k-Kurzform (Gold/Schaden),
    kleine Werte (Vision/Kills/Level) als glatte Zahl."""
    try:
        v = float(v)
    except (TypeError, ValueError):
        return _esc(v)
    if abs(v) >= 10000:
        return f"{v / 1000:.0f}k"
    if abs(v) >= 1000:
        return f"{v / 1000:.1f}k"
    return f"{v:.0f}"


# --- SVG-Liniendiagramm -----------------------------------------------------

def _line_chart(title: str, unit: str, lines: list, *, width: int = 560,
                height: int = 240) -> str:
    """Inline-SVG-Liniendiagramm mit Achsen (Redesign 2026-07-24).

    `lines`: Liste (values, stroke, label). X-Achse = Minute (Index) mit
    Gridlines + Beschriftung alle 5 min; Y-Achse mit 3-4 Zwischen-Gridlines und
    Wert-Labels. Groesser als zuvor (default 560x240) und im 2-Spalten-Grid
    dargestellt. Leere/kurze Serien werden robust behandelt."""
    lines = [(vals, col, lab) for vals, col, lab in lines if vals]
    if not lines:
        return f'<div class="chart-empty">{_esc(title)}: keine Daten</div>'

    # Mehr Rand fuer Achsen-Beschriftung (links Y-Werte, unten Minuten).
    pad_l, pad_r, pad_t, pad_b = 46, 12, 16, 24
    plot_w = width - pad_l - pad_r
    plot_h = height - pad_t - pad_b
    n = max(len(v) for v, _, _ in lines)
    vmax = max((max(v) for v, _, _ in lines if v), default=1) or 1
    vmin = min((min(v) for v, _, _ in lines if v), default=0)
    vmin = min(vmin, 0)
    span = (vmax - vmin) or 1

    def x(i):
        return pad_l + (plot_w * i / max(1, n - 1))

    def y(val):
        return pad_t + plot_h - (plot_h * (val - vmin) / span)

    parts = [f'<svg viewBox="0 0 {width} {height}" class="chart" '
             f'role="img" aria-label="{_esc(title)}">']

    # Y-Gridlines + Wert-Labels (Baseline + 3 Zwischenschritte bis vmax).
    for frac in (0.0, 0.25, 0.5, 0.75, 1.0):
        gv = vmin + span * frac
        yy = y(gv)
        cls = "axis" if abs(gv) < 1e-9 else "grid"
        parts.append(f'<line x1="{pad_l}" y1="{yy:.1f}" x2="{width - pad_r}" '
                     f'y2="{yy:.1f}" class="{cls}"/>')
        parts.append(f'<text x="{pad_l - 5}" y="{yy + 3:.1f}" class="tick-y" '
                     f'text-anchor="end">{_axis_label(gv)}</text>')

    # X-Gridlines + Minuten-Ticks alle 5 min.
    for m in range(0, n, 5):
        xx = x(m)
        parts.append(f'<line x1="{xx:.1f}" y1="{pad_t}" x2="{xx:.1f}" '
                     f'y2="{pad_t + plot_h:.1f}" class="grid"/>')
        parts.append(f'<text x="{xx:.1f}" y="{height - 6}" class="tick-x" '
                     f'text-anchor="middle">{m}</text>')

    for vals, col, _lab in lines:
        pts = " ".join(f"{x(i):.1f},{y(val):.1f}" for i, val in enumerate(vals))
        parts.append(f'<polyline points="{pts}" fill="none" stroke="{col}" '
                     f'stroke-width="2.2" stroke-linejoin="round" '
                     f'stroke-linecap="round"/>')
    parts.append("</svg>")

    legend = " ".join(
        f'<span class="lg"><i style="background:{col}"></i>{_esc(lab)}</span>'
        for _v, col, lab in lines)
    return (f'<div class="chart-box"><div class="chart-title">{_esc(title)}'
            f'<span class="chart-unit">{_esc(unit)} · min</span>'
            f'<span class="chart-legend">{legend}</span></div>'
            + "".join(parts) + "</div>")


# --- Gewinnchance ueber Zeit (heuristisch) ----------------------------------

# Ehrlichkeits-Hinweis: steht als Untertitel IM Chart, nicht nur im Sektions-Lede -
# wer die Kurve screenshottet, nimmt den Hinweis mit (Nutzer-Wunsch 2026-07-26).
_WINPROB_TITLE = "Gewinnchance (heuristisch)"
_WINPROB_NOTE = ("Heuristische Schätzung aus Gold, Kills, Level und Objectives — "
                 "kein trainiertes Modell.")

# Flaechen-Toene: dieselben Hex-Werte wie die CSS-Tokens --win/--loss. Wie bei
# _C_ME_STROKE bewusst explizit statt var() - in SVG-Praesentationsattributen
# sind Hex robuster; bei 18 % Deckkraft traegt der Ton in beiden Themes.
_C_WIN_FILL = "#1E8567"      # Flaeche ueber 50 % (Win-Ton)
_C_LOSS_FILL = "#BC4761"     # Flaeche unter 50 % (Loss-Ton)


def _winprob_chart(values: list, *, width: int = 1160,
                   height: int = 260) -> str:
    """Volle-Breite-Chart der heuristischen Gewinnchance (0-100 %) je Minute.

    Y-Achse fix 0-100 % mit betonter 50-%-Mittellinie; die Flaeche zwischen Kurve
    und Mittellinie wird ueber 50 % in Win-, darunter in Loss-Farbe gefuellt (zwei
    identische Flaechen-Pfade, je auf die obere/untere Haelfte geclippt - CSP-safe,
    ohne Skript). X-Achse wie die uebrigen Charts: Minuten-Ticks alle 5 min.
    Leere Serie (zu kurzes Spiel, s. analysis.WINPROB_MIN_FRAMES) -> "" , der
    Aufrufer laesst den Chart dann ganz weg."""
    if not values:
        return ""
    pad_l, pad_r, pad_t, pad_b = 46, 12, 16, 24
    plot_w = width - pad_l - pad_r
    plot_h = height - pad_t - pad_b
    n = len(values)

    def x(i):
        return pad_l + (plot_w * i / max(1, n - 1))

    def y(val):
        return pad_t + plot_h - plot_h * val

    y_mid = y(0.5)
    parts = [f'<svg viewBox="0 0 {width} {height}" class="chart" role="img" '
             f'aria-label="{_esc(_WINPROB_TITLE)}">']
    # Clip-Rechtecke: obere Haelfte (ueber 50 %) und untere Haelfte.
    parts.append(
        f'<defs><clipPath id="wp-above"><rect x="{pad_l}" y="{pad_t}" '
        f'width="{plot_w}" height="{y_mid - pad_t:.1f}"/></clipPath>'
        f'<clipPath id="wp-below"><rect x="{pad_l}" y="{y_mid:.1f}" '
        f'width="{plot_w}" height="{pad_t + plot_h - y_mid:.1f}"/></clipPath></defs>')

    # Y-Gridlines + Prozent-Labels; 50 % ist die betonte Mittellinie.
    for frac in (0.0, 0.25, 0.5, 0.75, 1.0):
        yy = y(frac)
        cls = "axis" if abs(frac - 0.5) < 1e-9 else "grid"
        parts.append(f'<line x1="{pad_l}" y1="{yy:.1f}" x2="{width - pad_r}" '
                     f'y2="{yy:.1f}" class="{cls}"/>')
        parts.append(f'<text x="{pad_l - 5}" y="{yy + 3:.1f}" class="tick-y" '
                     f'text-anchor="end">{frac * 100:.0f} %</text>')

    # X-Gridlines + Minuten-Ticks alle 5 min (gleiche Achse wie die anderen Charts).
    for m in range(0, n, 5):
        xx = x(m)
        parts.append(f'<line x1="{xx:.1f}" y1="{pad_t}" x2="{xx:.1f}" '
                     f'y2="{pad_t + plot_h:.1f}" class="grid"/>')
        parts.append(f'<text x="{xx:.1f}" y="{height - 6}" class="tick-x" '
                     f'text-anchor="middle">{m}</text>')

    pts = " ".join(f"{x(i):.1f},{y(v):.1f}" for i, v in enumerate(values))
    # Flaechen-Pfad: Kurve hin, auf der 50-%-Linie zurueck.
    area = (f'M {x(0):.1f},{y_mid:.1f} L '
            + " L ".join(f"{x(i):.1f},{y(v):.1f}" for i, v in enumerate(values))
            + f' L {x(n - 1):.1f},{y_mid:.1f} Z')
    parts.append(f'<path d="{area}" fill="{_C_WIN_FILL}" fill-opacity="0.18" '
                 f'clip-path="url(#wp-above)"/>')
    parts.append(f'<path d="{area}" fill="{_C_LOSS_FILL}" fill-opacity="0.18" '
                 f'clip-path="url(#wp-below)"/>')
    parts.append(f'<polyline points="{pts}" fill="none" '
                 f'stroke="{_C_ME_STROKE}" stroke-width="2.2" '
                 f'stroke-linejoin="round" stroke-linecap="round"/>')
    parts.append("</svg>")

    return (f'<div class="chart-box chart-wide">'
            f'<div class="chart-title">{_esc(_WINPROB_TITLE)}'
            f'<span class="chart-unit">% · min</span>'
            f'<span class="chart-sub">{_esc(_WINPROB_NOTE)}</span></div>'
            + "".join(parts) + "</div>")


def _duo_and_team_charts(report: dict) -> str:
    me_champ = report["me"]["champ"]
    opp = None
    for p in report["team"]:
        if p["is_me"]:
            opp = p["counterpart"]
    opp_champ = opp["champ"] if opp else "Gegenpart"

    duo = report["duo_series"]
    tvt = report["team_series"]
    blocks = []

    # Schaden-Diagramme nur, wenn Schaden vorliegt (Timeline-Pfad). Key-frei
    # (Live-Dump) fehlt der Schaden komplett -> Block weglassen, nicht leer
    # rendern (der Disclaimer unten erklaert die Luecke).
    has_dmg = report.get("has_damage", True)
    metrics = [("spent", "Item-Gold (Kampfkraft)")]
    if has_dmg:
        metrics.append(("dmg", "Schaden"))
    metrics.append(("vision", "Vision"))

    # Du vs. Gegenpart.
    for metric, unit in metrics:
        d = duo.get(metric, {})
        blocks.append(_line_chart(
            f"{me_champ} vs. {opp_champ} — {unit}", unit,
            [(d.get("me", []), _C_ME_STROKE, me_champ),
             (d.get("opp", []), _C_OPP_STROKE, opp_champ)]))
    # Team vs. Team (zusaetzlich Kills + aufsummierte Champion-Level, key-frei in
    # allen Pfaden). Drittes Tupel-Element = Titel-Suffix (fuer Level abweichend
    # von der Achsen-Einheit: Titel 'Level (Summe)', Einheit 'Level').
    team_specs = [(m, u, u) for m, u in metrics] + [
        ("kills", "Kills", "Kills"), ("level", "Level", "Level (Summe)")]
    for metric, unit, title_suffix in team_specs:
        t = tvt.get(metric, {})
        blocks.append(_line_chart(
            f"Team vs. Team — {title_suffix}", unit,
            [(t.get("me", []), _C_ME_STROKE, "Eigenes Team"),
             (t.get("opp", []), _C_OPP_STROKE, "Gegner")]))

    # Gewinnchance zuletzt und ueber die volle Breite (eigene Zeile im Grid) -
    # sie fasst alle Team-Signale darueber zu EINER Kurve zusammen.
    blocks.append(_winprob_chart(report.get("winprob") or []))

    return '<div class="chart-grid">' + "".join(blocks) + "</div>"


# --- Team-Block -------------------------------------------------------------

def _pb_row(label, delta, scale: float) -> str:
    """Eine Diverging-Balken-Zeile (Label | Track | Delta-Wert), Win/Loss-gefaerbt.

    Gemeinsame Basis der Fokus-Phasen-Balken (`_phase_bars`) UND der Impact-
    Phasen-Balken in der Sup-vs-Sup-Kachel (`_impact_phase_bars`) - beide sollen
    identisch aussehen. `delta` None -> 'kein Gegenpart'."""
    if delta is None:
        return (f'<div class="pb-row"><span class="pb-lab">{_esc(label)}</span>'
                f'<span class="pb-na">kein Gegenpart</span></div>')
    w = 50 * abs(delta) / scale
    pos = delta >= 0
    col = "var(--win)" if pos else "var(--loss)"
    bar = (f'<div class="pb-track"><div class="pb-mid"></div>'
           f'<div class="pb-fill" style="width:{w:.0f}%;background:{col};'
           f'{"left:50%" if pos else "right:50%"}"></div></div>')
    return (f'<div class="pb-row"><span class="pb-lab">{_esc(label)}</span>{bar}'
            f'<span class="pb-val" style="color:{col}">'
            f'{_signed(delta)}</span></div>')


def _phase_bars(deltas: list) -> str:
    """Diverging-Balken je Phase fuer die Rollen-Fokusmetrik (me-opp-Delta)."""
    # Skala ueber alle Phasen der Fokusmetrik, damit die Balken vergleichbar sind.
    focus = deltas[0]["focus"] if deltas else "gold"
    mags = [abs(ph["metrics"].get(focus, {}).get("delta") or 0) for ph in deltas]
    scale = max(mags) or 1
    flabel = {"gold": "Gold", "cs": "CS", "dmg": "Schaden",
              "vision": "Vision"}.get(focus, focus)
    rows = [_pb_row(ph["label"], ph["metrics"].get(focus, {}).get("delta"), scale)
            for ph in deltas]
    return (f'<div class="pb-head">Fokus: {flabel} vs. Gegenpart '
            f'(Phasen-Delta)</div>' + "".join(rows))


def _build_eval_html(p: dict) -> str:
    """Build-Eval Stufe 1 (Reihenfolge) + Stufe 2 (Timing vs. Gegenpart), §8b.
    Nur wenn ein KB-Core existiert; sonst leer (kein Vergleichsmassstab)."""
    be = p.get("build_eval")
    if not be or not be.get("has_core"):
        return ""
    order = be["order"]
    ocls = "tag-strength" if order["ok"] else "tag-weak"
    timing = be.get("timing") or []
    if timing:
        chips = []
        for t in timing:
            opp = "" if t["opp"] is None else f" (Gegner {t['opp']:.0f})"
            cls = "be-t be-behind" if t["behind"] else "be-t"
            chips.append(f'<span class="{cls}">{t["n"]}. Item Min '
                         f'{t["mine"]:.0f}{_esc(opp)}</span>')
        timing_html = "".join(chips)
    else:
        timing_html = '<span class="muted">keine Fertig-Items</span>'
    return (f'<div class="be">'
            f'<div class="be-order"><span class="be-h">Build</span>'
            f'<span class="{ocls}">{_esc(order["text"])}</span></div>'
            f'<div class="be-timing">{timing_html}</div></div>')


def _build_replay_html(p: dict) -> str:
    """Build-Eval Stufe 3: Engine-Replay (Phase 5, §8b). Zeigt 'X/Y Käufe
    engine-konform' (regulaere Fertig-Items) + optional die Boots-Teilmenge und
    die Abweichungen mit der Engine-Alternative. `build_replay` None (fehlender
    Static-Cache/builds.yaml) -> leer; 'nicht bewertbar', wenn kein KB-Wissen."""
    br = p.get("build_replay")
    if not br:
        return ""
    if not br.get("evaluable"):
        return ('<div class="br"><span class="be-h">Engine-Check</span> '
                '<span class="muted">nicht bewertbar (kein Build-Wissen)</span></div>')
    score = br.get("score") or {}
    boots = br.get("boots") or {}
    total, hits = score.get("total", 0), score.get("hits", 0)
    if total == 0 and boots.get("total", 0) == 0:
        return ('<div class="br"><span class="be-h">Engine-Check</span> '
                '<span class="muted">keine bewertbaren Käufe</span></div>')
    scls = ("tag-strength" if total and hits == total
            else "tag-weak" if total and hits == 0 else "")
    head = (f'<span class="be-h">Engine-Check</span> '
            f'<span class="{scls}">{hits}/{total} Käufe engine-konform</span>')
    if boots.get("total", 0):
        head += (f' <span class="muted">· Boots {boots.get("hits", 0)}/'
                 f'{boots.get("total", 0)}</span>')
    devs = [pu for pu in br.get("purchases", []) if not pu.get("hit")]
    dev_html = ""
    if devs:
        rows = []
        for pu in devs:
            top = " / ".join(_esc(t) for t in (pu.get("engine_top") or [])) or "–"
            rows.append(f'<li>Min {pu["minute"]}: {_esc(pu["item"])} '
                        f'<span class="muted">→ Engine: {top}</span></li>')
        dev_html = f'<ul class="br-dev">{"".join(rows)}</ul>'
    return f'<div class="br">{head}{dev_html}</div>'


def _gank_lane(times: list, span: float, dot_cls: str, label: str,
               label_cls: str) -> str:
    """Eine Zeile des Gank-Strips: Champ-Label + Mini-SVG-Achse mit einem Punkt
    je Kill/Assist. `span` ist fuer beide Zeilen identisch -> gleiche Zeitachse."""
    w, h = 200, 20
    pad = 6
    plot = w - 2 * pad
    dots = "".join(
        f'<circle cx="{pad + plot * min(t, span) / span:.1f}" cy="{h/2:.0f}" '
        f'r="3.2" class="{dot_cls}"/>' for t in times)
    axis = (f'<line x1="{pad}" y1="{h/2:.0f}" x2="{w - pad}" y2="{h/2:.0f}" '
            f'class="gk-axis"/>')
    cnt = ("0" if not times else str(len(times)))
    return (f'<div class="gk-row">'
            f'<span class="gk-lab {label_cls}">{_esc(label)}</span>'
            f'<svg viewBox="0 0 {w} {h}" class="gk-svg" role="img" '
            f'aria-label="Kill-Beteiligungs-Zeitpunkte {_esc(label)}">'
            f'{axis}{dots}</svg>'
            f'<span class="gk-cnt muted">{cnt}</span></div>')


def _gank_strip(p: dict, duration_min: float) -> str:
    """Zwei-Zeilen-Zeitleiste der Kill-Beteiligungen (Jungle-Gank-Proxy, §8b
    Sektion 7): oben der Karten-Spieler (Akzentfarbe), darunter - falls vorhanden
    - der Rollen-Gegenpart (Gegner-Farbe) auf DERSELBEN Zeitachse. So laesst sich
    ablesen, wann welcher Jungler aktiv war. Kein Gegenpart -> einzeilig."""
    times = p.get("gank_times", []) or []
    opp_times = p.get("gank_times_opp")
    cp = p.get("counterpart") or {}
    me_label = p.get("champ") or "Du"
    # Gemeinsame Zeitachse ueber beide Zeilen (Spielende + spaetester Punkt).
    all_t = list(times) + list(opp_times or [])
    span = max(duration_min, max(all_t) if all_t else 0, 1.0)
    lanes = [_gank_lane(times, span, "gk-dot", me_label, "gk-lab-me")]
    if opp_times is not None:
        opp_label = cp.get("champ") or "Gegner"
        lanes.append(_gank_lane(opp_times, span, "gk-dot-opp", opp_label,
                                "gk-lab-opp"))
    return (f'<div class="gk"><div class="gk-h">Gank-/Aktiv-Timing</div>'
            + "".join(lanes) + "</div>")


def _impact_tile_row(champ: str, comps: dict, scale: float, lab_cls: str,
                     badge: str = "") -> str:
    """Eine Zeile der Impact-Kachel: Champ-Label + Segment-Balken (+ Quote)."""
    return (f'<div class="tci-row"><span class="tci-lab {lab_cls}">'
            f'{_esc(champ)}</span>{_impact_bar(comps, scale)}{badge}</div>')


def _impact_phase_bars(rows: list | None) -> str:
    """EINE Early/Mid/Late-Gruppe mit dem kombinierten Impact je Phase.

    Ergaenzt die Gesamt-Ansicht der Sup-vs-Sup-Kachel um den Phasen-Verlauf -
    im selben Diverging-Stil wie `_phase_bars` (`_pb_row`), Delta = me − opp,
    Win/Loss-gefaerbt, gemeinsame Skala ueber die drei Phasen. Die drei
    minuten-aufgeloesten Komponenten sind dabei schon in `analysis` gemergt
    (Schaden + Erlitten + CC·Gewicht), genau wie im Gesamt-Balken darueber -
    darum reicht eine Gruppe (Nutzer-Feedback 2026-07-25). `rows` =
    `impact_pair["phase_rows"]` (analysis.impact_phase_rows); None/leer ->
    nichts rendern. Heilung/Shield und Saves liegen nur als Match-Endwert vor -
    dafuer der Hinweis am Fuss."""
    if not rows:
        return ""
    scale = max((abs(r.get("delta") or 0) for r in rows), default=0) or 1
    bars = "".join(_pb_row(r["label"], r.get("delta"), scale) for r in rows)
    return (f'<div class="tcp"><div class="tcp-h">'
            f'{_esc(analysis.IMPACT_PHASE_LABEL)}</div>{bars}'
            f'<div class="tcp-note">Heilung/Saves nur als Gesamtwert</div></div>')


def _impact_tile(p: dict) -> str:
    """Sup-vs-Sup-Impact-Kachel fuer die UTILITY-Team-Karte (Erweiterung
    2026-07-25).

    Die Karte fokussiert sonst nur Vision (`ROLE_FOCUS["UTILITY"]`) - der
    Composite-Impact (Schaden + Heilung/Shield + Getankt + Utility) stand bis
    dahin nur in der eigenen Sektion. Bausteine sind DIESELBEN wie dort
    (`_IMPACT_SEG`, `_impact_bar` inkl. Save-Chip, `_quote_badge`,
    `_impact_legend`); kachel-lokal ist nur der Massstab: groesster Total des
    Paares, damit der Balken die schmale Kartenbreite ausnutzt. Ohne
    Impact-Daten (key-freier Pfad) fehlt `impact_pair` -> die Kachel entfaellt
    ersatzlos (der Disclaimer erklaert das Fehlen global).

    Unter den Gesamt-Balken steht - sofern die Minuten-Serien vorliegen
    (`phase_rows`, Erweiterung 2026-07-25) - EINE Early/Mid/Late-Gruppe mit dem
    genauso gemergten Impact je Phase (Schaden + Erlitten + CC·Gewicht)."""
    pair = p.get("impact_pair") or {}
    me_c = pair.get("me")
    if not me_c:
        return ""
    opp_c = pair.get("opp")
    scale = max(me_c.get("total", 0) or 0,
                (opp_c or {}).get("total", 0) or 0, 1)
    cp = p.get("counterpart") or {}
    rows = [_impact_tile_row(p.get("champ") or "Du", me_c, scale, "tci-me",
                             _quote_badge(pair.get("quote")))]
    head = "Impact"
    if opp_c:
        rows.append(_impact_tile_row(cp.get("champ") or "Gegner", opp_c, scale,
                                     "tci-opp"))
        head = "Impact — Support vs. Support"
    return (f'<div class="tci"><div class="tci-h">{head}</div>'
            f'{_impact_legend("tci-legend")}{"".join(rows)}'
            f'{_impact_phase_bars(pair.get("phase_rows"))}</div>')


def _team_card(p: dict, duration_min: float = 0.0) -> str:
    role = (p["role"] or "").title()
    cp = p["counterpart"]
    cp_txt = (f"vs. {_esc(cp['champ'])}" if cp else "kein Gegenpart")
    k, d, a = p["kda"]
    ctx = p["context"]
    me_badge = '<span class="me-badge">DU</span>' if p["is_me"] else ""
    sanity = p["item_sanity"]
    miss = sanity["missing"]
    if not sanity["core"]:
        item_line = '<span class="muted">kein Core-Set (KB)</span>'
    elif miss:
        item_line = ('<span class="tag-weak">fehlt:</span> '
                     + ", ".join(_esc(m) for m in miss))
    else:
        item_line = '<span class="tag-strength">Core komplett</span>'

    dp = p["deaths"]
    # Todes-Aufschluesselung Teamfight vs. Pick (aus den Kill-Clustern, §8b S.4).
    dk = p.get("death_kind") or {}
    dk_txt = ""
    if (dk.get("teamfight") or 0) + (dk.get("pick") or 0) > 0:
        dk_txt = (f' <span class="muted">({dk.get("teamfight", 0)} Teamfight · '
                  f'{dk.get("pick", 0)} Pick)</span>')
    # Objective-Praesenz (Proxy, §8b Sektion 5).
    obj = p.get("objective") or {}
    obj_html = ""
    if obj.get("total"):
        obj_html = (f'<span class="tc-obj" title="Kill-Beteiligung ±60 s um '
                    f'eigene Elite-Objectives">Objective-Präsenz '
                    f'{obj["present"]}/{obj["total"]}</span>')
    gank = _gank_strip(p, duration_min) if "gank_times" in p else ""
    return f"""
    <div class="tcard{' tcard-me' if p['is_me'] else ''}">
      <div class="tc-head">
        <div class="tc-champ">{_esc(p['champ'])}{me_badge}
          <small>{_esc(role)} · {cp_txt}</small></div>
        <div class="tc-kda">{k}/{d}/{a}<small>KP {int(round(ctx['kp']*100))}%</small></div>
      </div>
      {_phase_bars(p['deltas'])}
      {_impact_tile(p)}
      {_build_eval_html(p)}
      {_build_replay_html(p)}
      {gank}
      <div class="tc-foot">
        <span title="Tode Early 0–10 / Mid 10–20 / Late 20+">Tode: {dp['early']} · {dp['mid']} · {dp['late']}{dk_txt}</span>
        <span class="tc-items">{item_line}</span>
      </div>
      <div class="tc-foot tc-foot-2">{obj_html}</div>
    </div>"""


# --- Roster-Zeile (Spielernamen genau einmal) -------------------------------

def _roster_row(report: dict) -> str:
    """Kompakte Roster-Zeile je Rolle: eigener Spielername+Champ vs. Gegner.

    Die EINZIGE Stelle mit Spielernamen - alle Detail-Bloecke darunter nutzen nur
    Champ-Namen. Datenquelle: scoreboard (Rollen-Paarung) + ranked_names (Namen)."""
    names = report["ranked_names"]
    rows = []
    for r in report["scoreboard"]:
        role = _esc(r["role"].title())
        me = r["me"]
        opp = r["opp"]
        me_name = _esc(_disp_name(names.get(me["pid"], {}).get("name", ""))) if me else ""
        me_champ = _esc(me["champ"]) if me else "—"
        if opp:
            opp_name = _esc(_disp_name(names.get(opp["pid"], {}).get("name", "")))
            opp_cell = (f'<span class="ro-name">{opp_name}</span>'
                        f'<span class="ro-champ">{_esc(opp["champ"])}</span>')
        else:
            opp_cell = '<span class="ro-champ muted">kein Gegenpart</span>'
        rows.append(
            f'<div class="ro-row"><span class="ro-role">{role}</span>'
            f'<span class="ro-side ro-me"><span class="ro-name">{me_name}</span>'
            f'<span class="ro-champ">{me_champ}</span></span>'
            f'<span class="ro-vs">vs.</span>'
            f'<span class="ro-side ro-opp">{opp_cell}</span></div>')
    return f'<div class="roster">{"".join(rows)}</div>'


# --- Side-by-side-Scoreboard ------------------------------------------------

def _kda_ratio(kda) -> float:
    k, d, a = kda
    return (k + a) / max(1, d)


def _scoreboard(report: dict) -> str:
    """Side-by-side-Scoreboard: je Rolle eine Zeile, in jeder Metrik-Spalte der
    eigene Wert (links) vs. der Gegenpart (rechts), der bessere hervorgehoben.

    Rollenreihenfolge TOP->UTILITY (aus dem Modell). Schaden-Spalte nur bei
    has_damage. Datenquelle: report['scoreboard'] (Endwerte je Paar)."""
    has_dmg = report.get("has_damage", True)
    metrics = [(k, lab, hb) for k, lab, hb in analysis.SCOREBOARD_METRICS
               if k != "dmg" or has_dmg]

    def _fmt_val(key, v):
        if key == "kda":
            return f"{v[0]}/{v[1]}/{v[2]}"
        return _fmt(v)

    def _pair(key, hb, me_v, opp_v):
        # Vergleichswert (KDA ueber Ratio, sonst Rohwert); besseren hervorheben.
        mk = _kda_ratio(me_v) if key == "kda" else me_v
        ok = _kda_ratio(opp_v) if key == "kda" else opp_v
        me_cls, opp_cls = "sb-v", "sb-v"
        if mk != ok:
            better_me = (mk > ok) if hb else (mk < ok)
            me_cls = "sb-v sb-better" if better_me else "sb-v sb-worse"
            opp_cls = "sb-v sb-worse" if better_me else "sb-v sb-better"
        return (f'<td class="sb-cell sb-col-{key}">'
                f'<span class="{me_cls}">{_fmt_val(key, me_v)}</span>'
                f'<span class="sb-sep">·</span>'
                f'<span class="{opp_cls}">{_fmt_val(key, opp_v)}</span></td>')

    head = "".join(f'<th class="sb-col sb-col-{k}">{_esc(lab)}</th>'
                   for k, lab, _hb in metrics)
    rows = []
    for r in report["scoreboard"]:
        me, opp = r["me"], r["opp"]
        me_champ = _esc(me["champ"]) if me else "—"
        opp_champ = _esc(opp["champ"]) if opp else "—"
        matchup = (f'<td class="sb-matchup"><span class="sb-role">'
                   f'{_esc(r["role"].title())}</span>'
                   f'<span class="sb-champs"><b>{me_champ}</b> '
                   f'<span class="muted">vs</span> {opp_champ}</span></td>')
        if not (me and opp):
            # Ohne Gegenpart nur die eigenen Werte, kein Vergleich.
            cells = "".join(
                f'<td class="sb-cell sb-col-{k}"><span class="sb-v">'
                f'{_fmt_val(k, me["vals"][k]) if me else "–"}</span></td>'
                for k, _lab, _hb in metrics)
        else:
            cells = "".join(_pair(k, hb, me["vals"][k], opp["vals"][k])
                            for k, _lab, hb in metrics)
        rows.append(f'<tr>{matchup}{cells}</tr>')
    return (f'<div class="tbl-scroll"><table class="scoreboard"><thead><tr>'
            f'<th class="sb-matchup">Rolle · Du vs. Gegner</th>{head}</tr></thead>'
            f'<tbody>{"".join(rows)}</tbody></table></div>')


# --- Objective/Deaths -------------------------------------------------------

def _objectives_block(report: dict) -> str:
    obj = report["objectives"]
    tw = obj["towers"]
    el = obj["elites"]

    def elite_list(items):
        if not items:
            return '<span class="muted">keine</span>'
        return ", ".join(
            f'{_esc((e["subtype"] or e["monster"] or "").title())} '
            f'<span class="muted">{e["minute"]:.0f}′</span>' for e in items)

    me = report["me"]
    me_player = next(p for p in report["team"] if p["is_me"])
    dp = me_player["deaths"]
    death_times = ", ".join(f"{t:.0f}′" for t in dp["times"]) or "—"

    return f"""
    <div class="grid-2">
      <div class="card">
        <div class="mini-h">Objectives (eigenes Team vs. Gegner)</div>
        <div class="stat-inline">
          <div class="si"><div class="si-v">{tw['me']} : {tw['opp']}</div><div class="si-l">Türme</div></div>
          <div class="si"><div class="si-v">{len(el['me'])} : {len(el['opp'])}</div><div class="si-l">Elite-Monster</div></div>
        </div>
        <p class="note"><strong>Eigene Elite-Kills:</strong> {elite_list(el['me'])}</p>
        <p class="note"><strong>Gegner:</strong> {elite_list(el['opp'])}</p>
      </div>
      <div class="card">
        <div class="mini-h">Deine Tode ({_esc(me['champ'])})</div>
        <div class="stat-inline">
          <div class="si"><div class="si-v warn">{dp['early']}</div><div class="si-l">Early 0–10</div></div>
          <div class="si"><div class="si-v">{dp['mid']}</div><div class="si-l">Mid 10–20</div></div>
          <div class="si"><div class="si-v">{dp['late']}</div><div class="si-l">Late 20+</div></div>
        </div>
        <p class="note"><strong>Zeitpunkte:</strong> {death_times}</p>
      </div>
    </div>"""


# --- Sektion: Schaden je Phase (nur has_damage) -----------------------------

def _phase_pair_bars(rows: list, me_label: str, opp_label: str) -> str:
    """Paar-Balken je Phase (Early/Mid/Late): eigener Wert vs. Gegenpart, auf die
    gemeinsame Maximalskala normiert. `rows`: [{label, me, opp}]."""
    scale = max([abs(r["me"] or 0) for r in rows]
                + [abs(r["opp"] or 0) for r in rows if r["opp"] is not None]
                + [1])
    out = []
    for r in rows:
        me_w = 100 * (r["me"] or 0) / scale
        if r["opp"] is None:
            opp_bar = '<div class="gb-bar"><span class="gb-na">—</span></div>'
        else:
            opp_w = 100 * (r["opp"] or 0) / scale
            opp_bar = (f'<div class="gb-bar"><i style="width:{opp_w:.0f}%;'
                       f'background:{_C_OPP_STROKE}"></i>'
                       f'<span>{_fmt(r["opp"])}</span></div>')
        out.append(
            f'<div class="gb-row"><span class="gb-lab">{_esc(r["label"])}</span>'
            f'<div class="gb-bars">'
            f'<div class="gb-bar"><i style="width:{me_w:.0f}%;'
            f'background:{_C_ME_STROKE}"></i><span>{_fmt(r["me"])}</span></div>'
            f'{opp_bar}</div></div>')
    legend = (f'<div class="gb-legend"><span class="lg"><i style="background:'
              f'{_C_ME_STROKE}"></i>{_esc(me_label)}</span>'
              f'<span class="lg"><i style="background:{_C_OPP_STROKE}"></i>'
              f'{_esc(opp_label)}</span></div>')
    return legend + "".join(out)


def _phase_pair_card(title: str, rows: list, me_label: str,
                     opp_label: str) -> str:
    """Eine Schaden-je-Phase-Karte (frei betitelt) + Paar-Balken (me vs. opp)."""
    return (f'<div class="card"><div class="mini-h">{_esc(title)}</div>'
            f'{_phase_pair_bars(rows, me_label, opp_label)}</div>')


def _damage_phase_section(report: dict) -> str:
    dp = report.get("damage_phases")
    if not dp:
        return ""
    me_champ = report["me"]["champ"]
    opp = next((p["counterpart"] for p in report["team"] if p["is_me"]), None)
    opp_champ = opp["champ"] if opp else "Gegenpart"
    # Reihenfolge: Me-Paar, dann Carry-Lane-Paare (ADC, MID - je nur falls nicht
    # der eigene Spieler und Gegenpart vorhanden), zuletzt Team vs. Team.
    cards = [_phase_pair_card(f"Schaden je Phase — {me_champ} vs. {opp_champ}",
                              dp["duo"], me_champ, opp_champ)]
    for pair in dp.get("role_pairs", []):
        cards.append(_phase_pair_card(
            f'Schaden je Phase — {pair["me_champ"]} vs. {pair["opp_champ"]}',
            pair["rows"], pair["me_champ"], pair["opp_champ"]))
    cards.append(_phase_pair_card("Schaden je Phase — Team vs. Team",
                                  dp["team"], "Eigenes Team", "Gegner"))
    return '<div class="grid-2">' + "".join(cards) + "</div>"


# --- Sektion: Composite-Impact-Score (nur mit impact_raw) -------------------

_IMPACT_SEG = (("damage", "Schaden", "var(--accent)"),
               ("healShield", "Heilung/Shield", "var(--win)"),
               ("tanked", "Getankt", "var(--even)"),
               ("utility", "Utility (CC+Saves)", "var(--util)"))


def _impact_bar(comps: dict, scale: float) -> str:
    """Segmentierter Balken (Schaden/Heilung/Getankt/Utility) fuer einen Spieler.

    Utility fasst CC-Zeit + gerettete Leben in EINEM Segment zusammen; bei
    `saves > 0` haengt zusaetzlich ein Text-Chip ('N× Leben gerettet') an der
    Zeile (die Save-Zahl ist in `comps` erhalten)."""
    segs = []
    for key, _lab, col in _IMPACT_SEG:
        w = 100 * (comps.get(key, 0) or 0) / scale
        if w > 0:
            segs.append(f'<i style="width:{w:.1f}%;background:{col}"></i>')
    saves = comps.get("saves", 0) or 0
    chip = (f'<span class="imp-chip">{saves}× Leben gerettet</span>'
            if saves > 0 else "")
    return (f'<div class="imp-bar">{"".join(segs)}</div>'
            f'<span class="imp-tot">{_fmt(comps.get("total", 0))}</span>{chip}')


def _impact_legend(extra_cls: str = "") -> str:
    """Farb-Legende der Impact-Segmente - EINE Quelle fuer die Impact-Sektion und
    die Sup-vs-Sup-Kachel der UTILITY-Team-Karte (`extra_cls` nur fuer die
    kompaktere Kachel-Variante)."""
    cls = ("imp-legend " + extra_cls).strip()
    inner = " ".join(
        f'<span class="lg"><i style="background:{col}"></i>{_esc(lab)}</span>'
        for _k, lab, col in _IMPACT_SEG)
    return f'<div class="{cls}">{inner}</div>'


def _quote_badge(q) -> str:
    """Kompaktes Badge fuer die rollen-faire Impact-Quote (Anteil des eigenen
    Impacts am Rollen-Gegenpart, in Prozent). Farbklasse ueber Theme-Tokens:
    >=100 % Win, 80-100 % neutral, <80 % Loss. `None` (kein Gegenpart / opp==0)
    -> kein Badge."""
    if q is None:
        return ""
    pct = round(q * 100)
    cls = "q-win" if q >= 1.0 else ("q-neutral" if q >= 0.8 else "q-loss")
    return (f'<span class="imp-quote {cls}" '
            f'title="Anteil am Rollen-Gegenpart">{pct} %</span>')


def _impact_section(report: dict) -> str:
    imp = report.get("impact")
    if not imp:
        return ""
    scores = imp["scores"]
    scale = max([s["total"] for s in scores.values()] + [1])
    # Rollen-faire Quote je Paar (Anteil am Gegenpart) - reine Anzeigeschicht.
    pairs = [(r["me"]["pid"] if r["me"] else None,
              r["opp"]["pid"] if r["opp"] else None)
             for r in report["scoreboard"]]
    quotes = analysis.impact_quotes(scores, pairs)
    rows = []
    for r in report["scoreboard"]:
        me, opp = r["me"], r["opp"]
        me_bar = (_impact_bar(scores[me["pid"]], scale)
                  if me and me["pid"] in scores else '<span class="muted">—</span>')
        opp_bar = (_impact_bar(scores[opp["pid"]], scale)
                   if opp and opp["pid"] in scores else '<span class="muted">—</span>')
        badge = _quote_badge(quotes.get(me["pid"])) if me else ""
        rows.append(
            f'<div class="imp-row"><span class="imp-role">'
            f'{_esc(r["role"].title())}</span>'
            f'<div class="imp-side"><span class="imp-champ">'
            f'{_esc(me["champ"]) if me else "—"}</span>{me_bar}{badge}</div>'
            f'<div class="imp-side"><span class="imp-champ">'
            f'{_esc(opp["champ"]) if opp else "—"}</span>{opp_bar}</div></div>')
    # Zusammenfassung: groesster Rueckstand (niedrigste Quote < 1) zum Gegenpart.
    below = [(r, quotes[r["me"]["pid"]]) for r in report["scoreboard"]
             if r["me"] and quotes.get(r["me"]["pid"]) is not None
             and quotes[r["me"]["pid"]] < 1.0]
    summary = ""
    if below:
        worst_r, worst_q = min(below, key=lambda t: t[1])
        summary = (f'<p class="note imp-quote-summary">Größter Rückstand zum '
                   f'Gegenpart: <strong>{_esc(worst_r["me"]["champ"])}</strong> '
                   f'({round(worst_q * 100)} %)</p>')
    return (f'<div class="card">{_impact_legend()}'
            f'{"".join(rows)}{summary}</div>')


# --- Sektion: Comp-Diagnose beider Teams (key-frei) -------------------------

def _comp_side_row(label: str, side: dict, cls: str) -> str:
    dmg = ("?" if side["ad_pct"] is None
           else f'{side["ad_pct"]}% AD / {side["ap_pct"]}% AP')
    cc = "—" if side["cc"] is None else f'{side["cc"]:.2f}'
    return (f'<div class="cmp-row {cls}"><span class="cmp-team">{_esc(label)}'
            f'</span><span class="cmp-k"><b>{_esc(dmg)}</b><small>Schadenstyp'
            f'</small></span><span class="cmp-k"><b>{side["frontline"]}</b>'
            f'<small>Frontline</small></span><span class="cmp-k"><b>{_esc(cc)}'
            f'</b><small>CC/min</small></span></div>')


def _comp_section(report: dict) -> str:
    comp = report.get("comp")
    if not comp:
        return ""
    return (f'<div class="card">'
            f'{_comp_side_row("Eigenes Team", comp["me"], "cmp-me")}'
            f'{_comp_side_row("Gegner", comp["opp"], "cmp-opp")}'
            f'<p class="note cmp-verdict">{_esc(comp["verdict"])}</p></div>')


# --- Sektion: Teamfight-Erkennung (key-frei) --------------------------------

_TF_BADGE = {"gewonnen": "tf-won", "verloren": "tf-lost", "neutral": "tf-neutral"}


def _tf_champ(entry: dict) -> str:
    """Ein Champ-Label einer Teamfight-Karte. Gefallene (`died`) werden
    durchgestrichen UND ausgegraut (Klasse `tf-dead`) und tragen ein dezentes
    Unicode-✕ vor dem Namen; Ueberlebende normal."""
    name = _esc(entry.get("champ") or "?")
    if entry.get("died"):
        return ('<span class="tf-champ tf-dead">'
                '<span class="tf-x">✕</span>'
                f'<span class="tf-name">{name}</span></span>')
    return f'<span class="tf-champ"><span class="tf-name">{name}</span></span>'


def _tf_team(entries: list, side_cls: str) -> str:
    """Eine Team-Spalte einer Teamfight-Karte (Champs bereits rollensortiert)."""
    if not entries:
        body = '<span class="tf-empty">—</span>'
    else:
        body = "".join(_tf_champ(e) for e in entries)
    return f'<div class="tf-team {side_cls}">{body}</div>'


def _teamfight_section(report: dict) -> str:
    """Teamfight-Sektion als kompakte Karten (Redesign 2026-07-24, Nutzer-
    Feedback): Kopfzeile mit Minute-Chip + Ergebnis (aus eigener Sicht) +
    Ausgang-Badge, darunter zwei Team-Spalten (links eigenes Team, rechts
    Gegner) mit rollensortierten Champs; Gefallene durchgestrichen/ausgegraut.
    Der Kipp-Punkt-Fight bekommt einen dezenten Akzent-Rand (`tf-tip`)."""
    fights = report.get("teamfights") or []
    if not fights:
        return ""
    cards = []
    for f in fights:
        badge_cls = _TF_BADGE.get(f["result"], "tf-neutral")
        tip_cls = " tf-tip" if f.get("tip") else ""
        tip_mark = ('<span class="tf-tip-tag">Kipp-Punkt</span>'
                    if f.get("tip") else "")
        cards.append(
            f'<div class="tf-card{tip_cls}">'
            '<div class="tf-card-head">'
            f'<span class="tf-min">Min {f["minute"]:.0f}</span>'
            f'<span class="tf-score">{f["my_kills"]}:{f["opp_kills"]}</span>'
            f'{tip_mark}'
            f'<span class="tf-badge {badge_cls}">{_esc(f["result"])}</span>'
            '</div>'
            '<div class="tf-teams">'
            f'{_tf_team(f.get("me") or [], "tf-team-me")}'
            '<span class="tf-vs">vs</span>'
            f'{_tf_team(f.get("opp") or [], "tf-team-opp")}'
            '</div></div>')
    return f'<div class="tf-cards">{"".join(cards)}</div>'


# --- Gesamt-HTML ------------------------------------------------------------

def _section(num: int, title: str, lede: str, body: str) -> str:
    """Ein nummerierter Report-Abschnitt (dynamische Nummer, s. render_html)."""
    lede_html = f'<p class="s-lede">{_esc(lede)}</p>' if lede else ""
    return (f'<section><div class="s-head"><span class="s-num">{num:02d}</span>'
            f'<h2>{_esc(title)}</h2></div>{lede_html}{body}</section>')


# Fehlende Schaden-Teile, die der key-freie Report immer betreffen - als
# Fliesstext-Fragment fuer die verschiedenen Disclaimer-Varianten.
_MISSING_PARTS = ("der <strong>Schaden-über-Zeit-Graph</strong>, die "
                  "<strong>Schadensanteil-Metrik</strong>, der "
                  "<strong>Schaden-Phasen-Vergleich</strong> und der "
                  "<strong>Composite-Impact-Score</strong>")

# Die vier zustandsbewussten Disclaimer-Texte (nur der <p>-Inhalt). Ausgewaehlt
# ueber report['damage_status'] - so weiss der Nutzer EHRLICH, warum der Schaden
# fehlt (Bugfix 2026-07-24: pauschales "kein Key" war irrefuehrend, wenn ein Key
# da war und nur Riots Indexierung noch lief oder endgueltig scheiterte).
_DISCLAIMER_BODY = {
    "no_key": (
        "<strong>Kein API-Key aktiv</strong> — dieser Report wurde "
        "<strong>key-frei</strong> aus dem Live-Client-Mitschnitt (Port 2999) "
        f"gebaut. Deshalb fehlen {_MISSING_PARTS}: der Schaden an Champions "
        "steht in den Live-Daten für niemanden zur Verfügung. Alles Übrige "
        "(Gold als Item-Gold, CS, Vision, KDA, Objectives, Todes-Timing, "
        "Comp-Diagnose, Teamfights, Build-Eval, Item-Abgleich) stammt "
        "vollständig aus dem Mitschnitt. Mit hinterlegtem, erreichbarem "
        "API-Key ergänzt der Timeline-Pfad die fehlenden Schaden-Diagramme."),
    "pending": (
        "<strong>Schaden-Analyse wird nachgeladen</strong> — dieser Report "
        "wird automatisch aktualisiert, sobald Riot das Match indexiert hat. "
        f"Bis dahin fehlen {_MISSING_PARTS}; alles Übrige stammt bereits "
        "vollständig aus dem Live-Client-Mitschnitt."),
    "failed": (
        "<strong>Schaden-Analyse nicht möglich</strong> — Riot hat das Match "
        "nicht rechtzeitig indexiert (kein Roster-Treffer über die "
        f"Match-History). Deshalb fehlen {_MISSING_PARTS}; alles Übrige stammt "
        "vollständig aus dem Live-Client-Mitschnitt. Manuell nachholen, sobald "
        "das Match indexiert ist: "
        "<code>uv run python -m pipeline postgame --latest</code>."),
    "disabled": (
        "<strong>Schaden-Analyse übersprungen</strong> — dieser Report wurde "
        "mit <code>--no-enrich</code> bewusst key-frei erzeugt. Deshalb fehlen "
        f"{_MISSING_PARTS}; alles Übrige stammt vollständig aus dem "
        "Live-Client-Mitschnitt. Ohne dieses Flag ergänzt der Timeline-Pfad die "
        "Schaden-Diagramme."),
}


def _disclaimer_block(report: dict) -> str:
    """Zustandsbewusster Disclaimer bei fehlenden Schadensdaten.

    Waehlt ueber report['damage_status'] einen von vier ehrlichen Texten
    (no_key / pending / failed / disabled). Bei has_damage=True (Schaden liegt
    vor) leerer String -> kein Disclaimer. Fehlt das Feld (Alt-Reports / der
    Timeline-Pfad ohne Status), faellt es auf 'no_key' zurueck."""
    if report.get("has_damage", True):
        return ""
    status = report.get("damage_status", "no_key")
    body = _DISCLAIMER_BODY.get(status, _DISCLAIMER_BODY["no_key"])
    return f"""
  <div class="disclaimer">
    <div class="label">Hinweis zur Datengrundlage</div>
    <p>{body}</p>
  </div>"""


def render_html(report: dict) -> str:
    win = report["win"]
    me = report["me"]
    v = report["verdict"]
    has_dmg = report.get("has_damage", True)
    live_dump = report.get("source") == "live_dump"
    # Der Live-Dump kennt kein Endergebnis (kein win-Feld) -> neutraler Chip
    # statt einer irrefuehrenden "Niederlage".
    if live_dump:
        result, result_col = "Live-Mitschnitt", "var(--muted)"
    else:
        result = "Sieg" if win else "Niederlage"
        result_col = "var(--win)" if win else "var(--loss)"

    # Section-01-Lede + Fusszeile quellenabhaengig (Schaden nur im Timeline-Pfad).
    lede_01 = ("Item-Gold (Wert der aktuell gehaltenen Items = Kampfkraft auf "
               "der Karte)" + (", Schaden an Champions und Vision"
               if has_dmg else " und Vision (Ward-Score)")
               + " — du gegen deinen Rollen-Gegenpart und dein Team gegen das "
               "gegnerische. Werte je Minute.")
    if report.get("winprob"):
        lede_01 += (" Zum Schluss die Gewinnchance über die Zeit — eine "
                    "heuristische Schätzung aus diesen Team-Signalen, kein "
                    "trainiertes Modell.")
    if live_dump:
        footer = ("Datengrundlage: Live-Client-Mitschnitt (Port 2999, key-frei) · "
                  "Gold = Item-Gold, Vision = Ward-Score · Massstab = Lobby "
                  "(Rollen-Gegenpart), Item-Set-Abgleich gegen builds.yaml Patch "
                  f"{_esc(report['patch'])} · Post-Game-Report Phase 3 (key-frei).")
    else:
        footer = ("Datengrundlage: Riot Match-V5 (Match + Timeline) · Massstab = "
                  "Lobby (Rollen-Gegenpart), Item-Set-Abgleich gegen builds.yaml "
                  f"Patch {_esc(report['patch'])} · Post-Game-Report Phase 1.")

    dur = report.get("duration_min", 0.0)
    team_cards = "".join(_team_card(p, dur) for p in report["team"])

    # Sektionen dynamisch nummerieren: Verlauf + Scoreboard immer, danach die
    # optionalen Phase-4b-Sektionen (nur wenn Daten vorliegen), dann Team +
    # Objectives. So bleiben die Nummern luecken-frei, egal welche Quelle.
    specs = [
        ("Verlauf über die Zeit", lede_01, _duo_and_team_charts(report)),
        ("Scoreboard — Du vs. Gegner",
         "Je Rolle eigenes Team gegen den direkten Gegenpart: in jeder "
         "Metrik-Spalte dein Wert (links) gegen den Gegner (rechts), der bessere "
         "hervorgehoben. Item-Gold = gehaltenes Item-Gold am Spielende.",
         _scoreboard(report)),
    ]
    if report.get("damage_phases"):
        specs.append((
            "Schaden nach Phasen",
            "Schaden an Champions je Spielphase (Early 0–10 / Mid 10–20 / "
            "Late 20+): du gegen deinen Rollen-Gegenpart und dein Team gegen das "
            "gegnerische. Phasen-Zuwachs, nicht kumuliert.",
            _damage_phase_section(report)))
    if report.get("impact"):
        specs.append((
            "Composite-Impact — Schaden + Heilung + Getankt + Utility",
            "Impact = Schaden + Heilung/Shield + getankter (abgefangener) "
            "Schaden (1:1:1) + Utility (CC-Zeit & gerettete Leben), als "
            "Segment-Balken je Rolle vs. Gegenpart — fair für Supports/Tanks und "
            "Utility-Kits (z. B. Zilean), nicht nur für Carrys. Das Badge je "
            "Zeile zeigt die rollen-faire Quote (Anteil des eigenen Impacts am "
            "Gegenpart) — absolute Punkte sind zwischen Rollen nicht vergleichbar.",
            _impact_section(report)))
    if report.get("comp"):
        specs.append((
            "Comp-Diagnose beider Teams",
            "Schadenstyp (AD/AP), Frontline-Zähler und CC-Last je Comp — "
            "key-frei aus Champion-Priors (Lobby bleibt der Maßstab).",
            _comp_section(report)))
    specs.append((
        "Dein Team, rollenbewusst",
        "Eine Karte je Mitspieler: Phasen-Delta zur Rollen-Fokusmetrik gegen den "
        "Gegenpart (Early 0–10 / Mid 10–20 / Late 20+), Kill-Participation, Tode "
        "(Teamfight/Pick), Build-Reihenfolge + -Timing, Objective-Präsenz. Ein "
        "niedriger Lane-Wert ist nicht automatisch schlecht.",
        f'<div class="tcards">{team_cards}</div>'))
    specs.append((
        "Objectives & Tode",
        "Objective-Kontrolle des Teams und wann deine Tode fielen.",
        _objectives_block(report)))
    # Teamfights bewusst als LETZTE nummerierte Sektion (Nutzer-Wunsch
    # 2026-07-25): grosser Block, erst fuer die Deep-Analyse interessant.
    if report.get("teamfights"):
        specs.append((
            "Teamfights",
            "Kill-Cluster (≥3 Kills in ~20 s) je als Karte: Minute, Ergebnis aus "
            "deiner Sicht und die beteiligten Champions nach Team getrennt (links "
            "dein Team, rechts der Gegner). Gefallene sind durchgestrichen und "
            "ausgegraut.",
            _teamfight_section(report)))
    sections = "".join(_section(i, t, l, b)
                       for i, (t, l, b) in enumerate(specs, start=1))

    return f"""<title>Post-Game {_esc(report['match_id'])} — {_esc(me['champ'])}</title>
<style>
{_CSS}
</style>
<div class="wrap">
  <header>
    <div class="eyebrow">League of Legends · Post-Game-Report</div>
    <h1>{_esc(me['champ'])} <span class="tag">{_esc((me['role'] or '').title())}</span></h1>
    <p class="sub">Vergleich <strong>innerhalb der tatsächlich gespielten Lobby</strong> —
      jeder Spieler gegen seinen direkten Rollen-Gegenpart, nicht gegen High-Elo.</p>
    <div class="meta-row">
      <span class="chip" style="color:{result_col};border-color:{result_col}">{result}</span>
      <span class="chip">{_esc(report['queue'])}</span>
      <span class="chip">{report['duration_min']:.0f} min</span>
      <span class="chip">Patch {_esc(report['patch'])}</span>
      <span class="chip">{_esc(report['match_id'])}</span>
    </div>
  </header>

  <div class="verdict">
    <div class="label">Auto-Verdikt</div>
    <ul class="verdict-lines">{_verdict_items(v)}</ul>
  </div>

  <!-- Roster: Spielernamen genau einmal, je Rolle Du vs. Gegner -->
  <div class="roster-wrap">
    <div class="label">Roster · je Rolle Du vs. Gegner</div>
    {_roster_row(report)}
  </div>
{sections}
{_disclaimer_block(report)}
  <footer>
    {_esc(footer)}
  </footer>
</div>
"""


# CSS-Token-System (uebernommen aus tmp/mavaluz_report.html) + Report-spezifische
# Regeln fuer Charts, Team-Karten, Phasen-Balken und Ranking-Zellen.
_CSS = """
:root {
  --bg:#E7EAEE; --card:#FAFBFC; --card-2:#F1F3F6; --ink:#171A20; --ink-2:#474D59;
  --muted:#78808C; --line:#D6DBE2; --line-strong:#C2C9D2; --accent:#A9781A;
  --accent-bright:#C79A34; --accent-soft:#EAD4A0; --win:#1E8567; --win-soft:#BFE0D4;
  --loss:#BC4761; --loss-soft:#EDCAD3; --even:#868D9A; --util:#6D5AB8;
  --shadow:0 1px 2px rgba(20,24,32,.06),0 6px 18px rgba(20,24,32,.05);
  --mono:ui-monospace,"SF Mono","SFMono-Regular",Menlo,Consolas,monospace;
  --sans:system-ui,-apple-system,"Segoe UI",Roboto,sans-serif;
}
@media (prefers-color-scheme:dark){:root{
  --bg:#0F1217; --card:#171B22; --card-2:#1E232B; --ink:#EBEEF3; --ink-2:#AEB4BF;
  --muted:#6E7480; --line:#262C35; --line-strong:#333A45; --accent:#D6A73E;
  --accent-bright:#E7BE5C; --accent-soft:#4A3C1C; --win:#46BB95; --win-soft:#204036;
  --loss:#E0788F; --loss-soft:#45242C; --even:#7A818D; --util:#9B87E0;
  --shadow:0 1px 2px rgba(0,0,0,.3),0 8px 24px rgba(0,0,0,.35);
}}
:root[data-theme="light"]{
  --bg:#E7EAEE; --card:#FAFBFC; --card-2:#F1F3F6; --ink:#171A20; --ink-2:#474D59;
  --muted:#78808C; --line:#D6DBE2; --line-strong:#C2C9D2; --accent:#A9781A;
  --accent-bright:#C79A34; --accent-soft:#EAD4A0; --win:#1E8567; --loss:#BC4761;
  --win-soft:#BFE0D4; --loss-soft:#EDCAD3; --even:#868D9A; --util:#6D5AB8;
  --shadow:0 1px 2px rgba(20,24,32,.06),0 6px 18px rgba(20,24,32,.05);
}
:root[data-theme="dark"]{
  --bg:#0F1217; --card:#171B22; --card-2:#1E232B; --ink:#EBEEF3; --ink-2:#AEB4BF;
  --muted:#6E7480; --line:#262C35; --line-strong:#333A45; --accent:#D6A73E;
  --accent-bright:#E7BE5C; --accent-soft:#4A3C1C; --win:#46BB95; --loss:#E0788F;
  --win-soft:#204036; --loss-soft:#45242C; --even:#7A818D; --util:#9B87E0;
  --shadow:0 1px 2px rgba(0,0,0,.3),0 8px 24px rgba(0,0,0,.35);
}
*{box-sizing:border-box;}
body{margin:0;background:var(--bg);color:var(--ink);font-family:var(--sans);
  line-height:1.55;-webkit-font-smoothing:antialiased;}
.wrap{max-width:1080px;margin:0 auto;padding:clamp(20px,4vw,56px) clamp(16px,4vw,40px) 80px;}
.eyebrow{font-family:var(--mono);font-size:12px;letter-spacing:.22em;
  text-transform:uppercase;color:var(--accent);font-weight:600;display:flex;
  align-items:center;gap:10px;}
.eyebrow::before{content:"";width:26px;height:2px;background:var(--accent);display:inline-block;}
h1{font-size:clamp(32px,6vw,54px);line-height:1.02;margin:16px 0 6px;
  letter-spacing:-.02em;font-weight:800;}
h1 .tag{color:var(--muted);font-weight:500;}
.sub{color:var(--ink-2);font-size:clamp(15px,2vw,18px);max-width:64ch;}
.meta-row{display:flex;flex-wrap:wrap;gap:8px;margin-top:18px;}
.chip{font-family:var(--mono);font-size:12px;color:var(--ink-2);background:var(--card);
  border:1px solid var(--line);border-radius:999px;padding:5px 12px;}
.verdict{margin:34px 0 12px;background:var(--card);border:1px solid var(--line);
  border-left:3px solid var(--accent);border-radius:12px;padding:22px 26px;box-shadow:var(--shadow);}
.verdict .label{font-family:var(--mono);font-size:11px;letter-spacing:.18em;
  text-transform:uppercase;color:var(--muted);margin-bottom:12px;}
.verdict-lines{margin:0;padding:0;list-style:none;display:flex;
  flex-direction:column;gap:9px;}
.verdict-lines li{position:relative;padding-left:18px;margin:0;
  font-size:clamp(14px,2vw,16.5px);line-height:1.4;font-weight:500;color:var(--ink);}
.verdict-lines li::before{content:"›";position:absolute;left:2px;top:-1px;
  color:var(--accent);font-weight:700;}
.verdict-lines li.lead{font-weight:700;font-size:clamp(15px,2.3vw,18px);}
.verdict strong{color:var(--accent);font-weight:700;}
.disclaimer{margin:40px 0 0;background:var(--card);border:1px solid var(--line);
  border-left:3px solid var(--loss);border-radius:12px;padding:18px 22px;box-shadow:var(--shadow);}
.disclaimer .label{font-family:var(--mono);font-size:11px;letter-spacing:.18em;
  text-transform:uppercase;color:var(--loss);margin-bottom:8px;font-weight:600;}
.disclaimer p{margin:0;font-size:14.5px;line-height:1.5;color:var(--ink-2);}
.disclaimer strong{color:var(--ink);font-weight:700;}
.disclaimer code{font-family:var(--mono);font-size:12.5px;background:var(--line);
  padding:1px 6px;border-radius:5px;color:var(--ink);white-space:nowrap;}
section{margin-top:40px;}
.s-head{display:flex;align-items:baseline;gap:14px;margin-bottom:6px;}
.s-num{font-family:var(--mono);font-size:13px;color:var(--accent);font-weight:600;}
h2{font-size:clamp(20px,3vw,26px);margin:0;letter-spacing:-.01em;font-weight:750;}
.s-lede{color:var(--ink-2);margin:4px 0 20px;max-width:70ch;font-size:15px;}
.card{background:var(--card);border:1px solid var(--line);border-radius:14px;
  padding:clamp(16px,3vw,24px);box-shadow:var(--shadow);}
.grid-2{display:grid;grid-template-columns:1fr 1fr;gap:16px;}
@media (max-width:760px){.grid-2{grid-template-columns:1fr;}}
.muted{color:var(--muted);}
.note{color:var(--muted);font-size:13px;margin:8px 0 0;}
.mini-h{font-weight:650;color:var(--ink);margin-bottom:12px;font-size:15px;}

/* charts: max 2 nebeneinander, groesser, mit Achsen-Ticks */
.chart-grid{display:grid;grid-template-columns:1fr 1fr;gap:18px;}
@media (max-width:720px){.chart-grid{grid-template-columns:1fr;}}
.chart-box{background:var(--card);border:1px solid var(--line);border-radius:12px;
  padding:14px 16px;box-shadow:var(--shadow);}
.chart-title{font-size:13px;font-weight:650;color:var(--ink);margin-bottom:8px;
  display:flex;flex-direction:column;gap:4px;}
/* Gewinnchance: eigene Zeile ueber beide Spalten (auch im 1-spaltigen Umbruch) */
.chart-wide{grid-column:1/-1;}
.chart-unit{font-family:var(--mono);font-size:10.5px;color:var(--muted);
  letter-spacing:.04em;text-transform:uppercase;}
.chart-sub{font-weight:500;font-size:12px;color:var(--muted);}
.chart-legend{display:flex;gap:12px;flex-wrap:wrap;font-weight:500;}
.chart-legend .lg{display:flex;align-items:center;gap:5px;font-family:var(--mono);
  font-size:11px;color:var(--ink-2);}
.chart-legend i{width:10px;height:3px;border-radius:2px;display:inline-block;}
.chart{width:100%;height:auto;display:block;}
.chart .axis{stroke:var(--line-strong);stroke-width:1;}
.chart .grid{stroke:var(--line);stroke-width:1;stroke-dasharray:2 3;}
.chart .tick-x,.chart .tick-y{font-family:var(--mono);font-size:9.5px;fill:var(--muted);}
.chart-empty{background:var(--card);border:1px dashed var(--line);border-radius:12px;
  padding:20px;color:var(--muted);font-size:13px;font-family:var(--mono);}

/* team cards */
.tcards{display:grid;grid-template-columns:1fr 1fr;gap:14px;}
@media (max-width:720px){.tcards{grid-template-columns:1fr;}}
.tcard{background:var(--card);border:1px solid var(--line);border-radius:12px;
  padding:16px 18px;box-shadow:var(--shadow);}
.tcard-me{border-left:3px solid var(--accent);}
.tc-head{display:flex;justify-content:space-between;align-items:flex-start;margin-bottom:12px;}
.tc-champ{font-weight:700;font-size:16px;}
.tc-champ small{display:block;color:var(--muted);font-weight:500;font-size:12px;font-family:var(--mono);}
.tc-kda{font-family:var(--mono);font-weight:700;font-size:15px;text-align:right;}
.tc-kda small{display:block;color:var(--muted);font-weight:500;font-size:11px;}
.me-badge{font-family:var(--mono);font-size:9px;font-weight:700;letter-spacing:.1em;
  background:var(--accent-soft);color:var(--accent);padding:2px 6px;border-radius:5px;margin-left:8px;vertical-align:middle;}
.pb-head{font-family:var(--mono);font-size:10.5px;letter-spacing:.04em;text-transform:uppercase;
  color:var(--muted);margin-bottom:8px;}
.pb-row{display:grid;grid-template-columns:96px 1fr 62px;align-items:center;gap:10px;margin-bottom:7px;}
.pb-lab{font-family:var(--mono);font-size:11px;color:var(--ink-2);}
.pb-track{position:relative;height:18px;background:var(--card-2);border:1px solid var(--line);border-radius:5px;overflow:hidden;}
.pb-mid{position:absolute;left:50%;top:0;bottom:0;width:1px;background:var(--line-strong);}
.pb-fill{position:absolute;top:2px;bottom:2px;border-radius:3px;}
.pb-val{font-family:var(--mono);font-size:12.5px;font-weight:700;text-align:right;}
.pb-na{font-size:11.5px;color:var(--muted);font-family:var(--mono);}
.tc-foot{display:flex;justify-content:space-between;gap:10px;margin-top:12px;padding-top:10px;
  border-top:1px solid var(--line);font-size:12.5px;color:var(--ink-2);font-family:var(--mono);}
.tc-items{text-align:right;}
.tag-strength{color:var(--win);font-weight:650;}
.tag-weak{color:var(--loss);font-weight:650;}

/* roster row (Spielernamen genau einmal) */
.roster-wrap{margin:14px 0 0;background:var(--card);border:1px solid var(--line);
  border-radius:12px;padding:16px 20px;box-shadow:var(--shadow);}
.roster-wrap .label{font-family:var(--mono);font-size:11px;letter-spacing:.16em;
  text-transform:uppercase;color:var(--muted);margin-bottom:12px;}
.roster{display:grid;gap:6px;}
.ro-row{display:grid;grid-template-columns:84px 1fr 28px 1fr;align-items:center;
  gap:10px;font-size:14px;padding:4px 0;border-bottom:1px solid var(--line);}
.ro-row:last-child{border-bottom:none;}
.ro-role{font-family:var(--mono);font-size:11px;letter-spacing:.04em;
  text-transform:uppercase;color:var(--accent);font-weight:600;}
.ro-side{display:flex;flex-direction:column;line-height:1.25;}
.ro-opp{text-align:right;}
.ro-name{font-weight:650;color:var(--ink);}
.ro-champ{font-family:var(--mono);font-size:12px;color:var(--ink-2);}
.ro-vs{font-family:var(--mono);font-size:11px;color:var(--muted);text-align:center;}
@media (max-width:620px){.ro-row{grid-template-columns:64px 1fr 24px 1fr;font-size:13px;}}

/* scoreboard (side-by-side, je Metrik Du links / Gegner rechts) */
table.scoreboard td.sb-cell{text-align:center;}
.sb-matchup{white-space:nowrap;}
.sb-role{font-family:var(--mono);font-size:10.5px;letter-spacing:.04em;
  text-transform:uppercase;color:var(--accent);font-weight:600;display:block;}
.sb-champs{font-family:var(--sans);font-size:13px;}
.sb-champs b{font-weight:700;}
.sb-col{text-align:center;}
.sb-v{font-variant-numeric:tabular-nums;color:var(--ink-2);}
.sb-sep{color:var(--muted);margin:0 5px;}
.sb-better{color:var(--win);font-weight:750;}
.sb-worse{color:var(--muted);font-weight:500;}

/* ranking table (Rest-Styles, weiterhin fuer generische Tabellen) */
.tbl-scroll{overflow-x:auto;border-radius:12px;border:1px solid var(--line);}
table{border-collapse:collapse;width:100%;font-size:14px;}
thead th{font-family:var(--mono);font-size:11px;letter-spacing:.05em;text-transform:uppercase;
  color:var(--muted);text-align:right;padding:11px 12px;background:var(--card-2);
  border-bottom:1px solid var(--line);white-space:nowrap;font-weight:600;}
thead th:first-child,tbody td:first-child{text-align:left;}
tbody td{padding:9px 12px;text-align:right;border-bottom:1px solid var(--line);
  font-variant-numeric:tabular-nums;font-family:var(--mono);white-space:nowrap;}
tbody tr{background:var(--card);}
tbody tr.own{background:linear-gradient(0deg,var(--accent-soft),transparent 160%);}
tbody tr:last-child td{border-bottom:none;}
.champ-name{font-family:var(--sans);font-weight:650;}
.role-cell{font-family:var(--mono);color:var(--ink-2);font-size:12.5px;}
.pct-bar{display:inline-block;width:40px;height:6px;border-radius:4px;background:var(--card-2);
  overflow:hidden;vertical-align:middle;border:1px solid var(--line);margin-right:6px;}
.pct-bar i{display:block;height:100%;background:var(--accent);}
.pct-n{font-weight:700;}
tbody td small{display:block;color:var(--muted);font-weight:500;font-size:10.5px;}

/* stat inline */
.stat-inline{display:flex;gap:26px;flex-wrap:wrap;margin-top:6px;}
.si-v{font-family:var(--mono);font-size:22px;font-weight:700;font-variant-numeric:tabular-nums;}
.si-l{font-size:12.5px;color:var(--muted);}
.si-v.good{color:var(--win);} .si-v.warn{color:var(--loss);}

footer{margin-top:48px;padding-top:20px;border-top:1px solid var(--line);color:var(--muted);
  font-size:13px;font-family:var(--mono);}
@media (prefers-reduced-motion:reduce){*{transition:none!important;}}
a{color:var(--accent);}

/* Phase 4b: Schaden-Phasen (Paar-Balken) */
.gb-legend{display:flex;gap:14px;margin-bottom:10px;}
.gb-legend .lg,.imp-legend .lg{display:flex;align-items:center;gap:6px;
  font-family:var(--mono);font-size:11px;color:var(--ink-2);}
.gb-legend i,.imp-legend i{width:11px;height:11px;border-radius:3px;display:inline-block;}
.gb-row{display:grid;grid-template-columns:78px 1fr;align-items:center;gap:10px;margin-bottom:9px;}
.gb-lab{font-family:var(--mono);font-size:11px;color:var(--ink-2);}
.gb-bars{display:flex;flex-direction:column;gap:4px;}
.gb-bar{display:flex;align-items:center;gap:8px;height:14px;}
.gb-bar i{height:11px;border-radius:3px;min-width:1px;}
.gb-bar span{font-family:var(--mono);font-size:11px;color:var(--ink-2);font-variant-numeric:tabular-nums;}
.gb-na{color:var(--muted);font-family:var(--mono);font-size:11px;}

/* Phase 4b: Composite-Impact (Segment-Balken) */
.imp-legend{display:flex;gap:14px;margin-bottom:12px;flex-wrap:wrap;}
.imp-row{display:grid;grid-template-columns:64px 1fr 1fr;align-items:center;gap:12px;
  margin-bottom:8px;}
.imp-role{font-family:var(--mono);font-size:10.5px;letter-spacing:.04em;
  text-transform:uppercase;color:var(--accent);font-weight:600;}
.imp-side{display:flex;align-items:center;gap:8px;min-width:0;}
.imp-champ{font-size:12px;font-weight:650;width:74px;flex:0 0 auto;
  overflow:hidden;text-overflow:ellipsis;white-space:nowrap;}
.imp-bar{flex:1 1 auto;display:flex;height:14px;border-radius:4px;overflow:hidden;
  background:var(--card-2);border:1px solid var(--line);min-width:20px;}
.imp-bar i{height:100%;}
.imp-tot{font-family:var(--mono);font-size:11px;color:var(--ink-2);
  font-variant-numeric:tabular-nums;flex:0 0 auto;}
.imp-chip{flex:0 0 auto;font-size:10px;font-weight:600;line-height:1;
  padding:3px 7px;border-radius:999px;background:var(--util);color:#fff;
  white-space:nowrap;}
.imp-quote{flex:0 0 auto;font-family:var(--mono);font-size:10.5px;font-weight:700;
  line-height:1;padding:3px 7px;border-radius:999px;white-space:nowrap;
  font-variant-numeric:tabular-nums;border:1px solid transparent;}
.imp-quote.q-win{background:var(--win-soft);color:var(--win);border-color:var(--win);}
.imp-quote.q-neutral{background:var(--card-2);color:var(--ink-2);border-color:var(--line-strong);}
.imp-quote.q-loss{background:var(--loss-soft);color:var(--loss);border-color:var(--loss);}
.imp-quote-summary{margin-top:12px;color:var(--ink-2);font-size:13.5px;}
.imp-quote-summary strong{color:var(--accent);font-weight:700;}

/* Phase 4b: Comp-Diagnose */
.cmp-row{display:grid;grid-template-columns:110px repeat(3,1fr);align-items:center;
  gap:12px;padding:10px 4px;border-bottom:1px solid var(--line);}
.cmp-me{border-left:3px solid var(--accent);padding-left:10px;}
.cmp-opp{border-left:3px solid var(--even);padding-left:10px;}
.cmp-team{font-weight:700;font-size:13px;}
.cmp-k{display:flex;flex-direction:column;}
.cmp-k b{font-family:var(--mono);font-size:14px;font-variant-numeric:tabular-nums;}
.cmp-k small{color:var(--muted);font-size:10.5px;}
.cmp-verdict{margin-top:12px;color:var(--ink-2);font-size:13.5px;}

/* Phase 4b: Teamfights (Karten, Team-Split, Gefallene ausgegraut) — Redesign.
   Kompakt-Layout 2026-07-25: 3 Kacheln je Zeile, responsiv auf 2/1 Spalten
   (gleiche Breakpoint-Logik wie .tcards/.chart-grid). */
.tf-cards{display:grid;grid-template-columns:repeat(3,1fr);gap:10px;}
@media (max-width:900px){.tf-cards{grid-template-columns:1fr 1fr;}}
@media (max-width:600px){.tf-cards{grid-template-columns:1fr;}}
.tf-card{background:var(--card);border:1px solid var(--line);border-radius:10px;
  padding:9px 11px;box-shadow:var(--shadow);min-width:0;}
.tf-tip{border-left:3px solid var(--accent);}
.tf-card-head{display:flex;align-items:center;flex-wrap:wrap;gap:4px 7px;
  margin-bottom:8px;}
.tf-min{font-family:var(--mono);font-size:10.5px;font-weight:600;color:var(--accent);
  background:var(--accent-soft);border-radius:999px;padding:2px 8px;}
.tf-score{font-family:var(--mono);font-weight:700;font-size:12.5px;
  font-variant-numeric:tabular-nums;color:var(--ink);}
.tf-tip-tag{font-family:var(--mono);font-size:8px;font-weight:700;letter-spacing:.05em;
  text-transform:uppercase;color:var(--accent);}
.tf-badge{margin-left:auto;font-family:var(--mono);font-size:8.5px;font-weight:700;
  letter-spacing:.06em;text-transform:uppercase;padding:2px 6px;border-radius:5px;}
.tf-won{color:var(--win);background:var(--win-soft);}
.tf-lost{color:var(--loss);background:var(--loss-soft);}
.tf-neutral{color:var(--muted);background:var(--card-2);}
.tf-teams{display:grid;grid-template-columns:1fr auto 1fr;align-items:start;gap:7px;}
.tf-team{display:flex;flex-direction:column;gap:3px;min-width:0;}
.tf-team-opp{align-items:flex-end;text-align:right;}
.tf-vs{align-self:center;font-family:var(--mono);font-size:9px;color:var(--muted);}
.tf-champ{display:inline-flex;align-items:center;gap:3px;font-size:11px;
  line-height:1.35;font-weight:600;color:var(--ink);max-width:100%;}
.tf-team-me .tf-champ{border-left:2px solid var(--accent-soft);padding-left:5px;}
.tf-team-opp .tf-champ{flex-direction:row-reverse;border-right:2px solid var(--line-strong);
  padding-right:5px;}
.tf-name{overflow:hidden;text-overflow:ellipsis;white-space:nowrap;}
.tf-dead{color:var(--muted);font-weight:500;}
.tf-dead .tf-name{text-decoration:line-through;text-decoration-color:var(--muted);}
.tf-x{font-size:8.5px;font-weight:700;color:var(--loss);flex:0 0 auto;}
.tf-empty{color:var(--muted);font-size:11px;}
@media (max-width:520px){.tf-teams{gap:6px;}.tf-champ{font-size:11.5px;}}

/* Phase 4b: Build-Eval + Gank-Strip + Objective in den Team-Karten */
.be{margin-top:10px;padding-top:10px;border-top:1px dashed var(--line);font-size:12px;}
.be-order{display:flex;align-items:center;gap:8px;flex-wrap:wrap;}
.be-h{font-family:var(--mono);font-size:10px;letter-spacing:.08em;text-transform:uppercase;
  color:var(--muted);}
.be-timing{display:flex;gap:6px;flex-wrap:wrap;margin-top:6px;}
.be-t{font-family:var(--mono);font-size:11px;color:var(--ink-2);background:var(--card-2);
  border:1px solid var(--line);border-radius:5px;padding:2px 7px;}
.be-t.be-behind{color:var(--loss);border-color:var(--loss-soft);}
/* Phase 5: Build-Eval Stufe 3 (Engine-Replay) */
.br{margin-top:8px;font-size:12px;}
.br-dev{margin:5px 0 0;padding-left:16px;list-style:disc;color:var(--ink-2);
  font-size:11.5px;line-height:1.5;}
.br-dev li{margin:1px 0;}
.gk{margin-top:10px;}
.gk-h{font-family:var(--mono);font-size:10px;letter-spacing:.06em;text-transform:uppercase;
  color:var(--muted);margin-bottom:2px;}
.gk-row{display:flex;align-items:center;gap:8px;}
.gk-lab{flex:0 0 68px;font-size:11.5px;font-weight:600;overflow:hidden;
  text-overflow:ellipsis;white-space:nowrap;}
.gk-lab-me{color:var(--accent);}
.gk-lab-opp{color:var(--even);}
.gk-cnt{flex:0 0 auto;font-family:var(--mono);font-size:11px;min-width:14px;text-align:right;}
.gk-svg{flex:1 1 auto;width:100%;height:auto;display:block;}
.gk-axis{stroke:var(--line-strong);stroke-width:1.5;}
.gk-dot{fill:var(--accent);}
.gk-dot-opp{fill:var(--even);}
.tc-foot-2{border-top:none;padding-top:4px;margin-top:0;justify-content:flex-start;}
.tc-obj{color:var(--ink-2);}

/* UTILITY-Karte: Sup-vs-Sup-Impact-Kachel (Erweiterung 2026-07-25) */
.tci{margin-top:10px;padding-top:10px;border-top:1px solid var(--line);}
.tci-h{font-family:var(--mono);font-size:10px;letter-spacing:.06em;
  text-transform:uppercase;color:var(--muted);margin-bottom:6px;}
.tci-legend{gap:10px;margin-bottom:8px;}
.tci-row{display:flex;align-items:center;gap:8px;margin-bottom:5px;}
.tci-lab{flex:0 0 62px;font-size:11.5px;font-weight:600;overflow:hidden;
  text-overflow:ellipsis;white-space:nowrap;}
.tci-me{color:var(--accent);}
.tci-opp{color:var(--even);}
.tci .imp-chip{font-size:9.5px;padding:2px 6px;}
/* Impact-Phasen-Balken der Kachel (kombinierter Impact je Early-Mid-Late) */
.tcp{margin-top:9px;}
.tcp-h{font-family:var(--mono);font-size:9.5px;letter-spacing:.06em;
  text-transform:uppercase;color:var(--muted);margin-bottom:6px;}
.tcp .pb-row{grid-template-columns:78px 1fr 56px;gap:8px;margin-bottom:4px;}
.tcp .pb-track{height:13px;}
.tcp .pb-val{font-size:11.5px;}
.tcp-note{font-family:var(--mono);font-size:9.5px;color:var(--muted);margin-top:2px;}

/* Phase 6: Trend-Report (Prioliste + Mini-Balken + Splits) */
.tr-prio{display:flex;flex-direction:column;gap:12px;}
.tr-item{display:grid;grid-template-columns:34px 1fr;gap:14px;background:var(--card);
  border:1px solid var(--line);border-radius:12px;padding:14px 16px;box-shadow:var(--shadow);}
.tr-item.tr-top{border-left:3px solid var(--accent);}
.tr-rank{font-family:var(--mono);font-size:20px;font-weight:700;color:var(--accent);
  font-variant-numeric:tabular-nums;text-align:center;}
.tr-body{min-width:0;}
.tr-head{display:flex;align-items:baseline;gap:12px;flex-wrap:wrap;margin-bottom:8px;}
.tr-label{font-weight:700;font-size:15px;color:var(--ink);}
.tr-mean{font-family:var(--mono);font-size:13px;font-variant-numeric:tabular-nums;color:var(--ink-2);}
.tr-mean.tr-bad{color:var(--loss);}
.tr-cons{display:flex;align-items:center;gap:8px;margin-left:auto;}
.tr-cons-bar{width:90px;height:8px;border-radius:4px;background:var(--card-2);
  border:1px solid var(--line);overflow:hidden;}
.tr-cons-bar i{display:block;height:100%;background:var(--loss);}
.tr-cons-n{font-family:var(--mono);font-size:11px;font-weight:700;color:var(--ink-2);
  font-variant-numeric:tabular-nums;}
.tr-mini{display:block;width:100%;height:auto;}
.tr-mini .tr-axis{stroke:var(--line-strong);stroke-width:1;}
.tr-empty{color:var(--muted);font-size:13.5px;}
.tr-splits{display:grid;grid-template-columns:1fr 1fr;gap:18px;}
@media (max-width:720px){.tr-splits{grid-template-columns:1fr;}}
.tr-recur{margin:6px 0 0;padding-left:18px;color:var(--ink-2);font-size:13.5px;line-height:1.7;}
"""


# ============================================================================
# Phase 6: Trend-Report (postgame/trend.html)
# ============================================================================

def _trend_date(ms) -> str:
    """Datums-String (YYYY-MM-DD) aus einem ms-Zeitstempel; leer bei None."""
    if not ms:
        return "?"
    import time
    return time.strftime("%Y-%m-%d", time.localtime(ms / 1000.0))


def _game_color(win) -> str:
    """Balken-Farbe je Spiel nach Ergebnis (Win/Loss-Faerbung, s. §6): Sieg =
    Gruen, Niederlage = Rot, unbekannt = neutral."""
    if win is True:
        return "var(--win)"
    if win is False:
        return "var(--loss)"
    return "var(--even)"


def _trend_minibars(per_game: list, kind: str) -> str:
    """Mini-Balkenreihe der Per-Game-Werte (chronologisch) als Inline-SVG.

    Eine Spalte je Spiel, Hoehe proportional zum Betrag, Faerbung nach Win/Loss.
    Bei `delta`/`quote` markiert eine Nulllinie (0 bzw. Quote 1.0) den Gegenpart-
    Gleichstand: Balken darunter = hinten, darueber = vorn. Bei `count`
    (fruehe Tode) waechst der Balken vom Boden."""
    n = len(per_game)
    if n == 0:
        return '<div class="tr-empty">—</div>'
    vals = [g["value"] for g in per_game]
    col_w, gap, h = 14, 4, 46
    width = n * col_w + (n - 1) * gap
    mid = h / 2.0

    if kind == "quote":
        centered = [v - 1.0 for v in vals]
    elif kind == "delta":
        centered = list(vals)
    else:                       # count: vom Boden wachsend
        centered = None
    if centered is not None:
        scale = max((abs(c) for c in centered), default=1.0) or 1.0
    else:
        scale = max(vals + [1])

    bars, base_y = [], (mid if centered is not None else h)
    for i, g in enumerate(per_game):
        x = i * (col_w + gap)
        col = _game_color(g.get("win"))
        if centered is not None:
            c = centered[i]
            bh = (abs(c) / scale) * (mid - 2)
            y = (mid - bh) if c >= 0 else mid
        else:
            bh = (g["value"] / scale) * (h - 3)
            y = h - bh
        bars.append(
            f'<rect x="{x:.1f}" y="{y:.1f}" width="{col_w}" '
            f'height="{max(bh, 1):.1f}" rx="2" fill="{col}"/>')
    axis = (f'<line class="tr-axis" x1="0" y1="{base_y:.1f}" x2="{width}" '
            f'y2="{base_y:.1f}"/>')
    return (f'<svg class="tr-mini" viewBox="0 0 {width} {h}" '
            f'preserveAspectRatio="none" role="img" '
            f'aria-label="Verlauf der letzten {n} Spiele">{axis}'
            f'{"".join(bars)}</svg>')


def _trend_mean(finding: dict) -> tuple[str, bool]:
    """(Anzeige-Text des Mittelwerts, ist_Rueckstand) je Befund-Art."""
    kind, mean = finding.get("kind"), finding.get("mean", 0)
    if kind == "delta":
        return f"Ø {_signed(mean)} vs. Gegenpart", mean < 0
    if kind == "quote":
        return f"Ø {mean * 100:.0f} % des Gegenparts", mean < 1.0
    return f"Ø {mean:.1f} frühe Tode/Spiel", mean >= 1.0


def _trend_priority(agg: dict) -> str:
    """Prioliste: Rang, Metrik, Mittel-Delta, Konsistenz-Balken + Mini-Balken."""
    prio = agg.get("priority") or []
    if not prio:
        return ('<div class="tr-empty">Noch keine belastbaren Trend-Befunde — '
                'mindestens 3 auswertbare Spiele je Metrik nötig.</div>')
    items = []
    for i, f in enumerate(prio, start=1):
        mean_txt, bad = _trend_mean(f)
        cons = f.get("consistency", 0)
        top = ' tr-top' if i == 1 else ''
        items.append(
            f'<div class="tr-item{top}"><div class="tr-rank">{i}</div>'
            f'<div class="tr-body"><div class="tr-head">'
            f'<span class="tr-label">{_esc(f["label"])}</span>'
            f'<span class="tr-mean{" tr-bad" if bad else ""}">{_esc(mean_txt)}</span>'
            f'<span class="tr-cons"><span class="tr-cons-bar">'
            f'<i style="width:{cons * 100:.0f}%"></i></span>'
            f'<span class="tr-cons-n">{f["behind"]}/{f["n"]}</span></span></div>'
            f'{_trend_minibars(f.get("per_game", []), f.get("kind"))}</div></div>')
    return f'<div class="tr-prio">{"".join(items)}</div>'


def _trend_split(rows: list, kind_label: str) -> str:
    """Split-Tabelle je Rolle bzw. Champion: Winrate + Kern-Deltas (Ø)."""
    if not rows:
        return '<div class="tr-empty">—</div>'
    head = (f'<thead><tr><th>{_esc(kind_label)}</th><th>Spiele</th>'
            f'<th>Winrate</th><th>Gold Ø</th><th>CS Ø</th><th>Vis Ø</th></tr></thead>')
    body = []
    for r in rows:
        wr = r.get("winrate")
        wr_txt = f'{wr * 100:.0f} %' if wr is not None else "—"
        d = r.get("deltas", {})
        body.append(
            f'<tr><td class="champ-name">{_esc(r["name"])}</td>'
            f'<td>{r["games"]}</td><td>{wr_txt}</td>'
            f'<td>{_signed(d.get("gold")) if d.get("gold") is not None else "—"}</td>'
            f'<td>{_signed(d.get("cs")) if d.get("cs") is not None else "—"}</td>'
            f'<td>{_signed(d.get("vision")) if d.get("vision") is not None else "—"}</td></tr>')
    return (f'<div class="tbl-scroll"><table>{head}'
            f'<tbody>{"".join(body)}</tbody></table></div>')


def render_trend_html(agg: dict, *, ident: str | None = None) -> str:
    """Self-contained HTML-Seite der Trend-Aggregation (Prioliste + Splits).

    Nimmt den Aggregations-Ausgang aus `trend.aggregate` und rendert ihn im
    Report-Design (gleiche Theme-Tokens, theme-aware, ohne externe Assets)."""
    meta = agg.get("meta", {})
    n = agg.get("n", 0)
    d_from, d_to = _trend_date(meta.get("date_from")), _trend_date(meta.get("date_to"))
    ident_chip = (f'<span class="chip">{_esc(_disp_name(ident))}</span>'
                  if ident else '<span class="chip">alle Records</span>')
    keyless = meta.get("keyless", 0)

    recur = agg.get("recurring") or []
    recur_html = ("".join(f"<li>{_esc(x)}</li>" for x in recur)
                  if recur else "<li>Noch keine wiederkehrenden Befunde.</li>")

    specs = [
        ("Prioliste — woran du arbeiten solltest",
         "Deine Gegenpart-Rückstände über die letzten Spiele, sortiert nach "
         "Konsistenz × Schwere. Der Balken je Zeile zeigt in wie vielen Spielen "
         "du hinten lagst; die Mini-Balken darunter sind der Per-Game-Verlauf "
         "(chronologisch, Sieg grün / Niederlage rot). Nur Metriken mit "
         "mindestens 3 auswertbaren Spielen.",
         _trend_priority(agg)),
        ("Nach Champion",
         "Winrate und Kern-Deltas (Ø vs. Gegenpart) je gespieltem Champion.",
         _trend_split(agg.get("by_champ", []), "Champion")),
        ("Nach Rolle",
         "Winrate und Kern-Deltas (Ø vs. Gegenpart) je Rolle.",
         _trend_split(agg.get("by_role", []), "Rolle")),
        ("Wiederkehrende Befunde",
         "Was sich durch mehrere Spiele zieht (konsistenteste zuerst).",
         f'<ul class="tr-recur">{recur_html}</ul>'),
    ]
    sections = "".join(_section(i, t, l, b)
                       for i, (t, l, b) in enumerate(specs, start=1))

    footer = (f"Trend über {n} Spiele · Zeitraum {d_from} – {d_to} · "
              f"{keyless} von {n} Records key-frei (ohne Schaden-Daten) · "
              f"Massstab = Lobby (Rollen-Gegenpart) · Post-Game-Report Phase 6.")

    return f"""<title>Post-Game-Trend — {n} Spiele</title>
<style>
{_CSS}
</style>
<div class="wrap">
  <header>
    <div class="eyebrow">League of Legends · Post-Game-Trend</div>
    <h1>Trend <span class="tag">letzte {n} Spiele</span></h1>
    <p class="sub">Aggregierte Gegenpart-Deltas über mehrere Spiele — die
      <strong>rauschfreie Übungsliste</strong>, nicht die Story eines einzelnen
      Spiels.</p>
    <div class="meta-row">
      {ident_chip}
      <span class="chip">{n} Spiele</span>
      <span class="chip">{_esc(d_from)} – {_esc(d_to)}</span>
      <span class="chip">{keyless} key-frei</span>
    </div>
  </header>
{sections}
  <footer>
    {_esc(footer)}
  </footer>
</div>
"""
