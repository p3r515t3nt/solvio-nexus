// Der Verlauf eines Chats als Hauptansicht.
//
// Nutzer- und Assistentenblasen kommen aus dem Core-Verlauf; Assistententext
// erhält native Inlineformatierung ohne Linkaktionen. Unter einer Nutzernachricht
// steht ihr Zustellstand: knapper Fortschritt, solange SOLVIO liest; eine
// Fehlerzeile, wenn die Zustellung blockiert ist — nie ein behaupteter Erfolg.
// Hat eine Nachricht einen Auftrag erzeugt, folgt direkt dahinter die
// vorhandene Auftragskarte (`AgentResultView`) mit Dateien und Abbruch.
import SwiftUI
import SolvioApprovalsKit

struct ConversationView: View {
    @ObservedObject var app: AppModel
    @ObservedObject var model: ConversationModel
    let conversationID: String
    var selectionLocked = false
    @Environment(\.dynamicTypeSize) private var typeSize

    /// Karten pollen den Core selbst; mehr als eine Handvoll gleichzeitig
    /// waere ein Chor gegen einen Core im lokalen Netz. Offene Auftraege und die
    /// juengsten bekommen eine Karte, aeltere eine Zeile mit Weg zum Ergebnis.
    static let embeddedCardLimit = 3

    /// Darstellung allein: Originaltext und Accessibility bleiben unverändert.
    static func bubbleText(_ message: ConversationMessage) -> AttributedString {
        guard message.role == "assistant" else { return AttributedString(message.text) }
        var text = (try? AttributedString(markdown: message.text,
            options: .init(interpretedSyntax: .inlineOnlyPreservingWhitespace)))
            ?? AttributedString(message.text)
        // Modelltext darf keine Browser-, App- oder sonstige URL-Aktion eröffnen.
        // Quellen und Ergebnisdateien behalten ihre vorhandenen geprüften Wege.
        text.link = nil
        return text
    }

    private var detail: ConversationDetail? {
        guard let detail = model.detail, detail.conversation.conversation_id == conversationID else { return nil }
        return detail
    }
    private var runsByID: [String: AgentRun] {
        Dictionary((detail?.auftraege ?? []).map { ($0.id, $0) }, uniquingKeysWith: { first, _ in first })
    }
    private var cardRunIDs: Set<String> {
        guard let detail else { return [] }
        let linked = detail.messages.compactMap { $0.delivery?.linkedRunID }
        var chosen = Set(linked.filter { runsByID[$0]?.offen ?? true })
        for id in linked.reversed() where chosen.count < Self.embeddedCardLimit { chosen.insert(id) }
        return chosen
    }
    private var unlinkedRuns: [AgentRun] {
        guard let detail else { return [] }
        let linked = Set(detail.messages.compactMap { $0.delivery?.linkedRunID })
        return detail.auftraege.filter { !linked.contains($0.id) }
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 16) {
            if !model.detailError.isEmpty {
                Text(verbatim: model.detailError).font(.footnote).foregroundStyle(Theme.warn)
                    .fixedSize(horizontal: false, vertical: true)
            }
            if !model.pendingHint.isEmpty {
                Text(verbatim: model.pendingHint).font(.footnote).foregroundStyle(Theme.warn)
                    .fixedSize(horizontal: false, vertical: true).accessibilityIdentifier("chat.pending")
            }
            if let detail {
                if detail.messages.isEmpty {
                    Text("Dieser Chat ist noch leer. Schreib SOLVIO, was zu tun ist.")
                        .font(.subheadline).foregroundStyle(Theme.ink2)
                }
                ForEach(detail.messages) { message in
                    bubble(message)
                    if let delivery = message.delivery, message.isUser {
                        deliveryLine(delivery)
                        if let source = delivery.sourceChat(from: conversationID) {
                            Button {
                                Haptics.tap(); model.select(source.conversation_id)
                            } label: {
                                Text(verbatim: "Aus Chat " + source.displayTitle)
                                    .font(.caption).multilineTextAlignment(.trailing)
                                    .fixedSize(horizontal: false, vertical: true)
                                    .frame(minHeight: 44)
                            }
                            .buttonStyle(.plain).foregroundStyle(Theme.ink2)
                            .frame(maxWidth: .infinity, alignment: .trailing)
                            .accessibilityIdentifier("chat.source.\(message.message_id)")
                            .disabled(selectionLocked)
                        }
                        if let runID = delivery.linkedRunID { taskCard(runID) }
                    }
                }
                if !unlinkedRuns.isEmpty {
                    Text("Aufträge aus diesem Chat").font(.caption.weight(.semibold)).foregroundStyle(Theme.ink2)
                    ForEach(unlinkedRuns) { run in taskCard(run.id) }
                }
            } else if model.detailError.isEmpty {
                ProgressView("Verlauf wird gelesen …").tint(Theme.blue).foregroundStyle(Theme.ink2)
            }
        }
        .accessibilityIdentifier("chat.history")
    }

    private func bubble(_ message: ConversationMessage) -> some View {
        HStack {
            if message.isUser { Spacer(minLength: 32) }
            VStack(alignment: .leading, spacing: 4) {
                Text(Self.bubbleText(message)).font(.body).textSelection(.enabled)
                    .foregroundStyle(message.isUser ? Theme.onBlue : Theme.ink)
                    .fixedSize(horizontal: false, vertical: true)
                Text(clockTime(message.created_at)).font(.caption2)
                    .foregroundStyle(message.isUser ? Theme.onBlue.opacity(0.7) : Theme.ink3)
            }
            .padding(.horizontal, 14).padding(.vertical, 10)
            .background(message.isUser ? Theme.blue : Theme.surface,
                        in: RoundedRectangle(cornerRadius: 18, style: .continuous))
            if !message.isUser { Spacer(minLength: 32) }
        }
        .accessibilityElement(children: .combine)
        .accessibilityLabel((message.isUser ? (detail?.conversation.isReadOnly == true ? "Im Raum: " : "Du: ") : "SOLVIO: ") + message.text)
        .accessibilityIdentifier("chat.bubble.\(message.message_id)")
    }

    @ViewBuilder private func deliveryLine(_ delivery: ConversationDelivery) -> some View {
        if delivery.isOpen {
            HStack(spacing: 8) {
                ProgressView().tint(Theme.blue).scaleEffect(0.8)
                Text(delivery.status == "running" ? "SOLVIO antwortet …" : "SOLVIO liest …").font(.caption)
            }
            .foregroundStyle(Theme.ink2).frame(maxWidth: .infinity, alignment: .trailing)
            .accessibilityIdentifier("chat.delivery.\(delivery.delivery_id).open")
        } else if delivery.isBlocked {
            Text(verbatim: ConversationErrorText.text(delivery.error_code ?? ""))
                .font(.footnote).foregroundStyle(Theme.warn)
                .padding(12).frame(maxWidth: .infinity, alignment: .leading)
                .background(Theme.tintGold, in: RoundedRectangle(cornerRadius: 12))
                .accessibilityIdentifier("chat.delivery.\(delivery.delivery_id).blocked")
        }
    }

    @ViewBuilder private func taskCard(_ runID: String) -> some View {
        let run = runsByID[runID]
        VStack(alignment: .leading, spacing: 10) {
            if cardRunIDs.contains(runID) {
                AgentResultView(app: app, runID: runID, embedded: true)
                    .id(runID + ":" + (app.client?.pairedCoreInstanceID ?? "") + ":" + (app.client?.deviceIdentifier ?? ""))
            } else {
                NavigationLink { AgentResultView(app: app, runID: runID) } label: {
                    HStack {
                        VStack(alignment: .leading, spacing: 4) {
                            Text(verbatim: run?.auftrag ?? "Auftrag").font(.subheadline.weight(.medium)).foregroundStyle(Theme.ink)
                                .lineLimit(typeSize.isAccessibilitySize ? nil : 2)
                            Text(verbatim: run?.zustand ?? "Ergebnis ansehen").font(.caption).foregroundStyle(Theme.ink2)
                        }
                        Spacer(minLength: 8)
                        Image(systemName: "chevron.right").font(.caption.weight(.semibold)).foregroundStyle(Theme.ink3)
                    }.frame(minHeight: 44)
                }.buttonStyle(.plain)
            }
            if run?.anbietergrenze?.grund == "cost_approval_required" {
                Text("Dieser Auftrag wartet auf einen neuen Kostenrahmen. Öffne die Auftragskarte, um den Gesamtbetrag zu prüfen und freizugeben.")
                    .font(.footnote).foregroundStyle(Theme.warn).fixedSize(horizontal: false, vertical: true)
                    .accessibilityIdentifier("chat.cost-approval.\(runID)")
            }
        }
        .padding(Theme.Space.card).frame(maxWidth: .infinity, alignment: .leading)
        .background(Theme.surface, in: RoundedRectangle(cornerRadius: Theme.Radius.card, style: .continuous))
        .accessibilityIdentifier("chat.task.\(runID)")
    }
}
