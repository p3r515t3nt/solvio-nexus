// Der Owner-Weg einer Rechteänderung — die Runde, die kein Unit-Test je sah.
//
// Bis zur Geräteabnahme am 03.09.2026 wartete das auslösende Blatt die ganze
// Face-ID-Runde ab und verdeckte dabei genau die Karte, über die freigegeben
// wird. Der Eigentümer sass vor einem stummen Bildschirm, und nach zwei
// Minuten schloss sich das Blatt mit unveränderten Rechten. Kein Test hat das
// bemerkt, weil keiner die Runde überhaupt durchlief.
//
// Diese Datei durchläuft sie — mit einem Mac, der antwortet wie der echte.
import XCTest
@testable import SolvioApprovals

/// Ein Mac, der antwortet wie der echte — und mitschreibt, was er bekam.
///
/// Er kann nichts freigeben. Face ID fällt im echten Freigabeweg; hier wird nur
/// nachgestellt, was der Core danach zurückmeldet.
private final class FakeMac: TresorTransport, @unchecked Sendable {
    var eintrag = TresorEntry(secret_ref: "secret://google/refresh",
                              version: 7,
                              allowed_capabilities: ["gmail_list_recent",
                                                     "calendar_list_events"])
    var bekannt = ["gmail_list_recent", "calendar_list_events",
                   "document_find", "document_ask"]
    /// Antworten auf `tresorMutate`, der Reihe nach.
    var antworten: [TresorMutationResult] = []
    /// Was gerade beim Mac offen liegt und auf Face ID wartet.
    var offen: [PendingApproval] = []

    struct Gesendet {
        let capability: String
        let arguments: [String: String]
        let secret: String?
        let sha: String
        let staging: String
    }
    private(set) var gesendet: [Gesendet] = []
    var pauseMutation = false
    var pauseBeforeSending = false
    var beforeSending: CheckedContinuation<Void, Never>?
    var gate: CheckedContinuation<Void, Never>?

    func tresorOverview() async throws -> TresorOverview {
        TresorOverview(zustand: "healthy", grund: "", zugaenge: [eintrag],
                       bekannte_faehigkeiten: bekannt)
    }

    func tresorLedger(ref: String) async throws -> [TresorLedgerRow] { [] }

    func listPending() async throws -> [PendingApproval] { offen }

    func tresorMutate(capability: String, arguments: [String: String],
                      secret: String?, secretSHA256: String,
                      stagingID: String) async throws -> TresorMutationResult {
        if pauseBeforeSending { await withCheckedContinuation { beforeSending = $0 } }
        try Task.checkCancellation()
        gesendet.append(Gesendet(capability: capability, arguments: arguments,
                                 secret: secret, sha: secretSHA256,
                                 staging: stagingID))
        if pauseMutation { await withCheckedContinuation { gate = $0 } }
        return antworten.isEmpty ? TresorMutationResult() : antworten.removeFirst()
    }
}

private func warteAnfrage(_ tool: String,
                          gueltigNoch: Double = 300) -> PendingApproval {
    PendingApproval(approval_id: "a-1", tool: tool, mode: "face_id",
                    task: "Ändern, wofür ein Zugang benutzt werden darf",
                    workspace: "", human_summary: "Rechte ändern",
                    action_digest: "digest-1",
                    expires_at: Date().timeIntervalSince1970 + gueltigNoch)
}

private func antwort(ok: Bool? = nil, outcome: String? = nil,
                     reason: String? = nil, satz: String? = nil,
                     staging: String? = nil) -> TresorMutationResult {
    TresorMutationResult(ok: ok, outcome: outcome, reason: reason,
                         human_message: satz, request_id: nil,
                         staging_id: staging)
}

@MainActor
final class TresorRescopeFlowTests: XCTestCase {

    /// Wartet auf einen Zustand, statt auf eine Uhr zu hoffen.
    private func warteBis(_ grenze: TimeInterval = 8,
                          _ erfuellt: () -> Bool) async -> Bool {
        let ende = Date().addingTimeInterval(grenze)
        while Date() < ende {
            if erfuellt() { return true }
            try? await Task.sleep(nanoseconds: 100_000_000)
        }
        return erfuellt()
    }

    private func aufbau() -> (TresorModel, FakeMac) {
        let mac = FakeMac()
        return (TresorModel(client: mac), mac)
    }

    func testGoogleUsesOneCodeAndResumesOnlyWithStagingAfterDecision() async throws {
        for denied in [false, true] {
            let (model, mac) = aufbau()
            let code = try GoogleAuthorizationCode(code: "synthetic-google-code", accountID: "subject",
                accountEmail: "fixture@example.invalid", scopes: GoogleSignInClient.scopes)
            mac.antworten = [antwort(ok: false, outcome: "approval_required", staging: "google-stage"),
                             antwort(ok: !denied, outcome: denied ? "rejected_by_policy" : "success",
                                     reason: denied ? "denied" : "", satz: denied ? "Abgelehnt." : "Google verbunden.")]
            mac.offen = [warteAnfrage("google_connect")]
            let started = await model.startGoogleConnect(code, binding: String(repeating: "a", count: 64))
            XCTAssertEqual(started, .wartetAufFreigabe)
            XCTAssertThrowsError(try code.takeCode())
            XCTAssertEqual(mac.gesendet.count, 1)
            XCTAssertEqual(mac.gesendet[0].secret, "synthetic-google-code")
            XCTAssertFalse(mac.gesendet[0].arguments.values.contains("synthetic-google-code"))
            let second = try GoogleAuthorizationCode(code: "synthetic-second-code", accountID: "subject",
                accountEmail: "fixture@example.invalid", scopes: GoogleSignInClient.scopes)
            _ = await model.startGoogleConnect(second, binding: String(repeating: "a", count: 64))
            XCTAssertEqual(mac.gesendet.count, 1)
            XCTAssertThrowsError(try second.takeCode())
            mac.offen = []
            let finished = await warteBis { model.pending == nil }
            XCTAssertTrue(finished)
            XCTAssertEqual(mac.gesendet.count, 2)
            XCTAssertNil(mac.gesendet[1].secret)
            XCTAssertEqual(mac.gesendet[1].staging, "google-stage")
            XCTAssertEqual(mac.gesendet[0].arguments, mac.gesendet[1].arguments)
            XCTAssertEqual(mac.gesendet[0].sha, mac.gesendet[1].sha)
            XCTAssertEqual(model.ergebnis?.gelungen, !denied)
        }
    }

    func testEnrollmentChangeDropsLateResponseAndStopsOldApprovalResume() async throws {
        for firstReplyStillPending in [false, true] {
            let coreA = FakeMac()
            let old = TresorModel(client: coreA, restoreEnrollment: false)
            coreA.pauseMutation = firstReplyStillPending
            coreA.antworten = [antwort(ok: false, outcome: "approval_required", staging: "old-core-stage")]
            coreA.offen = [warteAnfrage("google_connect")]
            let codeA = try GoogleAuthorizationCode(code: "synthetic-code-a", accountID: "subject",
                accountEmail: "fixture@example.invalid", scopes: GoogleSignInClient.scopes)
            let running = Task { await old.startGoogleConnect(codeA, binding: String(repeating: "a", count: 64)) }
            let sent = await warteBis { coreA.gesendet.count == 1 && (!firstReplyStillPending || coreA.gate != nil) }
            XCTAssertTrue(sent)
            old.invalidate()
            coreA.gate?.resume(); coreA.gate = nil
            _ = await running.value
            coreA.offen = []
            let coreB = FakeMac()
            let current = TresorModel(client: coreB, restoreEnrollment: false)
            coreB.antworten = [antwort(ok: true, outcome: "success", satz: "Google verbunden.")]
            let codeB = try GoogleAuthorizationCode(code: "synthetic-code-b", accountID: "subject",
                accountEmail: "fixture@example.invalid", scopes: GoogleSignInClient.scopes)
            _ = await current.startGoogleConnect(codeB, binding: String(repeating: "b", count: 64))
            try await Task.sleep(nanoseconds: 1_200_000_000)
            XCTAssertEqual(coreA.gesendet.count, 1)
            XCTAssertEqual(coreB.gesendet.count, 1)
            XCTAssertEqual(coreB.gesendet[0].secret, "synthetic-code-b")
            XCTAssertNil(old.pending)
            XCTAssertNil(old.ergebnis)
            XCTAssertTrue(old.notice.isEmpty)
            XCTAssertEqual(current.ergebnis?.gelungen, true)
        }
    }

    func testEnrollmentChangeCancelsChallengeBeforeAnyGoogleCodeIsSent() async throws {
        let oldCore = FakeMac()
        oldCore.pauseBeforeSending = true
        let old = TresorModel(client: oldCore, restoreEnrollment: false)
        let code = try GoogleAuthorizationCode(code: "synthetic-old-code", accountID: "subject",
            accountEmail: "fixture@example.invalid", scopes: GoogleSignInClient.scopes)
        let task = Task { await old.startGoogleConnect(code, binding: String(repeating: "a", count: 64)) }
        let preparing = await warteBis { oldCore.beforeSending != nil }
        XCTAssertTrue(preparing)
        old.invalidate()
        oldCore.beforeSending?.resume(); oldCore.beforeSending = nil
        let result = await task.value
        XCTAssertEqual(result, .fehlgeschlagen)
        XCTAssertTrue(oldCore.gesendet.isEmpty)
        XCTAssertNil(old.pending)
        XCTAssertTrue(old.notice.isEmpty)
        XCTAssertThrowsError(try code.takeCode())
    }

    // 1 — „Ändern" erzeugt genau EINE Anfrage.
    func testEinTippErzeugtGenauEineAnfrage() async {
        let (model, mac) = aufbau()
        mac.antworten = [antwort(ok: false, outcome: "approval_required",
                                 staging: "st-1")]
        mac.offen = [warteAnfrage("secret_rescope")]

        let start = await model.startRescope(ref: "secret://google/refresh",
                                             capabilities: ["gmail_list_recent",
                                                            "document_find"],
                                             expectedVersion: 7)
        XCTAssertEqual(start, .wartetAufFreigabe)
        XCTAssertEqual(mac.gesendet.count, 1)
        XCTAssertEqual(mac.gesendet.first?.capability, "secret_rescope")
        XCTAssertNotNil(model.pending)
    }

    // 2 — Der Aufruf kehrt zurück, WÄHREND die Freigabe noch offen ist.
    //     Genau das erlaubt dem Blatt, sich sofort zu schliessen.
    func testDerAufrufKehrtVorDerEntscheidungZurueck() async {
        let (model, mac) = aufbau()
        mac.antworten = [antwort(ok: false, outcome: "approval_required",
                                 staging: "st-1")]
        mac.offen = [warteAnfrage("secret_rescope")]

        let vorher = Date()
        let start = await model.startRescope(ref: "secret://google/refresh",
                                             capabilities: ["document_find"],
                                             expectedVersion: 7)
        let gedauert = Date().timeIntervalSince(vorher)

        XCTAssertEqual(start, .wartetAufFreigabe)
        // Die Warteschleife pollt im Sekundentakt. Wer auf sie wartet, braucht
        // mindestens eine Sekunde; wer sie dem Modell überlässt, ist sofort da.
        XCTAssertLessThan(gedauert, 0.5)
        XCTAssertFalse(mac.offen.isEmpty, "Die Freigabe steht noch offen")
    }

    // 3 — Das Blatt zu schliessen bricht NICHTS ab: die Runde läuft weiter und
    //     endet von selbst mit einem Ergebnis.
    func testDieRundeUeberlebtDasSchliessenDesBlattes() async {
        let (model, mac) = aufbau()
        mac.antworten = [antwort(ok: false, outcome: "approval_required",
                                 staging: "st-1")]
        mac.offen = [warteAnfrage("secret_rescope")]

        _ = await model.startRescope(ref: "secret://google/refresh",
                                     capabilities: ["document_find"],
                                     expectedVersion: 7)
        XCTAssertNotNil(model.pending, "Die Anfrage lebt weiter")

        // Der Eigentümer gibt frei: die Anfrage verschwindet, der zweite
        // Versuch gelingt.
        mac.antworten = [antwort(ok: true, outcome: "success",
                                 satz: "Berechtigung erweitert.")]
        mac.offen = []

        let fertig = await warteBis { model.ergebnis != nil }
        XCTAssertTrue(fertig, "Die Runde ist ohne das Blatt zu Ende gelaufen")
        XCTAssertEqual(model.ergebnis, .erledigt("Berechtigung erweitert."))
        XCTAssertNil(model.pending)
        XCTAssertEqual(mac.gesendet.count, 2, "Anstoss und Fortsetzung")
    }

    // 4 — Face ID bleibt zwingend: solange der Mac nicht bestätigt, entsteht
    //     kein Erfolg. Die App erfindet keinen.
    func testOhneEntscheidungEntstehtKeinErfolg() async {
        let (model, mac) = aufbau()
        mac.antworten = [antwort(ok: false, outcome: "approval_required",
                                 staging: "st-1")]
        mac.offen = [warteAnfrage("secret_rescope")]

        _ = await model.startRescope(ref: "secret://google/refresh",
                                     capabilities: ["document_find"],
                                     expectedVersion: 7)
        // Zwei Runden vergehen lassen, ohne dass jemand entscheidet.
        try? await Task.sleep(nanoseconds: 2_200_000_000)
        XCTAssertNil(model.ergebnis, "Kein Ergebnis ohne Entscheidung")
        XCTAssertNotNil(model.pending, "Die Anfrage steht weiter offen")
        XCTAssertEqual(mac.gesendet.count, 1, "Kein zweiter Versuch ohne Freigabe")
    }

    // 5 — Erfolg erst, wenn der Core ihn bestätigt. Face ID allein genügt nicht:
    //     die Anfrage verschwindet, der Core sagt aber noch „nicht freigegeben".
    func testVerschwundeneAnfrageAlleinIstKeinErfolg() async {
        let (model, mac) = aufbau()
        mac.antworten = [antwort(ok: false, outcome: "approval_required",
                                 staging: "st-1")]
        mac.offen = [warteAnfrage("secret_rescope")]
        _ = await model.startRescope(ref: "secret://google/refresh",
                                     capabilities: ["document_find"],
                                     expectedVersion: 7)

        // Anfrage weg, aber der Core trägt sie nicht.
        mac.antworten = [antwort(ok: false, outcome: "rejected_by_policy",
                                 reason: "not_approved",
                                 satz: "Das ist noch nicht freigegeben.")]
        mac.offen = []

        let fertig = await warteBis { model.ergebnis != nil }
        XCTAssertTrue(fertig)
        XCTAssertFalse(model.ergebnis?.gelungen ?? true,
                       "Kein Erfolg ohne bestätigten Core-Ausgang")
    }

    // 6 — Eine Ablehnung heisst Ablehnung.
    func testAblehnungWirdAlsAblehnungGezeigt() async {
        let (model, mac) = aufbau()
        mac.antworten = [antwort(ok: false, outcome: "approval_required",
                                 staging: "st-1")]
        mac.offen = [warteAnfrage("secret_rescope")]
        _ = await model.startRescope(ref: "secret://google/refresh",
                                     capabilities: ["document_find"],
                                     expectedVersion: 7)

        mac.antworten = [antwort(ok: false, outcome: "rejected_by_policy",
                                 reason: "denied",
                                 satz: "Das hast du abgelehnt. Dabei bleibt es.")]
        mac.offen = []

        _ = await warteBis { model.ergebnis != nil }
        XCTAssertEqual(model.ergebnis,
                       .abgelehnt("Das hast du abgelehnt. Dabei bleibt es."))
    }

    // 7 — Ein Fehler heisst Fehler, nicht Absage.
    func testFehlerWirdAlsFehlerGezeigt() async {
        let (model, mac) = aufbau()
        mac.antworten = [antwort(ok: false, outcome: "capability_failed",
                                 reason: "scope_changed_meanwhile",
                                 satz: "Der Umfang hat sich inzwischen bewegt.")]
        let start = await model.startRescope(ref: "secret://google/refresh",
                                             capabilities: ["document_find"],
                                             expectedVersion: 7)
        XCTAssertEqual(start, .fehlgeschlagen)
        XCTAssertEqual(model.ergebnis,
                       .fehler("Der Umfang hat sich inzwischen bewegt."))
    }

    // 8 — Ein Ablauf heisst Ablauf. Nicht „hat nicht geklappt".
    func testAblaufWirdAlsAblaufGezeigt() async {
        let (model, mac) = aufbau()
        mac.antworten = [antwort(ok: false, outcome: "approval_required",
                                 staging: "st-1")]
        // Die Anfrage ist bereits abgelaufen, als wir sie zum ersten Mal sehen.
        mac.offen = [warteAnfrage("secret_rescope", gueltigNoch: -1)]
        _ = await model.startRescope(ref: "secret://google/refresh",
                                     capabilities: ["document_find"],
                                     expectedVersion: 7)

        let fertig = await warteBis { model.ergebnis != nil }
        XCTAssertTrue(fertig, "Die Runde endet an der Frist der Anfrage")
        guard case .abgelaufen = model.ergebnis else {
            return XCTFail("Erwartet: abgelaufen, bekommen: \(String(describing: model.ergebnis))")
        }
        XCTAssertNil(model.pending)
    }

    // 9 — Ein zweiter Tipp erzeugt keine zweite Freigabe.
    func testZweiterAnstossErzeugtKeineZweiteAnfrage() async {
        let (model, mac) = aufbau()
        mac.antworten = [antwort(ok: false, outcome: "approval_required",
                                 staging: "st-1"),
                         antwort(ok: false, outcome: "approval_required",
                                 staging: "st-2")]
        mac.offen = [warteAnfrage("secret_rescope")]

        _ = await model.startRescope(ref: "secret://google/refresh",
                                     capabilities: ["document_find"],
                                     expectedVersion: 7)
        _ = await model.startRescope(ref: "secret://google/refresh",
                                     capabilities: ["document_find"],
                                     expectedVersion: 7)
        XCTAssertEqual(mac.gesendet.count, 1, "Genau eine Anfrage, nicht zwei")
    }

    // 10 — Gesendet wird exakt die Liste, die die Vorschau zeigt.
    func testGesendetWirdGenauDieVorschau() async {
        let (model, mac) = aufbau()
        mac.antworten = [antwort(ok: false, outcome: "approval_required",
                                 staging: "st-1")]
        mac.offen = [warteAnfrage("secret_rescope")]

        let diff = TresorScopeDiff(
            bisher: ["gmail_list_recent", "calendar_list_events"],
            gewaehlt: ["gmail_list_recent", "calendar_list_events",
                       "document_find", "document_ask"])
        _ = await model.startRescope(ref: "secret://google/refresh",
                                     capabilities: diff.vollstaendig,
                                     expectedVersion: 7)

        let geschickt = mac.gesendet.first?.arguments["capabilities"] ?? ""
        XCTAssertEqual(geschickt, diff.vollstaendig.joined(separator: ", "))
        XCTAssertTrue(diff.entfaellt.isEmpty, "Eine Ergänzung verliert nichts")
        // Die vollständige Liste geht hinaus, nie nur die Ergänzung.
        XCTAssertTrue(geschickt.contains("gmail_list_recent"))
        XCTAssertTrue(geschickt.contains("calendar_list_events"))
    }

    // 11 — Die Fassung, die der Mensch gesehen hat, reist mit.
    func testDieGeseheneFassungReistMit() async {
        let (model, mac) = aufbau()
        mac.antworten = [antwort(ok: false, outcome: "approval_required",
                                 staging: "st-1")]
        mac.offen = [warteAnfrage("secret_rescope")]

        _ = await model.startRescope(ref: "secret://google/refresh",
                                     capabilities: ["document_find"],
                                     expectedVersion: 7)
        XCTAssertEqual(mac.gesendet.first?.arguments["expected_version"], "7")

        // Und sie bleibt beim zweiten Versuch dieselbe — die Zustimmung galt
        // genau diesem Stand.
        mac.antworten = [antwort(ok: true, outcome: "success", satz: "Erledigt.")]
        mac.offen = []
        _ = await warteBis { model.ergebnis != nil }
        XCTAssertEqual(mac.gesendet.last?.arguments["expected_version"], "7")
    }

    // 12 — Auf diesem Weg reist niemals ein Wert.
    func testKeinWertReistMit() async {
        let (model, mac) = aufbau()
        mac.antworten = [antwort(ok: false, outcome: "approval_required",
                                 staging: "st-1")]
        mac.offen = [warteAnfrage("secret_rescope")]

        _ = await model.startRescope(ref: "secret://google/refresh",
                                     capabilities: ["document_find"],
                                     expectedVersion: 7)
        mac.antworten = [antwort(ok: true, outcome: "success", satz: "Erledigt.")]
        mac.offen = []
        _ = await warteBis { model.ergebnis != nil }

        for g in mac.gesendet {
            XCTAssertNil(g.secret, "Ein Rescope trägt keinen Wert")
            XCTAssertEqual(g.sha, "", "Und keinen Abdruck eines Wertes")
            XCTAssertEqual(Set(g.arguments.keys),
                           ["secret_ref", "capabilities", "expected_version"],
                           "Nur diese drei Argumente, nichts sonst")
        }
    }
}
