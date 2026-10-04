// Tresor — deine Zugänge, sichtbar als Zugänge und nie als Werte.
//
// Der Bildschirm zeigt, DASS es einen Zugang gibt, wofür er da ist und wann er
// zuletzt benutzt wurde. Er zeigt nie den Wert — und er hat auch keinen Weg
// dorthin: es gibt keine Schaltfläche „Anzeigen", kein Aufdecken, kein
// Kopieren. Wer ein Passwort braucht, ersetzt es; wer es nachsehen will,
// schaut in seinen Passwortmanager.
//
// Das ist eine Produktentscheidung und keine fehlende Funktion. Ein Tresor,
// der auf Tippen alles zeigt, ist ein Zettel mit einem Deckel.
//
// Sprache: keine internen Wörter. Kein `secret_ref`, kein `ciphertext`, kein
// `InvocationContext`. Was hier steht, steht in derselben Sprache wie der Rest
// von SOLVIO Nexus.
import SwiftUI

struct TresorView: View {
    /// Wie man von hier zu den Freigaben kommt. Die Ansicht kennt die
    /// Reiterleiste nicht — sie sagt nur, dass sie hin moechte.
    var onOpenApprovals: () -> Void = {}

    @StateObject private var model = TresorModel()
    @State private var showAdd = false
    @State private var replacing: TresorEntry? = nil
    @State private var confirmDelete: TresorEntry? = nil
    @State private var scoping: TresorEntry? = nil
    @Environment(\.scenePhase) private var scenePhase
    @State private var preparedPush = PushCredentialImport.isPrepared()
    @State private var importingPush = false
    @State private var pushImportNotice = ""

    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: Theme.Space.section) {
                headline
                if model.pending != nil { pendingCard }
                if model.pending == nil, let e = model.ergebnis { ergebnisCard(e) }
                if preparedPush && !model.entries.contains(where: { $0.secret_ref == PushCredentialImport.reference }) {
                    VStack(alignment: .leading, spacing: 10) {
                        Text("Apple-Mitteilungen einrichten").font(.headline)
                        Text("Der Mac hat den Zugang vorbereitet. Er darf nur allgemeine Hinweise für SOLVIO Nexus über Apple senden. Du bestätigst das einmal mit Face ID.")
                            .font(.subheadline).foregroundStyle(Theme.ink2)
                        Button("Apple-Zugang übernehmen") { importPush() }
                            .disabled(!model.loaded || !model.reachable || model.client == nil || model.working || model.pending != nil || importingPush)
                    }.padding(Theme.Space.card).card()
                }
                if !pushImportNotice.isEmpty { Text(pushImportNotice).font(.footnote).foregroundStyle(Theme.warn) }
                if model.entries.isEmpty && model.loaded {
                    EmptyState(icon: "lock.rectangle.stack",
                               title: "Noch kein Zugang",
                               text: "Hier liegen die Zugänge, die SOLVIO für dich "
                                   + "benutzen darf — ohne sie je zu zeigen.")
                } else {
                    zugaenge
                }
                hinzufuegen
                if !model.ledger.isEmpty { verlauf }
                hinweis
            }
            .padding(.horizontal, Theme.Space.margin)
            .padding(.top, 8)
            .padding(.bottom, 24)
        }
        .background(Theme.bg)
        .navigationTitle("Tresor")
        .navigationBarTitleDisplayMode(.inline)
        // KEIN Plus in der Werkzeugleiste. Es gab eines, und es war doppelt
        // gemoppelt: dieselbe Handlung zweimal auf einem Bildschirm, einmal als
        // Symbol ohne Wort und einmal als beschriftete Karte. Bei der Abnahme
        // wurde das Symbol schlicht nicht gefunden — es stand dunkel auf
        // dunkler Leiste, weil `HomeView` dem Stapel ein dunkles Schema gibt.
        // Statt ihm eine Farbe zu geben und beide stehenzulassen, bleibt die
        // Karte: eine Handlung, ein Ort, mit Wort.
        .refreshable { await model.refresh() }
        .task { await model.refresh() }
        .onChange(of: scenePhase) { phase in
            if phase == .active { preparedPush = PushCredentialImport.isPrepared() }
        }
        .sheet(isPresented: $showAdd) { TresorAddSheet(model: model) }
        .sheet(item: $scoping) { entry in
            TresorScopeSheet(model: model, entry: entry)
        }
        .sheet(item: $replacing) { entry in
            TresorReplaceSheet(model: model, entry: entry)
        }
        .alert("Zugang löschen?", isPresented: Binding(
            get: { confirmDelete != nil },
            set: { if !$0 { confirmDelete = nil } })) {
            Button("Abbrechen", role: .cancel) { confirmDelete = nil }
            Button("Löschen", role: .destructive) {
                if let entry = confirmDelete {
                    Task { _ = await model.delete(ref: entry.secret_ref) }
                }
                confirmDelete = nil
            }
        } message: {
            Text("\(confirmDelete?.title ?? "") wird endgültig entfernt. "
                 + "SOLVIO kann sich damit nicht mehr anmelden.")
        }
    }

    private func importPush() {
        guard model.loaded, model.reachable, model.client != nil,
              !model.working, model.pending == nil, !importingPush else { return }
        importingPush = true
        pushImportNotice = ""
        Task {
            defer { importingPush = false; preparedPush = PushCredentialImport.isPrepared() }
            do {
                let value = try PushCredentialImport.take()
                preparedPush = false
                _ = await model.add(entry: PushCredentialImport.draft, secret: value)
            } catch {
                pushImportNotice = "Der vorbereitete Apple-Zugang konnte nicht übernommen werden. Bitte lasse ihn am Mac erneut vorbereiten."
            }
        }
    }

    // MARK: - Kopf

    private var headline: some View {
        VStack(alignment: .leading, spacing: 8) {
            HStack(spacing: 10) {
                StatusDot(color: tone)
                Text(model.reachable ? tresorZustandswort(model.zustand)
                                     : "SOLVIO ist gerade nicht erreichbar.")
                    .font(.display(.title2, weight: .semibold))
                    .foregroundStyle(Theme.ink)
                    .fixedSize(horizontal: false, vertical: true)
            }
            if model.reachable && !model.grund.isEmpty {
                Text(model.grund).font(.subheadline).foregroundStyle(Theme.ink2)
                    .fixedSize(horizontal: false, vertical: true)
            }
            if model.loaded && !model.reachable {
                Text("Stand: \(relativeTime(model.snapshotAt))")
                    .font(.caption).foregroundStyle(Theme.ink3)
            }
        }
        .padding(Theme.Space.card)
        .frame(maxWidth: .infinity, alignment: .leading)
        .card()
    }

    private var tone: Color {
        if !model.reachable { return Theme.ink3 }
        switch model.zustand {
        case "healthy": return Theme.good
        case "auth_required", "degraded": return Theme.warn
        case "unavailable": return Theme.bad
        default: return Theme.ink3
        }
    }

    // MARK: - Wartende Freigabe

    /// Die wartende Freigabe — mit EINEM Weg nach vorn, nicht mit zweien.
    ///
    /// Vorher stand hier „Ich habe freigegeben". Das war ein Knopf, der den
    /// Nutzer fragte, was die App selbst wissen kann — und er kam nach einem
    /// Reiterwechsel, den auch niemand angekuendigt hatte. Jetzt fuehrt die
    /// Karte direkt zu den Freigaben, und der Rest passiert von allein
    /// (`TresorModel.awaitApprovalAndFinish`).
    private var pendingCard: some View {
        VStack(alignment: .leading, spacing: 10) {
            HStack(spacing: 8) {
                ProgressView().controlSize(.small)
                Text("Warte auf deine Freigabe")
                    .font(.display(.headline)).foregroundStyle(Theme.ink)
            }
            Text(model.notice.isEmpty
                 ? "Bestätige das mit Face ID — ich mache dann von selbst weiter."
                 : model.notice)
                .font(.subheadline).foregroundStyle(Theme.ink2)
                .fixedSize(horizontal: false, vertical: true)
            Button {
                onOpenApprovals()
            } label: {
                HStack(spacing: 8) {
                    Image(systemName: "faceid")
                    Text("Jetzt mit Face ID bestätigen")
                }
                .font(.subheadline.weight(.semibold))
                .frame(maxWidth: .infinity).padding(.vertical, 12)
                .background(Theme.blue).foregroundStyle(Theme.onBlue)
                .clipShape(RoundedRectangle(cornerRadius: Theme.Radius.row))
            }
            .buttonStyle(PressScale())
            Button("Abbrechen") { model.clearPending() }
                .font(.footnote).foregroundStyle(Theme.ink3)
        }
        .padding(Theme.Space.card)
        .frame(maxWidth: .infinity, alignment: .leading)
        .card()
    }

    /// Wie es ausging — sichtbar, benannt und unterscheidbar.
    ///
    /// Vorher gab es diese Karte nicht: `notice` stand ausschliesslich in der
    /// Wartekarte, und die verschwand genau dann, wenn das Ergebnis eintraf.
    /// Erfolg, Absage und Fehler sahen deshalb alle gleich aus — nach nichts.
    private func ergebnisCard(_ e: TresorErgebnis) -> some View {
        let farbe: Color = {
            switch e {
            case .erledigt: return Theme.good
            case .abgelehnt: return Theme.ink2
            case .abgelaufen: return Theme.warn
            case .fehler: return Theme.bad
            }
        }()
        let ueberschrift: String = {
            switch e {
            case .erledigt: return "Erledigt"
            case .abgelehnt: return "Abgelehnt"
            case .abgelaufen: return "Abgelaufen"
            case .fehler: return "Nicht ausgeführt"
            }
        }()
        return VStack(alignment: .leading, spacing: 8) {
            HStack(spacing: 8) {
                Image(systemName: e.symbol).foregroundStyle(farbe)
                Text(ueberschrift)
                    .font(.display(.headline)).foregroundStyle(Theme.ink)
                Spacer(minLength: 0)
                Button("Verstanden") { model.clearErgebnis() }
                    .font(.footnote).foregroundStyle(Theme.ink3)
            }
            Text(e.satz)
                .font(.subheadline).foregroundStyle(Theme.ink2)
                .fixedSize(horizontal: false, vertical: true)
        }
        .padding(Theme.Space.card)
        .frame(maxWidth: .infinity, alignment: .leading)
        .card()
        .accessibilityElement(children: .combine)
        .accessibilityLabel("\(ueberschrift). \(e.satz)")
    }

    // MARK: - Zugänge

    private var zugaenge: some View {
        VStack(alignment: .leading, spacing: 10) {
            sectionHeader("ZUGÄNGE", count: model.entries.count)
            ForEach(model.entries) { entry in
                TresorRow(entry: entry,
                          onScope: { scoping = entry },
                          onReplace: { replacing = entry },
                          onDisable: {
                              Task {
                                  _ = entry.status == "active"
                                      ? await model.disable(ref: entry.secret_ref)
                                      : await model.enable(ref: entry.secret_ref)
                              }
                          },
                          onDelete: { confirmDelete = entry })
            }
        }
    }

    private var verlauf: some View {
        VStack(alignment: .leading, spacing: 10) {
            sectionHeader("ZULETZT BENUTZT", count: model.ledger.count)
            ForEach(model.ledger.prefix(8)) { row in
                TresorLedgerLine(row: row, name: name(for: row.secret_ref))
            }
        }
    }

    /// Der sichtbare Weg zum Hinzufuegen — und der eigentliche.
    ///
    /// Ein Symbol in der Werkzeugleiste ist ein Weg fuer jemanden, der weiss,
    /// dass es ihn gibt. Die Gestaltungsakte nennt „Hinzufuegen" als eine der
    /// vier Handlungen des Tresors; sie gehoert damit in den Inhalt, nicht nur
    /// in eine Leiste. Der Knopf steht unter der Liste, weil der Blick zuerst
    /// den Zugaengen gehoert.
    private var hinzufuegen: some View {
        Button { showAdd = true } label: {
            HStack(spacing: 10) {
                Image(systemName: "plus.circle.fill")
                    .font(.title3).foregroundStyle(Theme.blue)
                Text("Zugang hinzufügen")
                    .font(.body.weight(.medium)).foregroundStyle(Theme.ink)
                Spacer()
                Image(systemName: "chevron.right")
                    .font(.footnote.weight(.semibold)).foregroundStyle(Theme.ink3)
            }
            .padding(Theme.Space.card)
            .frame(maxWidth: .infinity, alignment: .leading)
            .card()
        }
        .buttonStyle(PressScale())
        .accessibilityLabel("Zugang hinzufügen")
    }

    private var hinweis: some View {
        Text("SOLVIO zeigt hier nie einen Wert — auch dir nicht. "
             + "Wenn du ein Passwort nachsehen willst, schau in deinen "
             + "Passwortmanager. Wenn es sich geändert hat, ersetze es hier.")
            .font(.caption).foregroundStyle(Theme.ink3)
            .fixedSize(horizontal: false, vertical: true)
            .padding(.horizontal, 4)
    }

    private func name(for ref: String) -> String {
        model.entries.first { $0.secret_ref == ref }?.title ?? ref
    }

    private func sectionHeader(_ title: String, count: Int) -> some View {
        HStack(spacing: 6) {
            Text(title).font(.caption.weight(.semibold)).kerning(0.8)
                .foregroundStyle(Theme.ink3)
            Text("\(count)").font(.caption).foregroundStyle(Theme.ink3)
            Spacer()
        }
        .padding(.horizontal, 4)
    }
}

// MARK: - Zeilen

struct TresorRow: View {
    let entry: TresorEntry
    var onScope: () -> Void
    var onReplace: () -> Void
    var onDisable: () -> Void
    var onDelete: () -> Void

    var body: some View {
        VStack(alignment: .leading, spacing: 6) {
            HStack(spacing: 10) {
                StatusDot(color: entry.status == "active" ? Theme.good : Theme.warn)
                Text(entry.title)
                    .font(.subheadline.weight(.semibold)).foregroundStyle(Theme.ink)
                Spacer(minLength: 0)
                Menu {
                    Button("Zugriffsrechte", action: onScope)
                    Button("Ersetzen", action: onReplace)
                    Button(entry.status == "active" ? "Deaktivieren" : "Wieder freigeben",
                           action: onDisable)
                    Button("Löschen", role: .destructive, action: onDelete)
                } label: {
                    Image(systemName: "ellipsis.circle")
                        .font(.body).foregroundStyle(Theme.ink3)
                        .frame(width: 44, height: 44, alignment: .trailing)
                }
                .accessibilityLabel("Aktionen für \(entry.title)")
            }
            Text("\(tresorStatuswort(entry.status)) · \(tresorArtwort(entry.kind))")
                .font(.caption).foregroundStyle(Theme.ink2)
            Text(tresorZielwort(entry.allowed_targets))
                .font(.caption).foregroundStyle(Theme.ink2)
            if !entry.account_label.isEmpty {
                Text(entry.account_label).font(.caption).foregroundStyle(Theme.ink3)
            }
            Text(entry.last_used_at.isEmpty
                 ? "Noch nicht benutzt"
                 : "Zuletzt verwendet: \(relativeTime(wissenEpoch(entry.last_used_at)))")
                .font(.caption).foregroundStyle(Theme.ink3)
        }
        .padding(Theme.Space.card)
        .frame(maxWidth: .infinity, alignment: .leading)
        .card()
        .accessibilityElement(children: .combine)
    }
}

struct TresorLedgerLine: View {
    let row: TresorLedgerRow
    let name: String

    var body: some View {
        HStack(alignment: .top, spacing: 10) {
            Image(systemName: row.outcome == "used" ? "checkmark.circle"
                                                    : "xmark.circle")
                .font(.footnote)
                .foregroundStyle(row.outcome == "used" ? Theme.good : Theme.warn)
                .padding(.top, 2)
                .accessibilityHidden(true)
            VStack(alignment: .leading, spacing: 2) {
                Text(satz)
                    .font(.caption).foregroundStyle(Theme.ink2)
                    .fixedSize(horizontal: false, vertical: true)
            }
            Spacer(minLength: 0)
        }
        .padding(.horizontal, 4)
        .accessibilityElement(children: .combine)
    }

    /// Ein Satz statt einer Tabelle: „Amazon wurde heute um 14:32 vom Browser
    /// für amazon.de verwendet." Das ist die Auskunft, die ein Mensch braucht —
    /// und sie enthält keinen Wert.
    private var satz: String {
        let host = URL(string: row.target)?.host ?? row.target
        let zeit = clockTime(wissenEpoch(row.at))
        let wer = executorWort(row.executor)
        if row.outcome == "used" {
            return "\(name) wurde um \(zeit) \(wer) für \(host) verwendet."
        }
        return "\(name): um \(zeit) abgelehnt (\(ablehnungswort(row.denied_reason)))."
    }

    private func executorWort(_ executor: String) -> String {
        switch executor {
        case "browser": return "vom Browser"
        case "home_assistant": return "von der Haussteuerung"
        case "http": return "von SOLVIO"
        case "provider": return "vom Sprachmodell"
        case "machine": return "von einem Gerät"
        default: return "von SOLVIO"
        }
    }

    private func ablehnungswort(_ reason: String) -> String {
        switch reason {
        case "target_not_allowed": return "falsches Ziel"
        case "capability_not_allowed": return "nicht dafür gedacht"
        case "executor_not_allowed", "executor_module_mismatch": return "falscher Weg"
        case "not_active": return "gesperrt"
        case "origin_not_allowed": return "falsche Herkunft"
        case "background_not_allowed": return "nicht im Hintergrund"
        default: return "nicht erlaubt"
        }
    }
}
