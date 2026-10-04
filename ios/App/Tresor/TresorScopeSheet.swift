// Zugriffsrechte eines Zugangs ändern — die eine Stelle, an der ein Mensch
// sieht, was seine Zustimmung wirklich bewirkt.
//
// Die gefährliche Eigenschaft dieser Operation steht nicht im Namen: der Core
// ERSETZT die Rechteliste, er ergänzt sie nicht. Zwei Rechte hinzufügen heißt
// also, alle anderen mitzuschicken — sonst sind sie fort. Ein Bildschirm, der
// nur die neue Liste zeigt, macht diesen Verlust unsichtbar.
//
// Deshalb zeigt dieses Blatt die DIFFERENZ, bevor Face ID kommt: was
// hinzukommt, was wegfällt, was bleibt. Wer „nichts entfällt" liest, soll
// sich darauf verlassen können.
//
// Und es schickt die Fassung mit, die es gelesen hat. Zwischen „das steht
// hier" und „Face ID war eben" liegen Sekunden bis Minuten; ändert jemand den
// Umfang in dieser Zeit, weist der Mac ab. Die Zustimmung galt einem anderen
// Stand.
//
// Kein Wert kommt hier vor — weder angezeigt noch eingegeben noch gebraucht.
import SwiftUI

/// Was eine Rechteaenderung bedeutet — ohne Bildschirm, damit sie pruefbar ist.
///
/// Die Rechnung steht bewusst NICHT im View: sie ist der einzige Schutz davor,
/// dass ein Mensch beim Hinzufuegen von zwei Rechten zehn verliert. Etwas, das
/// so viel traegt, gehoert dorthin, wo ein Test es erreicht.
struct TresorScopeDiff {
    let bisher: Set<String>
    let gewaehlt: Set<String>

    var kommtHinzu: [String] { gewaehlt.subtracting(bisher).sorted() }
    var entfaellt: [String] { bisher.subtracting(gewaehlt).sorted() }
    var bleibt: [String] { bisher.intersection(gewaehlt).sorted() }
    var unveraendert: Bool { kommtHinzu.isEmpty && entfaellt.isEmpty }

    /// Was zum Mac geht: die VOLLSTAENDIGE Liste, nie eine Ergaenzung.
    var vollstaendig: [String] { gewaehlt.sorted() }
}

struct TresorScopeSheet: View {
    @ObservedObject var model: TresorModel
    let entry: TresorEntry
    @Environment(\.dismiss) private var dismiss

    @State private var gewaehlt: Set<String> = []
    @State private var busy = false

    /// Alles, worüber entschieden werden kann: was der Zugang heute darf, und
    /// was der Mac überhaupt kennt. Sortiert, damit die Liste sich nicht
    /// zwischen zwei Blicken umsortiert.
    private var auswahl: [String] {
        Array(Set(entry.allowed_capabilities).union(model.bekannteFaehigkeiten))
            .sorted()
    }

    private var bisher: Set<String> { Set(entry.allowed_capabilities) }
    private var diff: TresorScopeDiff {
        TresorScopeDiff(bisher: bisher, gewaehlt: gewaehlt)
    }
    private var kommtHinzu: [String] { diff.kommtHinzu }
    private var entfaellt: [String] { diff.entfaellt }
    private var unveraendert: Bool { diff.unveraendert }

    var body: some View {
        NavigationStack {
            Form {
                Section {
                    Text(entry.title).font(.headline)
                    Text("Diese Rechte darf SOLVIO mit diesem Zugang benutzen. "
                         + "Der Zugang selbst wird dabei nicht angefasst.")
                        .font(.caption).foregroundStyle(Theme.ink2)
                }

                Section("Rechte") {
                    ForEach(auswahl, id: \.self) { name in
                        Toggle(isOn: binding(for: name)) {
                            VStack(alignment: .leading, spacing: 2) {
                                Text(name).font(.body.monospaced())
                                if !bisher.contains(name) {
                                    Text("neu")
                                        .font(.caption2)
                                        .foregroundStyle(Theme.ink2)
                                }
                            }
                        }
                        .accessibilityLabel(name)
                    }
                }

                Section("Was sich ändert") {
                    if unveraendert {
                        Text("Nichts. Die Rechte bleiben, wie sie sind.")
                            .font(.callout).foregroundStyle(Theme.ink2)
                    } else {
                        aenderung("Kommt hinzu", kommtHinzu, warnend: false)
                        aenderung("Entfällt", entfaellt, warnend: true)
                    }
                }

                if !entfaellt.isEmpty {
                    Section {
                        Text("Was entfällt, kann SOLVIO danach nicht mehr. "
                             + "Das ist die sichere Richtung — aber sieh es dir an.")
                            .font(.caption).foregroundStyle(Theme.warn)
                    }
                }
            }
            .navigationTitle("Zugriffsrechte")
            .navigationBarTitleDisplayMode(.inline)
            .toolbar {
                ToolbarItem(placement: .cancellationAction) {
                    Button("Abbrechen") { dismiss() }
                }
                ToolbarItem(placement: .confirmationAction) {
                    Button("Ändern") { commit() }
                        .disabled(unveraendert || gewaehlt.isEmpty || busy)
                }
            }
        }
        .interactiveDismissDisabled(busy)
        .onAppear { gewaehlt = bisher }
    }

    @ViewBuilder
    private func aenderung(_ titel: String, _ namen: [String],
                           warnend: Bool) -> some View {
        if !namen.isEmpty {
            VStack(alignment: .leading, spacing: 4) {
                Text(titel).font(.caption).foregroundStyle(Theme.ink2)
                ForEach(namen, id: \.self) { name in
                    Text(name)
                        .font(.callout.monospaced())
                        .foregroundStyle(warnend ? Theme.warn : Theme.ink)
                }
            }
        }
    }

    private func binding(for name: String) -> Binding<Bool> {
        Binding(get: { gewaehlt.contains(name) },
                set: { an in
                    if an { gewaehlt.insert(name) } else { gewaehlt.remove(name) }
                })
    }

    /// Es geht immer die VOLLSTÄNDIGE Liste hinaus, nie eine Ergänzung — der
    /// Mac ersetzt. Und die Fassung, die dieses Blatt gelesen hat.
    private func commit() {
        busy = true
        let vollstaendig = diff.vollstaendig
        let fassung = entry.version
        Task {
            // Zurück kommt der Aufruf, sobald der Mac die Anfrage angenommen
            // hat — nicht erst nach Face ID. Genau deshalb darf dieses Blatt
            // jetzt gehen: es verdeckt sonst die Karte, über die freigegeben
            // wird, und das Schliessen bricht nichts ab. Der Vorgang gehört
            // dem Modell, und das Modell gehört dem Bildschirm dahinter.
            _ = await model.startRescope(ref: entry.secret_ref,
                                         capabilities: vollstaendig,
                                         expectedVersion: fassung)
            busy = false
            dismiss()
        }
    }
}
