// Hinweise — was SOLVIO dir sagen will.
//
// Assistentenkommunikation, keine Systemprotokolle. Drei sichtbare Stufen:
// wichtig (Goldkarte), normal (Zeile mit blauem Punkt solange ungelesen),
// informativ (ruhige Zeile). Keine Benachrichtigungsflut — die Liste ist
// kurz, gelesenes tritt zurueck.
import SwiftUI

struct InboxView: View {
    @ObservedObject var model: DesignModel

    private var fresh: [MockNotice] { model.notices.filter { !$0.read } }
    private var earlier: [MockNotice] { model.notices.filter { $0.read } }

    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: Theme.Space.section) {
                if !model.reachable { OfflineCard() }
                if model.notices.isEmpty {
                    EmptyState(icon: "tray",
                               title: "Keine neuen Hinweise.",
                               text: "Wenn SOLVIO im Hintergrund etwas für dich findet, steht es hier.")
                }
                if !fresh.isEmpty {
                    noticeSection("Neu", fresh)
                }
                if !earlier.isEmpty {
                    noticeSection("Früher", earlier)
                }
            }
            .padding(.horizontal, Theme.Space.margin)
            .padding(.top, 8)
        }
        .background(Theme.bg)
        .navigationTitle("Hinweise")
        .navigationDestination(for: MockNotice.ID.self) { id in
            if let notice = model.notices.first(where: { $0.id == id }) {
                NoticeDetail(notice: notice)
            }
        }
    }

    private func noticeSection(_ title: String, _ items: [MockNotice]) -> some View {
        VStack(alignment: .leading, spacing: 10) {
            Text(title)
                .font(.display(.headline))
                .foregroundStyle(Theme.ink)
            VStack(spacing: 10) {
                ForEach(items) { notice in
                    NavigationLink(value: notice.id) { NoticeRow(notice: notice) }
                        .buttonStyle(PressScale())
                }
            }
        }
    }
}

struct NoticeRow: View {
    let notice: MockNotice

    var body: some View {
        HStack(alignment: .top, spacing: 12) {
            if notice.priority == .important {
                Image(systemName: "exclamationmark.circle.fill")
                    .font(.body)
                    .foregroundStyle(Theme.inkOnGold)
                    .padding(.top, 2)
                    .accessibilityLabel("Wichtig")
            } else {
                Circle()
                    .fill(notice.read ? Color.clear : Theme.blue)
                    .frame(width: 8, height: 8)
                    .padding(.top, 7)
                    .accessibilityLabel(notice.read ? "" : "Ungelesen")
            }
            VStack(alignment: .leading, spacing: 4) {
                Text(notice.summary)
                    .font(.subheadline.weight(notice.read ? .regular : .medium))
                    .foregroundStyle(notice.priority == .important ? Theme.inkOnGold : Theme.ink)
                    .multilineTextAlignment(.leading)
                    .lineLimit(3)
                Text("\(notice.source) · \(notice.time)")
                    .font(.caption)
                    .foregroundStyle(notice.priority == .important
                                     ? Theme.inkOnGold.opacity(0.7) : Theme.ink2)
            }
            Spacer(minLength: 0)
        }
        .padding(Theme.Space.card)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(
            notice.priority == .important
                ? AnyShapeStyle(Theme.tintGold)
                : AnyShapeStyle(Theme.surface),
            in: RoundedRectangle(cornerRadius: Theme.Radius.card, style: .continuous))
        .opacity(notice.read && notice.priority != .important ? 0.8 : 1)
    }
}

struct NoticeDetail: View {
    let notice: MockNotice

    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: Theme.Space.section) {
                Text(notice.summary)
                    .font(.display(.title3, weight: .semibold))
                    .foregroundStyle(Theme.ink)
                    .fixedSize(horizontal: false, vertical: true)

                if !notice.findings.isEmpty {
                    VStack(alignment: .leading, spacing: 8) {
                        Text("Einzelheiten")
                            .font(.display(.headline))
                            .foregroundStyle(Theme.ink)
                        VStack(alignment: .leading, spacing: 8) {
                            ForEach(notice.findings, id: \.self) { finding in
                                HStack(alignment: .top, spacing: 8) {
                                    Circle().fill(Theme.ink3)
                                        .frame(width: 5, height: 5).padding(.top, 7)
                                    Text(finding)
                                        .font(.callout)
                                        .foregroundStyle(Theme.ink2)
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
                    Label(notice.source, systemImage: "sparkle")
                    Spacer()
                    Text(notice.time)
                }
                .font(.caption)
                .foregroundStyle(Theme.ink3)

                if !notice.read {
                    Button { Haptics.tap() } label: {
                        Label("Als gelesen markieren", systemImage: "checkmark.circle")
                            .font(.body.weight(.medium))
                            .foregroundStyle(Theme.blue)
                            .frame(maxWidth: .infinity)
                            .frame(height: 50)
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
    }
}
