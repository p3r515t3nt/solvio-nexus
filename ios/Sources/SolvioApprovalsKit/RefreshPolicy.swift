// Wie oft die App nachfragt — und wie sie sich zurueckhaelt, wenn niemand antwortet.
//
// Die Regel entstand aus einem gemessenen Abend: eine Freigabe lag bereit, die App
// stand offen im Vordergrund, und sie fragte nie nach. Sie konnte es nicht — es gab
// nur Pull-to-Refresh. Der Mensch musste raten, wann er ziehen soll.
//
// Was hier NICHT passiert: aus Nachfragen wird keine Befugnis. Die App darf von
// selbst erfahren, dass etwas ansteht; freigegeben wird weiterhin ausschliesslich
// mit Face ID und einer frischen App-Attest-Aussage.
//
// Reiner Typ ohne Zeitgeber und ohne Netz, damit `swift test` ihn auf dem Mac
// pruefen kann. Wer Kadenz in einer View versteckt, testet sie nie.
import Foundation

public struct RefreshPolicy: Sendable, Equatable {
    /// Der normale Takt im Vordergrund. Kurz genug, dass eine Freigabe „sofort"
    /// erscheint, lang genug, dass ein Mac damit nichts zu tun hat.
    public static let defaultBase: TimeInterval = 3

    /// Die Obergrenze bei anhaltenden Fehlern. Ein Geraet, das im Aufzug steht,
    /// soll nicht im Sekundentakt gegen ein totes Netz laufen.
    public static let defaultCeiling: TimeInterval = 30

    public let base: TimeInterval
    public let ceiling: TimeInterval
    public let factor: Double

    /// Wie viele Versuche in Folge fehlgeschlagen sind.
    public private(set) var failures: Int

    public init(base: TimeInterval = RefreshPolicy.defaultBase,
                ceiling: TimeInterval = RefreshPolicy.defaultCeiling,
                factor: Double = 2,
                failures: Int = 0) {
        self.base = base
        self.ceiling = ceiling
        self.factor = factor
        self.failures = max(0, failures)
    }

    /// Wie lange bis zur naechsten Nachfrage.
    ///
    /// Verdoppelnd, gedeckelt. Ohne Deckel waere die App nach einer laengeren
    /// Netzstoerung praktisch tot; ohne Verdopplung haemmerte sie dagegen.
    public var interval: TimeInterval {
        guard failures > 0 else { return base }
        let grown = base * pow(factor, Double(failures))
        return min(grown, ceiling)
    }

    /// Nach einem geglueckten Abruf gilt sofort wieder der normale Takt —
    /// nicht schrittweise zurueck. Wer erreichbar ist, ist erreichbar.
    public mutating func succeeded() { failures = 0 }

    public mutating func failed() { failures += 1 }

    /// Ob gerade eine Stoerung anhaelt. Nur fuer die Anzeige gedacht.
    public var isBackingOff: Bool { failures > 0 }
}
