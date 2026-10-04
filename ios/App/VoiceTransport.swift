// Der Sprachweg zum Core — Transport.
//
// Das iPhone ist ein Endpunkt, kein zweites Gehirn: es schickt Mikrofon-Audio
// und spielt ab, was zurueckkommt. Gespraech, Gedaechtnis, Werkzeuge, Freigabe
// und Entscheidung bleiben im Mac. Hier gibt es deshalb KEINE Anbietersitzung,
// keinen Modellaufruf und keinen eigenen Gespraechszustand — nur Rahmen rein,
// Rahmen raus.
//
// Verbindung: derselbe TLS-Anker, dieselbe gepinnte Verbindung und dieselbe
// Geraetekennung wie der Freigabeweg. Es gibt kein zweites Geheimnis auf dem
// Telefon und keinen zweiten Port am Mac. Die Transportkennung darf lesen und
// sprechen — sie darf niemals freigeben; dafuer bleibt Face ID zustaendig, und
// dieser Weg kennt den Freigabepfad gar nicht.
import Foundation
import CoreFoundation
import SolvioApprovalsKit

/// Optional capability of this Core connection; unknown values keep legacy audio.
enum VoiceMode: String, Sendable {
    case turnBased = "turn_based"
    case fullDuplex = "full_duplex"
}

/// Was der Core ueber die Verbindung sagt. Bewusst die Vokabeln des
/// freigegebenen Satellitenprotokolls — kein zweites, unvertraegliches.
enum VoiceWireEvent {
    /// Die Anbietersitzung steht; ab jetzt wird wirklich zugehoert.
    case sessionReady(mode: VoiceMode)
    case handoffReady(sessionID: String, conversationID: String, version: Int)
    case conversationFlushed(sessionID: String, conversationID: String, status: String, providerClosed: Bool)
    /// Alles Abspielbereite sofort verwerfen (Barge-in oder Sitzungsende).
    case flush
    /// Der Core beendet die Sitzung, mit Grund.
    case sessionEnd(String)
    /// Sprachaudio zum Abspielen: PCM16, 16 kHz, mono.
    case audio(Data)
    /// Eine Auskunft des Cores, die kein Audio ist — etwa: eine lange Aufgabe
    /// hat begonnen. Information, nie ein Zustand des Cores.
    case notice(kind: String)
    /// Die Verbindung ist weg. `retriable` unterscheidet „kurz gestolpert"
    /// von „so nicht" — nur das Erste rechtfertigt einen neuen Versuch.
    case closed(reason: VoiceCloseReason)
}

enum VoiceCloseReason: Equatable {
    /// Netz weg, Mac neu gestartet, Wechsel des WLANs — ein neuer Versuch lohnt.
    case transport
    /// Der Mac spricht gerade mit dem Wohnzimmer. Ein neuer Versuch lohnt erst,
    /// wenn dort Schluss ist — die App sagt das dem Menschen, statt zu hämmern.
    case busy
    /// Dieses Geraet ist nicht (mehr) registriert. Kein Wiederholen.
    case unauthorized
    /// C3: der verlangte Chat laesst sich fuer diese Sitzung nicht binden
    /// (fremd, geloescht, ohne Sitzungsbeweis). Der Core schliesst mit 4401
    /// `conversation_not_bindable`; es gibt KEINEN ungebundenen Ersatz.
    case conversationNotBindable
    /// Der Core hat ordentlich beendet.
    case ended(String)

    var retriable: Bool {
        switch self {
        case .transport: return true
        case .busy, .unauthorized, .conversationNotBindable, .ended: return false
        }
    }

    /// Was ein Abbruch bedeutet, entscheidet der Code, mit dem der Core
    /// schliesst — vor dem Protokollwechsel der HTTP-Status, danach der
    /// WebSocket-Schliesscode. Beides ist die Wahrheit des Cores, nicht eine
    /// Vermutung der App.
    static func classify(httpStatus: Int?, closeCode: Int?, closeReason: String) -> VoiceCloseReason {
        switch httpStatus {
        case 401, 403: return .unauthorized
        case 409: return .busy
        default: break
        }
        switch closeCode {
        case 4401: return closeReason == "conversation_not_bindable" ? .conversationNotBindable : .unauthorized
        case 4409: return .busy
        default: return .transport
        }
    }
}

/// Die Verbindung. Ein Objekt je Gespraech; danach ist es verbraucht.
///
/// Bewusst `URLSessionWebSocketTask` und nicht eine eigene Implementierung:
/// derselbe `URLSession`-Stack, derselbe Pinning-Delegate, dieselbe
/// Zertifikatspruefung wie beim Freigabeweg. Ein zweiter TLS-Pfad waere ein
/// zweiter Ort, an dem die Pruefung spaeter auseinanderlaufen kann.
final class VoiceTransport: NSObject, @unchecked Sendable {

    /// Rahmen groesser als das ist ein Missverstaendnis, kein Sprachaudio.
    private static let maxFrame = 64 * 1024

    private let endpoint: URL
    private let deviceID: String
    private let transportCred: String
    /// C3: der Chat, an den diese Sitzung gebunden sein soll. Geht nur dann
    /// in die Sitzungs-Assertion ein, wenn gesetzt.
    let conversationID: String?
    private let session: URLSession
    private var task: URLSessionWebSocketTask?
    private let events: (VoiceWireEvent) -> Void
    private var closed = false
    /// Signiert den clientDataHash der Sitzungsbindung. Produktion: App Attest.
    private let signer: @Sendable (Data) async throws -> Data
    /// Nur fuer Hosttests: faengt Steuernachrichten ab, statt sie zu senden.
    private let controlSink: (@Sendable ([String: String]) -> Void)?

    /// - Parameters:
    ///   - pairing: liefert Endpunkt UND den gepinnten Fingerabdruck — beides
    ///     stammt aus derselben Kopplung wie der Freigabeweg.
    ///   - conversationID: der zu bindende Chat (C3) oder nil fuer eine
    ///     ungebundene Sitzung wie bisher.
    init(pairing: PairingPayload, deviceID: String, transportCred: String, conversationID: String? = nil,
         signer: @escaping @Sendable (Data) async throws -> Data = { hash in
             try await AppAttestManager.assert(keyId: AppAttestManager.ensureKeyId(), clientDataHash: hash)
         },
         controlSink: (@Sendable ([String: String]) -> Void)? = nil,
         events: @escaping (VoiceWireEvent) -> Void) {
        // Aus `https://host:port` wird `wss://host:port` — derselbe Host, derselbe
        // Port, dasselbe Zertifikat. Nur das Schema wechselt.
        var components = URLComponents(string: pairing.endpoint)
        components?.scheme = "wss"
        self.endpoint = (components?.url ?? URL(string: pairing.endpoint)!)
            .appendingPathComponent("v1").appendingPathComponent("voice")
        self.deviceID = deviceID
        self.transportCred = transportCred
        self.conversationID = (conversationID?.isEmpty == false) ? conversationID : nil
        self.signer = signer
        self.controlSink = controlSink
        self.events = events
        let configuration = URLSessionConfiguration.ephemeral
        configuration.waitsForConnectivity = false
        configuration.timeoutIntervalForRequest = 15
        // Derselbe Delegate wie im Freigabeweg: globale TLS-Pruefung bleibt an,
        // zusaetzlich muss der Fingerabdruck des Blattzertifikats passen.
        self.session = URLSession(configuration: configuration,
                                  delegate: PinningDelegate(pinned: pairing.tls_fingerprint),
                                  delegateQueue: nil)
        super.init()
    }

    func connect() {
        var request = URLRequest(url: endpoint)
        request.setValue(deviceID, forHTTPHeaderField: "X-Device-Id")
        request.setValue(transportCred, forHTTPHeaderField: "X-Transport-Cred")
        let task = session.webSocketTask(with: request)
        task.maximumMessageSize = Self.maxFrame
        self.task = task
        task.resume()
        receive()
    }

    /// Mikrofonaudio abgeben: PCM16, 16 kHz, mono — genau das Format, das der
    /// Core vom Satelliten kennt (`SATELLITE_RATE = 16000`).
    func send(audio: Data) {
        guard !closed, let task else { return }
        task.send(.data(audio)) { [weak self] error in
            if error != nil { self?.fail(.transport) }
        }
    }

    func send(control: [String: String]) {
        guard !closed else { return }
        if let controlSink { controlSink(control); return }
        guard let task,
              let data = try? JSONSerialization.data(withJSONObject: control),
              let text = String(data: data, encoding: .utf8) else { return }
        task.send(.string(text)) { [weak self] error in
            if error != nil { self?.fail(.transport) }
        }
    }

    /// Keep reading until the Core confirms final persistence; local audio is already off.
    func requestEnd(reason: String) {
        send(control: ["type": "session_end", "reason": reason])
    }

    /// Ordentlich Schluss machen. Danach kommt nichts mehr.
    func close(reason: String = "user") {
        guard !closed else { return }
        send(control: ["type": "session_end", "reason": reason])
        closed = true
        task?.cancel(with: .normalClosure, reason: nil)
        task = nil
        session.invalidateAndCancel()
    }

    // MARK: - Empfang

    private func receive() {
        guard let task else { return }
        task.receive { [weak self] result in
            guard let self, !self.closed else { return }
            switch result {
            case let .success(message):
                self.handle(message)
                self.receive()          // genau ein Nachfolger je Nachricht
            case let .failure(error):
                self.fail(self.classify(error))
            }
        }
    }

    func handle(_ message: URLSessionWebSocketTask.Message) {
        guard !closed else { return }
        switch message {
        case let .data(data):
            events(.audio(data))
        case let .string(text):
            guard let payload = try? JSONSerialization.jsonObject(with: Data(text.utf8))
                    as? [String: Any],
                  let kind = payload["type"] as? String else { return }
            switch kind {
            case "session_ready":
                events(.sessionReady(mode: VoiceMode(rawValue: payload["voice_mode"] as? String ?? "") ?? .turnBased))
                let rawVersion = payload["handoff_protocol"] as? NSNumber
                let version = rawVersion.flatMap { CFGetTypeID($0) == CFBooleanGetTypeID() ? nil : $0 as? Int } ?? 0
                events(.handoffReady(sessionID: payload["session_id"] as? String ?? "",
                                     conversationID: payload["conversation_id"] as? String ?? "", version: version))
            case "conversation_flushed":
                guard let id = payload["session_id"] as? String, let chat = payload["conversation_id"] as? String,
                      let status = payload["status"] as? String,
                      let rawClosed = payload["provider_closed"] as? NSNumber, CFGetTypeID(rawClosed) == CFBooleanGetTypeID() else { return }
                let closed = rawClosed.boolValue
                events(.conversationFlushed(sessionID: id, conversationID: chat, status: status, providerClosed: closed))
            case "flush": events(.flush)
            case "session_end":
                events(.sessionEnd(payload["reason"] as? String ?? ""))
            case "notice":
                events(.notice(kind: payload["kind"] as? String ?? ""))
            case "session_challenge":
                // Der Core fragt, ob diese Sitzung wirklich von dieser App auf
                // diesem Geraet kommt. Antwortet die App nicht, laeuft das
                // Gespraech ganz normal weiter — nur eben mit der
                // vorsichtigeren Freigabezeile, also so wie bisher.
                if let core = payload["core_instance_id"] as? String,
                   let nonce = payload["session_nonce"] as? String {
                    Task { await self.answerSessionChallenge(core: core, nonce: nonce) }
                }
            default: break          // unbekannte Typen sind kein Fehler
            }
        @unknown default:
            break
        }
    }

    /// Beantwortet die Sitzungsfrage des Cores mit einer frischen
    /// App-Attest-Assertion.
    ///
    /// Sie belegt die App-Instanz, nicht den Menschen — Face ID bleibt Face ID.
    /// Scheitert hier irgendetwas, wird geschwiegen: der Core wartet nur kurz
    /// und faehrt ohne den Beweis fort. Ein Gespraech an einem fehlenden
    /// Schluessel scheitern zu lassen, waere die schlechtere Antwort.
    ///
    /// C3: ist ein Chat verlangt, geht seine Kennung in die Bindung UND als
    /// `conversation_id` in die Antwort — der Core prueft beides gegeneinander
    /// und gegen die Eigentuemerschaft, bevor irgendetwas dort landet.
    func answerSessionChallenge(core: String, nonce: String) async {
        do {
            let binding = VoiceSessionBinding(coreInstanceID: core,
                                              deviceID: deviceID,
                                              sessionNonce: nonce,
                                              conversationID: conversationID)
            let assertion = try await signer(binding.clientDataHash())
            var control = ["type": "session_assertion",
                           "session_nonce": nonce,
                           "assertion": assertion.base64EncodedString()]
            if let conversationID { control["conversation_id"] = conversationID }
            send(control: control)
        } catch {
            // Kein Beweis ist kein Fehler, sondern die vorsichtigere Zeile.
        }
    }

    /// Ein HTTP-Status vor dem Protokollwechsel sagt genau, warum es nicht ging.
    /// Ihn zu unterscheiden ist der Unterschied zwischen „gleich nochmal" und
    /// „das wird nie klappen".
    private func classify(_ error: Error) -> VoiceCloseReason {
        let status = (task?.response as? HTTPURLResponse)?.statusCode
        let code = task.map { $0.closeCode.rawValue }.flatMap { $0 == 0 ? nil : $0 }
        let reason = task?.closeReason.flatMap { String(data: $0, encoding: .utf8) } ?? ""
        return VoiceCloseReason.classify(httpStatus: status, closeCode: code, closeReason: reason)
    }

    private func fail(_ reason: VoiceCloseReason) {
        guard !closed else { return }
        closed = true
        task?.cancel(with: .abnormalClosure, reason: nil)
        task = nil
        session.invalidateAndCancel()
        events(.closed(reason: reason))
    }
}
