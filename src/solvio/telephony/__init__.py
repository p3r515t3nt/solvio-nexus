"""Telefonie — SOLVIO ruft an, ein Anbieter spricht.

Diese Schicht ist bewusst duenn. SOLVIO baut keinen SIP-Stack, keinen
RTP-Stack und keinen Sprachagenten: das Telefonnetz und die Stimme sind
austauschbare Teile. Was hier wohnt, ist das, was SOLVIO besitzen MUSS —
die Bindung an einen bestaetigten Empfaenger, die Freigabe, die Kostengrenze
und die Wahrheit ueber den Ausgang eines Gespraechs.

Der Anbieter (heute ElevenLabs Agents ueber Twilio) steht hinter einer
anbieterneutralen Naht. Ein Wechsel spaeter ist ein neuer Adapter, keine
neue Faehigkeit.
"""
