// Ein Eintrag im Wissen — gross der Satz, darunter die ehrliche Herkunft.
//
// „Warum weiß SOLVIO das?" zeigt WOERTLICH den Satz, den der Core abgeleitet
// hat, plus die Kette der Beobachtungen in Menschenworten. Die App formuliert
// keine eigene Herkunftsauskunft — eine erfundene Auskunft waere schlimmer als
// keine.
//
// Vergessen und Aendern laufen ueber den Mutationsweg: frische Nonce plus
// App-Attest-Assertion, OHNE Face-ID-Theater — unter Approval Policy V2 ist
// die registrierte App dafuer ein vertrauenswuerdiger Ursprung. Sagt der Core
// wider Erwarten „braucht Freigabe", steht sein Satz ehrlich auf dem Schirm.
import SwiftUI

enum WissenEntry: Hashable {
    case memory(MemoryItem)
    case candidate(MemoryCandidate)
}

struct WissenDetailView: View {
    @ObservedObject var model: WissenModel
    let entry: WissenEntry
    @Environment(\.dismiss) private var dismiss

    @State private var provenance: MemoryProvenance?
    @State private var askForget = false
    @State private var editing = false
    @State private var draft = ""

    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: Theme.Space.section) {
                switch entry {
                case let .memory(item): memoryBody(item)
                case let .candidate(candidate): candidateBody(candidate)
                }
                if !model.notice.isEmpty {
                    Text(model.notice)
                        .font(.footnote).foregroundStyle(Theme.ink2)
                        .frame(maxWidth: .infinity)
                        .multilineTextAlignment(.center)
                }
            }
            .padding(.horizontal, Theme.Space.margin)
            .padding(.vertical, 8)
        }
        .background(Theme.bg)
        .navigationTitle("Wissen")
        .navigationBarTitleDisplayMode(.inline)
    }

    // MARK: - Eintrag

    @ViewBuilder
    private func memoryBody(_ item: MemoryItem) -> some View {
        Text(item.content)
            .font(.display(.title3, weight: .semibold))
            .foregroundStyle(Theme.ink)
            .fixedSize(horizontal: false, vertical: true)

        // Die Meta-Tafel der Designakte: links das Wort, rechts der Wert.
        // Zeilen gibt es NUR, wo der Core wirklich Daten geliefert hat —
        // eine erfundene Zeitangabe waere schlimmer als eine fehlende.
        VStack(spacing: 0) {
            metaRow("Herkunft", herkunftswort(item.lifecycle))
            if let since = wissenEpoch(item.created_at) {
                Divider()
                metaRow("Zuerst erkannt", relativeTime(since))
            }
            if let updated = wissenEpoch(item.updated_at) {
                Divider()
                metaRow("Zuletzt bestätigt", relativeTime(updated))
            }
            if item.sensitivity == "sensitive" {
                Divider()
                metaRow("Schutz", "Besonders geschützt")
            }
            Divider()
            // „AKTUELL" ist der EINZIGE Status, den die Liste des Cores
            // kennt: sie liefert ausschliesslich aktive Eintraege. Wer hier
            // steht, ist also aktuell — mehr behauptet die App nicht.
            HStack(alignment: .firstTextBaseline) {
                Text("Status").font(.footnote).foregroundStyle(Theme.ink2)
                Spacer(minLength: 12)
                Text("AKTUELL")
                    .font(.caption2.weight(.semibold))
                    .kerning(1)
                    .foregroundStyle(Theme.inkOnGold)
                    .padding(.horizontal, 8)
                    .padding(.vertical, 3)
                    .background(Theme.tintGold, in: Capsule())
            }
            .padding(.vertical, 8)
            .accessibilityElement(children: .combine)
        }
        .padding(.horizontal, Theme.Space.card)
        .padding(.vertical, 8)
        .frame(maxWidth: .infinity, alignment: .leading)
        .card()

        whySection(item)
        actions(item)
    }

    /// Der Satz des Cores, woertlich — und darunter die Kette, vorlesbar.
    @ViewBuilder
    private func whySection(_ item: MemoryItem) -> some View {
        VStack(alignment: .leading, spacing: 10) {
            Text("Warum weiß SOLVIO das?")
                .font(.display(.headline)).foregroundStyle(Theme.ink)
            VStack(alignment: .leading, spacing: 10) {
                if let sentence = provenance?.explanation ?? item.explanation {
                    Text(sentence)
                        .font(.callout)
                        .foregroundStyle(Theme.ink)
                        .fixedSize(horizontal: false, vertical: true)
                }
                if let chain = provenance?.chain, !chain.isEmpty {
                    VStack(alignment: .leading, spacing: 8) {
                        ForEach(Array(chain.enumerated()), id: \.offset) { _, step in
                            HStack(alignment: .top, spacing: 8) {
                                Circle().fill(Theme.ink3)
                                    .frame(width: 5, height: 5).padding(.top, 7)
                                    .accessibilityHidden(true)
                                Text(provenanceLine(step))
                                    .font(.callout).foregroundStyle(Theme.ink2)
                            }
                        }
                    }
                } else if provenance == nil {
                    Text("Ich sehe nach …")
                        .font(.callout).foregroundStyle(Theme.ink3)
                }
            }
            .padding(Theme.Space.card)
            .frame(maxWidth: .infinity, alignment: .leading)
            .background(Theme.surface2,
                        in: RoundedRectangle(cornerRadius: Theme.Radius.row, style: .continuous))
            .overlay(RoundedRectangle(cornerRadius: Theme.Radius.row, style: .continuous)
                .stroke(Theme.line))
        }
        .task { provenance = try? await model.client?.memoryProvenance(id: item.id) }
    }

    @ViewBuilder
    private func actions(_ item: MemoryItem) -> some View {
        // Zwei Geisterknoepfe nebeneinander (Designakte): beides sind
        // Nebenwege, keiner ist der Hauptweg dieser Seite. Rot bleibt Wort
        // und Linie, nie Flaeche — Loeschen soll erreichbar sein, nicht
        // locken. 48 Punkte halten die 44er-Untergrenze.
        HStack(spacing: 10) {
            Button {
                Haptics.tap()
                draft = item.content
                editing = true
            } label: {
                Label("Ändern", systemImage: "pencil")
                    .font(.body.weight(.medium)).foregroundStyle(Theme.blue)
                    .frame(maxWidth: .infinity).frame(height: 48)
                    .overlay(RoundedRectangle(cornerRadius: 16, style: .continuous)
                        .stroke(Theme.blue.opacity(0.35)))
                    .contentShape(Rectangle())
            }
            .buttonStyle(PressScale())

            Button {
                Haptics.tap()
                askForget = true
            } label: {
                Label("Vergessen", systemImage: "trash")
                    .font(.body.weight(.medium)).foregroundStyle(Theme.bad)
                    .frame(maxWidth: .infinity).frame(height: 48)
                    .overlay(RoundedRectangle(cornerRadius: 16, style: .continuous)
                        .stroke(Theme.bad.opacity(0.35)))
                    .contentShape(Rectangle())
            }
            .buttonStyle(PressScale())
        }
        .disabled(model.working)
        .confirmationDialog("Soll ich das vergessen?", isPresented: $askForget,
                            titleVisibility: .visible) {
            Button("Vergessen", role: .destructive) {
                Task { if await model.forget(memoryID: item.id) { dismiss() } }
            }
            Button("Behalten", role: .cancel) {}
        }
        .sheet(isPresented: $editing) {
            correctSheet(item)
        }
    }

    /// Aendern heisst: DU sagst, wie es richtig ist — und genau dieser Satz
    /// geht als Auftrag an den Core.
    private func correctSheet(_ item: MemoryItem) -> some View {
        NavigationStack {
            VStack(alignment: .leading, spacing: Theme.Space.card) {
                Text("Wie stimmt es?")
                    .font(.display(.headline)).foregroundStyle(Theme.ink)
                TextField("So stimmt es …", text: $draft, axis: .vertical)
                    .lineLimit(3...8)
                    .padding(12)
                    .background(Theme.surface2,
                                in: RoundedRectangle(cornerRadius: Theme.Radius.row,
                                                     style: .continuous))
                    .overlay(RoundedRectangle(cornerRadius: Theme.Radius.row, style: .continuous)
                        .stroke(Theme.line))
                Spacer()
                Button {
                    Haptics.tap()
                    let statement = draft.trimmingCharacters(in: .whitespacesAndNewlines)
                    guard !statement.isEmpty else { return }
                    Task {
                        if await model.correct(memoryID: item.id, statement: statement) {
                            editing = false
                            dismiss()
                        }
                    }
                } label: {
                    Text("So merken")
                        .font(.body.weight(.semibold)).foregroundStyle(Theme.onBlue)
                        .frame(maxWidth: .infinity).frame(height: 52)
                        .background(Theme.blue,
                                    in: RoundedRectangle(cornerRadius: 16, style: .continuous))
                }
                .buttonStyle(PressScale())
                .disabled(model.working
                          || draft.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty)
            }
            .padding(Theme.Space.margin)
            .background(Theme.bg)
            .navigationTitle("Ändern")
            .navigationBarTitleDisplayMode(.inline)
            .toolbar {
                ToolbarItem(placement: .cancellationAction) {
                    Button("Abbrechen") { editing = false }
                }
            }
        }
        .presentationDetents([.medium])
    }

    // MARK: - Vorschlag

    @ViewBuilder
    private func candidateBody(_ candidate: MemoryCandidate) -> some View {
        Text(candidate.statement)
            .font(.display(.title3, weight: .semibold))
            .foregroundStyle(Theme.ink)
            .fixedSize(horizontal: false, vertical: true)

        VStack(alignment: .leading, spacing: 8) {
            factLine(icon: "sparkles", text: "SOLVIO möchte sich das merken.")
            if let n = candidate.observations, n > 0 {
                let talks = candidate.independent_conversations ?? 0
                factLine(icon: "text.bubble",
                         text: talks > 1
                             ? "\(n) Beobachtungen in \(talks) Gesprächen"
                             : "\(n) Beobachtungen")
            }
            if let first = wissenEpoch(candidate.first_seen) {
                factLine(icon: "clock", text: "Zuerst aufgefallen \(relativeTime(first))")
            }
        }
        .padding(Theme.Space.card)
        .frame(maxWidth: .infinity, alignment: .leading)
        .card()

        if let contradicts = candidate.contradicts {
            VStack(alignment: .leading, spacing: 6) {
                Text("Bisher gemerkt")
                    .font(.display(.headline)).foregroundStyle(Theme.ink)
                Text(contradicts.content)
                    .font(.callout).foregroundStyle(Theme.ink2)
                    .fixedSize(horizontal: false, vertical: true)
                    .padding(Theme.Space.card)
                    .frame(maxWidth: .infinity, alignment: .leading)
                    .background(Theme.surface2,
                                in: RoundedRectangle(cornerRadius: Theme.Radius.row,
                                                     style: .continuous))
                    .overlay(RoundedRectangle(cornerRadius: Theme.Radius.row, style: .continuous)
                        .stroke(Theme.line))
            }
        }

        VStack(spacing: 10) {
            Button {
                Haptics.tap()
                Task {
                    if await model.confirmCandidate(candidate.candidate_id) { dismiss() }
                }
            } label: {
                Text("Bestätigen")
                    .font(.body.weight(.semibold)).foregroundStyle(Theme.onBlue)
                    .frame(maxWidth: .infinity).frame(height: 52)
                    .background(Theme.blue,
                                in: RoundedRectangle(cornerRadius: 16, style: .continuous))
            }
            .buttonStyle(PressScale())
            Button {
                Haptics.tap()
                Task {
                    if await model.declineCandidate(candidate.candidate_id) { dismiss() }
                }
            } label: {
                Text("Ablehnen")
                    .font(.body.weight(.medium)).foregroundStyle(Theme.ink2)
                    .frame(maxWidth: .infinity).frame(height: 52)
                    .background(Theme.surface,
                                in: RoundedRectangle(cornerRadius: 16, style: .continuous))
                    .overlay(RoundedRectangle(cornerRadius: 16, style: .continuous)
                        .stroke(Theme.line))
            }
            .buttonStyle(PressScale())
        }
        .disabled(model.working)
    }

    // MARK: - Bausteine

    /// Eine Zeile der Meta-Tafel: Wort links, Wert rechts. Der Wert traegt
    /// das Gewicht — er ist die Auskunft, das Wort nur ihr Schild.
    private func metaRow(_ label: String, _ value: String) -> some View {
        HStack(alignment: .firstTextBaseline) {
            Text(label).font(.footnote).foregroundStyle(Theme.ink2)
            Spacer(minLength: 12)
            Text(value)
                .font(.footnote.weight(.medium))
                .foregroundStyle(Theme.ink)
                .multilineTextAlignment(.trailing)
        }
        .padding(.vertical, 8)
        .accessibilityElement(children: .combine)
    }

    private func factLine(icon: String, text: String) -> some View {
        HStack(spacing: 10) {
            Image(systemName: icon)
                .font(.footnote)
                .foregroundStyle(Theme.ink3)
                .frame(width: 18)
                .accessibilityHidden(true)
            Text(text).font(.callout).foregroundStyle(Theme.ink2)
        }
        .accessibilityElement(children: .combine)
    }

    /// Eine Kettenzeile in Menschenworten. Uebersetzt NUR die Art des
    /// Ereignisses — Inhalt hat die Kette absichtlich keinen.
    private func provenanceLine(_ step: MemoryProvenanceEntry) -> String {
        let note = step.note ?? ""
        let kind: String
        switch true {
        case note.hasPrefix("stated:"): kind = "Du hast es gesagt"
        case note.hasPrefix("observed:"): kind = "Im Gespräch beobachtet"
        case note.hasPrefix("confirmed:"): kind = "Von dir bestätigt"
        case note.hasPrefix("corrected:"): kind = "Von dir korrigiert"
        case note.hasPrefix("contradicted:"): kind = "Du hast widersprochen"
        default:
            switch step.source_type ?? "" {
            case "user_direct": kind = "Aus einem Gespräch mit dir"
            case "solvio_inference": kind = "Von SOLVIO abgeleitet"
            default: kind = "Festgehalten"
            }
        }
        if let at = wissenEpoch(step.at) {
            return "\(kind) · \(relativeTime(at))"
        }
        return kind
    }
}
