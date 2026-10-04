// Das Gespraech, wie der Bildschirm es sieht.
//
// Jeder Zustand hier kommt aus einem ECHTEN Ereignis — Verbindung, Mikrofon,
// abgespieltes Audio, eine Nachricht des Cores. Es gibt keinen Zeitgeber, der
// einen Zustand vortaeuscht, und keinen Zustand, den die App sich ausdenkt.
// Wo die App etwas selbst beobachtet (dass gerade jemand spricht), ist das
// ausdruecklich eine Beobachtung und keine Entscheidung: ob wirklich
// unterbrochen wurde, entscheidet der Core, und seine Antwort ist `flush`.
//
// Die Namen sind die des Entwurfs. Fachbegriffe des Cores — Provider, Session,
// Tool, Hermes — erscheinen hier nicht und kommen nie in die Anzeige.
import Foundation
import SolvioApprovalsKit
import SwiftUI
import UIKit

enum VoiceState: Equatable {
    case ready
    case listening
    case thinking
    case speaking
    case deepWork
    case reconnecting
    case offline(String)
    case ended
}

@MainActor
final class VoiceSession: ObservableObject {

    @Published private(set) var state: VoiceState = .ready
    @Published private(set) var isActive = false
    @Published private(set) var handoff = VoiceHandoff()
    @Published private(set) var handoffUnsupported = false
    private var beginGeneration: UUID?
    private var finalizationTask: Task<Void, Never>?
    private let requestPermission: () async -> Bool
    private let connectTransport: (VoiceTransport) -> Void
    private let startAudio: () throws -> Void
    private let beginBackgroundTask: (@escaping @Sendable () -> Void) -> UIBackgroundTaskIdentifier
    private let endBackgroundTask: (UIBackgroundTaskIdentifier) -> Void
    private var closingBackgroundTask = UIBackgroundTaskIdentifier.invalid
    private var closingBackgroundGeneration: UUID?
    @Published private(set) var micActive = false
    @Published var muted = false {
        didSet {
            micActive = running && !muted
            // Stumm heisst auch fuer die Praesenz stumm: der Pegel faellt
            // sofort auf null, statt den letzten Laut stehen zu lassen.
            audio.micMutedForLevel = muted
            audio.captureMuted = muted
            if muted { audioLevel = 0 }
        }
    }
    /// Nur fuer die Abnahme sichtbar: die zuletzt gemessene Zeit von
    /// „der Mensch faengt an zu reden" bis „der Lautsprecher ist still".
    /// Der Mensch hat ausdruecklich Schluss gesagt („SOLVIO, stopp"). Dann
    /// geht der Sprachraum SOFORT zu — ohne Abschiedssatz auf dem Bildschirm
    /// und ohne ein gesprochenes Wort. Wer „stopp" sagt, will nicht noch eine
    /// Antwort darauf.
    @Published private(set) var dismissNow = false
    @Published private(set) var lastBargeInMs: Int?
    @Published private(set) var bargeInCount = 0
    /// Die eine Zahl der Praesenz: wie laut es JETZT ist, 0…1. Fluechtig —
    /// sie wird ueberschrieben, nie gesammelt, nie geloggt.
    @Published private(set) var audioLevel: Double = 0

    private let audio: VoiceAudio
    private let levelRelay = LevelRelay()
    private var transport: VoiceTransport?
    private var pairing: PairingPayload?
    private var deviceID = ""
    private var transportCred = ""
    /// C3: der gebundene Chat dieser Sitzung; bleibt ueber Wiederverbindungen
    /// gleich. Nil = ungebunden, wie bisher.
    private(set) var conversationID: String?
    private var running = false
    private var starting = false
    private var attempts = 0
    private var reconnectTask: Task<Void, Never>?
    private var settleTask: Task<Void, Never>?
    private var micWatch: Task<Void, Never>?
    private var connectionID: UUID?

    init(audio: VoiceAudio = VoiceAudio(), requestPermission: @escaping () async -> Bool = { await VoiceAudio.requestPermission() },
         connectTransport: @escaping (VoiceTransport) -> Void = { $0.connect() },
         startAudio: (() throws -> Void)? = nil,
         beginBackgroundTask: @escaping (@escaping @Sendable () -> Void) -> UIBackgroundTaskIdentifier = {
             UIApplication.shared.beginBackgroundTask(withName: "SOLVIO Sprachabschluss", expirationHandler: $0)
         },
         endBackgroundTask: @escaping (UIBackgroundTaskIdentifier) -> Void = { UIApplication.shared.endBackgroundTask($0) }) {
        self.audio = audio; self.requestPermission = requestPermission
        self.connectTransport = connectTransport
        self.startAudio = startAudio ?? { try audio.start() }
        self.beginBackgroundTask = beginBackgroundTask
        self.endBackgroundTask = endBackgroundTask
    }

    var hasVisibleFailure: Bool {
        if case .offline = state { return true }
        return handoff.state == .unknown
    }
    var chatStatus: String {
        if handoff.state == .ending { return "Sprachverlauf wird gespeichert …" }
        if handoff.state == .unknown { return "Sprachabschluss nicht bestätigt. Textentwurf bleibt erhalten." }
        switch state {
        case .ready: return "Sprache wird verbunden …"
        case .listening: return muted ? "Stummgeschaltet" : "SOLVIO hört zu"
        case .thinking: return "SOLVIO denkt nach"
        case .speaking: return "SOLVIO spricht"
        case .deepWork: return "SOLVIO arbeitet am Auftrag"
        case .reconnecting: return "Verbindung wird wiederhergestellt …"
        case .offline(let message): return message
        case .ended: return "Sprache beendet"
        }
    }

    func finishForText(conversationID: String? = nil) async throws {
        if isActive { end(reason: "text_handoff") }
        while handoff.state == .ending {
            try await Task.sleep(nanoseconds: 50_000_000)
        }
        if let conversationID {
            guard !handoff.blocksText(in: conversationID) else { throw VoiceHandoffError() }
        } else if handoff.blocksText { throw VoiceHandoffError() }
    }

    /// Explicit recovery reads the bound chat; it does not manufacture a flush receipt.
    func useStoredHistory(conversationID: String,
                          load: () async throws -> ConversationDetail) async throws {
        guard handoff.hasUncertainty(in: conversationID) else { throw VoiceHandoffError() }
        let snapshot = handoff.sessionID
        let detail = try await load()
        guard !Task.isCancelled, detail.conversation.conversation_id == conversationID,
              handoff.sessionID == snapshot, handoff.acceptStoredHistory(conversationID: conversationID) else {
            throw VoiceHandoffError()
        }
    }

    /// Hoechstens so oft neu verbinden, dann ist Schluss. Eine Schleife ohne
    /// Ende waere eine Batterieheizung und keine Erholung.
    private static let maxAttempts = 3

    // MARK: - Start und Ende

    func begin(pairing: PairingPayload, deviceID: String, transportCred: String,
               conversationID: String? = nil) async {
        // `running` wird erst NACH der Mikrofonfrage gesetzt, und die kann
        // Sekunden dauern. In dieser Luecke darf kein zweiter Anlauf durch —
        // sonst steht die zweite Verbindung vor einem besetzten Core.
        guard !running, !starting, handoff.state != .ending,
              !handoff.hasUncertainty(in: conversationID ?? "") else { return }
        let generation = UUID(); beginGeneration = generation
        starting = true; isActive = true; dismissNow = false
        defer { if beginGeneration == generation { starting = false } }
        self.pairing = pairing
        self.deviceID = deviceID
        self.transportCred = transportCred
        self.conversationID = (conversationID?.isEmpty == false) ? conversationID : nil
        handoffUnsupported = false
        attempts = 0
        state = .ready
        lastBargeInMs = nil
        bargeInCount = 0
        audioLevel = 0
        audio.voiceMode = .turnBased

        let permitted = await requestPermission()
        guard beginGeneration == generation, !Task.isCancelled else { return }
        guard permitted else {
            isActive = false
            state = .offline("SOLVIO braucht dein Mikrofon, um zuzuhören.")
            return
        }
        // An explicit new conversation starts listening after its permission
        // succeeds. Automatic reconnects use openConnection and keep mute.
        muted = false
        openConnection()
    }

    /// Ende — vom Menschen oder vom System. Danach ist alles frei: kein
    /// Mikrofon und Engine stoppen sofort. Der Socket wartet begrenzt auf den Verlauf.
    func end(reason: String = "user") {
        // Acquire the short iOS finishing allowance BEFORE stopping audio.
        // Deactivating audio can otherwise let iOS suspend the socket while
        // the Core still drains the final transcript. No recording continues.
        if transport != nil, handoff.state == .ready { protectClosingConnection() }
        beginGeneration = nil; starting = false; isActive = false
        reconnectTask?.cancel(); reconnectTask = nil
        stopAudio()
        guard transport != nil else {
            releaseClosingConnection()
            handoff.fail()
            if case .offline = state { return }
            state = .ended
            return
        }
        if handoff.state == .ending { return }
        handoff.ending()
        state = .ended
        guard handoff.state == .ending else { closeTransport(reason: reason); return }
        transport?.requestEnd(reason: reason)
        // Keep the bounded close owned even if navigation releases the view.
        finalizationTask = Task { [self] in
            do { try await Task.sleep(nanoseconds: 25_000_000_000) } catch { return }
            guard self.handoff.state == .ending else { return }
            self.handoff.fail(); self.closeTransport(reason: "handoff_timeout")
        }
    }

    private func stopAudio() {
        settleTask?.cancel(); settleTask = nil
        micWatch?.cancel(); micWatch = nil
        running = false; micActive = false; audioLevel = 0
        audio.stop()
    }

    private func closeTransport(reason: String) {
        connectionID = nil
        finalizationTask?.cancel(); finalizationTask = nil
        transport?.close(reason: reason); transport = nil
        releaseClosingConnection()
    }

    private func protectClosingConnection() {
        guard closingBackgroundGeneration == nil else { return }
        let generation = UUID()
        closingBackgroundGeneration = generation
        closingBackgroundTask = beginBackgroundTask { [weak self] in
            Task { @MainActor in
                guard let self, self.closingBackgroundGeneration == generation else { return }
                // Expiration is not confirmation; retain the existing warning.
                self.handoff.fail()
                self.stopAudio(); self.isActive = false; self.state = .ended
                self.closeTransport(reason: "handoff_background_expired")
            }
        }
    }

    private func releaseClosingConnection() {
        closingBackgroundGeneration = nil
        let task = closingBackgroundTask
        closingBackgroundTask = .invalid
        if task != .invalid { endBackgroundTask(task) }
    }

    private func openConnection() {
        guard let pairing else { return }
        let connectionID = UUID()
        self.connectionID = connectionID
        handoff.start(conversationID: conversationID)
        audio.voiceMode = .turnBased
        let transport = VoiceTransport(
            pairing: pairing, deviceID: deviceID, transportCred: transportCred,
            conversationID: conversationID
        ) { [weak self] event in
            Task { @MainActor in
                guard let self, self.connectionID == connectionID else { return }
                self.handle(event)
            }
        }
        self.transport = transport
        connectTransport(transport)

        // Die Senke steht VOR der Quelle. Andersherum bleibt ein Fenster
        // zwischen `start()` und dem Setzen von `onFrame`, in dem ein Rahmen
        // entsteht, gezaehlt wird und trotzdem nirgends ankommt — die Auskunft
        // waere dann um genau diesen Rahmen unwahr.
        audio.onDry = { [weak self] in
            Task { @MainActor in
                guard let self, self.running else { return }
                self.transport?.send(control: ["type": "underrun"])
            }
        }
        audio.onLocalSpeechEnd = { [weak self] in
            Task { @MainActor in
                guard let self, self.running, self.state == .listening else { return }
                // Eine echte Beobachtung des Geraets, kein Zeitgeber, der einen
                // Zustand vortaeuscht: das Mikrofon hat gehoert, dass der Satz
                // zu Ende ist. Ob die RUNDE zu Ende ist, entscheidet weiterhin
                // der Core — und sein erstes Audio setzt den Zustand weiter.
                self.state = .thinking
            }
        }
        audio.onLocalBargeIn = { [weak self] heardMs in
            Task { @MainActor in
                guard let self, self.running else { return }
                // Der Lautsprecher IST schon still. Hier wird nur noch gesagt,
                // dass es passiert ist und wie weit SOLVIO gekommen war — die
                // Entscheidung, was das fuer die Runde heisst, faellt im Core.
                self.transport?.send(control: ["type": "barge_in",
                                               "played_ms": String(heardMs)])
                if self.state == .speaking { self.state = .listening }
                if let onset = self.audio.consumeOnset() {
                    self.lastBargeInMs = onset
                    self.bargeInCount += 1
                }
            }
        }
        audio.onFrame = { [weak self] frame in
            guard let self else { return }
            Task { @MainActor in
                guard self.running, self.connectionID == connectionID, !self.muted else { return }
                self.transport?.send(audio: frame)
            }
        }
        // Vom Audiothread auf den Hauptthread, aber ohne Rueckstau: haengt
        // schon ein Sprung in der Luft, wird nur der Wert ueberschrieben.
        let relay = levelRelay
        audio.onLevel = { [weak self] level in
            guard relay.offer(level) else { return }
            Task { @MainActor [weak self] in
                let value = Double(relay.take())
                guard let self, self.running else { return }
                self.audioLevel = value
            }
        }
        audio.micMutedForLevel = muted
        do {
            try startAudio()
        } catch {
            self.connectionID = nil
            audio.stop()
            audio.onFrame = nil
            audio.onDry = nil
            audio.onLevel = nil
            isActive = false; handoff.fail()
            state = .offline(error.localizedDescription)
            transport.close(reason: "audio")
            self.transport = nil
            return
        }
        running = true
        micActive = !muted
        // Der Core oeffnet die Anbietersitzung, sobald das hier ankommt; bis
        // `session_ready` zurueckkommt, wird schon gesendet und dort gepuffert.
        transport.send(control: ["type": "session_start"])
    }

    // MARK: - Ereignisse

    func handle(_ event: VoiceWireEvent) {
        switch event {
        case let .handoffReady(sessionID, conversationID, version):
            handoff.bind(sessionID: sessionID, conversationID: conversationID, version: version)
            handoffUnsupported = handoff.state != .ready

        case let .conversationFlushed(sessionID, conversationID, status, providerClosed):
            guard handoff.accept(sessionID: sessionID, conversationID: conversationID,
                                 status: status, providerClosed: providerClosed) else { return }
            beginGeneration = nil; starting = false; isActive = false
            stopAudio(); state = .ended
            closeTransport(reason: "handoff_complete")

        case let .sessionReady(mode):
            guard handoff.state != .ending, handoff.state != .unknown else { return }
            audio.voiceMode = mode
            attempts = 0
            state = .listening
            watchMicrophone()

        case let .audio(pcm):
            guard handoff.state != .ending, handoff.state != .unknown else { return }
            audio.play(pcm)
            if state != .speaking { state = .speaking }
            scheduleSettle()

        case .flush:
            // Der Core hat unterbrochen — entweder weil der Mensch dazwischen
            // gesprochen hat, oder weil die Sitzung endet. Beides heisst hier
            // dasselbe: sofort still.
            let wasSpeaking = (state == .speaking)
            let heard = audio.heardSoFar()
            if wasSpeaking, heard >= 0 {
                // Auch wenn der ANBIETER die Unterbrechung zuerst bemerkt hat,
                // muss das Modell erfahren, wie viel wirklich zu hoeren war.
                // Sonst glaubt es weiter, Saetze gesagt zu haben, die nie
                // erklungen sind — und antwortet beim naechsten Mal darauf.
                // `heard`, nicht `barge_in`: unterbrochen hat hier der Core.
                // Das Geraet reicht nur die Wahrheit nach, wie viel davon
                // wirklich zu hoeren war.
                transport?.send(control: ["type": "heard",
                                          "played_ms": String(heard)])
            }
            if let ms = audio.flush(), wasSpeaking {
                lastBargeInMs = ms
                bargeInCount += 1
                NSLog("SOLVIO barge-in: Lautsprecher nach \(ms) ms still")
            }
            if wasSpeaking { state = .listening }

        case let .sessionEnd(reason):
            end(reason: reason.isEmpty ? "core" : reason)
            // Der Core hat den Stopp-Satz erkannt, die laufende Antwort
            // abgebrochen und das Telefon geleert — der Stopp-Satz wird nicht
            // einmal Teil des Gespraechs. Hier bleibt nur, den Raum zu
            // schliessen.
            if reason == "silent_stop" { dismissNow = true }

        case let .notice(kind):
            // Der Core sagt, dass eine lange Aufgabe laeuft. Der Satz dazu
            // steht in der Ansicht; hier wird nur der Zustand gesetzt.
            if kind == "deep_work" { state = .deepWork }

        case let .closed(reason):
            handleClose(reason)
        }
    }

    /// Was das Telefon ueber sich selbst sagen kann — Zahlen, nie Ton.
    ///
    /// `bi` ist die Abnahme des Dazwischenredens: wie oft, und wie viele
    /// Millisekunden vom Beginn des Sprechens bis der Lautsprecher still war.
    /// Ohne diese Zeile waere „Barge-in funktioniert" eine Behauptung.
    private var report: String {
        "mute=\(muted ? 1 : 0) bi=\(bargeInCount)/\(lastBargeInMs ?? -1) " + audio.note
    }

    /// Ein stummes Mikrofon darf nicht wie Zuhoeren aussehen.
    ///
    /// Bisher blieb der Bildschirm bei „Hoert zu", waehrend gar nichts hinausging,
    /// und der Core legte nach 30 Sekunden von selbst auf. Das ist die
    /// schlechteste Art zu scheitern: sie sieht aus wie Erfolg. Zwei Sekunden
    /// nach `session_ready` steht fest, ob das Mikrofon liefert — bis dahin ist
    /// die Engine sicher angelaufen, und es sind noch keine 30 vergangen.
    ///
    /// Die Zahlen gehen als `hello` an den Core. Das ist die Nachricht, die das
    /// Protokoll fuer „so bin ich" schon hat; sie enthaelt Zaehler, nie Ton.
    private func watchMicrophone() {
        micWatch?.cancel()
        micWatch = Task { [weak self] in
            // Erst kurz nachsehen, ob die Aufnahme ueberhaupt angelaufen ist,
            // und notfalls einmal nachfassen — lange bevor der Mensch etwas
            // merkt.
            try? await Task.sleep(nanoseconds: 700_000_000)
            guard let self, !Task.isCancelled, self.running else { return }
            self.audio.rescueInput()

            try? await Task.sleep(nanoseconds: 1_800_000_000)
            guard !Task.isCancelled, self.running else { return }
            self.transport?.send(control: ["type": "hello", "info": self.report])
            guard !self.audio.isProducing, !self.muted else {
                // Laeuft es, wird von hier an alle fuenf Sekunden gemeldet, wie
                // die Wiedergabe steht. Zahlen, nie Ton.
                while !Task.isCancelled, self.running {
                    try? await Task.sleep(nanoseconds: 5_000_000_000)
                    guard !Task.isCancelled, self.running else { return }
                    self.transport?.send(control: ["type": "hello", "info": self.report])
                }
                return
            }
            self.state = .offline("Dein iPhone nimmt gerade keinen Ton auf. Prüf, ob eine andere App das Mikrofon belegt.")
            self.running = false; self.isActive = false; self.handoff.fail()
            self.audio.stop()
            self.micActive = false
            self.audioLevel = 0
            // Erst hinausschicken lassen, dann aufloesen. Wer die Verbindung
            // im selben Atemzug abbricht, verwirft die eigene Meldung —
            // `send` ist nicht fertig, wenn es zurueckkehrt.
            try? await Task.sleep(nanoseconds: 400_000_000)
            self.transport?.close(reason: "no_audio")
            self.transport = nil
        }
    }

    /// Nach dem letzten Audio-Rahmen ist der Satz noch nicht zu Ende gespielt.
    /// Erst wenn eine kurze Weile nichts Neues kam, ist wieder Zuhoeren dran.
    private func scheduleSettle() {
        settleTask?.cancel()
        settleTask = Task { [weak self] in
            try? await Task.sleep(nanoseconds: 700_000_000)
            guard let self, !Task.isCancelled else { return }
            await MainActor.run {
                if self.state == .speaking { self.state = .listening }
            }
        }
    }

    private func handleClose(_ reason: VoiceCloseReason) {
        if handoff.state == .ending {
            handoff.fail(); stopAudio(); isActive = false
            closeTransport(reason: "handoff_unconfirmed")
            return
        }
        connectionID = nil
        audio.stop()
        micActive = false
        audioLevel = 0
        transport = nil

        switch reason {
        case .busy:
            running = false; isActive = false; handoff.fail()
            state = .offline("SOLVIO spricht gerade im Wohnzimmer.")
        case .unauthorized:
            running = false; isActive = false; handoff.fail()
            state = .offline("Dieses iPhone ist nicht mehr gekoppelt.")
        case .conversationNotBindable:
            // Der Chat war verlangt und ist nicht bindbar. Kein Ersatz ohne
            // Chat — sonst landete das Gespraech irgendwo, nur nicht dort,
            // wo der Mensch es sehen wollte.
            running = false; isActive = false; handoff.fail()
            state = .offline("Dieser Chat ist für Sprache nicht verfügbar.")
        case let .ended(why):
            running = false; isActive = false; handoff.fail()
            state = why == "user" ? .ended : .ended
        case .transport:
            handoff.fail()
            guard running, attempts < Self.maxAttempts else {
                running = false; isActive = false; handoff.fail()
                state = .offline("SOLVIO ist gerade nicht erreichbar.")
                return
            }
            attempts += 1
            state = .reconnecting
            let delay = UInt64(pow(2.0, Double(attempts)) * 400_000_000)   // 0,8 / 1,6 / 3,2 s
            reconnectTask?.cancel()
            reconnectTask = Task { [weak self] in
                try? await Task.sleep(nanoseconds: delay)
                guard let self, !Task.isCancelled else { return }
                await MainActor.run {
                    guard self.running else { return }
                    self.running = false          // openConnection setzt es neu
                    self.openConnection()
                }
            }
        }
    }

    /// Ein neuer Anlauf nach „nicht erreichbar" — vom Menschen ausgeloest.
    func retry() async {
        guard let pairing else { return }
        end(reason: "retry")
        await begin(pairing: pairing, deviceID: deviceID, transportCred: transportCred,
                    conversationID: conversationID)
    }

    // MARK: - Nachrichten des Cores, die kein Audio sind

}

/// Traegt den Pegel vom Audiothread zum Hauptthread — genau EIN Wert und
/// hoechstens EIN anstehender Sprung. Kommt schneller Nachschub, wird der Wert
/// ueberschrieben statt aufgereiht: die Praesenz will wissen, wie laut es IST,
/// nicht, wie laut es der Reihe nach war.
private final class LevelRelay: @unchecked Sendable {
    private let lock = NSLock()
    private var value: Float = 0
    private var pending = false

    /// Legt den Wert ab. `true` heisst: es haengt noch kein Sprung in der
    /// Luft — der Rufer soll einen anstossen.
    func offer(_ new: Float) -> Bool {
        lock.lock(); defer { lock.unlock() }
        value = new
        if pending { return false }
        pending = true
        return true
    }

    func take() -> Float {
        lock.lock(); defer { lock.unlock() }
        pending = false
        return value
    }
}
