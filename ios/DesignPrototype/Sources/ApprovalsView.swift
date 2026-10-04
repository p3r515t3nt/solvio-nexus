// Freigaben — die Stelle, an der Autoritaet ausgeuebt wird.
//
// Das hier ist PRAESENTATION. Die Sicherheitssemantik bleibt exakt die des
// produktiven Freigabewegs: die Liste ist nur Entdeckung, massgeblich ist
// allein die signierte Anfrage, der Auftrag steht woertlich und monospaced,
// die KI-Zusammenfassung ist als unverbindlich gekennzeichnet, und ohne
// verifizierte Anfrage gibt es keinen Entscheidungsknopf. Face ID bleibt die
// tatsaechliche Grenze — daran aendert dieses Design nichts.
import SwiftUI

struct ApprovalsView: View {
    @ObservedObject var model: DesignModel

    var body: some View {
        ScrollView {
            VStack(spacing: 12) {
                if !model.reachable { OfflineCard() }
                if model.approvals.isEmpty {
                    EmptyState(icon: "checkmark.shield",
                               title: "Nichts wartet auf dich.",
                               text: "Wenn SOLVIO etwas tun will, das deine Freigabe braucht, erscheint es hier.")
                } else {
                    ForEach(model.approvals) { approval in
                        NavigationLink(value: approval.id) {
                            ApprovalRow(approval: approval)
                        }
                        .buttonStyle(PressScale())
                    }
                }
            }
            .padding(.horizontal, Theme.Space.margin)
            .padding(.top, 8)
        }
        .background(Theme.bg)
        .navigationTitle("Freigaben")
        .navigationDestination(for: String.self) { id in
            if let approval = model.approvals.first(where: { $0.id == id }) {
                ApprovalDetail(approval: approval)
            }
        }
    }
}

struct ApprovalRow: View {
    let approval: MockApproval

    var body: some View {
        HStack(alignment: .top, spacing: 14) {
            Image(systemName: approval.icon)
                .font(.system(size: 18, weight: .semibold))
                .foregroundStyle(Theme.blue)
                .frame(width: 40, height: 40)
                .background(Theme.tintBlue,
                            in: RoundedRectangle(cornerRadius: Theme.Radius.chip, style: .continuous))
            VStack(alignment: .leading, spacing: 4) {
                Text(approval.summary)
                    .font(.body.weight(.medium))
                    .foregroundStyle(Theme.ink)
                    .multilineTextAlignment(.leading)
                    .lineLimit(2)
                Text("\(approval.toolLabel) · wartet \(approval.waitingSince)")
                    .font(.caption)
                    .foregroundStyle(Theme.ink2)
            }
            Spacer()
            Image(systemName: "chevron.right")
                .font(.footnote.weight(.semibold))
                .foregroundStyle(Theme.ink3)
                .padding(.top, 12)
        }
        .padding(Theme.Space.card)
        .card()
    }
}

struct ApprovalDetail: View {
    let approval: MockApproval
    /// Mock der signierten Pruefung: erst wenn sie da ist, gibt es Knoepfe.
    @State private var verified = false

    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: Theme.Space.section) {
                // WAS und WO — die Kopfkarte.
                VStack(alignment: .leading, spacing: 12) {
                    HStack(spacing: 12) {
                        Image(systemName: approval.icon)
                            .font(.system(size: 20, weight: .semibold))
                            .foregroundStyle(Theme.blue)
                            .frame(width: 44, height: 44)
                            .background(Theme.tintBlue,
                                        in: RoundedRectangle(cornerRadius: 12, style: .continuous))
                        VStack(alignment: .leading, spacing: 2) {
                            Text(approval.toolLabel)
                                .font(.display(.title3, weight: .semibold))
                                .foregroundStyle(Theme.ink)
                            Text(approval.workspace)
                                .font(.subheadline)
                                .foregroundStyle(Theme.ink2)
                        }
                    }
                    // Folge — was passiert danach. Produktsprache statt Modus-Enum.
                    HStack(alignment: .top, spacing: 8) {
                        Image(systemName: "info.circle.fill")
                            .font(.footnote)
                            .foregroundStyle(Theme.warn)
                            .padding(.top, 2)
                        Text(approval.consequence)
                            .font(.footnote)
                            .foregroundStyle(Theme.ink2)
                    }
                    .padding(12)
                    .frame(maxWidth: .infinity, alignment: .leading)
                    .background(Theme.tintGold,
                                in: RoundedRectangle(cornerRadius: 12, style: .continuous))
                }
                .padding(Theme.Space.card)
                .card()

                // DER AUFTRAG — massgeblich, woertlich, monospaced. Unveraendert
                // aus dem produktiven Sicherheitsentwurf uebernommen.
                VStack(alignment: .leading, spacing: 8) {
                    sectionLabel("AUFTRAG — MASSGEBLICH")
                    Text(verbatim: approval.task)
                        .font(.callout.monospaced())
                        .foregroundStyle(Theme.ink)
                        .textSelection(.enabled)
                        .frame(maxWidth: .infinity, alignment: .leading)
                        .padding(14)
                        .background(Theme.surface2,
                                    in: RoundedRectangle(cornerRadius: 12, style: .continuous))
                        .overlay(RoundedRectangle(cornerRadius: 12).stroke(Theme.line))
                }

                // KI-Zusammenfassung — deutlich als unverbindlich markiert.
                VStack(alignment: .leading, spacing: 8) {
                    sectionLabel("ZUSAMMENFASSUNG — NUR HINWEIS")
                    Text(verbatim: approval.summary)
                        .font(.footnote)
                        .foregroundStyle(Theme.ink2)
                    Text("Dieser Text stammt von der KI und ist nicht verbindlich. Maßgeblich ist allein der Auftrag oben.")
                        .font(.caption2)
                        .foregroundStyle(Theme.ink3)
                }

                // Entscheidung.
                VStack(spacing: 10) {
                    if verified {
                        Button { Haptics.success() } label: {
                            HStack(spacing: 10) {
                                Image(systemName: "faceid")
                                    .font(.system(size: 20, weight: .semibold))
                                Text("Mit Face ID freigeben")
                                    .font(.body.weight(.semibold))
                            }
                            .foregroundStyle(Theme.onBlue)
                            .frame(maxWidth: .infinity)
                            .frame(height: 56)
                            .background(Theme.blue,
                                        in: RoundedRectangle(cornerRadius: 16, style: .continuous))
                        }
                        .buttonStyle(PressScale())
                        Button(role: .destructive) { Haptics.warning() } label: {
                            Text("Ablehnen")
                                .font(.body.weight(.medium))
                                .foregroundStyle(Theme.bad)
                                .frame(maxWidth: .infinity)
                                .frame(height: 50)
                                .background(Theme.surface,
                                            in: RoundedRectangle(cornerRadius: 16, style: .continuous))
                                .overlay(RoundedRectangle(cornerRadius: 16).stroke(Theme.line))
                        }
                        .buttonStyle(PressScale())
                    } else {
                        HStack(spacing: 10) {
                            ProgressView()
                            Text("Signierte Anfrage wird geprüft …")
                                .font(.subheadline)
                                .foregroundStyle(Theme.ink2)
                        }
                        .frame(maxWidth: .infinity)
                        .frame(height: 56)
                    }
                }
            }
            .padding(.horizontal, Theme.Space.margin)
            .padding(.vertical, 8)
        }
        .background(Theme.bg)
        .navigationTitle("Freigabe")
        .navigationBarTitleDisplayMode(.inline)
        .task {
            try? await Task.sleep(nanoseconds: 600_000_000)
            withAnimation(Motion.standard) { verified = true }
        }
    }

    private func sectionLabel(_ text: String) -> some View {
        Text(text)
            .font(.caption.weight(.semibold))
            .kerning(0.8)
            .foregroundStyle(Theme.ink3)
    }
}

struct EmptyState: View {
    let icon: String
    let title: String
    let text: String

    var body: some View {
        VStack(spacing: 12) {
            Image(systemName: icon)
                .font(.system(size: 40, weight: .light))
                .foregroundStyle(Theme.ink3)
            Text(title)
                .font(.display(.headline))
                .foregroundStyle(Theme.ink)
            Text(text)
                .font(.subheadline)
                .foregroundStyle(Theme.ink2)
                .multilineTextAlignment(.center)
        }
        .frame(maxWidth: .infinity)
        .padding(.vertical, 64)
        .padding(.horizontal, 24)
    }
}
