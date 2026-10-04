// SOLVIO's original Canvas2D handoff, ported with iOS state/reduced-motion rules.
// Motion represents presence, never proof of microphone capture or transmission.
export class Presence {
  constructor(canvas, mascot, glasses) {
    this.cv = canvas; this.mimg = mascot; this.gimg = glasses;
    this.mode = 'home'; this.brightness = .38; this.level = null;
    this.params = {in:0,out:0,wave:0,swirl:0,orbit:0,pulse:0};
    this.reduced = matchMedia('(prefers-reduced-motion: reduce)');
    this.pts = Array.from({length:80}, (_,i)=>({a:i*2.39996,r:60+(i*43%105),
      sp:(.12+(i%9)*.05)*(i%2?1:-1),sz:.7+(i%4)*.4,ph:i*.731,gold:i%5===0}));
    this.t0 = this.seqStart = this.last = performance.now();
    this.dirty = true;
    this.resize = new ResizeObserver(()=> {this.dirty=true;});
    this.resize.observe(canvas);
    this.onMotion = ()=> {this.dirty=true;};
    this.reduced.addEventListener('change',this.onMotion);
    this.raf = requestAnimationFrame(this.tick);
  }
  setState(state) {
    const names = {idle:'home',listening:'zuhoeren',thinking:'denken',speaking:'sprechen',deepWork:'deepwork'};
    const next = names[state] || 'home';
    if (this.mode === 'sprechen' && next === 'zuhoeren') {
      Object.assign(this.params,{in:1,out:0,wave:.55,swirl:0,orbit:0,pulse:0});
    }
    this.mode = next;
    this.brightness = state==='offline' ? .38 : state==='ended' ? .6 : state==='reconnecting' ? .75 : 1;
    this.dirty = true;
  }
  setLevel(level) {
    const bounded=value=>Number.isFinite(value)?Math.max(0,Math.min(1,value*8)):0;
    this.level=level?{input:bounded(level.input),output:bounded(level.output)}:null;
    // Levels are transient presentation data. Reduced Motion remains a still image.
    if(!this.reduced.matches)this.dirty=true;
  }
  stop() {
    cancelAnimationFrame(this.raf); this.resize.disconnect();
    this.reduced.removeEventListener('change',this.onMotion);
  }
  tick = (now) => {
    this.raf = requestAnimationFrame(this.tick);
    if (document.hidden || (this.reduced.matches && !this.dirty)) return;
    if (!this.dirty && now-this.last < (this.mode==='home'?1000/30:1000/60)) return;
    const dt = Math.min(.1, (now-this.last)/1000);
    this.last = now; this.dirty = false;


    const cv = this.cv; if (!cv || !cv.isConnected) return;
    const box = cv.getBoundingClientRect(); const w = box.width, h = box.height; if (!w || !h) return;
    const dpr = Math.min(2, window.devicePixelRatio || 1);
    if (cv.width !== Math.round(w * dpr) || cv.height !== Math.round(h * dpr)) { cv.width = Math.round(w * dpr); cv.height = Math.round(h * dpr); }
    const ctx = cv.getContext('2d'); ctx.setTransform(dpr, 0, 0, dpr, 0, 0); ctx.clearRect(0, 0, w, h);

    const tempo = 1, energie = this.brightness;
    const t = this.reduced.matches ? 0 : (now - this.t0) / 1000 * tempo;
    const M = this.mode;
    const T = this.reduced.matches ? 3 : (now - this.seqStart) / 1000;
    const ph = (a, b) => Math.max(0, Math.min(1, (T - a) / (b - a)));
    const swoosh = ph(0.1, 0.85), headp = ph(0.5, 1.5), flash = ph(1.35, 1.75), ignite = ph(2.0, 2.5), settled = ph(2.1, 2.75);
    const ease = (x) => x <= 0 ? 0 : x >= 1 ? 1 : x * x * (3 - 2 * x);
    const tgt = { in: 0, out: 0, wave: 0, swirl: 0, orbit: 0, pulse: 0 };
    if (M === 'zuhoeren') { tgt.in = 1; tgt.wave = 0.55; }
    if (M === 'denken') tgt.swirl = 1;
    if (M === 'sprechen') { tgt.out = 1; tgt.wave = 1; tgt.pulse = 1; }
    if (M === 'deepwork') tgt.orbit = 1;
    const P = this.params; for (const k in tgt) P[k] += (tgt[k] - P[k]) * (this.reduced.matches ? 1 : 1 - Math.pow(0.95, dt * 60));
    const liveLevel = this.level && !this.reduced.matches ? (M === 'sprechen' ? this.level.output : this.level.input) : null;
    const env = liveLevel ?? (0.35 + 0.65 * Math.abs(Math.sin(t * 2.1) * Math.sin(t * 5.3)));
    const cx = w / 2, cy = h * 0.42;
    const breath = 1 + 0.035 * Math.sin(t * 1.1) + 0.09 * P.pulse * env;
    const R = Math.min(w, h) * 0.235 * breath;
    ctx.globalAlpha = 0.2 + 0.8 * settled;
    const boost = 1 + 0.55 * P.pulse * env;
    let g = ctx.createRadialGradient(cx, cy, R * 0.2, cx, cy, R * 2.8);
    g.addColorStop(0, `rgba(31,125,219,${0.22 * energie * boost})`); g.addColorStop(0.55, `rgba(31,125,219,${0.07 * energie * boost})`); g.addColorStop(1, 'rgba(31,125,219,0)');
    ctx.fillStyle = g; ctx.fillRect(0, 0, w, h);
    if (P.out > 0.02) {
      const vg = ctx.createRadialGradient(cx, cy, Math.min(w, h) * 0.35, cx, cy, Math.max(w, h) * 0.75);
      vg.addColorStop(0, 'rgba(31,125,219,0)'); vg.addColorStop(1, `rgba(31,125,219,${0.16 * P.out * env})`);
      ctx.fillStyle = vg; ctx.fillRect(0, 0, w, h);
    }
    const ripple = (dir, amt) => {
      if (amt < 0.02) return;
      for (let i = 0; i < 3; i++) {
        const k = (t * 0.55 + i / 3) % 1;
        const r = dir < 0 ? R * (2.5 - 1.5 * k) : R * (1 + 1.7 * k);
        ctx.beginPath(); ctx.arc(cx, cy, r, 0, 7);
        ctx.strokeStyle = `rgba(106,177,240,${Math.sin(Math.PI * k) * 0.30 * amt})`; ctx.lineWidth = 1.2; ctx.stroke();
      }
    };
    ripple(-1, P.in);
    if (P.out > 0.02) {
      const lxs = [cx - R * 0.42, cx + R * 0.42], ly = cy + R * 0.02;
      for (let i = 0; i < 4; i++) {
        const k = (t * 0.7 + i / 4) % 1;
        const rr = R * 0.3 + k * w * 0.75;
        const gold = rr < R * 1.1;
        const a = (1 - k) * (0.10 + 0.30 * env) * P.out;
        lxs.forEach(x => {
          ctx.beginPath(); ctx.arc(x, ly, rr, 0, 7);
          ctx.strokeStyle = gold ? `rgba(214,165,69,${a})` : `rgba(106,177,240,${a})`;
          ctx.lineWidth = gold ? 2 : 1.3; ctx.stroke();
        });
      }
    }
    if (P.swirl > 0.02) for (let j = 0; j < 3; j++) {
      ctx.beginPath();
      for (let s = 0; s <= 60; s++) {
        const u = s / 60;
        const rr = R * (0.55 + 0.55 * Math.sin(6.283 * u * 1.5 + t * 1.6 + j * 2.1));
        const aa = 6.283 * u + t * (0.5 + 0.15 * j) + j;
        const x = cx + Math.cos(aa) * rr, y = cy + Math.sin(aa) * rr * 0.9;
        s ? ctx.lineTo(x, y) : ctx.moveTo(x, y);
      }
      ctx.strokeStyle = j === 1 ? `rgba(214,165,69,${0.20 * P.swirl})` : `rgba(80,150,235,${0.22 * P.swirl})`;
      ctx.lineWidth = 1.4; ctx.stroke();
    }
    if (P.orbit > 0.02) [0.5, -0.35, 1.2].forEach((tl, j) => {
      ctx.save(); ctx.translate(cx, cy); ctx.rotate(tl);
      ctx.beginPath(); ctx.ellipse(0, 0, R * 1.7, R * 0.55, 0, 0, 7);
      ctx.strokeStyle = `rgba(106,177,240,${0.12 * P.orbit})`; ctx.lineWidth = 1; ctx.stroke();
      for (let k2 = 0; k2 < 4; k2++) {
        const a = t * (0.6 + 0.13 * j) + k2 * 1.57 + j;
        const x = Math.cos(a) * R * 1.7, y = Math.sin(a) * R * 0.55;
        const gold = (k2 + j) % 3 === 0;
        ctx.beginPath(); ctx.arc(x, y, gold ? 2.2 : 1.6, 0, 7);
        ctx.fillStyle = gold ? `rgba(214,165,69,${0.9 * P.orbit})` : `rgba(140,190,245,${0.8 * P.orbit})`;
        ctx.shadowColor = gold ? '#d6a545' : '#4d9bea'; ctx.shadowBlur = 8; ctx.fill(); ctx.shadowBlur = 0;
      }
      ctx.restore();
    });
    const amb = (1 - 0.5 * P.orbit) * energie * (settled < 0.35 ? 0 : settled);
    if (amb > 0.001) this.pts.forEach(p => {
      p.a += p.sp * (1 + P.swirl * 1.8) * (this.reduced.matches ? 0 : dt);
      const rr = p.r * R / 86 * (1 + 0.06 * Math.sin(t * 0.9 + p.ph));
      const x = cx + Math.cos(p.a) * rr, y = cy + Math.sin(p.a) * rr * 0.96;
      const al = (0.22 + 0.42 * Math.abs(Math.sin(t * 0.7 + p.ph))) * amb;
      ctx.beginPath(); ctx.arc(x, y, p.sz, 0, 7);
      ctx.fillStyle = p.gold ? `rgba(214,165,69,${al})` : `rgba(120,180,245,${al})`; ctx.fill();
    });
    ctx.save();
    ctx.shadowColor = '#2f8ae0'; ctx.shadowBlur = 18;
    ctx.beginPath(); ctx.arc(cx, cy, R, 0, 7); ctx.strokeStyle = 'rgba(90,160,235,0.85)'; ctx.lineWidth = 2; ctx.stroke();
    ctx.beginPath(); ctx.arc(cx, cy, R * 1.22, t * 0.3, t * 0.3 + 4.4); ctx.strokeStyle = 'rgba(70,140,220,0.28)'; ctx.lineWidth = 1.2; ctx.stroke();
    ctx.beginPath(); ctx.arc(cx, cy, R * 0.82, -t * 0.42, -t * 0.42 + 3.6); ctx.strokeStyle = 'rgba(120,185,245,0.30)'; ctx.lineWidth = 1.2; ctx.stroke();
    ctx.shadowColor = '#d6a545'; ctx.shadowBlur = 16;
    ctx.beginPath(); ctx.arc(cx, cy, R * 1.06, t * 0.55, t * 0.55 + 1.7); ctx.strokeStyle = 'rgba(214,165,69,0.75)'; ctx.lineWidth = 1.8; ctx.stroke();
    ctx.shadowColor = '#2f8ae0'; ctx.shadowBlur = 12; ctx.lineCap = 'round';
    ctx.beginPath(); ctx.arc(cx + R * 0.1, cy + R * 0.12, R * 1.34, -2.75, -1.45); ctx.strokeStyle = 'rgba(106,177,240,0.35)'; ctx.lineWidth = 3; ctx.stroke();
    ctx.beginPath(); ctx.arc(cx, cy - R * 0.25, R * 1.28, 0.75, 2.4); ctx.strokeStyle = 'rgba(106,177,240,0.28)'; ctx.lineWidth = 2.2; ctx.stroke();
    ctx.restore();
    g = ctx.createRadialGradient(cx, cy, 0, cx, cy, R);
    g.addColorStop(0, 'rgba(40,90,160,0.30)'); g.addColorStop(0.8, 'rgba(25,55,105,0.12)'); g.addColorStop(1, 'rgba(20,45,90,0)');
    ctx.beginPath(); ctx.arc(cx, cy, R, 0, 7); ctx.fillStyle = g; ctx.fill();
    if (P.wave > 0.02) {
      const n = 26, scale = Math.min(1, w * .8 / 203), bw = 3 * scale, gap = 5 * scale, total = n * (bw + gap) - gap, y0 = h * 0.78;
      const wenv = liveLevel ?? (M === 'sprechen' ? env : 0.5 + 0.3 * Math.sin(t * 3.7));
      ctx.fillStyle = `rgba(106,177,240,${0.30 + 0.5 * P.wave})`;
      for (let i = 0; i < n; i++) {
        const x = cx - total / 2 + i * (bw + gap);
        const center = 1 - Math.abs(i - (n - 1) / 2) / ((n - 1) / 2);
        const bh = P.wave * (3 + 30 * Math.abs(Math.sin(i * 0.9 + t * 6)) * wenv * (0.35 + 0.65 * center));
        ctx.fillRect(x, y0 - bh / 2, bw, bh);
      }
    }
    ctx.globalAlpha = 1;
    const logoW = Math.min(w,h) * .235 * 1.283;
    for (const el of [this.mimg, this.gimg]) {
      el.style.width = logoW + 'px'; el.style.left = cx + 'px'; el.style.top = cy + 'px';
      el.style.transform = 'translate(-50%, -50%) scale(' + (1.02 - .02 * Math.cos(t * 2 * Math.PI / 3.4)) + ')';
    }
    // Logo-Geometrie (aus gemessener SVG): Gesichtskreis-Zentrum ≈ Bildmitte, Brillenlinie ~R*0.14 darunter
    const cyH = cy + R * 0.02;           // Zentrum des gezeichneten Kopf-Kreises = Logo-Gesichtszentrum
    const headR = R * 0.66;              // Radius passend zum Logo-Gesichtskreis
    const glassY = cy + R * 0.14;        // exakte Brillenlinie des Maskottchens (gemessen)
    if (settled < 1) {
      const fade = 1 - ignite;
      // 1) Blauer Kopf-Kreis zeichnet sich von oben beidseitig herab — unten schließt sich das Logo-Lächeln
      if (headp > 0 && fade > 0) {
        const hp = ease(headp);
        ctx.save(); ctx.shadowColor = '#2f8ae0'; ctx.shadowBlur = 20; ctx.lineCap = 'round';
        ctx.lineWidth = Math.max(6, R * 0.075); ctx.strokeStyle = `rgba(72,148,232,${0.95 * fade})`;
        ctx.beginPath(); ctx.arc(cx, cyH, headR, -1.5708, -1.5708 + 3.1416 * hp); ctx.stroke();
        ctx.beginPath(); ctx.arc(cx, cyH, headR, -1.5708, -1.5708 - 3.1416 * hp, true); ctx.stroke();
        ctx.restore();
      }
      // 2) Haar-Swoosh: kurze Locke oben am Kopf (wie die Haartolle im Logo)
      if (swoosh > 0 && fade > 0) {
        const s = ease(swoosh);
        ctx.save(); ctx.shadowColor = '#6ab1f0'; ctx.shadowBlur = 18; ctx.lineCap = 'round';
        ctx.beginPath(); ctx.arc(cx, cyH, headR * 0.92, -2.5, -2.5 + 1.1 * s);
        ctx.strokeStyle = `rgba(120,185,245,${0.9 * fade})`; ctx.lineWidth = Math.max(5, R * 0.065); ctx.stroke(); ctx.restore();
      }
      // 3) Gold-Lichtstrahl verdichtet sich waagerecht auf der echten Brillenlinie — keine Einzelpunkte, keine Augen
      if (flash > 0 && flash < 1 && fade > 0) {
        const gy = glassY;
        const spread = (1 - ease(flash)) * headR * 1.5 + headR * 0.6;
        const a = Math.sin(Math.PI * flash) * 0.7 * fade;
        ctx.save();
        const lg = ctx.createLinearGradient(cx - spread, gy, cx + spread, gy);
        lg.addColorStop(0, 'rgba(214,165,69,0)'); lg.addColorStop(0.5, `rgba(230,185,90,${a})`); lg.addColorStop(1, 'rgba(214,165,69,0)');
        ctx.strokeStyle = lg; ctx.lineWidth = 3; ctx.lineCap = 'round'; ctx.shadowColor = '#d6a545'; ctx.shadowBlur = 16;
        ctx.beginPath(); ctx.moveTo(cx - spread, gy); ctx.lineTo(cx + spread, gy); ctx.stroke();
        ctx.restore();
        const fg = ctx.createRadialGradient(cx, gy, 0, cx, gy, headR * 1.1);
        fg.addColorStop(0, `rgba(214,165,69,${0.28 * a})`); fg.addColorStop(1, 'rgba(214,165,69,0)');
        ctx.fillStyle = fg; ctx.fillRect(0, 0, w, h);
      }
      // 4) Zündung: Schockwelle
      if (ignite > 0 && ignite < 1) {
        [0, 0.12].forEach((d, i) => {
          const k = Math.max(0, Math.min(1, ignite - d)); if (!k) return;
          ctx.beginPath(); ctx.arc(cx, cy, R * (0.7 + k * 3.6), 0, 7);
          ctx.strokeStyle = i ? `rgba(214,165,69,${0.5 * (1 - k)})` : `rgba(106,177,240,${0.55 * (1 - k)})`;
          ctx.lineWidth = i ? 1.8 : 2.6; ctx.stroke();
        });
      }
    }
    // Brille snappt am Ende der Funken-Phase entschieden ein; Vollgesicht übernimmt bei Zündung
    const full = true;
    const gOp = ease(Math.max(0, Math.min(1, (flash - 0.45) / 0.4)));
    const mOp = ease(ignite);
    if (this.mimg) this.mimg.style.opacity = String(full ? mOp : 0);
    if (this.gimg) this.gimg.style.opacity = String(full ? Math.max(0, gOp - mOp) : Math.max(gOp, mOp));
  };
}
