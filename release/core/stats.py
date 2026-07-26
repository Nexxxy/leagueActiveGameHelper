"""Gemeinsame statistische Hilfsfunktionen (Struktur-Review 2026-07-17 T5).

Vereinheitlicht die zuvor doppelt implementierte Shrinkage-Formel (Befund D3):
app/recommend._shrunk (Item-Prior, SHRINK_K) und die Inline-Rechnung in
pipeline/aggregate._cc_priors (CC-Prior, CC_SHRINK_K) nutzen jetzt dieselbe
Funktion. Die K-Konstanten bleiben getrennte Tunables an ihren Orten.
"""


def shrunk(rate: float, n: float, base: float, k: float) -> float:
    """Geschrumpfte Rate Richtung Basisrate: (rate*n + k*base) / (n+k).

    `k` ist die Staerke des Beta-Priors (Pseudo-Beobachtungen der Basisrate):
    kleine `n` ziehen das Ergebnis nahe an `base`, grosse `n` nahe an den
    Rohwert `rate`. Siehe Befund D3.
    """
    return (rate * n + k * base) / (n + k)
