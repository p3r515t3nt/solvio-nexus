// SOLVIO Designsprache — die Tokens.
//
// Grundlage ist die real gemessene SOLVIO-Marke (Blau #1F7DDB, Gold #D6A545,
// Quelle: Developer/Solvio/brand/source/solvio-icon.svg) und die in SOLVIO
// Forms produktiv erprobte Palette (ios/Solvio/Design/SolvioColors.swift).
// Es wird keine zweite Markenwelt erfunden — dieselbe Identitaet, auf die
// Begleiter-App uebertragen.
//
// Regeln:
// * Gold ist Akzent, nie Fliesstext auf Weiss (zu wenig Kontrast).
// * Grau heisst „nicht nachgesehen", nie „gesund".
// * Der Sprachraum (VoiceCanvas) ist in HELL und DUNKEL derselbe tiefe
//   Nachtblau-Raum — das Gespraech hat einen eigenen Ort.
import SwiftUI
import UIKit

// MARK: - Farben

extension Color {
    init(hex: String) {
        var value: UInt64 = 0
        Scanner(string: hex.replacingOccurrences(of: "#", with: "")).scanHexInt64(&value)
        self.init(.sRGB,
                  red: Double((value >> 16) & 0xFF) / 255,
                  green: Double((value >> 8) & 0xFF) / 255,
                  blue: Double(value & 0xFF) / 255)
    }

    init(light: Color, dark: Color) {
        self.init(uiColor: UIColor { trait in
            trait.userInterfaceStyle == .dark ? UIColor(dark) : UIColor(light)
        })
    }
}

enum Theme {
    // Marke
    static let blue        = Color(hex: "#1F7DDB")
    static let bluePressed = Color(hex: "#1565B8")
    static let gold        = Color(light: Color(hex: "#D6A545"), dark: Color(hex: "#E6B558"))

    // Flaechen
    static let bg          = Color(light: Color(hex: "#F7F9FC"), dark: Color(hex: "#0C1220"))
    static let surface     = Color(light: .white,                dark: Color(hex: "#171B2A"))
    static let surface2    = Color(light: Color(hex: "#FAFBFD"), dark: Color(hex: "#1A1F30"))
    static let tintBlue    = Color(light: Color(hex: "#EAF2FB"), dark: Color(hex: "#16263D"))
    static let tintGold    = Color(light: Color(hex: "#FBF3E0"), dark: Color(hex: "#2C2517"))

    // Text
    static let ink         = Color(light: Color(hex: "#0F1B2D"), dark: Color(hex: "#F1F4F9"))
    static let ink2        = Color(light: Color(hex: "#5B6B80"), dark: Color(hex: "#9AA6B8"))
    static let ink3        = Color(hex: "#7A8699")
    static let inkOnGold   = Color(light: Color(hex: "#6B5A2A"), dark: Color(hex: "#E6CB88"))
    static let onBlue      = Color.white

    // Linien
    static let line        = Color(light: Color(hex: "#EBF0F6"), dark: Color(hex: "#252B3A"))

    // Semantik
    static let good        = Color(light: Color(hex: "#3A8657"), dark: Color(hex: "#62B87F"))
    static let warn        = Color(light: Color(hex: "#A37714"), dark: Color(hex: "#D4A23E"))
    static let bad         = Color(light: Color(hex: "#BB3B3B"), dark: Color(hex: "#E06C6C"))

    // Der Sprachraum: bewusst in beiden Erscheinungen derselbe.
    static let voiceTop    = Color(hex: "#0C1626")
    static let voiceBottom = Color(hex: "#13233C")

    // MARK: Radien
    enum Radius {
        static let chip: CGFloat = 10
        static let row: CGFloat = 14
        static let card: CGFloat = 20
        static let hero: CGFloat = 28
    }

    // MARK: Abstand (4er-Raster)
    enum Space {
        static let margin: CGFloat = 20
        static let card: CGFloat = 16
        static let section: CGFloat = 24
    }
}

// MARK: - Typografie
//
// Anzeige-Ebene rund (passt zur Wortmarke), Lauftext System. Immer ueber
// Text-Stile, nie feste Groessen — Dynamic Type bleibt intakt.

extension Font {
    static func display(_ style: TextStyle, weight: Weight = .semibold) -> Font {
        .system(style, design: .rounded).weight(weight)
    }
}

/// Die Wortmarke als Text — wie in SOLVIO Forms. Das Wortmarken-SVG ist die
/// Weiss-Variante fuer dunkle Flaechen; im UI wird gesetzt, nicht gerastert.
struct Wordmark: View {
    var size: Font.TextStyle = .title2
    var color: Color = Theme.ink
    var body: some View {
        Text("SOLVIO")
            .font(.display(size, weight: .bold))
            .kerning(1.5)
            .foregroundStyle(color)
    }
}

/// Die Bildmarke aus der kanonischen Quelle.
struct Mark: View {
    var size: CGFloat = 44
    var body: some View {
        Image("SolvioMark")
            .resizable()
            .scaledToFit()
            .frame(width: size, height: size)
            .accessibilityHidden(true)
    }
}

// MARK: - Bewegung

enum Motion {
    static let standard = Animation.easeOut(duration: 0.28)
    static let breathe  = Animation.easeInOut(duration: 2.6).repeatForever(autoreverses: true)
    static let ripple   = Animation.easeOut(duration: 1.8).repeatForever(autoreverses: false)
    static let orbit    = Animation.linear(duration: 3.4).repeatForever(autoreverses: false)
}

// MARK: - Haptik

@MainActor
enum Haptics {
    static func tap()     { UIImpactFeedbackGenerator(style: .light).impactOccurred() }
    static func state()   { UISelectionFeedbackGenerator().selectionChanged() }
    static func success() { UINotificationFeedbackGenerator().notificationOccurred(.success) }
    static func warning() { UINotificationFeedbackGenerator().notificationOccurred(.warning) }
}

// MARK: - Bausteine

/// Karte auf der Grundflaeche. Schatten nur hell — dunkel traegt die Flaeche.
struct CardBackground: ViewModifier {
    @Environment(\.colorScheme) private var scheme
    var radius: CGFloat = Theme.Radius.card
    func body(content: Content) -> some View {
        content
            .background(Theme.surface, in: RoundedRectangle(cornerRadius: radius, style: .continuous))
            .shadow(color: scheme == .dark ? .clear : Color(hex: "#0F1B2D").opacity(0.06),
                    radius: 12, y: 6)
    }
}

extension View {
    func card(radius: CGFloat = Theme.Radius.card) -> some View {
        modifier(CardBackground(radius: radius))
    }
}

/// Statuspunkt mit Wort daneben — nie Farbe allein.
struct StatusDot: View {
    let color: Color
    var body: some View {
        Circle().fill(color).frame(width: 9, height: 9)
            .accessibilityHidden(true)
    }
}
