// SOLVIO NEXUS — das Zeichnen, funktional identisch zur tick()-Referenz.
//
// Jede Ebene, jede Farbe, jeder Radius kommt aus dem Handoff
// (design_handoff_solvio_nexus). Reihenfolge hinten -> vorn:
//
//   Hintergrund-Glow · Sprech-Vignette · Ripples (Zuhoeren nach innen,
//   Sprechen aus den BRILLENGLAESERN, nah gold / fern blau) · Denk-Spiralen
//   (2 blau, 1 gold) · Deep-Work-Orbits mit Leuchtpunkten · Ambient-Partikel ·
//   Kern-Ringe (Hauptring, zwei rotierende Teilboegen, EIN goldener
//   Teilbogen, zwei weiche Logo-Anspielungen aussen) · Kern-Fuellung ·
//   Wave-Equalizer · Erwachen-Morph (Kopfkreis, Haar-Swoosh, Gold-Strahl auf
//   der Brillenlinie, Zuendung).
//
// Zwei bewusste Uebersetzungen gegenueber Canvas-2D:
// * `shadowBlur` gibt es in SwiftUI-Canvas nicht — der Schein ist ueberall
//   als gestapelte Striche uebersetzt (breit/transparent unter schmal/klar).
//   Der Blur-FILTER kommt nicht in Frage: er erzeugte auf grossen Pfaden
//   GPU-Kachelartefakte (Pass 2 der visuellen Schleife).
// * Die Stimm-Huellkurve `env` nutzt den ECHTEN Pegel, wo einer da ist
//   (Mikrofon/Wiedergabe); nur ohne Pegel gilt die Pseudo-Huellkurve der
//   Referenz. Der Auftrag verlangt echte Audio-Reaktivitaet — eine
//   vorgetaeuschte Stimme waere genau die Art Dekoration, die SOLVIO nicht
//   macht.
import SwiftUI

/// Ein Ambient-Partikel — Felder wie in der Referenz:
/// `{a, r: 60..165, sp, sz, ph, gold: 22%}`.
struct AmbientPoint {
    let a: Double
    let r: Double
    let sp: Double
    let sz: Double
    let ph: Double
    let gold: Bool

    static func field() -> [AmbientPoint] {
        (0..<80).map { _ in
            AmbientPoint(
                a: .random(in: 0..<(2 * .pi)),
                r: .random(in: 60...165),
                sp: (Bool.random() ? 1 : -1) * .random(in: 0.12...0.62),
                sz: .random(in: 0.7...2.2),
                ph: .random(in: 0..<(2 * .pi)),
                gold: Double.random(in: 0..<1) < 0.22)
        }
    }
}

enum PresenceRenderer {

    // Farbwerte der Referenz.
    static let blue      = Color(red: 31/255, green: 125/255, blue: 219/255)
    static let softBlue  = Color(red: 106/255, green: 177/255, blue: 240/255)
    static let lightBlue = Color(red: 120/255, green: 185/255, blue: 245/255)
    static let ringBlue  = Color(red: 90/255, green: 160/255, blue: 235/255)
    static let headBlue  = Color(red: 72/255, green: 148/255, blue: 232/255)
    static let dotBlue   = Color(red: 140/255, green: 190/255, blue: 245/255)
    static let gold      = Color(red: 214/255, green: 165/255, blue: 69/255)
    static let beamGold  = Color(red: 230/255, green: 185/255, blue: 90/255)

    /// Gestapelter Strich — die Uebersetzung von `shadowBlur`.
    private static func glowStroke(_ ctx: GraphicsContext, _ path: Path,
                                   color: Color, alpha: Double, width: Double,
                                   glow: Double) {
        guard alpha > 0.004 else { return }
        ctx.stroke(path, with: .color(color.opacity(alpha * 0.18)), lineWidth: width + glow)
        ctx.stroke(path, with: .color(color.opacity(alpha * 0.38)), lineWidth: width + glow * 0.4)
        ctx.stroke(path, with: .color(color.opacity(alpha)), lineWidth: width)
    }

    private static func arc(_ c: CGPoint, _ r: Double, _ from: Double,
                            _ to: Double, clockwise: Bool = false) -> Path {
        var p = Path()
        p.addArc(center: c, radius: r, startAngle: .radians(from),
                 endAngle: .radians(to), clockwise: clockwise)
        return p
    }

    private static func circle(_ c: CGPoint, _ r: Double) -> Path {
        Path(ellipseIn: CGRect(x: c.x - r, y: c.y - r, width: r * 2, height: r * 2))
    }

    // MARK: Einstieg — die tick()-Uebersetzung

    static func draw(in ctx: GraphicsContext, size: CGSize,
                     parameters P: PresenceParameters, awaken A: AwakenPhases,
                     audio: Double, t: Double, state: PresenceState,
                     invite: Bool = false) {
        let w = size.width, h = size.height
        // Die Referenz-Huellkurve — echter Pegel gewinnt, wo einer da ist.
        let pseudo = abs(sin(t * 2.1) * sin(t * 5.3))
        let env = 0.35 + 0.65 * (audio > 0.02 ? audio : pseudo)
        let cx = w / 2, cy = h * 0.42
        let breath = 1 + 0.035 * sin(t * 1.1) + 0.09 * P.pulse * env
        let R = min(w, h) * 0.235 * breath
        let c = CGPoint(x: cx, y: cy)
        let settled = A.settled
        let dim = P.brightness
        // Globale Deckkraft der Energie-Ebenen: blendet mit dem Erwachen ein.
        let energy = (0.2 + 0.8 * settled) * dim
        let boost = 1 + 0.55 * P.pulse * env

        // -- Hintergrund-Glow --------------------------------------------
        // Der Schein laeuft VOR der naechsten Canvas-Kante auf null aus: ein
        // Glow, der an der Kante abgeschnitten wird, ZEICHNET die Kante — auf
        // dem Start stand der Orb dadurch in einem sichtbar helleren Rechteck
        // (zweite Live-Rueckmeldung). Endfarbe hue-treu transparent statt
        // .clear, damit nichts gegen transparentes Schwarz truebt. Der
        // mittlere Stopp bleibt beim Referenzradius R*0.56 (= 0.2 von R*2.8).
        let glowEnd = min(R * 2.8,
                          max(R * 1.2, min(min(cx, w - cx), min(cy, h - cy))))
        ctx.fill(Path(CGRect(origin: .zero, size: size)), with: .radialGradient(
            Gradient(stops: [
                .init(color: blue.opacity(0.22 * energy * boost), location: 0),
                .init(color: blue.opacity(0.07 * energy * boost),
                      location: min(0.9, R * 0.56 / glowEnd)),
                .init(color: blue.opacity(0), location: 1),
            ]), center: c, startRadius: R * 0.2, endRadius: glowEnd))

        // -- Sprech-Vignette ---------------------------------------------
        if P.rippleOut > 0.02 {
            ctx.fill(Path(CGRect(origin: .zero, size: size)), with: .radialGradient(
                Gradient(stops: [
                    .init(color: .clear, location: 0),
                    .init(color: blue.opacity(0.16 * P.rippleOut * env * dim), location: 1),
                ]), center: c, startRadius: min(w, h) * 0.35, endRadius: max(w, h) * 0.75))
        }

        // -- Ripples: Zuhoeren kollabiert nach innen ----------------------
        if P.rippleIn > 0.02 {
            for i in 0..<3 {
                let k = (t * 0.55 + Double(i) / 3).truncatingRemainder(dividingBy: 1)
                let r = R * (2.5 - 1.5 * k)
                ctx.stroke(circle(c, r),
                           with: .color(softBlue.opacity(sin(.pi * k) * 0.30 * P.rippleIn * dim)),
                           lineWidth: 1.2)
            }
        }

        // -- Ripples: die Stimme kommt aus der Brille ---------------------
        if P.rippleOut > 0.02 {
            let sources = [CGPoint(x: cx - R * 0.42, y: cy + R * 0.02),
                           CGPoint(x: cx + R * 0.42, y: cy + R * 0.02)]
            for i in 0..<4 {
                let k = (t * 0.7 + Double(i) / 4).truncatingRemainder(dividingBy: 1)
                let rr = R * 0.3 + k * w * 0.75
                let isGold = rr < R * 1.1
                let a = (1 - k) * (0.10 + 0.30 * env) * P.rippleOut * dim
                for s in sources {
                    ctx.stroke(circle(s, rr),
                               with: .color((isGold ? gold : softBlue).opacity(a)),
                               lineWidth: isGold ? 2 : 1.3)
                }
            }
        }

        // -- Denken: drei sich windende Spiralen --------------------------
        if P.swirl > 0.02 {
            for j in 0..<3 {
                let jd = Double(j)
                var path = Path()
                for s in 0...60 {
                    let u = Double(s) / 60
                    let rr = R * (0.55 + 0.55 * sin(6.283 * u * 1.5 + t * 1.6 + jd * 2.1))
                    let aa = 6.283 * u + t * (0.5 + 0.15 * jd) + jd
                    let pt = CGPoint(x: cx + cos(aa) * rr, y: cy + sin(aa) * rr * 0.9)
                    if s == 0 { path.move(to: pt) } else { path.addLine(to: pt) }
                }
                let color = j == 1 ? gold.opacity(0.20 * P.swirl * dim)
                                   : Color(red: 80/255, green: 150/255, blue: 235/255)
                                       .opacity(0.22 * P.swirl * dim)
                ctx.stroke(path, with: .color(color), lineWidth: 1.4)
            }
        }

        // -- Deep Work: Orbit-Bahnen mit Leuchtpunkten --------------------
        if P.orbit > 0.02 {
            for (j, tilt) in [0.5, -0.35, 1.2].enumerated() {
                let jd = Double(j)
                var orbitCtx = ctx
                orbitCtx.translateBy(x: cx, y: cy)
                orbitCtx.rotate(by: .radians(tilt))
                let track = Path(ellipseIn: CGRect(x: -R * 1.7, y: -R * 0.55,
                                                   width: R * 3.4, height: R * 1.1))
                orbitCtx.stroke(track, with: .color(softBlue.opacity(0.12 * P.orbit * dim)),
                                lineWidth: 1)
                for k2 in 0..<4 {
                    let a = t * (0.6 + 0.13 * jd) + Double(k2) * 1.57 + jd
                    let pos = CGPoint(x: cos(a) * R * 1.7, y: sin(a) * R * 0.55)
                    let isGold = (k2 + j) % 3 == 0
                    let dotR: Double = isGold ? 2.2 : 1.6
                    let color = isGold ? gold.opacity(0.9 * P.orbit * dim)
                                       : dotBlue.opacity(0.8 * P.orbit * dim)
                    // Punkt + weicher Hof (die shadowBlur-Uebersetzung).
                    orbitCtx.fill(circle(pos, dotR * 2.4),
                                  with: .color(color.opacity(0.25)))
                    orbitCtx.fill(circle(pos, dotR), with: .color(color))
                }
            }
        }

        // -- Ambient-Partikel --------------------------------------------
        // amb blendet vor settled=0.35 aus — verhindert streunende
        // Gold-Punkte waehrend des Morphs (die laesen sich als Augen).
        let amb = (1 - 0.5 * P.orbit) * energy * (settled < 0.35 ? 0 : settled)
            * P.coherence
        if amb > 0.001 {
            for p in ambient {
                let a = p.a + p.sp * (1 + P.swirl * 1.8) * t
                let rr = p.r * R / 86 * (1 + 0.06 * sin(t * 0.9 + p.ph))
                let pos = CGPoint(x: cx + cos(a) * rr, y: cy + sin(a) * rr * 0.96)
                let al = (0.22 + 0.42 * abs(sin(t * 0.7 + p.ph))) * amb
                ctx.fill(circle(pos, p.sz),
                         with: .color((p.gold ? gold : lightBlue).opacity(al)))
            }
        }

        // -- Kern-Ringe ---------------------------------------------------
        // Kohaerenzverlust (reconnecting) treibt die Ringe auseinander.
        let drift = (1 - P.coherence) * R * 0.3
        let ringAlpha = energy
        glowStroke(ctx, circle(c, R + drift), color: ringBlue,
                   alpha: 0.85 * ringAlpha, width: 2, glow: 14)
        ctx.stroke(arc(c, R * 1.22 + drift * 1.4, t * 0.3, t * 0.3 + 4.4),
                   with: .color(Color(red: 70/255, green: 140/255, blue: 220/255)
                       .opacity(0.28 * ringAlpha)), lineWidth: 1.2)
        ctx.stroke(arc(c, R * 0.82, -t * 0.42, -t * 0.42 + 3.6),
                   with: .color(lightBlue.opacity(0.30 * ringAlpha)), lineWidth: 1.2)
        glowStroke(ctx, arc(c, R * 1.06 + drift, t * 0.55, t * 0.55 + 1.7),
                   color: gold, alpha: 0.75 * ringAlpha * P.coherence,
                   width: 1.8, glow: 10)
        // Die zwei weichen Logo-Anspielungen aussen (Swoosh + Laecheln).
        glowStroke(ctx, arc(CGPoint(x: cx + R * 0.1, y: cy + R * 0.12), R * 1.34,
                            -2.75, -1.45),
                   color: softBlue, alpha: 0.35 * ringAlpha, width: 3, glow: 8)
        glowStroke(ctx, arc(CGPoint(x: cx, y: cy - R * 0.25), R * 1.28, 0.75, 2.4),
                   color: softBlue, alpha: 0.28 * ringAlpha, width: 2.2, glow: 8)

        // -- Kern-Fuellung ------------------------------------------------
        ctx.fill(circle(c, R), with: .radialGradient(
            Gradient(stops: [
                .init(color: Color(red: 40/255, green: 90/255, blue: 160/255)
                    .opacity(0.30 * dim), location: 0),
                .init(color: Color(red: 25/255, green: 55/255, blue: 105/255)
                    .opacity(0.12 * dim), location: 0.8),
                .init(color: .clear, location: 1),
            ]), center: c, startRadius: 0, endRadius: R))

        // -- Einladung ----------------------------------------------------
        // Nur am Start (invite) und nur in Ruhe: zwei goldene Ringe loesen
        // sich langsam vom Kern — die Geste sagt „beruehr mich", bevor es
        // Worte tun. Bei eingefrorener Zeit (Reduced Motion) stehen sie
        // still als leiser Doppelring.
        if invite && state == .idle {
            for i in 0..<2 {
                let k = (t * 0.4 + Double(i) * 0.5).truncatingRemainder(dividingBy: 1)
                let r = R * (1.10 + 0.55 * k)
                let a = (1 - k) * k * 4 * 0.20 * settled * dim
                glowStroke(ctx, circle(c, r), color: gold, alpha: a,
                           width: 1.3, glow: 7)
            }
        }

        // -- Wave-Equalizer ----------------------------------------------
        if P.wave > 0.02 {
            let n = 26, bw = 3.0, gap = 5.0
            let total = Double(n) * (bw + gap) - gap
            // 0.62 statt 0.78: auf dem Geraet sassen die Balken unter der
            // Mikrofon-Pille und dem Statustext — Animation, die von Text
            // verdeckt wird, ist keine. Jetzt schwingen sie frei zwischen
            // Orb und Text.
            let y0 = h * 0.62
            let wenv = state == .speaking ? env : 0.5 + 0.3 * sin(t * 3.7)
            let color = softBlue.opacity((0.30 + 0.5 * P.wave) * dim)
            var bars = Path()
            for i in 0..<n {
                let id = Double(i)
                let x = cx - total / 2 + id * (bw + gap)
                let center = 1 - abs(id - Double(n - 1) / 2) / (Double(n - 1) / 2)
                let bh = P.wave * (3 + 30 * abs(sin(id * 0.9 + t * 6)) * wenv
                    * (0.35 + 0.65 * center))
                bars.addRect(CGRect(x: x, y: y0 - bh / 2, width: bw, height: bh))
            }
            ctx.fill(bars, with: .color(color))
        }

        // -- Erwachen-Morph ----------------------------------------------
        // Logo-Geometrie aus der gemessenen SVG: Kopfkreis-Zentrum knapp
        // unter der Bildmitte, Brillenlinie bei R*0.14 darunter.
        let cyH = cy + R * 0.02
        let headR = R * 0.66
        let glassY = cy + R * 0.14
        if settled < 1 {
            let fade = 1 - A.ignite
            // 1) Der blaue Kopfkreis zeichnet sich beidseitig von oben —
            //    der untere Schluss IST das Logo-Laecheln.
            if A.headp > 0, fade > 0 {
                let hp = ease(A.headp)
                let lw = max(6, R * 0.075)
                glowStroke(ctx, arc(CGPoint(x: cx, y: cyH), headR,
                                    -.pi / 2, -.pi / 2 + .pi * hp),
                           color: headBlue, alpha: 0.95 * fade, width: lw, glow: 16)
                glowStroke(ctx, arc(CGPoint(x: cx, y: cyH), headR,
                                    -.pi / 2, -.pi / 2 - .pi * hp, clockwise: true),
                           color: headBlue, alpha: 0.95 * fade, width: lw, glow: 16)
            }
            // 2) Haar-Swoosh — die Haartolle des Logos.
            if A.swoosh > 0, fade > 0 {
                let s = ease(A.swoosh)
                glowStroke(ctx, arc(CGPoint(x: cx, y: cyH), headR * 0.92,
                                    -2.5, -2.5 + 1.1 * s),
                           color: lightBlue, alpha: 0.9 * fade,
                           width: max(5, R * 0.065), glow: 14)
            }
            // 3) Gold-Lichtstrahl verdichtet sich auf der Brillenlinie —
            //    KEINE Einzelpunkte: die laesen sich als Augen, und das Logo
            //    hat keine Augen.
            if A.flash > 0, A.flash < 1, fade > 0 {
                let spread = (1 - ease(A.flash)) * headR * 1.5 + headR * 0.6
                let a = sin(.pi * A.flash) * 0.7 * fade
                var beam = Path()
                beam.move(to: CGPoint(x: cx - spread, y: glassY))
                beam.addLine(to: CGPoint(x: cx + spread, y: glassY))
                let shading = GraphicsContext.Shading.linearGradient(
                    Gradient(stops: [
                        .init(color: gold.opacity(0), location: 0),
                        .init(color: beamGold.opacity(a), location: 0.5),
                        .init(color: gold.opacity(0), location: 1),
                    ]),
                    startPoint: CGPoint(x: cx - spread, y: glassY),
                    endPoint: CGPoint(x: cx + spread, y: glassY))
                ctx.stroke(beam, with: shading, lineWidth: 7)
                ctx.stroke(beam, with: shading, lineWidth: 3)
                ctx.fill(circle(CGPoint(x: cx, y: glassY), headR * 1.1),
                         with: .radialGradient(
                            Gradient(stops: [
                                .init(color: gold.opacity(0.28 * a), location: 0),
                                .init(color: .clear, location: 1),
                            ]), center: CGPoint(x: cx, y: glassY),
                            startRadius: 0, endRadius: headR * 1.1))
            }
            // 4) Zuendung: zwei Schockwellen, blau vor gold.
            if A.ignite > 0, A.ignite < 1 {
                for (i, d) in [0.0, 0.12].enumerated() {
                    let k = min(1, max(0, A.ignite - d))
                    guard k > 0 else { continue }
                    let color = i == 1 ? gold.opacity(0.5 * (1 - k))
                                       : softBlue.opacity(0.55 * (1 - k))
                    ctx.stroke(circle(c, R * (0.7 + k * 3.6)),
                               with: .color(color), lineWidth: i == 1 ? 1.8 : 2.6)
                }
            }
        }
    }

    /// Das Partikelfeld — einmal je Prozess, wie das `pts[]` der Referenz
    /// einmal je Mount entsteht.
    static let ambient: [AmbientPoint] = AmbientPoint.field()
}
