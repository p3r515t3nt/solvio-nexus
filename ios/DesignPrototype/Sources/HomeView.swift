// Start — der erste Eindruck.
//
// Hierarchie in zwei Sekunden: Wer bin ich (Marke + Gruss + ein Statussatz),
// was tue ich hier (EIN grosser Knopf: Mit SOLVIO sprechen), was braucht mich
// (hoechstens eine Handvoll ruhiger Zeilen). Kein Dashboard aus sechs gleichen
// Kacheln — die Konversation dominiert, alles andere ist Zweitrang.
import SwiftUI

struct HomeView: View {
    @ObservedObject var model: DesignModel
    @Binding var talking: Bool
    @Binding var tab: AppTab

    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: Theme.Space.section) {
                header
                talkButton
                if !model.reachable { OfflineCard() }
                if !model.approvals.isEmpty { attentionCard }
                todaySection
                quietSection
            }
            .padding(.horizontal, Theme.Space.margin)
            .padding(.top, 8)
            .padding(.bottom, 32)
        }
        .background(Theme.bg)
    }

    // MARK: Kopf — Marke, Gruss, ein Satz.

    private var header: some View {
        VStack(alignment: .leading, spacing: 14) {
            HStack(spacing: 10) {
                Mark(size: 40)
                Wordmark(size: .title3)
                Spacer()
            }
            VStack(alignment: .leading, spacing: 6) {
                Text(model.greeting)
                    .font(.display(.largeTitle, weight: .bold))
                    .foregroundStyle(Theme.ink)
                HStack(spacing: 8) {
                    StatusDot(color: model.statusLine.color)
                    Text(model.statusLine.text)
                        .font(.subheadline)
                        .foregroundStyle(Theme.ink2)
                }
                .accessibilityElement(children: .combine)
            }
        }
        .padding(.top, 8)
    }

    // MARK: Der Knopf.

    private var talkButton: some View {
        Button {
            Haptics.tap()
            talking = true
        } label: {
            HStack(spacing: 14) {
                Image(systemName: "waveform")
                    .font(.system(size: 26, weight: .semibold))
                    .symbolRenderingMode(.hierarchical)
                Text("Mit SOLVIO sprechen")
                    .font(.display(.title3, weight: .semibold))
            }
            .foregroundStyle(Theme.onBlue)
            .frame(maxWidth: .infinity)
            .frame(height: 76)
            .background(
                RoundedRectangle(cornerRadius: Theme.Radius.hero, style: .continuous)
                    .fill(Theme.blue)
            )
            .shadow(color: Theme.blue.opacity(0.35), radius: 18, y: 8)
        }
        .buttonStyle(PressScale())
        .disabled(!model.reachable)
        .opacity(model.reachable ? 1 : 0.45)
        .accessibilityHint("Startet ein Gespräch mit SOLVIO.")
    }

    // MARK: Braucht dich.

    private var attentionCard: some View {
        Button { tab = .approvals } label: {
            HStack(spacing: 14) {
                Image(systemName: "faceid")
                    .font(.title2)
                    .foregroundStyle(Theme.inkOnGold)
                VStack(alignment: .leading, spacing: 2) {
                    Text(model.approvals.count == 1 ? "1 Freigabe wartet"
                                                    : "\(model.approvals.count) Freigaben warten")
                        .font(.headline)
                        .foregroundStyle(Theme.inkOnGold)
                    Text(model.approvals.first?.summary ?? "")
                        .font(.footnote)
                        .foregroundStyle(Theme.inkOnGold.opacity(0.8))
                        .lineLimit(1)
                }
                Spacer()
                Image(systemName: "chevron.right")
                    .font(.footnote.weight(.semibold))
                    .foregroundStyle(Theme.inkOnGold.opacity(0.6))
            }
            .padding(Theme.Space.card)
            .background(Theme.tintGold,
                        in: RoundedRectangle(cornerRadius: Theme.Radius.card, style: .continuous))
        }
        .buttonStyle(PressScale())
    }

    // MARK: Heute.

    private var todaySection: some View {
        VStack(alignment: .leading, spacing: 10) {
            Text("Heute")
                .font(.display(.headline))
                .foregroundStyle(Theme.ink)
            VStack(spacing: 0) {
                if let next = model.tasks.first(where: { $0.active }) {
                    HomeRow(icon: "calendar", tint: Theme.blue,
                            title: next.title,
                            subtitle: "Als Nächstes: \(next.nextRun ?? "—")",
                            value: "") { tab = .planned }
                    Divider().padding(.leading, 56)
                }
                HomeRow(icon: "tray.fill", tint: Theme.blue,
                        title: "Hinweise",
                        subtitle: model.unreadCount == 0 ? "Nichts Neues." : nil,
                        value: model.unreadCount == 0 ? "" : "\(model.unreadCount) neu") { tab = .inbox }
            }
            .card()
        }
    }

    // MARK: Ruhige Zeilen.

    private var quietSection: some View {
        VStack(spacing: 0) {
            NavigationLink(value: "system") {
                HStack(spacing: 14) {
                    iconTile("checkmark.seal.fill",
                             tint: model.systemHeadline.tone)
                    VStack(alignment: .leading, spacing: 1) {
                        Text("System").font(.body).foregroundStyle(Theme.ink)
                        Text(model.systemHeadline.text)
                            .font(.footnote).foregroundStyle(Theme.ink2)
                    }
                    Spacer()
                    Image(systemName: "chevron.right")
                        .font(.footnote.weight(.semibold)).foregroundStyle(Theme.ink3)
                }
                .padding(Theme.Space.card)
            }
            Divider().padding(.leading, 56)
            // Zukunftsplatz: Wissen. Im Prototyp sichtbar ausgegraut — die
            // Produktion liefert diese Zeile ERST, wenn Knowledge/Memory UI
            // wirklich existiert. Kein Fake-Bildschirm dahinter.
            HStack(spacing: 14) {
                iconTile("brain", tint: Theme.ink3)
                VStack(alignment: .leading, spacing: 1) {
                    Text("Wissen").font(.body).foregroundStyle(Theme.ink3)
                    Text("Was SOLVIO über dich weiß.")
                        .font(.footnote).foregroundStyle(Theme.ink3)
                }
                Spacer()
                Text("Bald")
                    .font(.caption.weight(.semibold))
                    .foregroundStyle(Theme.ink3)
                    .padding(.horizontal, 10).padding(.vertical, 4)
                    .background(Theme.line, in: Capsule())
            }
            .padding(Theme.Space.card)
            .accessibilityLabel("Wissen. Bald verfügbar.")
        }
        .card()
    }

    private func iconTile(_ symbol: String, tint: Color) -> some View {
        Image(systemName: symbol)
            .font(.system(size: 17, weight: .semibold))
            .foregroundStyle(tint)
            .frame(width: 34, height: 34)
            .background(tint.opacity(0.12),
                        in: RoundedRectangle(cornerRadius: Theme.Radius.chip, style: .continuous))
    }
}

// MARK: - Bausteine

struct HomeRow: View {
    let icon: String
    let tint: Color
    let title: String
    var subtitle: String? = nil
    let value: String
    var action: () -> Void

    var body: some View {
        Button(action: { Haptics.tap(); action() }) {
            HStack(spacing: 14) {
                Image(systemName: icon)
                    .font(.system(size: 17, weight: .semibold))
                    .foregroundStyle(tint)
                    .frame(width: 34, height: 34)
                    .background(Theme.tintBlue,
                                in: RoundedRectangle(cornerRadius: Theme.Radius.chip, style: .continuous))
                VStack(alignment: .leading, spacing: 1) {
                    Text(title).font(.body).foregroundStyle(Theme.ink)
                    if let subtitle {
                        Text(subtitle).font(.footnote).foregroundStyle(Theme.ink2)
                    }
                }
                Spacer()
                if !value.isEmpty {
                    Text(value).font(.subheadline).foregroundStyle(Theme.ink2)
                }
                Image(systemName: "chevron.right")
                    .font(.footnote.weight(.semibold)).foregroundStyle(Theme.ink3)
            }
            .padding(Theme.Space.card)
            .contentShape(Rectangle())
        }
        .buttonStyle(.plain)
    }
}

struct OfflineCard: View {
    var body: some View {
        HStack(spacing: 12) {
            Image(systemName: "wifi.slash").foregroundStyle(Theme.warn)
            VStack(alignment: .leading, spacing: 2) {
                Text("SOLVIO ist gerade nicht erreichbar.")
                    .font(.subheadline.weight(.medium)).foregroundStyle(Theme.ink)
                Text("Angezeigt wird der Stand von vor 12 Minuten.")
                    .font(.caption).foregroundStyle(Theme.ink2)
            }
            Spacer()
        }
        .padding(Theme.Space.card)
        .background(Theme.tintGold,
                    in: RoundedRectangle(cornerRadius: Theme.Radius.card, style: .continuous))
    }
}

/// Sanfte Druckreaktion — Tastgefuehl ohne Effekthascherei.
struct PressScale: ButtonStyle {
    func makeBody(configuration: Configuration) -> some View {
        configuration.label
            .scaleEffect(configuration.isPressed ? 0.98 : 1)
            .animation(.easeOut(duration: 0.12), value: configuration.isPressed)
    }
}
