// Die Chatliste — aufklappbar vom Kopf der Startansicht.
//
// Sie zeigt nur, was der Core liefert: Titel, letzte Aktivitaet, ein Punkt bei
// laufenden Auftraegen, ein Zaehler bei offenen Zustellungen. „Neuer Chat" legt
// ausdruecklich einen an; ein Doppeltipp erzeugt keinen zweiten.
import SwiftUI

struct ConversationListView: View {
    @ObservedObject var model: ConversationModel
    let onSelect: (String) -> Void
    let onCreate: () -> Void
    @Environment(\.dismiss) private var dismiss
    @Environment(\.dynamicTypeSize) private var typeSize

    var body: some View {
        NavigationStack {
            List {
                Section {
                    Button {
                        Haptics.tap(); onCreate()
                    } label: {
                        Label(model.creating ? "Chat wird angelegt …" : "Neuer Chat", systemImage: "plus.bubble")
                            .font(.body.weight(.semibold)).frame(minHeight: 44)
                    }
                    .disabled(model.creating).accessibilityIdentifier("chats.new")
                    if !model.createError.isEmpty {
                        Text(verbatim: model.createError).font(.footnote).foregroundStyle(Theme.warn)
                    }
                }
                Section {
                    if !model.listError.isEmpty {
                        Text(verbatim: model.listError).font(.footnote).foregroundStyle(Theme.warn)
                    }
                    if model.conversations.isEmpty && model.listError.isEmpty {
                        Text("Noch keine Chats. Schreib SOLVIO einfach — der erste Chat entsteht mit deiner ersten Nachricht.")
                            .font(.subheadline).foregroundStyle(Theme.ink2)
                    }
                    ForEach(model.conversations) { chat in
                        Button {
                            Haptics.tap(); onSelect(chat.conversation_id); dismiss()
                        } label: { row(chat) }
                        .buttonStyle(.plain)
                        .accessibilityIdentifier("chats.row.\(chat.conversation_id)")
                        .accessibilityLabel(accessibilityLabel(chat))
                    }
                } header: {
                    Text(model.workingCount > 0 ? "SOLVIO arbeitet an \(model.workingCount) \(model.workingCount == 1 ? "Auftrag" : "Aufträgen")" : "Chats")
                }
            }
            .navigationTitle("Chats").navigationBarTitleDisplayMode(.inline)
            .toolbar {
                ToolbarItem(placement: .cancellationAction) {
                    Button("Schließen") { dismiss() }.accessibilityIdentifier("chats.close")
                }
            }
        }
    }

    private func row(_ chat: ConversationSummary) -> some View {
        let selected = chat.conversation_id == model.selectedID
        return HStack(alignment: .top, spacing: 12) {
            VStack(alignment: .leading, spacing: 4) {
                Text(verbatim: chat.displayTitle)
                    .font(.body.weight(selected ? .semibold : .regular)).foregroundStyle(Theme.ink)
                    .lineLimit(typeSize.isAccessibilitySize ? nil : 2)
                HStack(spacing: 8) {
                    Text(relativeTime(chat.last_activity_at)).font(.caption).foregroundStyle(Theme.ink3)
                    if (chat.open_delivery_count ?? 0) > 0 {
                        Text("\(chat.open_delivery_count ?? 0) in Arbeit").font(.caption.weight(.medium)).foregroundStyle(Theme.blue)
                    }
                }
            }
            Spacer(minLength: 0)
            if (chat.open_task_count ?? 0) > 0 {
                Circle().fill(Theme.gold).frame(width: 10, height: 10).padding(.top, 6)
                    .accessibilityHidden(true)
            }
            if selected {
                Image(systemName: "checkmark").font(.caption.weight(.semibold)).foregroundStyle(Theme.blue).padding(.top, 4)
                    .accessibilityHidden(true)
            }
        }
        .frame(maxWidth: .infinity, minHeight: 44, alignment: .leading).contentShape(Rectangle())
    }

    private func accessibilityLabel(_ chat: ConversationSummary) -> String {
        var parts = [chat.displayTitle, relativeTime(chat.last_activity_at)]
        if (chat.open_task_count ?? 0) > 0 { parts.append("Auftrag läuft") }
        if (chat.open_delivery_count ?? 0) > 0 { parts.append("\(chat.open_delivery_count ?? 0) Nachrichten in Arbeit") }
        if chat.conversation_id == model.selectedID { parts.append("ausgewählt") }
        return parts.joined(separator: ", ")
    }
}
