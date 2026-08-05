"""Stance & Vorsprungs-Anzeige: Lage-Einschaetzung (defensiv/ausgewogen/
aggressiv) plus die zwei getrennten Gold-Vorspruenge gegenueber dem direkten
Gegenpart.

Aus recommend.py ausgelagert (Struktur-Review 2026-07-17, Befund S2). Reine
Anzeige-Schicht - steuert die Item-Empfehlung nicht (Befund D, 2026-07-13).

Zwei Vorspruenge mit verschiedenen Aufgaben (Befund 2026-07-17, Live-Fall Gwen):
  - fielded_lead: gemessenes Item-Gold (eigenes minus Gegenpart). Das ist die
    ehrliche Kampfkraft auf der Map und treibt Anzeige UND Stance. Der alte
    CS/Level/Kills-Proxy schaetzte VERDIENTES Gold und log dabei um mehrere
    Tausend (er ignorierte gebanktes Gold und mass am feedenden Gegner vorbei).
  - earned_lead: geschaetztes VERDIENTES Gold (Item-Gold + Bank). Nur DAS darf
    `gold_state` konditionieren, weil die KB-by_state-Statistiken aus der
    Timeline-`totalGold`-Differenz (verdientes Gold) gebaut wurden.
"""


# fielded_lead-Schwelle: ab dieser gemessenen Item-Gold-Differenz gilt der
# Vorsprung als aussagekraeftig (Stance vorne/hinten).
LEAD_GOLD = 1000
# earned_lead-Schwelle fuer gold_state: an die Train-Definition der Pipeline
# angeglichen (pipeline.aggregate.GOLD_LEAD = 1500). Nur so wird live derselbe
# Zustand abgefragt, unter dem die by_state-Zellen gezaehlt wurden.
STATE_LEAD_GOLD = 1500
# Ab so viel gebanktem Gold zusaetzlich ein klarer Hinweis, es beim naechsten
# Reset auszugeben (gehortetes Gold arbeitet nicht).
BANK_NOTE_GOLD = 1200
# Geschaetzte Gegner-Bank fuer earned_lead: die Bank der Gegner ist ueber die
# Live Client Data API unbeobachtbar - eine kleine feste Schaetzung.
OPP_BANK_EST = 700


def _counterpart(my_role: str | None, enemies: list[dict]) -> dict | None:
    """Direkter Gegenpart (gleiche Rolle). Fuer JUNGLE bevorzugt der EINDEUTIGE
    Smite-Traeger, unabhaengig vom Slot: Die Live-API liefert keine Positionen,
    die Rolle wird aus der Scoreboard-Reihenfolge geraten und kann den Jungler
    falsch einsortieren. Genau ein Smite-Traeger -> der ist der Gegen-Jungler.
    Kein oder mehrere Smites -> Fallback auf den Rollen-Vergleich."""
    if my_role == "JUNGLE":
        smiters = [e for e in enemies if e.get("has_smite")]
        if len(smiters) == 1:
            return smiters[0]
    if not my_role:
        return None
    return next((e for e in enemies if e.get("role") and e.get("role") == my_role), None)


def fielded_lead(my_gold_spent: int, my_role: str | None,
                 enemies: list[dict]) -> tuple[int | None, dict | None]:
    """Anzeige-Vorsprung = eigenes Item-Gold minus Item-Gold des Gegenparts.
    Misst die tatsaechlich gebaute Kampfkraft (nicht CS/Level/Kills). Rueckgabe:
    (lead, gegner) oder (None, None), wenn kein Gegenpart bekannt ist - dann gibt
    es bewusst KEINE Vorsprungs-Aussage."""
    opp = _counterpart(my_role, enemies)
    if opp is None:
        return None, None
    return my_gold_spent - opp.get("gold_spent", 0), opp


def earned_lead(my_gold_spent: int, current_gold: int | None,
                opp: dict | None) -> int | None:
    """Schaetzung des VERDIENTEN Gold-Vorsprungs (fuer gold_state / KB-by_state).
    Eigenes verdientes Gold ~ Item-Gold + Bank (fast exakt; Consumables sind eine
    bekannte Unschaerfe); Gegner ~ Item-Gold + OPP_BANK_EST (Bank unbeobachtbar).
    None ohne erkannten Gegenpart."""
    if opp is None:
        return None
    my_earned = my_gold_spent + (current_gold or 0)
    opp_earned = opp.get("gold_spent", 0) + OPP_BANK_EST
    return int(my_earned - opp_earned)


def _k(gold: float) -> str:
    """Kompakte k-Darstellung eines Gold-Betrags (Bestandsstil: 1.3k)."""
    return f"{gold / 1000:.1f}k"


def lead_note(lead: int | None, opp: dict | None,
              team_lead: int | None, current_gold: int | None) -> str:
    """Anzeige-Note im Bestandsstil, z.B.:
      ' Items: -1.8k hinter Sylas | Team +1.7k | 1.3k auf der Bank'
    Der Gegenpart wird beim CHAMPION-Namen genannt. Team-Kontext = Item-Gold des
    eigenen Teams minus Gegnerteam (None -> weggelassen). Bank = eigenes
    currentGold gerundet; ab BANK_NOTE_GOLD zusaetzlich der Reset-Hinweis. Leerer
    String ohne erkannten Gegenpart (fuehrendes Leerzeichen, wenn gefuellt, damit
    sie sich direkt an den Stance-Satz anschliesst)."""
    if lead is None or opp is None:
        return ""
    name = opp.get("name", "Gegenpart")
    if lead <= -LEAD_GOLD:
        items_txt = f"-{_k(abs(lead))} hinter {name}"
    elif lead >= LEAD_GOLD:
        items_txt = f"+{_k(lead)} vor {name}"
    else:
        items_txt = f"gleichauf mit {name}"
    parts = [f"Items: {items_txt}"]
    if team_lead is not None:
        sign = "+" if team_lead >= 0 else "-"
        parts.append(f"Team {sign}{_k(abs(team_lead))}")
    bank = int(round(current_gold)) if current_gold is not None else 0
    if bank >= 100:
        parts.append(f"{_k(bank)} auf der Bank")
    note = " | ".join(parts)
    if current_gold is not None and current_gold >= BANK_NOTE_GOLD:
        note += " - beim naechsten Reset ausgeben"
    return " " + note


def own_stance(my_scores: dict, enemy_fed: bool,
               lead: int | None = None, note: str = "") -> tuple[str, str]:
    """`lead` ist der fielded_lead (gemessenes Item-Gold vs. Gegenpart). `note`
    ist die vorgefertigte Items/Team/Bank-Zeile (lead_note), die an die
    Begruendung angehaengt wird - im Test-/Backtest-Pfad leer.

    `enemy_fed`: ist ein Gegner ABSOLUT stark fed (profiling.is_strongly_fed)?
    Ersetzt den frueheren relativen top_threat>=0.8-Trigger (Review-Befund E):
    in ausgeglichenen Spielen ist der reichste Gegner NICHT mehr automatisch
    'fed', die Stance kippt nicht mehr zu leicht auf defensiv.

    Die drei defensiven Texte sind bewusst reine Lagebeschreibung OHNE Kauf-
    Empfehlung: Seit Befund D (2026-07-13) ist die Stance reine Anzeige und
    steuert die Item-Empfehlung nicht. Ein "defensiver Kauf sichert ab" neben
    einer offensiven naechsten Empfehlung wirkte widerspruechlich (Befund H,
    review-2026-07-15.md)."""
    kills = my_scores.get("kills", 0)
    deaths = my_scores.get("deaths", 0)
    assists = my_scores.get("assists", 0)
    kda = (kills + assists) / max(1, deaths)
    kda_ahead = kda >= 3 and kills >= 3
    ahead = lead is not None and lead >= LEAD_GOLD
    behind = lead is not None and lead <= -LEAD_GOLD
    struggling = deaths >= 4 and kda < 1.5

    # Viele Tode ziehen NICHT mehr automatisch "defensiv" nach sich, wenn ein
    # messbarer Item-Gold-Vorsprung dagegen spricht: Wer 2k vor dem Gegenpart
    # liegt, baut weiter auf Druck, auch bei magerer KDA.
    if (kda_ahead or ahead) and not behind:
        if enemy_fed:
            stance, reason = "balanced", "Du bist vorne, aber ein Gegner ist sehr fed - nicht uebermuetig werden."
        else:
            stance, reason = "aggressive", "Du bist vorne - Vorsprung in Druck umwandeln."
    elif struggling:
        # Ehrliche Begruendung: Tode sind belegt, ein Rueckstand ist es nicht.
        stance, reason = ("defensive",
                          f"Du bist oft gestorben ({kills}/{deaths}/{assists}) - "
                          f"spiel vorsichtig, bis dein naechster Spike steht.")
    elif behind:
        stance, reason = ("defensive",
                          "Du liegst gegen deinen direkten Gegner zurueck - "
                          "Fights nur mit klarem Vorteil annehmen.")
    elif enemy_fed:
        stance, reason = "defensive", "Ein Gegner ist stark fed - halte Abstand, bis dein Build aufholt."
    else:
        stance, reason = "balanced", "Ausgeglichenes Spiel - Standard-Build weiterziehen."
    return stance, reason + note


def _stance_note(stance: str) -> str:
    """Zusatz-Hinweis, warum die Item-Empfehlung trotz defensiver Stance NICHT
    defensiv vorzieht. Nur bei defensiver Stance (Befund H,
    review-2026-07-15.md / Befund D, 2026-07-13); sonst leer, damit das Frontend
    per Falsy-Check rendern kann.

    Seit der Entfernung der Stance-Score-Schicht (Testsuite-Review 2026-08-04)
    gibt es keinen zweiten Modus mehr, in dem defensiv vorgezogen wuerde - die
    Note haengt darum nur noch an der Stance selbst."""
    if stance == "defensive":
        return ("Die Item-Empfehlung folgt bewusst weiter der gelernten "
                "High-Elo-Reihenfolge - defensives Vorziehen hat im Backtest "
                "auch bei Rueckstand nicht haeufiger gewonnen.")
    return ""
