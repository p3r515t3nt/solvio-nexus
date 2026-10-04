// Die Freigabe selbst.
//
// Diese Datei ist eine NEUE EINKLEIDUNG einer unveraenderten Sicherheitslogik.
// Verglichen mit der freigegebenen Fassung ist jede Pruefung dieselbe, in
// derselben Reihenfolge:
//
// 1. Die signierte Anfrage wird geholt und ueber die EXAKT empfangenen Bytes
//    verifiziert (`model.verifyChallenge`, unveraendert).
// 2. Widerspricht die ungesignte Liste der signierten Anfrage in irgendeinem
//    Feld, wird gesperrt — `detectMismatch` ist Feld fuer Feld dieselbe.
// 3. Vorher gibt es KEINEN Entscheidungsknopf.
// 4. Der Auftrag steht woertlich, monospaced, selektierbar; er scrollt, wenn er
//    lang ist, und wird nie gekuerzt.
// 5. Die KI-Zusammenfassung traegt ihre Warnung.
// 6. Freigeben ruft `model.decide(vc, approve: true)` — Face ID im Secure
//    Enclave plus frische App-Attest-Aussage, beides vom Mac geprueft.
//
// Was sich geaendert hat: Form, Farbe, Reihenfolge der ANZEIGE und ein Satz
// darueber, was die Freigabe bewirkt. Nichts davon beruehrt die Autoritaet.
import SwiftUI
import SolvioApprovalsKit

struct ApprovalDetailView: View {
    @ObservedObject var model: AppModel
    let approval: PendingApproval
    @State private var verified: VerifiedChallenge?
    @State private var loading = true
    @State private var mismatch = false

    /// The unsigned list is DISCOVERY ONLY. If it contradicts the signed challenge we fail
    /// closed and never silently prefer one over the other.
    private func detectMismatch(_ ch: ApprovalChallenge) -> Bool {
        ch.actionDigest != approval.action_digest || ch.mode != approval.mode
            || ch.workspace != approval.workspace || ch.task != approval.task
            || ch.toolID != approval.tool || ch.humanSummary != approval.human_summary
    }

    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: Theme.Space.section) {
                if loading {
                    HStack(spacing: 10) {
                        ProgressView()
                        Text("Signierte Anfrage wird geprüft …")
                            .font(.subheadline).foregroundStyle(Theme.ink2)
                    }
                    .frame(maxWidth: .infinity).frame(height: 80)
                } else if mismatch {
                    securityWarning
                } else if let vc = verified {
                    content(vc)
                } else {
                    unverifiable
                }
            }
            .padding(.horizontal, Theme.Space.margin)
            .padding(.vertical, 8)
        }
        .background(Theme.bg)
        .navigationTitle("Freigabe")
        .navigationBarTitleDisplayMode(.inline)
        .task { await load() }
    }

    // MARK: Der Normalfall

    @ViewBuilder
    private func content(_ vc: VerifiedChallenge) -> some View {
        let ch = vc.challenge
        let look = ToolLook.of(ch.toolID)

        // WAS und WO — und was danach passiert.
        VStack(alignment: .leading, spacing: 12) {
            HStack(spacing: 12) {
                Image(systemName: look.symbol)
                    .font(.system(size: 20, weight: .semibold))
                    .foregroundStyle(Theme.blue)
                    .frame(width: 44, height: 44)
                    .background(Theme.tintBlue,
                                in: RoundedRectangle(cornerRadius: 12, style: .continuous))
                VStack(alignment: .leading, spacing: 2) {
                    Text(look.label)
                        .font(.display(.title3, weight: .semibold))
                        .foregroundStyle(Theme.ink)
                    Text(ch.workspace.isEmpty ? ch.toolID : ch.workspace)
                        .font(.subheadline).foregroundStyle(Theme.ink2)
                }
                Spacer(minLength: 0)
            }
            HStack(alignment: .top, spacing: 8) {
                Image(systemName: "info.circle.fill")
                    .font(.footnote).foregroundStyle(Theme.warn).padding(.top, 2)
                Text(consequenceSentence(mode: ch.mode))
                    .font(.footnote).foregroundStyle(Theme.inkOnGold)
            }
            .padding(12)
            .frame(maxWidth: .infinity, alignment: .leading)
            .background(Theme.tintGold,
                        in: RoundedRectangle(cornerRadius: 12, style: .continuous))
        }
        .padding(Theme.Space.card)
        .card()

        // DER KAUF — geordnet, aber AUS dem signierten Auftrag.
        //
        // Kein zweiter Datenweg: was hier steht, ist derselbe Text, der
        // darunter woertlich steht. Er wird nur so angeordnet, dass der Betrag
        // gross und allein dasteht statt als achte Zeile in einem Block.
        // Laesst sich der Text nicht vollstaendig lesen, erscheint diese Karte
        // gar nicht — dann bleibt es beim woertlichen Auftrag, und das ist die
        // sichere Richtung.
        let kauf = ParsedPurchase.parse(ch.task)
        if ch.toolID.hasPrefix("purchase_place") && kauf.complete {
            PaymentApprovalCard(parsed: kauf)
        }

        // DER AUFTRAG — massgeblich, woertlich, monospaced.
        VStack(alignment: .leading, spacing: 8) {
            label("AUFTRAG — MASSGEBLICH")
            ScrollView(.vertical) {
                Text(verbatim: ch.task)
                    .font(.callout.monospaced())
                    .foregroundStyle(Theme.ink)
                    .textSelection(.enabled)
                    .frame(maxWidth: .infinity, alignment: .leading)
                    .padding(.vertical, 2)
            }
            .frame(maxHeight: 280)
            .padding(14)
            .background(Theme.surface2,
                        in: RoundedRectangle(cornerRadius: 12, style: .continuous))
            .overlay(RoundedRectangle(cornerRadius: 12).stroke(Theme.line))
        }

        // KI-Zusammenfassung — sichtbar unverbindlich.
        VStack(alignment: .leading, spacing: 8) {
            label("ZUSAMMENFASSUNG — NUR HINWEIS")
            Text(verbatim: ch.humanSummary)
                .font(.footnote).foregroundStyle(Theme.ink2)
            Text("Dieser Text stammt von der KI und ist NICHT verbindlich. Maßgeblich ist ausschließlich der Auftrag oben.")
                .font(.caption2).foregroundStyle(Theme.ink3)
        }

        // Entscheidung.
        VStack(spacing: 10) {
            Button { Task { await model.decide(vc, approve: true) } } label: {
                HStack(spacing: 10) {
                    Image(systemName: "faceid").font(.system(size: 20, weight: .semibold))
                    Text("Mit \(Biometrics.faceIDName) freigeben")
                        .font(.body.weight(.semibold))
                }
                .foregroundStyle(Theme.onBlue)
                .frame(maxWidth: .infinity).frame(height: 56)
                .background(Theme.blue,
                            in: RoundedRectangle(cornerRadius: 16, style: .continuous))
            }
            .buttonStyle(PressScale())
            .disabled(model.busy || !model.attested)
            .opacity(model.busy || !model.attested ? 0.5 : 1)

            Button(role: .destructive) { Task { await model.decide(vc, approve: false) } } label: {
                Text("Ablehnen")
                    .font(.body.weight(.medium)).foregroundStyle(Theme.bad)
                    .frame(maxWidth: .infinity).frame(height: 52)
                    .background(Theme.surface,
                                in: RoundedRectangle(cornerRadius: 16, style: .continuous))
                    .overlay(RoundedRectangle(cornerRadius: 16).stroke(Theme.line))
            }
            .buttonStyle(PressScale())
            .disabled(model.busy)

            if !model.attested {
                Text("Freigabe nur auf einem echten iPhone mit Secure Enclave und App Attest möglich.")
                    .font(.caption).foregroundStyle(Theme.ink3)
                    .multilineTextAlignment(.center)
            }
            if !model.status.isEmpty {
                Text(model.status).font(.footnote).foregroundStyle(Theme.ink2)
            }
        }
    }

    // MARK: Die beiden Sperren

    private var securityWarning: some View {
        VStack(alignment: .leading, spacing: 12) {
            Label("SICHERHEITSWARNUNG", systemImage: "exclamationmark.triangle.fill")
                .font(.headline).foregroundStyle(Theme.bad)
            Text("Die Übersichtsliste widerspricht der signierten Anfrage. Diese Freigabe wurde aus Sicherheitsgründen gesperrt.")
                .font(.callout).foregroundStyle(Theme.ink)
        }
        .padding(Theme.Space.card)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(Theme.bad.opacity(0.12),
                    in: RoundedRectangle(cornerRadius: Theme.Radius.card, style: .continuous))
    }

    private var unverifiable: some View {
        VStack(alignment: .leading, spacing: 12) {
            Label("Nicht verifizierbar", systemImage: "xmark.shield.fill")
                .font(.headline).foregroundStyle(Theme.bad)
            Text("Die signierte Anfrage konnte nicht geprüft werden. Keine Freigabe möglich.")
                .font(.callout).foregroundStyle(Theme.ink2)
            Button("Erneut versuchen") { Task { await load() } }
                .font(.body.weight(.medium)).foregroundStyle(Theme.blue)
        }
        .padding(Theme.Space.card)
        .frame(maxWidth: .infinity, alignment: .leading)
        .card()
    }

    private func label(_ text: String) -> some View {
        Text(text)
            .font(.caption.weight(.semibold)).kerning(0.8)
            .foregroundStyle(Theme.ink3)
    }

    private func load() async {
        loading = true; mismatch = false; verified = nil
        let vc = await model.verifyChallenge(approval.approval_id)
        if let vc, detectMismatch(vc.challenge) {
            mismatch = true
            model.status = "SICHERHEITSWARNUNG: Liste und signierte Anfrage widersprechen sich."
        } else {
            verified = vc
        }
        loading = false
    }
}
