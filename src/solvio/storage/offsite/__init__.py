"""Offsite Encrypted Backup V1 — die zweite Zielebene derselben Wahrheit.

Dieses Paket ist KEIN zweites Backup-System. Es nimmt den Satz, den die
lokale Maschine (`solvio.storage`) gebaut und geprueft hat, verpackt ihn in
eine age-Huelle und traegt ihn zu einem Dritten, der nichts lesen kann.

Bindend ist der Architekturvertrag in
`docs/design/offsite-encrypted-backup-v1/` — insbesondere:

* Ende-zu-Ende verschluesselt, BEVOR etwas das Haus verlaesst (DEBT-0109).
* Nichts wird automatisch eingeschaltet: kein geladener Job, kein Schalter,
  kein Credential — drei getrennte Besitzerhandlungen.
* Kein Modell und kein Agent kann eine Offsite-Sicherung ausloesen, lesen
  oder loeschen; es gibt kein `offsite_*` im Faehigkeitsvertrag.
* SOLVIO hat beim Provider KEIN Loeschrecht. Loeschen tut der Lifecycle
  des Providers oder der Mensch in der Konsole — nie dieser Code.
"""
