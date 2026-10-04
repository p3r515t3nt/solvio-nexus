#!/bin/bash
# SOLVIO — Portal-Arbeiter: das eine Stueck, das Administratorrechte braucht.
#
# Alles andere ist bereits eingerichtet. Dieses Skript legt die zweite
# Unix-Kennung an, unter der der angemeldete Browser laeuft, und haengt den
# Dienst ein. Ohne diesen Schritt gibt es keine echte Trennung — und dann gibt
# es auch keine angemeldeten Portale, sondern eine ehrliche Fehlermeldung.
#
# Es wird KEIN Passwort gelesen, KEIN Geheimnis angefasst und nichts an SOLVIO
# selbst geaendert. Jede Zeile steht hier zum Nachlesen.
#
#   sudo bash scripts/portal_user_setup.sh
#
# Rueckgaengig:  sudo bash scripts/portal_user_setup.sh --remove
set -euo pipefail

USER_NAME="solvio-portal"
USER_UID=502
USER_GID=502
HOME_DIR="/var/solvio-portal"
RUN_DIR="$HOME_DIR/run"
APP_DIR="/Users/Shared/solvio-portal"
PLIST="/Library/LaunchDaemons/de.solvio.portal-worker.plist"
CORE_USER="solvio"
CORE_UID="$(id -u "$CORE_USER")"

if [ "$(id -u)" != "0" ]; then
  echo "Dieses Skript braucht root. Bitte mit sudo starten." >&2
  exit 1
fi

if [ "${1:-}" = "--remove" ]; then
  launchctl bootout system "$PLIST" 2>/dev/null || true
  rm -f "$PLIST"
  dscl . -delete "/Users/$USER_NAME" 2>/dev/null || true
  dscl . -delete "/Groups/$USER_NAME" 2>/dev/null || true
  rm -rf "$HOME_DIR"
  chmod 0750 "/Users/$CORE_USER"
  echo "entfernt: Dienst, Konto, Gruppe, Zuhause. /Users/$CORE_USER wieder 0750."
  exit 0
fi

echo "== 1/6 Gruppe $USER_NAME (gid $USER_GID) =="
# Eine EIGENE Primaergruppe, ausdruecklich nicht 'staff'. Das ist der Kern:
# /Users/solvio ist 0750 mit Gruppe staff — ein Standardkonto landet ohne diese
# Angabe in staff und koennte den gesamten Core-Quellbaum lesen.
if ! dscl . -read "/Groups/$USER_NAME" >/dev/null 2>&1; then
  dscl . -create "/Groups/$USER_NAME"
  dscl . -create "/Groups/$USER_NAME" PrimaryGroupID "$USER_GID"
  dscl . -create "/Groups/$USER_NAME" RealName "SOLVIO Portal Worker"
fi
# Der Core muss den Socket erreichen duerfen; dafuer genuegt die Mitgliedschaft.
dseditgroup -o edit -a "$CORE_USER" -t user "$USER_NAME" 2>/dev/null || true

echo "== 2/6 Konto $USER_NAME (uid $USER_UID, kein Administrator) =="
if ! dscl . -read "/Users/$USER_NAME" >/dev/null 2>&1; then
  sysadminctl -addUser "$USER_NAME" \
    -fullName "SOLVIO Portal Worker" \
    -UID "$USER_UID" -GID "$USER_GID" \
    -shell /usr/bin/false -home "$HOME_DIR"
fi
# Keine Anmeldung, kein Erscheinen im Anmeldefenster, kein Passwort.
dscl . -create "/Users/$USER_NAME" IsHidden 1
dscl . -create "/Users/$USER_NAME" Password '*'
dscl . -delete "/Users/$USER_NAME" AuthenticationAuthority 2>/dev/null || true
# Ohne '-admin' ist es ein Standardkonto. Zur Sicherheit noch einmal ausdruecklich:
dseditgroup -o edit -d "$USER_NAME" -t user admin 2>/dev/null || true
dseditgroup -o edit -d "$USER_NAME" -t user wheel 2>/dev/null || true

echo "== 3/6 Zuhause und Socket-Verzeichnis =="
# Die TATSAECHLICH vergebene Kennung nehmen, nicht die gewuenschte. `sysadminctl`
# darf eine belegte oder anderweitig beanspruchte UID ablehnen und eine andere
# vergeben — hier war 502 nicht zu bekommen und es wurde 503. Wer stattdessen
# `$USER_UID` weiterverwendet, verschenkt das Zuhause an eine Kennung, die es
# gar nicht gibt, und der Dienst kann in seinem eigenen Ordner nicht schreiben.
REAL_UID="$(id -u "$USER_NAME")"
REAL_GID="$(id -g "$USER_NAME")"
echo "  vergeben: uid=$REAL_UID gid=$REAL_GID (gewuenscht war $USER_UID/$USER_GID)"
mkdir -p "$RUN_DIR"
chown -R "$REAL_UID:$REAL_GID" "$HOME_DIR"
chmod 0710 "$HOME_DIR"      # Gruppe darf durchqueren, sonst niemand
chmod 0710 "$RUN_DIR"       # der Socket darin traegt 0660

echo "== 4/6 /Users/$CORE_USER schliessen =="
# Zweite, unabhaengige Absicherung. Heute ist /Users/solvio 0750 mit Gruppe
# staff; waere das Arbeitskonto je in staff, laege der ganze Core-Quellbaum
# offen. Nebenwirkung: der Ordner ~/Public wird nicht mehr geteilt.
chmod 0700 "/Users/$CORE_USER"

echo "== 5/6 Dienst einhaengen =="
cat > "$PLIST" <<PLISTEOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key><string>de.solvio.portal-worker</string>
    <key>UserName</key><string>$USER_NAME</string>
    <key>GroupName</key><string>$USER_NAME</string>
    <key>InitGroups</key><true/>
    <key>ProgramArguments</key>
    <array>
        <string>$APP_DIR/venv/bin/python</string>
        <string>-u</string><string>-m</string><string>solvio.portal.service</string>
        <string>--socket</string><string>$RUN_DIR/portal.sock</string>
        <string>--core-uid</string><string>$CORE_UID</string>
    </array>
    <key>EnvironmentVariables</key>
    <dict>
        <key>PYTHONPATH</key><string>$APP_DIR/app</string>
        <key>HOME</key><string>$HOME_DIR</string>
        <key>PATH</key><string>/usr/bin:/bin:/usr/sbin:/sbin</string>
    </dict>
    <key>WorkingDirectory</key><string>$HOME_DIR</string>
    <key>KeepAlive</key><true/>
    <key>RunAtLoad</key><true/>
    <key>ProcessType</key><string>Background</string>
    <key>StandardOutPath</key><string>$HOME_DIR/worker.log</string>
    <key>StandardErrorPath</key><string>$HOME_DIR/worker.log</string>
</dict>
</plist>
PLISTEOF
chown root:wheel "$PLIST"
chmod 0644 "$PLIST"
launchctl bootout system "$PLIST" 2>/dev/null || true
launchctl bootstrap system "$PLIST"

echo "== 6/6 Nachpruefen =="
sleep 3
echo "  Konto      : $(id "$USER_NAME" 2>&1)"
echo "  Zuhause    : $(stat -f '%Su:%Sg %Sp' "$HOME_DIR")"
echo "  Administr. : $(dsmemberutil checkmembership -U "$USER_NAME" -G admin 2>&1)"
echo "  /Users/$CORE_USER : $(stat -f '%Sp' "/Users/$CORE_USER")"
echo "  Dienst     : $(launchctl print system/de.solvio.portal-worker 2>/dev/null | awk '/state =/{print $3}')"
echo "  Socket     : $(ls -l "$RUN_DIR/portal.sock" 2>&1)"
echo
echo "Fertig. SOLVIO kann den Portal-Arbeiter jetzt erreichen."
