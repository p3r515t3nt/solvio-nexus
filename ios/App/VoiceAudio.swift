// Mikrofon und Lautsprecher — die Hardwareseite des Gespraechs.
//
// Vier Dinge sind hier nicht verhandelbar, und jedes hat einen Grund:
//
// **Das Mikrofon laeuft nur, wenn der Mensch es gestartet hat.** Es gibt keinen
// Weckwortpfad auf dem Telefon, keine Hintergrund-Berechtigung und keinen
// Zustand, in dem die Engine ohne sichtbares Gespraech laeuft. `stop()` raeumt
// die Engine ab und gibt die Audiositzung frei — auch beim Fehler, auch beim
// Wechsel in den Hintergrund.
//
// **Es wird nichts aufgezeichnet.** Kein Puffer ueberlebt den Aufruf, in dem er
// entsteht; es gibt keine Datei, keinen Ringpuffer ueber Sekunden, kein Debug-
// Mitschneiden. Was das Mikrofon hoert, wird umgerechnet, abgeschickt und
// vergessen.
//
// **Echo muss weg.** Ohne Echokompensation hoert SOLVIO sich selbst, und die
// Spracherkennung des Anbieters haelt das fuer eine Unterbrechung — das Gespraech
// wuerde sich selbst zerreden. `setVoiceProcessingEnabled` schaltet die
// Sprachverarbeitung der Hardware ein; das ist derselbe Pfad, den Telefonie-Apps
// benutzen.
//
// **Alte Stimme darf die neue Frage nie ueberreden.** Beim `flush` wird die
// Warteschlange sofort verworfen und der Spieler angehalten — nicht ausgeblendet,
// nicht zu Ende gespielt.
import AVFoundation
import Foundation

/// Das Format, das der Core kennt: PCM16, 16 kHz, mono.
/// Gegenstueck zu `SATELLITE_RATE = 16000` in `realtime/voice_session.py`.
enum VoiceFormat {
    static let sampleRate: Double = 16_000
    static let channels: AVAudioChannelCount = 1
}

enum VoiceAudioError: Error, LocalizedError {
    case microphoneDenied
    case engineUnavailable

    var errorDescription: String? {
        switch self {
        case .microphoneDenied: return "SOLVIO braucht dein Mikrofon, um zuzuhören."
        case .engineUnavailable: return "Das Mikrofon ist gerade nicht verfügbar."
        }
    }
}

/// Nimmt auf, spielt ab, und haelt sonst nichts.
final class VoiceAudio: @unchecked Sendable {

    /// Wie viel unabgespieltes Audio hoechstens in der Warteschlange stehen darf.
    ///
    /// Zehn Sekunden waren FALSCH, und zwar gemessen: `q=10000ms verw=112`.
    /// Die Annahme dahinter war „mehr als zehn Sekunden laeuft eine Antwort nie
    /// vor" — sie stimmt nicht. Der Anbieter erzeugt eine lange Antwort
    /// schneller als in Echtzeit, das Telefon spielt sie aber in Echtzeit ab.
    /// Bei einer Antwort von einer halben Minute standen nach wenigen Sekunden
    /// zehn Sekunden Vorlauf da, und ab da wurde JEDER weitere Schnipsel
    /// verworfen — mitten im Satz, und ohne dass irgendetwas es sagte.
    ///
    /// Ton mitten aus einer Antwort zu schneiden ist nie richtig. Die Grenze
    /// ist jetzt eine Speichergrenze und keine Verhaltensannahme: 60 Sekunden
    /// sind rund vier Megabyte und laenger, als SOLVIO am Stueck spricht.
    private static let maxQueuedSeconds: Double = 60

    /// Ab welcher Lautstaerke das Mikrofon ueberhaupt hinausgeht.
    ///
    /// Ein Satellit steht fest im Raum; ein Telefon liegt in der Hand, und um
    /// es herum laeuft ein Fernseher. Ohne Schwelle geht JEDES Raumgeraeusch an
    /// den Anbieter, und der haelt es fuer eine Unterbrechung — SOLVIO haelt
    /// dann bei jedem Rascheln an. Die Entscheidung, was eine Unterbrechung
    /// IST, faellt weiterhin der Core; hier wird nur entschieden, was
    /// ueberhaupt Mikrofon ist und was Zimmer.
    ///
    /// Waehrend SOLVIO spricht, liegt die Schwelle hoeher: dazwischenreden soll
    /// absichtlich sein, nicht versehentlich.
    /// Das Tor richtet sich nach dem RAUM, nicht nach einer Zahl im Quelltext.
    ///
    /// Eine feste Schwelle war von Anfang an falsch, und die Messung sagt es
    /// deutlich: gemessen wurde ein Grundgeraeusch von 2 (still), 13, 24 und 27
    /// — alles im selben Wohnzimmer, je nachdem, ob der Fernseher lief. Bei 24
    /// stand das feste Tor von 12 dauerhaft offen; es ging nie mehr zu, und
    /// damit fiel auch die Bedingung „der Mensch war einmal still" aus. Das
    /// schnelle Verstummen loeste gar nicht mehr aus.
    ///
    /// Der Grundpegel wird deshalb mitgefuehrt: schnell nach unten, damit eine
    /// Stille sofort zaehlt, sehr langsam nach oben, damit ein einzelner Satz
    /// ihn nicht mitzieht. Das Tor liegt ein Stueck darueber.
    /// Der Grundpegel ist der UEBLICHE leise Pegel — nicht der leiseste
    /// Moment und nicht der Mittelwert.
    ///
    /// Zwei Anlaeufe waren falsch, und beide aus demselben Grund. Erst wurde
    /// das MINIMUM verfolgt: in jeder Sprechpause faellt ein Puffer auf nahezu
    /// null, gemessen stand deshalb `boden=0`. Dann ein gleitendes Mittel,
    /// schnell nach unten und langsam nach oben — das klebt ebenfalls an der
    /// unteren Huellkurve, nur traeger, denn solche Pausen kommen alle paar
    /// Sekunden.
    ///
    /// Ein Perzentil kennt beide Fehler nicht: kurze Einbrueche verschieben es
    /// nicht, und Sprache ist der obere Rand der Verteilung, nicht ihr Koerper.
    /// Gerechnet wird in Dezibel, weil ein linearer RMS stark rechtsschief ist
    /// und eine Mittelung darauf den lauten Ausreissern folgt statt dem Raum.
    private static let floorWindow = 144             // rund 3 s bei 21 ms je Puffer
    /// Der obere Rand dessen, was ohne Sprache zu hoeren ist — nicht die
    /// Mitte. Gemessen wird ohnehin nur bei geschlossenem Tor, also ist die
    /// Verteilung schon die des Raums.
    private static let floorPercentile: Float = 0.75
    private static let floorEvery = 8                // nur jeden achten Puffer sortieren
    private static let speakOverFloor: Float = 3.5
    private static let bargeOverFloor: Float = 7.0
    /// Untergrenzen, damit ein totenstiller Raum nicht jedes Rascheln
    /// durchlaesst.
    private static let speakFloorMin: Float = 0.010
    private static let bargeFloorMin: Float = 0.022

    private var noiseFloor: Float = 0.010
    private var floorRing = [Float]()
    private var floorAt = 0
    private var floorTick = 0
    /// Wie lange nach dem letzten lauten Puffer noch weitergesendet wird, damit
    /// eine Atempause mitten im Satz das Tor nicht zuschlaegt.
    /// Wie lange nach dem letzten lauten Puffer noch weitergesendet wird.
    ///
    /// Das ist teurer, als es aussieht: solange das Tor offen ist, geht echter
    /// Raumton hinaus, und die Turn-Erkennung des Anbieters sieht das Ende des
    /// Satzes erst danach — ihre eigene Wartezeit faengt also 600 ms zu spaet
    /// an. Es war der groesste Posten, den SOLVIO selbst in der Hand hat, und
    /// er lag vollstaendig VOR dem Nullpunkt der bisherigen Messung.
    ///
    /// 350 ms ueberbruecken eine Atempause mitten im Satz weiterhin.
    private static let gateHold: CFAbsoluteTime = 0.35

    private var gateOpen = false
    private var gateOpens = 0
    private var lastLoud: CFAbsoluteTime = 0
    private var loudestClosed: Float = 0

    /// Wie viel Ton dastehen muss, bevor der Spieler anfaengt.
    ///
    /// Ohne diesen Vorlauf spielt das Telefon jeden Schnipsel sofort und laeuft
    /// bei jedem WLAN-Ruckler leer — es hoert mitten im Satz auf und faengt
    /// wieder an. 250 ms sind kaum zu bemerken und ueberbruecken die
    /// Schwankung, mit der Audio ueber ein Heimnetz eintrifft.
    private static let primeSeconds: Double = 0.12
    /// Laeuft der Spieler gerade? Nach jedem Leerlaufen wird neu vorgelegt.
    private var primed = false

    private let engine = AVAudioEngine()
    private let player = AVAudioPlayerNode()
    private var capture: AVAudioConverter?
    /// Das Format, in dem der Spieler am Mischer haengt: dieselbe Rate wie der
    /// Draht, nur als Float. Damit ist auf dem Wiedergabeweg KEINE
    /// Ratenwandlung mehr noetig — die macht der Mischer, durchgehend und in
    /// einem Stueck. Vorher wurde jeder ankommende Schnipsel EINZELN
    /// hochgerechnet, und jede Schnipselgrenze war ein Bruch: genau das klang
    /// ruckelig.
    private var playWire: AVAudioFormat?
    private var captureFormat: AVAudioFormat?
    private var captureSource: AVAudioFormat?
    private var frames = 0
    /// Wie oft der Abgriff ueberhaupt gefeuert hat, und wo die Rahmen sonst
    /// hingehen. Ohne diese vier Zahlen ist „das Mikrofon liefert nichts" eine
    /// Vermutung; mit ihnen ist es eine Auskunft.
    private(set) var taps = 0
    private var convErrors = 0
    private var emptyOut = 0
    private var engineNote = ""
    private var restarts = 0
    /// Wiedergabe-Zaehler. Dieselbe Begruendung wie bei der Aufnahme: ohne
    /// Zahlen ist „es ruckelt" eine Vermutung.
    private var scheduled = 0
    private var dropped = 0
    private var dryRuns = 0
    /// Der Core will wissen, wenn die Wiedergabe leergelaufen ist — dafuer hat
    /// das Protokoll die Nachricht `underrun`, und der Satellit schickt sie
    /// genauso.
    var onDry: (() -> Void)?
    /// Der Mensch hat angefangen zu reden, waehrend SOLVIO sprach — und der
    /// Lautsprecher ist deshalb SCHON still. Mitgegeben wird, wie viele
    /// Millisekunden dieser Antwort tatsaechlich zu hoeren waren.
    ///
    /// Das ist eine Beobachtung, keine Entscheidung: ob die Runde vorbei ist,
    /// entscheidet der Core. Aber still werden darf das Geraet sofort, und das
    /// ist der ganze Unterschied zwischen „Maschine" und „Mensch": gemessen
    /// vergingen 162 bis 752 ms, bis die Erkennung des Anbieters griff — so
    /// lange redete SOLVIO weiter, obwohl laengst jemand sprach.
    var onLocalBargeIn: ((Int) -> Void)?
    /// Der Mensch hat aufgehoert zu reden — akustisch beobachtet, hier auf dem
    /// Geraet. Daraus wird das sichtbare Quittieren: zwischen „ich bin fertig"
    /// und „der erste Ton kommt" passierte auf dem Bildschirm bisher NICHTS,
    /// und genau diese Stille wird als Sekunden erlebt. Der Zustand
    /// „Denkt nach" war im Entwurf vorhanden und wurde von keiner Stelle je
    /// gesetzt.
    var onLocalSpeechEnd: (() -> Void)?
    /// Wie viele laute Puffer hintereinander noetig sind. Einer allein waere
    /// ein Klicken, ein Stuhl, ein Huesteln.
    /// Wie lange am Stueck geredet werden muss, damit es eine Unterbrechung ist.
    ///
    /// In ZEIT, nicht in Puffern. Der erste Anlauf zaehlte Puffer und ging von
    /// 21 ms je Puffer aus — iOS liefert hier aber rund 100 ms (nachgerechnet
    /// aus der Telemetrie: rund zehn Rahmen je Sekunde). „Zehn Puffer, also
    /// 210 ms" war in Wirklichkeit eine ganze Sekunde. Wer in Puffern rechnet,
    /// rechnet mit einer Zahl, die das Betriebssystem waehlt.
    ///
    /// A sustained interruption threshold reduces accidental triggers from background speech.
    private static let loudSecondsToInterrupt: CFAbsoluteTime = 0.35
    /// Wie lange SOLVIO mindestens zu hoeren gewesen sein muss, bevor er
    /// unterbrochen werden kann.
    ///
    /// Gemeldet und im Log belegt (`played_ms=0`): er fing an, sagte ein Wort
    /// und brach ab. Die Unterbrechung feuerte, bevor ueberhaupt etwas zu
    /// hoeren war — denn „spricht" gilt schon, sobald der erste Ton in der
    /// Warteschlange liegt, und in diesem Moment redet der Mensch seinen
    /// eigenen Satz noch zu Ende. Seine letzten Silben loesten die
    /// Unterbrechung aus.
    ///
    /// Ein Mensch faellt niemandem ins Wort, bevor der angefangen hat.
    private static let minHeardBeforeInterrupt = 700

    // Die Schonfrist am Antwortanfang stand einmal hier und steht jetzt im
    // Core: dort gilt sie fuer BEIDE Endpunkte. Der Satellit hatte sie nicht,
    // und genau dort trat der Fehler zuletzt auf.
    /// Wann der laufende laute Abschnitt begonnen hat.
    private var loudSince: CFAbsoluteTime = 0
    private var interruptedThisRun = false
    /// Ob der Mensch seit dem Beginn dieser Antwort einmal still war.
    private var gateClosedSinceUtterance = false
    /// Was von der laufenden Antwort schon abgespielt wurde.
    private var utteranceScheduled: Double = 0
    private var observers: [NSObjectProtocol] = []
    private var rebuilds = 0
    private var formatChanges = 0
    private var running = false

    /// Mikrofonrahmen, fertig fuer den Draht: PCM16, 16 kHz, mono.
    var onFrame: ((Data) -> Void)?
    /// Eine fluechtige Huellkurve (0…1) fuer die Praesenz — mal Mikrofon, mal
    /// Wiedergabe, je nachdem, was gerade zu hoeren ist.
    ///
    /// Der Meilenstein verbietet ausdruecklich, Lautstaerken aufzubewahren
    /// oder zu loggen: es gibt EINEN Wert, und der wird ueberschrieben. Keine
    /// Verlaufsliste, keine Datei, kein Logeintrag — die Huellkurve sagt, wie
    /// laut es JETZT ist, und mehr darf sie nicht wissen.
    ///
    /// Gerufen vom Audio- oder Wiedergabethread, hoechstens rund 30-mal pro
    /// Sekunde — wer auf den Hauptthread will, springt selbst.
    var onLevel: ((Float) -> Void)?
    private let levelLock = NSLock()
    private var envelope: Float = 0
    private var envelopeAt: CFAbsoluteTime = 0
    private var levelEmittedAt: CFAbsoluteTime = 0
    /// Ob das Mikrofon fuer die ANZEIGE stumm ist. Entschieden wird das in der
    /// Sitzung; hier wird nur verhindert, dass ein stummes Mikrofon trotzdem
    /// eine Huellkurve malt. Aufnahme und Tor bleiben davon unberuehrt.
    private var _micMutedForLevel = false
    /// Stumm heisst STUMM — auch fuer das Tor. Wer sein Mikrofon abgeschaltet
    /// hat und trotzdem redet (Telefonat, Gespraech im Raum), darf SOLVIO
    /// damit weder unterbrechen noch einen Satzschluss vortaeuschen. Ohne
    /// diesen Schalter lief die Unterbrechungserkennung stumm weiter — die
    /// Frage des Nutzers („beeinflusst Stumm das Gespraech?") haette sonst
    /// ehrlich mit Ja beantwortet werden muessen.
    private var _captureMuted = false
    private var _voiceMode: VoiceMode = .turnBased
    /// Selected only by this connection's Core-ready event, never by RMS.
    /// The audio and main threads share the existing short state lock.
    var voiceMode: VoiceMode {
        get { levelLock.lock(); defer { levelLock.unlock() }; return _voiceMode }
        set { levelLock.lock(); _voiceMode = newValue; levelLock.unlock() }
    }
    var captureMuted: Bool {
        get { levelLock.lock(); defer { levelLock.unlock() }; return _captureMuted }
        set { levelLock.lock(); _captureMuted = newValue; levelLock.unlock() }
    }
    var micMutedForLevel: Bool {
        get { levelLock.lock(); defer { levelLock.unlock() }; return _micMutedForLevel }
        set { levelLock.lock(); _micMutedForLevel = newValue; levelLock.unlock() }
    }
    /// Unter -50 dB ist fuer die Anzeige Stille; das weiche Knie liefert der
    /// Smoothstep, damit der Rand nicht flackert. Anstieg schnell (50 ms),
    /// Abfall traege (250 ms): die Praesenz zuckt beim ersten Laut und
    /// verklingt, statt abzureissen.
    private static let levelFloorDB: Float = -50
    private static let levelAttack: CFAbsoluteTime = 0.05
    private static let levelRelease: CFAbsoluteTime = 0.25
    private static let levelEmitEvery: CFAbsoluteTime = 1.0 / 30.0
    /// Wann der Mensch zuletzt hoerbar angefangen hat zu sprechen. Grundlage
    /// der Barge-in-Messung — eine lokale Beobachtung, keine Entscheidung.
    /// Wann der Mensch zuletzt hoerbar angefangen hat zu sprechen. Geschrieben
    /// vom Audiothread, gelesen vom Hauptthread — deshalb unter dem Schloss.
    private var _lastSpeechOnset: CFAbsoluteTime?
    var lastSpeechOnset: CFAbsoluteTime? {
        queueLock.lock(); defer { queueLock.unlock() }
        return _lastSpeechOnset
    }
    /// Wie viele Sekunden noch in der Warteschlange stehen.
    private var queuedSeconds: Double = 0
    private let queueLock = NSLock()
    /// Alles, was den Spieler anfasst, laeuft HIER — nie im Mikrofon-Rueckruf.
    ///
    /// Der Rueckruf des Abgriffs laeuft auf dem Echtzeit-Audiothread.
    /// `player.stop()` wartet dort darauf, dass genau dieser Thread fertig
    /// wird, und die App geht weg. Gemessen: zwei Abstuerze, beide unmittelbar
    /// nach einer Unterbrechung.
    ///
    /// Der Sprung hierher kostet Bruchteile einer Millisekunde. Gegenueber den
    /// 162 bis 752 ms, die der Umweg ueber Mac und Anbieter gekostet hat, ist
    /// das nichts — und er ist der Unterschied zwischen „schnell" und „stuerzt
    /// ab".
    private let control = DispatchQueue(label: "de.solvio.voice.playback")

    /// Ob gerade abgespielt wird — fuer die Anzeige, nicht fuer die Logik.
    /// Geschrieben von der Wiedergabe, gelesen vom Audiothread.
    private var _isSpeaking = false
    var isSpeaking: Bool {
        queueLock.lock(); defer { queueLock.unlock() }
        return _isSpeaking
    }
    private var speakingNow: Bool {
        queueLock.lock(); defer { queueLock.unlock() }
        return _isSpeaking
    }

    // MARK: - Lebenszyklus

    static func requestPermission() async -> Bool {
        await withCheckedContinuation { continuation in
            if #available(iOS 17.0, *) {
                AVAudioApplication.requestRecordPermission { continuation.resume(returning: $0) }
            } else {
                AVAudioSession.sharedInstance().requestRecordPermission {
                    continuation.resume(returning: $0)
                }
            }
        }
    }

    func start() throws {
        guard !running else { return }
        voiceMode = .turnBased

        let session = AVAudioSession.sharedInstance()
        // Modus `.default`, NICHT `.voiceChat`. Zwei Gruende, und beide sind
        // gemessen und nicht geschmacklich:
        //
        // `.voiceChat` legt die Ausgabe auf den Hoerer am Ohr und ignoriert
        // dabei `.defaultToSpeaker` — ein Geraet, das freihaendig auf dem Tisch
        // liegen soll, waere damit kaum zu hoeren.
        //
        // Und `.voiceChat` bringt eine eigene Sprachverarbeitung mit, die sich
        // mit der der Engine ins Gehege kommt. Die Engine soll sie machen:
        // `setVoiceProcessingEnabled` unten ist derselbe Pfad, den Apples
        // eigenes Beispiel benutzt.
        try session.setCategory(.playAndRecord, mode: .default,
                                options: [.defaultToSpeaker, .allowBluetooth])
        try session.setActive(true, options: [])

        let input = engine.inputNode
        let output = engine.outputNode
        // Echokompensation. Schlaegt sie fehl, laeuft das Gespraech trotzdem —
        // aber es ist eine Auskunft wert, weil Barge-in dann unruhig wird.
        do {
            try input.setVoiceProcessingEnabled(true)
            try output.setVoiceProcessingEnabled(true)
        } catch {
            NSLog("SOLVIO voice: Sprachverarbeitung nicht verfügbar (\(error))")
        }

        guard let wire = AVAudioFormat(commonFormat: .pcmFormatInt16,
                                       sampleRate: VoiceFormat.sampleRate,
                                       channels: VoiceFormat.channels,
                                       interleaved: true) else {
            throw VoiceAudioError.engineUnavailable
        }
        captureFormat = wire

        guard let voice = AVAudioFormat(commonFormat: .pcmFormatFloat32,
                                        sampleRate: VoiceFormat.sampleRate,
                                        channels: VoiceFormat.channels,
                                        interleaved: false) else {
            throw VoiceAudioError.engineUnavailable
        }
        playWire = voice
        engine.attach(player)
        _ = engine.mainMixerNode          // verdrahtet Mischer -> Ausgang
        engine.connect(player, to: engine.mainMixerNode, format: voice)

        // Der Abgriff MUSS vor `start()` stehen.
        //
        // Gemessen: mit einem Abgriff, der erst nach `start()` installiert
        // wurde, meldete das Telefon `taps=0` — die Rueckmeldung feuerte kein
        // einziges Mal. Die Engine entscheidet beim Start, ob die Eingangsseite
        // ueberhaupt laeuft, und ohne Abnehmer im Graph laesst sie sie aus. Wer
        // danach kommt, kommt zu spaet, und es gibt keinen Fehler, der das
        // sagen wuerde.
        //
        // Das Format wird ERST HIER gelesen, nachdem der Mischer angefasst und
        // der Spieler verbunden ist: der Zugriff auf `mainMixerNode` verdrahtet
        // den Ausgang mit und kann das Eingangsformat dabei noch veraendern.
        // Vorher gelesen ist es unter Umstaenden veraltet — und ein Abgriff mit
        // veraltetem Format liefert ebenfalls still nichts.
        let hardware = input.outputFormat(forBus: 0)
        guard hardware.sampleRate > 0, hardware.channelCount > 0 else {
            throw VoiceAudioError.engineUnavailable
        }
        input.installTap(onBus: 0, bufferSize: 1024, format: hardware) { [weak self] buffer, _ in
            self?.handleCapture(buffer)
        }

        // Auf Veraenderungen hoeren, statt sie zu verschlafen.
        let center = NotificationCenter.default
        observers.append(center.addObserver(
            forName: .AVAudioEngineConfigurationChange, object: engine, queue: nil
        ) { [weak self] _ in
            guard let self else { return }
            self.rebuilds += 1
            self.rebuildInput()
        })
        observers.append(center.addObserver(
            forName: AVAudioSession.interruptionNotification,
            object: AVAudioSession.sharedInstance(), queue: nil
        ) { [weak self] note in
            guard let self,
                  let raw = note.userInfo?[AVAudioSessionInterruptionTypeKey] as? UInt,
                  AVAudioSession.InterruptionType(rawValue: raw) == .ended else { return }
            try? AVAudioSession.sharedInstance().setActive(true, options: [])
            self.rebuilds += 1
            self.rebuildInput()
        })

        engine.prepare()
        try engine.start()
        running = true
        let live = input.outputFormat(forBus: 0)
        engineNote = "in \(Int(live.sampleRate))/\(live.channelCount)ch"
    }

    /// Eine Zeile Wahrheit ueber die Aufnahme — kein Ton, nur Zahlen. Sie geht
    /// als `hello` an den Core, weil das die Nachricht ist, die das Protokoll
    /// fuer „so bin ich" schon hat.
    var note: String {
        {
            queueLock.lock(); let q = Int(queuedSeconds * 1000); queueLock.unlock()
            // Kurz halten: der Core schreibt von `hello` die ersten 80
            // Zeichen mit, und was hinten abfaellt, ist nicht da.
            return "tor=\(gateOpen ? 1 : 0)/\(gateOpens) still=\(Int(loudestClosed * 1000)) "
                 + "snd=\(frames) q=\(q) leer=\(dryRuns) "
                 + "boden=\(Int(20 * log10f(max(noiseFloor, 1e-6))))dB"
        }()
    }

    /// Einmal nachfassen, wenn die Eingangsseite nicht angelaufen ist.
    ///
    /// Gemessen: der ERSTE Versuch nach dem App-Start meldete `taps=0`, der
    /// zweite `taps=26` — bei identischem Code. Beim allerersten
    /// `setActive(true)` ist die Audiositzung noch dabei, den Weg zum Mikrofon
    /// einzurichten; die Engine startet dann ohne Eingangsseite und sagt es
    /// nicht. Ein einziger Neustart, wenn nach kurzer Zeit kein einziger
    /// Abgriff gefeuert hat, holt das nach. Genau einer — eine Schleife waere
    /// eine Batterieheizung.
    func rescueInput() {
        guard running, taps == 0, restarts == 0 else { return }
        restarts += 1
        rebuildInput()
    }

    /// Die Eingangsseite neu aufbauen. Wird gebraucht, wenn der Weg zum
    /// Mikrofon sich unter der laufenden Engine veraendert hat — ein Anruf, ein
    /// Kopfhoerer, eine andere App, die den Ton uebernimmt. Ohne das haelt die
    /// Engine an, der Abgriff ist weg, und der Bildschirm sagt weiter
    /// „Hoert zu": wieder ein stiller Tod, wieder mitten im Gespraech.
    private func rebuildInput() {
        guard running else { return }
        let input = engine.inputNode
        engine.stop()
        input.removeTap(onBus: 0)
        let hardware = input.outputFormat(forBus: 0)
        guard hardware.sampleRate > 0, hardware.channelCount > 0 else { return }
        input.installTap(onBus: 0, bufferSize: 1024, format: hardware) { [weak self] buffer, _ in
            self?.handleCapture(buffer)
        }
        engine.prepare()
        do { try engine.start() } catch {
            NSLog("SOLVIO voice: Neustart der Aufnahme fehlgeschlagen (\(error))")
        }
        control.async { [weak self] in
            guard let self, self.primed else { return }
            self.player.play()
        }
    }

    /// Hat das Mikrofon seit dem Start ueberhaupt etwas geliefert?
    var isProducing: Bool { frames > 0 }

    /// Alles abraeumen. Muss nach jedem Ende laufen — auch nach einem Fehler.
    func stop() {
        voiceMode = .turnBased
        guard running else { return }
        running = false
        observers.forEach(NotificationCenter.default.removeObserver)
        observers.removeAll()
        engine.inputNode.removeTap(onBus: 0)
        player.stop()
        engine.stop()
        engine.detach(player)
        capture = nil
        captureSource = nil
        playWire = nil
        captureFormat = nil
        primed = false
        frames = 0
        taps = 0
        convErrors = 0
        emptyOut = 0
        restarts = 0
        scheduled = 0
        dropped = 0
        dryRuns = 0
        rebuilds = 0
        formatChanges = 0
        gateOpen = false
        gateOpens = 0
        loudestClosed = 0
        loudSince = 0
        interruptedThisRun = false
        queueLock.lock()
        utteranceScheduled = 0
        _isSpeaking = false
        _lastSpeechOnset = nil
        queueLock.unlock()
        setQueued(0)
        resetLevel(tell: false)
        // Die Sitzung freigeben, damit andere Apps den Ton zurueckbekommen und
        // das Mikrofonsymbol im System verschwindet.
        try? AVAudioSession.sharedInstance().setActive(false, options: [.notifyOthersOnDeactivation])
    }

    // MARK: - Aufnahme

    private func handleCapture(_ buffer: AVAudioPCMBuffer) {
        taps += 1
        guard running, let wire = captureFormat else { return }
        // Der Umrechner entsteht aus dem Format, in dem die Rahmen TATSAECHLICH
        // ankommen — nicht aus einem, das vorher abgefragt wurde. Aendert sich
        // das Format mitten im Gespraech (Kopfhoerer rein, Anruf dazwischen),
        // wird er neu gebaut statt stumm zu werden.
        if capture == nil || captureSource != buffer.format {
            capture = AVAudioConverter(from: buffer.format, to: wire)
            captureSource = buffer.format
            formatChanges += 1
        }
        guard let capture else { return }
        let ratio = wire.sampleRate / buffer.format.sampleRate
        let capacity = AVAudioFrameCount(Double(buffer.frameLength) * ratio) + 64
        guard let out = AVAudioPCMBuffer(pcmFormat: wire, frameCapacity: capacity) else { return }

        var consumed = false
        var error: NSError?
        let outcome = capture.convert(to: out, error: &error) { _, status in
            if consumed { status.pointee = .noDataNow; return nil }
            consumed = true
            status.pointee = .haveData
            return buffer
        }
        if outcome == .error || error != nil {
            convErrors += 1
            // Den Umrechner fallenlassen; der naechste Puffer baut ihn neu.
            self.capture = nil
            self.captureSource = nil
            return
        }
        guard out.frameLength > 0, let channel = out.int16ChannelData else {
            emptyOut += 1
            return
        }

        let count = Int(out.frameLength)
        let level = rms(channel[0], frames: count)
        let byteCount = count * MemoryLayout<Int16>.size

        // Stumm: der Grundpegel darf weiter lernen, aber das Tor bleibt zu —
        // keine Unterbrechung, kein Satzschluss, kein Rahmen verlaesst das
        // Geraet (das Senden ist zusaetzlich in der Sitzung gebremst).
        if captureMuted {
            if !speakingNow { feedLevel(0, playback: false) }
            return
        }

        // Das Tor. Der Puffer, der es aufstoesst, geht selbst mit hinaus —
        // deshalb braucht es keinen Rueckblick und liegt nie Ton herum.
        let speaking = speakingNow
        // Die Huellkurve der Praesenz: waehrend SOLVIO spricht, gehoert sie
        // dem Lautsprecher — sonst malte das eigene Echo als Zuhoeren mit.
        if !speaking { feedLevel(level, playback: false) }
        // Den Grundpegel nachfuehren, BEVOR verglichen wird — aber NUR aus
        // Puffern, in denen gerade niemand redet.
        //
        // Ueber alle Puffer gerechnet fiel er auf -120 dB: mit
        // Echokompensation ist die Stille zwischen zwei Woertern wirklich fast
        // null, und ein niedriges Perzentil landet genau dort. Ein
        // Grundgeraeuschpegel ist aber das, was der RAUM macht, wenn niemand
        // spricht — nicht die Luecke mitten im Satz.
        if !gateOpen {
            let decibel = 20 * log10f(max(level, 1e-6))
            if floorRing.count < Self.floorWindow {
                floorRing.append(decibel)
            } else {
                floorRing[floorAt] = decibel
                floorAt = (floorAt + 1) % Self.floorWindow
            }
            floorTick += 1
            if floorTick % Self.floorEvery == 0, floorRing.count >= 24 {
                let sorted = floorRing.sorted()
                let usual = sorted[Int(Float(sorted.count) * Self.floorPercentile)]
                noiseFloor = powf(10, usual / 20)
            }
        }
        let threshold = speaking
            ? max(Self.bargeFloorMin, noiseFloor * Self.bargeOverFloor)
            : max(Self.speakFloorMin, noiseFloor * Self.speakOverFloor)
        let now = CFAbsoluteTimeGetCurrent()
        if level > threshold {
            if !gateOpen {
                gateOpen = true
                gateOpens += 1
                if speakingNow {
                    queueLock.lock(); _lastSpeechOnset = now; queueLock.unlock()
                }
            }
            if loudSince == 0 { loudSince = now }
            lastLoud = now
        } else {
            loudSince = 0
            interruptedThisRun = false
            if gateOpen, now - lastLoud > Self.gateHold {
                gateOpen = false
                gateClosedSinceUtterance = true
                if !speaking { onLocalSpeechEnd?() }
            } else if !gateOpen {
                loudestClosed = max(loudestClosed, level)
            }
        }

        // SOFORT still, sobald wirklich jemand redet — nicht erst, wenn zwei
        // Server sich einig sind. Der Core erfaehrt es in derselben Bewegung
        // und entscheidet dort, was es fuer das Gespraech bedeutet.
        if voiceMode != .fullDuplex, speaking, !interruptedThisRun, loudSince > 0,
           now - loudSince >= Self.loudSecondsToInterrupt {
            let heard = heardMilliseconds()
            // Zwei Bedingungen, und beide kommen aus demselben Vorfall:
            //
            // Es muss wirklich etwas zu hoeren gewesen sein. Sonst unterbricht
            // der Mensch sich selbst, waehrend SOLVIO noch vorlegt.
            //
            // Und das Tor muss seit dem Beginn dieser Antwort einmal ZU
            // gewesen sein. Sonst zaehlt der auslaufende eigene Satz als neuer
            // Anlauf — der Mensch hat gar nicht neu angesetzt, er war nur noch
            // nicht fertig.
            if heard >= Self.minHeardBeforeInterrupt, gateClosedSinceUtterance {
                interruptedThisRun = true
                // NICHT hier anhalten: das ist der Echtzeit-Audiothread.
                interruptAutomatically(heardMilliseconds: heard)
            }
        }

        frames += 1
        if gateOpen {
            // Eine Kopie, die genau so lange lebt wie der Versand. Kein Puffer,
            // der etwas aufbewahrt.
            onFrame?(Data(bytes: channel[0], count: byteCount))
        } else {
            // Stille statt gar nichts: der Strom darf nicht abreissen, sonst
            // haelt der Core ihn fuer eine tote Verbindung.
            onFrame?(Data(count: byteCount))
        }
    }

    /// Only the RMS path uses this conditional interruption. Explicit Core
    /// flush and stop retain their unconditional hardware cleanup paths.
    func interruptAutomatically(heardMilliseconds: Int) {
        guard voiceMode != .fullDuplex else { return }
        control.async { [weak self] in
            // Ready may have selected duplex while this work waited.
            guard let self, self.voiceMode != .fullDuplex else { return }
            self.silenceNow()
            self.onLocalBargeIn?(heardMilliseconds)
        }
    }

    /// Lautstaerke eines Puffers. Eine lokale Beobachtung, keine Entscheidung:
    /// ob wirklich unterbrochen wurde, entscheidet der Core, und die Wahrheit
    /// darueber kommt als `flush` zurueck.
    private func rms(_ samples: UnsafePointer<Int16>, frames: Int) -> Float {
        guard frames > 0 else { return 0 }
        var sum: Double = 0
        for index in 0..<frames {
            let value = Double(samples[index]) / 32_768
            sum += value * value
        }
        return Float((sum / Double(frames)).squareRoot())
    }

    /// Aus einem RMS-Wert wird die eine fluechtige Zahl der Praesenz.
    ///
    /// Gerechnet wird in Dezibel, aus demselben Grund wie beim Grundpegel:
    /// linearer RMS ist rechtsschief, und eine Anzeige darauf klebte am
    /// Anschlag oder am Boden. Das Knie kommt aus dem Smoothstep — weich am
    /// Boden, weich an der Decke.
    private func feedLevel(_ value: Float, playback: Bool) {
        let decibel = 20 * log10f(max(value, 1e-6))
        let raw = min(max((decibel - Self.levelFloorDB) / -Self.levelFloorDB, 0), 1)
        let target = raw * raw * (3 - 2 * raw)

        let now = CFAbsoluteTimeGetCurrent()
        levelLock.lock()
        if !playback && _micMutedForLevel { levelLock.unlock(); return }
        // dt begrenzen: nach einer langen Pause soll der erste Wert kein
        // Sprung aus der Vergangenheit sein.
        let dt = envelopeAt == 0 ? Self.levelAttack : min(now - envelopeAt, 0.5)
        envelopeAt = now
        let tau = target > envelope ? Self.levelAttack : Self.levelRelease
        envelope += (target - envelope) * Float(1 - exp(-dt / tau))
        let due = now - levelEmittedAt >= Self.levelEmitEvery
        if due { levelEmittedAt = now }
        let smoothed = envelope
        let callback = onLevel
        levelLock.unlock()
        if due { callback?(smoothed) }
    }

    /// Huellkurve auf null — sofort, ohne Abklingen. Fuer den Moment, in dem
    /// der Lautsprecher schlagartig still wird: eine Praesenz, die nach dem
    /// Barge-in noch nachglueht, saehe aus, als rede SOLVIO weiter.
    private func resetLevel(tell: Bool) {
        levelLock.lock()
        envelope = 0
        envelopeAt = 0
        levelEmittedAt = 0
        let callback = onLevel
        levelLock.unlock()
        if tell { callback?(0) }
    }

    // MARK: - Wiedergabe

    func play(_ pcm16: Data) {
        guard running, let voice = playWire, !pcm16.isEmpty else { return }
        let frames = pcm16.count / MemoryLayout<Int16>.size
        guard frames > 0 else { return }

        queueLock.lock()
        let overflowing = queuedSeconds > Self.maxQueuedSeconds
        queueLock.unlock()
        if overflowing { dropped += 1; return }   // lieber eine Luecke als eine Lawine

        guard let out = AVAudioPCMBuffer(pcmFormat: voice,
                                         frameCapacity: AVAudioFrameCount(frames)),
              let dst = out.floatChannelData else { return }
        out.frameLength = AVAudioFrameCount(frames)
        // Ganzzahl zu Fliesskomma, Wert fuer Wert. Keine Ratenwandlung, kein
        // Zustand, keine Grenze zwischen zwei Schnipseln, an der etwas bricht.
        var level: Float = 0
        pcm16.withUnsafeBytes { raw in
            guard let src = raw.bindMemory(to: Int16.self).baseAddress else { return }
            let target = dst[0]
            for index in 0..<frames {
                target[index] = Float(src[index]) / 32_768
            }
            // Dieselbe Rechnung wie beim Mikrofon — ein Schnipsel, eine Zahl,
            // und die Zahl lebt nur bis zum Abspielen dieses Schnipsels.
            level = rms(src, frames: frames)
        }

        let seconds = Double(frames) / voice.sampleRate
        queueLock.lock()
        if !_isSpeaking {
            utteranceScheduled = 0                   // eine neue Antwort faengt an
            gateClosedSinceUtterance = false
        }
        utteranceScheduled += seconds
        _isSpeaking = true
        queueLock.unlock()
        adjustQueued(by: seconds)
        scheduled += 1

        control.async { [weak self] in
            guard let self, self.running else { return }
            self.player.scheduleBuffer(out, completionCallbackType: .dataPlayedBack) { [weak self] _ in
                // Erst die Huellkurve, dann die Buchhaltung: dieser Schnipsel
                // war GERADE zu hoeren — nicht, als er ankam. Der Anbieter
                // erzeugt schneller als Echtzeit; wer beim Ankommen malt, malt
                // die Zukunft. Nach einem Stopp ist `speaking` schon falsch,
                // und die verworfenen Schnipsel malen nichts mehr.
                if let self, self.speakingNow { self.feedLevel(level, playback: true) }
                self?.adjustQueued(by: -seconds)
            }
            // Erst losspielen, wenn genug dasteht.
            self.queueLock.lock()
            let ready = self.queuedSeconds >= Self.primeSeconds
            self.queueLock.unlock()
            if ready && !self.primed {
                self.primed = true
                self.player.play()
            }
        }
    }

    /// Barge-in: sofort still. Nicht ausblenden, nicht zu Ende spielen — was
    /// noch in der Warteschlange liegt, gehoert zur abgebrochenen Antwort und
    /// wuerde ueber die neue Frage reden.
    ///
    /// - Returns: die Zeit von der lokal beobachteten Sprechpause bis hierher,
    ///   in Millisekunden — oder `nil`, wenn es keinen Beginn zu messen gab.
    /// Die gemessene Zeit vom Sprechbeginn bis zur Stille — einmalig.
    func consumeOnset() -> Int? {
        queueLock.lock(); defer { queueLock.unlock() }
        guard let onset = _lastSpeechOnset else { return nil }
        _lastSpeechOnset = nil
        return Int((CFAbsoluteTimeGetCurrent() - onset) * 1000)
    }

    /// Wie viel von der laufenden Antwort bisher zu hoeren war. Fuer den Fall,
    /// dass der Anbieter die Unterbrechung zuerst bemerkt hat.
    func heardSoFar() -> Int { heardMilliseconds() }

    /// Wie viel von der laufenden Antwort tatsaechlich zu hoeren war.
    private func heardMilliseconds() -> Int {
        queueLock.lock(); defer { queueLock.unlock() }
        return Int(max(0, utteranceScheduled - queuedSeconds) * 1000)
    }

    /// Still. Laeuft AUSSCHLIESSLICH auf `control` — nie im Audiothread.
    private func silenceNow() {
        player.stop()
        setQueued(0)
        queueLock.lock()
        utteranceScheduled = 0
        _isSpeaking = false
        queueLock.unlock()
        primed = false
        resetLevel(tell: true)
    }

    @discardableResult
    func flush() -> Int? {
        queueLock.lock(); let waiting = queuedSeconds; queueLock.unlock()
        // Es gibt nichts abzubrechen. Frueher wurde hier trotzdem `primed`
        // zurueckgesetzt, und die naechste Antwort musste erneut 250 ms
        // sammeln, bevor der erste Ton hoerbar war — bei JEDEM Turn, weil der
        // Core bei jedem Sprechbeginn ein `flush` schickt.
        if waiting <= 0.001 { return nil }
        let onset = lastSpeechOnset
        // Kein sofortiges `play()`: die naechste Antwort legt erst wieder vor.
        control.async { [weak self] in self?.silenceNow() }
        queueLock.lock(); _lastSpeechOnset = nil; queueLock.unlock()
        guard let onset else { return nil }
        return Int((CFAbsoluteTimeGetCurrent() - onset) * 1000)
    }

    private func adjustQueued(by delta: Double) {
        queueLock.lock()
        queuedSeconds = max(0, queuedSeconds + delta)
        let empty = queuedSeconds <= 0.001
        queueLock.unlock()
        if empty {
            queueLock.lock(); _isSpeaking = false; queueLock.unlock()
            dryRuns += 1
            // KEIN `pause()` und KEIN erneutes Vorlegen. Beides war ein Fehler:
            // ein Spieler, der bei jeder kurzen Luecke anhaelt und dann wieder
            // 250 ms sammelt, macht aus einem Schluckauf eine Pause. Er laeuft
            // jetzt durch — eine Luecke ist dann Stille und kein Abbruch.
            onDry?()
        }
    }

    private func setQueued(_ value: Double) {
        queueLock.lock()
        queuedSeconds = value
        queueLock.unlock()
    }
}
