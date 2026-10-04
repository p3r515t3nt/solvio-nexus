// Zahlungen — was SOLVIO bezahlen darf, in welchen Grenzen, und was gerade offen ist.
//
// Der Bildschirm zeigt, DASS es ein Zahlungsmittel gibt, wofür es gilt und wie
// weit es reicht. Er zeigt nie eine Kartennummer — und er hat auch keinen Weg
// dorthin: es gibt keine Schaltfläche „Anzeigen", kein Aufdecken, kein
// Kopieren. Der Core gibt beides gar nicht erst heraus.
//
// Das ist eine Produktentscheidung und keine fehlende Funktion. SOLVIO ist
// nicht der Kartentresor und will es nicht sein.
//
// Sprache: keine internen Wörter. Kein `payment_intent_id`, kein
// `idempotency_key`, kein `execution_id`. Was hier steht, steht in derselben
// Sprache wie der Rest von SOLVIO Nexus.
import SwiftUI

struct PaymentView: View {
    /// Wie man von hier zu den Freigaben kommt. Die Ansicht kennt die
    /// Reiterleiste nicht — sie sagt nur, dass sie hin möchte.
    var onOpenApprovals: () -> Void = {}

    @StateObject private var model = PaymentModel()
    @State private var showAdd = false
    @State private var confirmRemove: PaymentMethod? = nil
    @State private var limitFor: PaymentMethod? = nil

    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: Theme.Space.section) {
                headline
                if model.pending != nil { pendingCard }
                // Der Satz des Cores gehoert auf den Bildschirm, nicht nur in
                // die Wartekarte. Vorher war jede Absage unsichtbar: der
                // Mensch tippte, nichts passierte, und die Begruendung stand
                // in einer Karte, die es gar nicht gab.
                if model.pending == nil && !model.notice.isEmpty { noticeCard }
                if !model.offen.isEmpty { offeneVorgaenge }
                if model.methods.isEmpty && model.loaded {
                    EmptyState(icon: "creditcard",
                               title: "Noch kein Zahlungsmittel",
                               text: "Hier liegt, womit SOLVIO für dich bezahlen "
                                   + "darf — als Verweis, nie als Karte.")
                } else {
                    zahlungsmittel
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
        .navigationTitle("Zahlungen")
        .navigationBarTitleDisplayMode(.inline)
        .refreshable { await model.refresh() }
        .task { await model.refresh() }
        .sheet(isPresented: $showAdd) { PaymentAddSheet(model: model) }
        .sheet(item: $limitFor) { method in
            PaymentLimitSheet(model: model, method: method)
        }
        .alert("Zahlungsmittel entfernen?", isPresented: Binding(
            get: { confirmRemove != nil },
            set: { if !$0 { confirmRemove = nil } })) {
            Button("Abbrechen", role: .cancel) { confirmRemove = nil }
            Button("Entfernen", role: .destructive) {
                if let method = confirmRemove {
                    Task { _ = await model.remove(ref: method.verweis) }
                }
                confirmRemove = nil
            }
        } message: {
            Text("\(confirmRemove?.title ?? "") wird endgültig entfernt. "
                 + "SOLVIO kann damit nichts mehr bezahlen.")
        }
    }

    // MARK: - Kopf

    private var headline: some View {
        VStack(alignment: .leading, spacing: 8) {
            HStack(spacing: 10) {
                StatusDot(color: tone)
                Text(model.reachable ? zustandssatz
                                     : "SOLVIO ist gerade nicht erreichbar.")
                    .font(.display(.title2, weight: .semibold))
                    .foregroundStyle(Theme.ink)
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

    private var zustandssatz: String {
        let aktiv = model.methods.filter(\.verfuegbar).count
        if aktiv == 0 && !model.methods.isEmpty { return "Alles gesperrt" }
        if !model.offen.isEmpty { return "Etwas wartet auf dich" }
        if aktiv == 0 { return "Noch nichts hinterlegt" }
        return aktiv == 1 ? "Ein Zahlungsmittel bereit"
                          : "\(aktiv) Zahlungsmittel bereit"
    }

    private var noticeCard: some View {
        HStack(alignment: .top, spacing: 10) {
            Image(systemName: "info.circle.fill")
                .font(.footnote).foregroundStyle(Theme.warn).padding(.top, 2)
            Text(model.notice)
                .font(.subheadline).foregroundStyle(Theme.ink)
                .fixedSize(horizontal: false, vertical: true)
            Spacer(minLength: 0)
        }
        .padding(Theme.Space.card)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(Theme.tintGold,
                    in: RoundedRectangle(cornerRadius: Theme.Radius.card,
                                         style: .continuous))
    }

    private var tone: Color {
        if !model.reachable { return Theme.ink3 }
        if !model.offen.isEmpty { return Theme.warn }
        if model.methods.isEmpty { return Theme.ink3 }
        return model.methods.contains(where: \.verfuegbar) ? Theme.good : Theme.warn
    }

    // MARK: - Wartende Freigabe

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

    // MARK: - Offene Vorgänge
    //
    // Das Wichtigste auf diesem Bildschirm und deshalb ganz oben. Der Betrag
    // steht gross; „Bezahlen" führt in die Freigabe, wo der SIGNIERTE Text
    // steht und Face ID fällt. Dieser Knopf bezahlt nichts — er fragt.

    private var offeneVorgaenge: some View {
        VStack(alignment: .leading, spacing: 10) {
            sectionHeader("WARTET AUF DICH", count: model.offen.count)
            ForEach(model.offen) { intent in
                PaymentIntentCard(intent: intent, working: model.working) {
                    // NICHT nach dem Warten springen: `pay` kehrt erst zurueck,
                    // wenn die Freigabe entschieden ist — der Sprung kaeme dann
                    // zu spaet und fuehrte auf eine leere Liste. Die Wartekarte
                    // hat ihren eigenen Knopf dorthin, und zwar sofort.
                    Task { _ = await model.pay(intent: intent) }
                }
            }
        }
    }

    // MARK: - Zahlungsmittel

    private var zahlungsmittel: some View {
        VStack(alignment: .leading, spacing: 10) {
            sectionHeader("ZAHLUNGSMITTEL", count: model.methods.count)
            ForEach(model.methods) { method in
                PaymentMethodRow(
                    method: method,
                    onToggle: {
                        Task {
                            _ = method.verfuegbar
                                ? await model.disable(ref: method.verweis)
                                : await model.enable(ref: method.verweis)
                        }
                    },
                    onLimits: { limitFor = method },
                    onRemove: { confirmRemove = method })
            }
        }
    }

    private var verlauf: some View {
        VStack(alignment: .leading, spacing: 10) {
            sectionHeader("ZULETZT", count: model.ledger.count)
            ForEach(model.ledger.prefix(8)) { row in
                PaymentLedgerLine(row: row)
            }
        }
    }

    private var hinzufuegen: some View {
        Button { showAdd = true } label: {
            HStack(spacing: 10) {
                Image(systemName: "plus.circle.fill")
                    .font(.title3).foregroundStyle(Theme.blue)
                Text("Zahlungsmittel hinterlegen")
                    .font(.body.weight(.medium)).foregroundStyle(Theme.ink)
                Spacer(minLength: 0)
                Image(systemName: "chevron.right")
                    .font(.caption.weight(.semibold)).foregroundStyle(Theme.ink3)
            }
            .padding(Theme.Space.card)
            .frame(maxWidth: .infinity, alignment: .leading)
            .card()
        }
        .buttonStyle(PressScale())
    }

    private var hinweis: some View {
        VStack(alignment: .leading, spacing: 8) {
            Text("Was SOLVIO nicht hat")
                .font(.footnote.weight(.semibold)).foregroundStyle(Theme.ink2)
            Text("Deine Kartennummer, die Prüfziffer und dein Bankzugang liegen "
                 + "nicht auf diesem Mac. SOLVIO kennt nur einen Verweis darauf "
                 + "— und jede einzelne Zahlung kostet dein Gesicht.")
                .font(.footnote).foregroundStyle(Theme.ink3)
                .fixedSize(horizontal: false, vertical: true)
        }
        .padding(Theme.Space.card)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(Theme.surface2,
                    in: RoundedRectangle(cornerRadius: Theme.Radius.card,
                                         style: .continuous))
    }

    private func sectionHeader(_ title: String, count: Int) -> some View {
        HStack {
            Text(title).font(.caption2.weight(.bold)).tracking(1.1)
                .foregroundStyle(Theme.ink3)
            Spacer()
            Text("\(count)").font(.caption2).foregroundStyle(Theme.ink3)
        }
    }
}

// MARK: - Ein offener Vorgang

struct PaymentIntentCard: View {
    let intent: PaymentIntentView
    let working: Bool
    let onPay: () -> Void

    var body: some View {
        VStack(alignment: .leading, spacing: 12) {
            VStack(alignment: .leading, spacing: 3) {
                Text(intent.zweck.isEmpty ? intent.haendler : intent.zweck)
                    .font(.body.weight(.semibold)).foregroundStyle(Theme.ink)
                Text(intent.haendler_herkunft)
                    .font(.caption.monospaced()).foregroundStyle(Theme.ink2)
                    .lineLimit(1).truncationMode(.middle)
            }
            Text(intent.betrag_lesbar)
                .font(.system(size: 32, weight: .semibold, design: .rounded))
                .foregroundStyle(Theme.ink)
                .minimumScaleFactor(0.6).lineLimit(1)
            HStack(spacing: 6) {
                Text(zahlungZustandswort(intent.zustand))
                    .font(.caption2.weight(.medium))
                    .padding(.horizontal, 8).padding(.vertical, 4)
                    .background(Theme.tintGold,
                                in: Capsule()).foregroundStyle(Theme.inkOnGold)
                Spacer(minLength: 0)
            }
            if intent.zustand == "ready_for_approval" {
                Button(action: onPay) {
                    HStack(spacing: 8) {
                        Image(systemName: "faceid")
                        Text("Mit \(Biometrics.faceIDName) bezahlen")
                    }
                    .font(.subheadline.weight(.semibold))
                    .frame(maxWidth: .infinity).padding(.vertical, 12)
                    .background(Theme.blue).foregroundStyle(Theme.onBlue)
                    .clipShape(RoundedRectangle(cornerRadius: Theme.Radius.row))
                }
                .buttonStyle(PressScale())
                .disabled(working)
            } else if intent.zustand == "reconciliation_required" {
                Text("Ich weiß nicht sicher, ob das durchgegangen ist. Frag mich, "
                     + "und ich sehe beim Anbieter nach — noch einmal versuchen "
                     + "tue ich es nicht.")
                    .font(.footnote).foregroundStyle(Theme.ink2)
                    .fixedSize(horizontal: false, vertical: true)
            } else if intent.zustand == "awaiting_sca" {
                Text("Deine Bank wartet auf dich. Bestätige die Zahlung in "
                     + "ihrer App — das kann ich nicht für dich.")
                    .font(.footnote).foregroundStyle(Theme.ink2)
                    .fixedSize(horizontal: false, vertical: true)
            }
        }
        .padding(Theme.Space.card)
        .frame(maxWidth: .infinity, alignment: .leading)
        .card()
    }
}

// MARK: - Eine Zeile je Zahlungsmittel

struct PaymentMethodRow: View {
    let method: PaymentMethod
    let onToggle: () -> Void
    let onLimits: () -> Void
    let onRemove: () -> Void

    var body: some View {
        VStack(alignment: .leading, spacing: 10) {
            HStack(alignment: .top, spacing: 12) {
                Image(systemName: "creditcard.fill")
                    .font(.system(size: 16, weight: .semibold))
                    .foregroundStyle(method.verfuegbar ? Theme.blue : Theme.ink3)
                    .frame(width: 38, height: 38)
                    .background(method.verfuegbar ? Theme.tintBlue : Theme.surface2,
                                in: RoundedRectangle(cornerRadius: 11,
                                                     style: .continuous))
                VStack(alignment: .leading, spacing: 3) {
                    HStack(spacing: 6) {
                        Text(method.title)
                            .font(.body.weight(.medium)).foregroundStyle(Theme.ink)
                        if !method.hinweis.isEmpty {
                            Text(method.hinweis)
                                .font(.caption.monospaced())
                                .foregroundStyle(Theme.ink2)
                        }
                    }
                    Text("\(zahlungArtwort(method.art)) · \(zahlungStatuswort(method.status))")
                        .font(.caption).foregroundStyle(Theme.ink2)
                    Text(grenzen).font(.caption).foregroundStyle(Theme.ink3)
                    if !method.haendler.isEmpty {
                        Text("Nur bei: " + method.haendler.joined(separator: ", "))
                            .font(.caption).foregroundStyle(Theme.ink3)
                    }
                    if !method.zuletzt_benutzt.isEmpty {
                        Text("Zuletzt benutzt: \(method.zuletzt_benutzt)")
                            .font(.caption2).foregroundStyle(Theme.ink3)
                    }
                }
                Spacer(minLength: 0)
            }
            HStack(spacing: 8) {
                smallButton(method.verfuegbar ? "Sperren" : "Freigeben",
                            role: method.verfuegbar ? .warn : .blue, action: onToggle)
                smallButton("Grenzen", role: .neutral, action: onLimits)
                smallButton("Entfernen", role: .bad, action: onRemove)
                Spacer(minLength: 0)
            }
        }
        .padding(Theme.Space.card)
        .frame(maxWidth: .infinity, alignment: .leading)
        .card()
    }

    private var grenzen: String {
        var parts = ["höchstens " + PaymentAmount.text(method.grenze_einzeln_minor,
                                                       method.waehrungen.first ?? "EUR")
                     + " je Zahlung"]
        if method.grenze_taeglich_minor > 0 {
            parts.append(PaymentAmount.text(method.grenze_taeglich_minor,
                                            method.waehrungen.first ?? "EUR")
                         + " je Tag")
        }
        return parts.joined(separator: " · ")
    }

    private enum Role { case blue, warn, bad, neutral }

    private func smallButton(_ title: String, role: Role,
                             action: @escaping () -> Void) -> some View {
        let tint: Color = {
            switch role {
            case .blue: return Theme.blue
            case .warn: return Theme.warn
            case .bad: return Theme.bad
            case .neutral: return Theme.ink2
            }
        }()
        return Button(action: action) {
            Text(title)
                .font(.caption.weight(.medium))
                .padding(.horizontal, 12).padding(.vertical, 7)
                .background(Theme.surface2, in: Capsule())
                .foregroundStyle(tint)
        }
        .buttonStyle(PressScale())
    }
}

// MARK: - Eine Zeile im Zahlungsbuch

struct PaymentLedgerLine: View {
    let row: PaymentLedgerRow

    var body: some View {
        HStack(alignment: .top, spacing: 10) {
            Circle().fill(tone).frame(width: 7, height: 7).padding(.top, 6)
            VStack(alignment: .leading, spacing: 2) {
                Text(wort).font(.footnote.weight(.medium)).foregroundStyle(Theme.ink)
                Text(zeile).font(.caption).foregroundStyle(Theme.ink3)
                    .lineLimit(2)
            }
            Spacer(minLength: 0)
        }
        .padding(.horizontal, Theme.Space.card)
        .padding(.vertical, 10)
        .frame(maxWidth: .infinity, alignment: .leading)
        .card()
    }

    private var wort: String {
        switch row.event {
        case "created": return "Kauf vorbereitet"
        case "quoted": return "Betrag bestätigt"
        case "approved": return "Freigegeben"
        case "claimed": return "Wird ausgeführt"
        case "charged": return "Bezahlt"
        case "declined": return "Abgelehnt"
        case "failed": return "Nicht bezahlt"
        case "ambiguous": return "Ausgang unklar"
        case "reconciled": return "Nachgesehen"
        case "cancelled": return "Storniert"
        case "expired": return "Abgelaufen"
        case "refunded": return "Zurückerstattet"
        case "denied": return "Verweigert"
        case "instrument_changed": return "Zahlungsmittel geändert"
        default: return row.event
        }
    }

    private var zeile: String {
        var parts: [String] = []
        if row.amount_minor > 0 && !row.currency.isEmpty {
            parts.append(PaymentAmount.text(row.amount_minor, row.currency))
        }
        if !row.description.isEmpty { parts.append(row.description) }
        if !row.merchant_id.isEmpty { parts.append(row.merchant_id) }
        if !row.failure_category.isEmpty && row.failure_category != "none" {
            parts.append(row.failure_category)
        }
        return parts.joined(separator: " · ")
    }

    private var tone: Color {
        switch row.event {
        case "charged": return Theme.good
        case "ambiguous", "denied": return Theme.warn
        case "declined", "failed": return Theme.bad
        case "refunded", "cancelled": return Theme.ink2
        default: return Theme.ink3
        }
    }
}
