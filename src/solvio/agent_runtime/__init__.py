"""Die Agentenlaufzeit: SOLVIO zerlegt ein Ziel, laesst Spezialisten arbeiten,
prueft deren Ergebnisse und fuehrt reale Wirkung ausschliesslich ueber den
bestehenden Autoritaetsstapel aus.

Die Besitzregel darueber ist nicht neu (ADR-0001/0003/0012):

    Agenten liefern Arbeit. SOLVIO besitzt Autoritaet.

Dieses Paket importiert bewusst **weder** `solvio.knowledge` **noch**
`solvio.memory` — ein Lauf kann Wissen und Gedaechtnis vorschlagen, nie
schreiben. Ein AST-Test nagelt das fest.
"""
