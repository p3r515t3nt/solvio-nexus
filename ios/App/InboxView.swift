// Hinweise — was SOLVIO dir sagen will.
//
// Assistentenkommunikation, keine Systemprotokolle. Drei sichtbare Stufen:
// wichtig (Goldkarte), normal (blauer Ungelesen-Punkt), gelesen (ruhig).
// Gelesen/ungelesen, Dringlichkeit und Kennung kommen aus dem Core; „als
// gelesen markieren" bleibt an die unveraenderte Kennung der angetippten
// Meldung gebunden.
import SwiftUI

struct InboxView: View {
    @ObservedObject var model: AppModel

    private var fresh: [InboxItem] { model.inbox.filter { !$0.gelesen } }
    private var earlier: [InboxItem] { model.inbox.filter { $0.gelesen } }

    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: Theme.Space.section) {
                PushSettingsView(client: model.client)
                if !model.reachable { OfflineCard(since: model.snapshotAt) }
                if model.inbox.isEmpty {
                    EmptyState(icon: "tray",
                               title: "Keine neuen Hinweise.",
                               text: "Wenn SOLVIO im Hintergrund etwas für dich findet, steht es hier.")
                }
                if !fresh.isEmpty { section("Neu", fresh) }
                if !earlier.isEmpty { section("Früher", earlier) }
            }
            .padding(.horizontal, Theme.Space.margin)
            .padding(.top, 8)
        }
        .background(Theme.bg)
        .navigationTitle("Hinweise")
        .task { await model.refreshControl() }
        .refreshable { await model.refreshControl() }
        .navigationDestination(for: InboxItem.self) { NoticeDetailView(model: model, item: $0) }
    }

    private func section(_ title: String, _ items: [InboxItem]) -> some View {
        VStack(alignment: .leading, spacing: 10) {
            Text(title).font(.display(.headline)).foregroundStyle(Theme.ink)
            VStack(spacing: 10) {
                ForEach(items) { item in
                    NavigationLink(value: item) { NoticeCard(item: item) }
                        .buttonStyle(PressScale())
                }
            }
        }
    }
}

struct NoticeCard: View {
    let item: InboxItem

    private var important: Bool { item.dringlichkeit == "wichtig" }

    var body: some View {
        HStack(alignment: .top, spacing: 12) {
            if important {
                Image(systemName: "exclamationmark.circle.fill")
                    .font(.body).foregroundStyle(Theme.inkOnGold)
                    .padding(.top, 2)
                    .accessibilityLabel("Wichtig")
            } else {
                Circle()
                    .fill(item.gelesen ? Color.clear : Theme.blue)
                    .frame(width: 8, height: 8).padding(.top, 7)
                    .accessibilityLabel(item.gelesen ? "" : "Ungelesen")
            }
            VStack(alignment: .leading, spacing: 4) {
                Text(item.zusammenfassung)
                    .font(.subheadline.weight(item.gelesen ? .regular : .medium))
                    .foregroundStyle(important ? Theme.inkOnGold : Theme.ink)
                    .multilineTextAlignment(.leading)
                    .lineLimit(3)
                Text("\(item.quelle) · \(relativeTime(item.zeit))")
                    .font(.caption)
                    .foregroundStyle(important ? Theme.inkOnGold.opacity(0.7) : Theme.ink2)
            }
            Spacer(minLength: 0)
        }
        .padding(Theme.Space.card)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(important ? AnyShapeStyle(Theme.tintGold) : AnyShapeStyle(Theme.surface),
                    in: RoundedRectangle(cornerRadius: Theme.Radius.card, style: .continuous))
        .opacity(item.gelesen && !important ? 0.8 : 1)
    }
}

struct NoticeDetailView: View {
    @ObservedObject var model: AppModel
    /// Wieder eine Kopie: das Antippen bindet an DIESE Meldung.
    let item: InboxItem
    @State private var detail: InboxItem?

    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: Theme.Space.section) {
                if let runID = (detail ?? item).agentRunID {
                    NavigationLink { AgentResultView(app: model, runID: runID) } label: {
                        Label("Ergebnis ansehen", systemImage: "doc.richtext")
                            .font(.body.weight(.medium)).foregroundStyle(Theme.blue)
                            .frame(maxWidth: .infinity).padding(16)
                            .background(Theme.tintBlue, in: RoundedRectangle(cornerRadius: 14))
                    }
                }
                Text((detail ?? item).zusammenfassung)
                    .font(.display(.title3, weight: .semibold))
                    .foregroundStyle(Theme.ink)
                    .fixedSize(horizontal: false, vertical: true)

                if let findings = detail?.befunde, !findings.isEmpty {
                    VStack(alignment: .leading, spacing: 8) {
                        Text("Einzelheiten")
                            .font(.display(.headline)).foregroundStyle(Theme.ink)
                        VStack(alignment: .leading, spacing: 8) {
                            ForEach(findings, id: \.self) { finding in
                                HStack(alignment: .top, spacing: 8) {
                                    Circle().fill(Theme.ink3)
                                        .frame(width: 5, height: 5).padding(.top, 7)
                                    Text(finding).font(.callout).foregroundStyle(Theme.ink2)
                                }
                            }
                        }
                        .padding(14)
                        .frame(maxWidth: .infinity, alignment: .leading)
                        .background(Theme.surface2,
                                    in: RoundedRectangle(cornerRadius: 12, style: .continuous))
                        .overlay(RoundedRectangle(cornerRadius: 12).stroke(Theme.line))
                    }
                }

                HStack {
                    Label(item.quelle, systemImage: "sparkle")
                    Spacer()
                    Text(relativeTime(item.zeit))
                }
                .font(.caption).foregroundStyle(Theme.ink3)

                if !item.gelesen {
                    Button {
                        Haptics.tap()
                        Task {
                            try? await model.client?.markRead(itemID: item.id)
                            await model.refreshControl()
                        }
                    } label: {
                        Label("Als gelesen markieren", systemImage: "checkmark.circle")
                            .font(.body.weight(.medium)).foregroundStyle(Theme.blue)
                            .frame(maxWidth: .infinity).frame(height: 52)
                            .background(Theme.tintBlue,
                                        in: RoundedRectangle(cornerRadius: 16, style: .continuous))
                    }
                    .buttonStyle(PressScale())
                }
            }
            .padding(.horizontal, Theme.Space.margin)
            .padding(.vertical, 8)
        }
        .background(Theme.bg)
        .navigationTitle("Hinweis")
        .navigationBarTitleDisplayMode(.inline)
        .task { detail = try? await model.client?.inboxItem(id: item.id) }
    }
}
