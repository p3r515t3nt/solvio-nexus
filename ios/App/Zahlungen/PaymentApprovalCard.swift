// Der Kaufbildschirm — gebaut AUSSCHLIESSLICH aus dem signierten Auftrag.
//
// Die Regel des Freigabeprotokolls V2 ist unveraendert: massgeblich ist die
// vom Mac signierte Challenge, und `human_summary` ist ein unverbindlicher
// Hinweis der KI. Diese Karte erfindet deshalb nichts und holt nichts nach.
// Sie LIEST den signierten `task` und ordnet ihn — mehr nicht.
//
// Warum ueberhaupt ordnen: der Auftrag ist eine Liste aus
// `Beschriftung: <json>`-Zeilen. Das ist eindeutig und pruefbar, aber niemand
// liest unter einem Kaufknopf zwoelf monospaced Zeilen zu Ende. Der Betrag
// gehoert gross und allein, nicht als achte Zeile in einem Block. Deshalb
// diese Karte — und deshalb bleibt der vollstaendige Auftrag DARUNTER
// weiterhin woertlich stehen. Es wird nichts versteckt und nichts ersetzt.
//
// Und wenn der Text nicht passt: dann zeigt diese Karte gar nichts. Eine
// Anzeige, die aus einem unverstandenen Text etwas Huebsches macht, ist
// gefaehrlicher als keine.
import SwiftUI

/// Die Felder eines Kaufs, wie sie im signierten Auftrag stehen.
struct ParsedPurchase: Equatable {
    var headline: String = ""
    var fields: [String: String] = [:]

    var haendler: String { fields["Händler"] ?? "" }
    var adresse: String { fields["Adresse"] ?? "" }
    var posten: String { fields["Artikel"] ?? "" }
    var betrag: String { fields["Gesamtbetrag"] ?? "" }
    var zwischensumme: String { fields["Zwischensumme"] ?? "" }
    var aufschlaege: String { fields["Hinzu kommen"] ?? "" }
    var belastet: String { fields["Belastet wird"] ?? "" }
    var zahlungsmittel: String { fields["Zahlungsmittel"] ?? "" }
    var lieferung: String { fields["Lieferung"] ?? "" }
    var gueltigBis: String { fields["Gültig bis"] ?? "" }
    var vorgang: String { fields["Vorgang"] ?? "" }
    var herkunft: String { fields["Angefragt über"] ?? "" }

    /// Ohne diese vier gibt es keinen Kaufbildschirm. Fehlt eines, faellt die
    /// Anzeige auf den woertlichen Auftrag zurueck — fail closed.
    var complete: Bool {
        !haendler.isEmpty && !betrag.isEmpty && !zahlungsmittel.isEmpty
            && !adresse.isEmpty
    }

    /// Liest den signierten Auftrag. Kein Netz, kein Zustand, keine Annahme.
    ///
    /// Der Core baut den Text als `Ueberschrift` + je Angabe eine Zeile
    /// `Beschriftung: <json-kodierter Wert>` + `Angefragt über: <Herkunft>`.
    /// Genau das wird hier rueckgelesen, und nichts wird ergaenzt.
    static func parse(_ task: String) -> ParsedPurchase {
        var out = ParsedPurchase()
        let lines = task.split(separator: "\n", omittingEmptySubsequences: false)
        for (index, raw) in lines.enumerated() {
            let line = String(raw)
            if index == 0 {
                out.headline = line
                continue
            }
            guard let split = line.firstIndex(of: ":") else { continue }
            let label = String(line[line.startIndex..<split])
                .trimmingCharacters(in: .whitespaces)
            let rest = String(line[line.index(after: split)...])
                .trimmingCharacters(in: .whitespaces)
            out.fields[label] = decodeJSONString(rest)
        }
        return out
    }

    /// `"84,99 EUR"` -> `84,99 EUR`. Nicht-Strings bleiben, wie sie sind.
    private static func decodeJSONString(_ raw: String) -> String {
        guard raw.hasPrefix("\""),
              let data = raw.data(using: .utf8),
              let value = try? JSONSerialization.jsonObject(
                with: data, options: [.fragmentsAllowed]) as? String
        else { return raw }
        return value
    }
}

/// Die Karte selbst. Zeigt nur, was im signierten Auftrag steht.
struct PaymentApprovalCard: View {
    let parsed: ParsedPurchase

    var body: some View {
        VStack(alignment: .leading, spacing: 16) {
            VStack(alignment: .leading, spacing: 4) {
                Text("KAUF")
                    .font(.caption2.weight(.bold))
                    .tracking(1.2)
                    .foregroundStyle(Theme.ink3)
                if !parsed.posten.isEmpty {
                    Text(parsed.posten)
                        .font(.display(.title3, weight: .semibold))
                        .foregroundStyle(Theme.ink)
                        .fixedSize(horizontal: false, vertical: true)
                }
            }

            // Der Haendler — MIT der Adresse, und die ist das Verbindliche.
            // „Amazon" ist ein Wort; `https://www.amazon.de` ist ein Ort.
            VStack(alignment: .leading, spacing: 2) {
                Text(parsed.haendler)
                    .font(.body.weight(.medium)).foregroundStyle(Theme.ink)
                Text(parsed.adresse)
                    .font(.caption.monospaced()).foregroundStyle(Theme.ink2)
                    .lineLimit(1).truncationMode(.middle)
            }

            // DER BETRAG. Gross, allein, unuebersehbar.
            VStack(alignment: .leading, spacing: 2) {
                Text(parsed.betrag)
                    .font(.system(size: 40, weight: .semibold, design: .rounded))
                    .foregroundStyle(Theme.ink)
                    .minimumScaleFactor(0.6).lineLimit(1)
                if !parsed.zwischensumme.isEmpty || !parsed.aufschlaege.isEmpty {
                    Text(breakdown)
                        .font(.footnote).foregroundStyle(Theme.ink2)
                        .fixedSize(horizontal: false, vertical: true)
                }
                if !parsed.belastet.isEmpty && parsed.belastet != "wie oben" {
                    // Rechnet der Anbieter um, steht die WIRKLICH belastete
                    // Waehrung hier — vor der Freigabe, nicht auf der Abrechnung.
                    Text("Deine Bank belastet: \(parsed.belastet)")
                        .font(.footnote.weight(.medium))
                        .foregroundStyle(Theme.warn)
                }
            }

            Divider().overlay(Theme.line)

            detail("Zahlungsmittel", parsed.zahlungsmittel)
            if !parsed.lieferung.isEmpty && parsed.lieferung != "keine" {
                detail("Lieferung", parsed.lieferung)
            }
            if !parsed.gueltigBis.isEmpty {
                detail("Gültig bis", humanTime(parsed.gueltigBis))
            }
            if !parsed.herkunft.isEmpty {
                detail("Angefragt über", parsed.herkunft)
            }
        }
        .padding(Theme.Space.card)
        .frame(maxWidth: .infinity, alignment: .leading)
        .card()
    }

    private var breakdown: String {
        var parts: [String] = []
        if !parsed.zwischensumme.isEmpty { parts.append(parsed.zwischensumme) }
        if !parsed.aufschlaege.isEmpty && parsed.aufschlaege != "keine" {
            parts.append(parsed.aufschlaege)
        }
        return parts.joined(separator: "  ·  ")
    }

    private func detail(_ label: String, _ value: String) -> some View {
        HStack(alignment: .top, spacing: 10) {
            Text(label)
                .font(.footnote).foregroundStyle(Theme.ink3)
                .frame(width: 118, alignment: .leading)
            Text(value)
                .font(.footnote.weight(.medium)).foregroundStyle(Theme.ink)
                .fixedSize(horizontal: false, vertical: true)
            Spacer(minLength: 0)
        }
    }

    /// `2026-08-27T16:45:06+00:00` -> `heute, 18:45`. Schlaegt das fehl, steht
    /// der Rohtext da — falsch geraten waere schlimmer als unschoen.
    private func humanTime(_ iso: String) -> String {
        let parser = ISO8601DateFormatter()
        parser.formatOptions = [.withInternetDateTime]
        guard let date = parser.date(from: iso) else { return iso }
        let out = DateFormatter()
        out.locale = Locale(identifier: "de_DE")
        out.dateFormat = Calendar.current.isDateInToday(date)
            ? "'heute,' HH:mm" : "d. MMM, HH:mm"
        return out.string(from: date)
    }
}
