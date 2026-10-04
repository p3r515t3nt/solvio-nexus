"""Adaptive Memory — SOLVIO darf lernen, ohne Gewissheit zu erfinden.

Der Bauplan steht in `docs/design/adaptive-memory-v1/`. Vier Module, vier
Verantwortungen, und eine Grenze, die zwischen ihnen verlaeuft:

    candidates.py   der Kandidatenspeicher — Vermutungen, KEIN Gedaechtnis
    policy.py       die deterministische Core-Entscheidung
    extractor.py    der Modellvorschlag — schlaegt vor, stempelt nichts
    pipeline.py     die Verdrahtung: Turn -> Vorschlag -> Policy -> Gedaechtnis

Die Grenze: alles unter `extractor.py` ist Vorschlag. Alles unter `policy.py`
ist Entscheidung. Ein Modell darf die Grenze nie ueberschreiten — es liefert
Beobachtungen, es loest keinen einzigen Zustandsuebergang aus.
"""
