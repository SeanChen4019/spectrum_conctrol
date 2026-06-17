"""
Generate pre-baked waterfall timeline for the 3-scene demo.
Each scene shows: interference onset → decision delay (overlap) → anti-jam response → recovery.
"""
from __future__ import annotations

import json, math, random
from pathlib import Path

OUT = Path(__file__).resolve().parents[1] / "outputs" / "demo_waterfall.json"
N, NC = 2048, 10


def base_spectrum(comm_ch, jam_channels, rng):
    sp = []
    center_bin = (comm_ch + 0.5) * N / NC
    ch_width = N / NC
    for i in range(N):
        ch = min(i * NC // N, NC - 1)
        amp = -88.0 + rng.gauss(0, 1.5)
        # Main comm peak
        rel = (i - center_bin) / ch_width
        if abs(rel) < 2.0:
            main = 26.0 * math.exp(-0.5 * (rel / 0.42) ** 2)
            ripple = 3.0 * math.sin(i * 0.15) * math.exp(-0.5 * (rel / 0.7) ** 2)
            amp += main + ripple + rng.gauss(0, 1.2)
        # Faint sidelobe tail bleeding into adjacent channels
        if 1.0 <= abs(rel) < 2.5:
            tail = 5.0 * math.exp(-0.5 * ((abs(rel) - 1.0) / 0.5) ** 2)
            amp += tail + rng.gauss(0, 0.6)
        # Jam channels
        if ch in jam_channels:
            amp += 10.0 + rng.gauss(0, 2.5)
            if rng.random() < 0.15:
                amp += rng.uniform(5, 12)
        sp.append(round(amp, 3))
    return sp


def perturb(base, frame, rng):
    result = []
    fade = [0.85 + 0.15 * math.sin(frame * 0.04 + i * 0.03) for i in range(N)]
    for i, b in enumerate(base):
        signal = (b + 88.0) * fade[i]
        noise = rng.gauss(0, 1.8)
        result.append(round(-88.0 + signal + noise, 3))
    return result


def gen_constellation(snr, frame, rng):
    """QPSK constellation with realistic AWGN + phase noise."""
    cn = 400
    # SNR-dependent noise: lower SNR → wider scatter
    noise_std = 0.08 if snr > 22 else 0.18 if snr > 15 else 0.35 if snr > 8 else 0.6
    ci, cq = [], []
    for i in range(cn):
        ix = 1 if i % 4 in (0, 1) else -1
        qx = 1 if i % 4 in (0, 2) else -1
        # Add Gaussian noise
        ni = ix + rng.gauss(0, noise_std)
        nq = qx + rng.gauss(0, noise_std)
        # Occasional deep fade (1 in 30 points)
        if rng.random() < 0.03:
            fade = rng.uniform(0.15, 0.5)
            ni *= fade
            nq *= fade
        # Phase rotation (simulates residual carrier offset)
        phase = rng.gauss(0, 0.04 if snr > 15 else 0.12)
        rot_i = ni * math.cos(phase) - nq * math.sin(phase)
        rot_q = ni * math.sin(phase) + nq * math.cos(phase)
        ci.append(round(rot_i, 4))
        cq.append(round(rot_q, 4))
    return ci, cq


def gen_time_domain(frame, rng):
    """670 unique 500-sample waveforms from image pixel data.
    Strategy: pre-generate all 670 slices at random non-overlapping positions
    from the full image, then each frame picks its pre-assigned slice."""
    total_pts = 500
    dt = 0.002
    t = [round(i * dt, 4) for i in range(total_pts)]

    if not hasattr(gen_time_domain, "_slices"):
        try:
            from PIL import Image, ImageFilter
            import os
            img_path = os.path.join(
                os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
                "transmitter_and_receive", "发射机-http", "p2.jpg")
            img = Image.open(img_path).convert("L")
            # Edge-detect to get sharp transients
            edges = img.filter(ImageFilter.FIND_EDGES)
            # Use edges as the base — they have large amplitude jumps
            w, h = edges.size
            flat = list(edges.getdata())
            gen_time_domain._slices = []

            # Generate 670 unique, mostly non-overlapping slices
            r = random.Random(2026060301)
            total_needed = 670
            step = max(1, (len(flat) - total_pts) // total_needed)
            for idx in range(total_needed):
                start = idx * step + r.randint(0, max(1, step - total_pts))
                start = min(start, len(flat) - total_pts)
                start = max(0, start)
                vals = [flat[start + i] / 255.0 * 0.88 + 0.02 for i in range(total_pts)]
                gen_time_domain._slices.append(vals)
        except Exception as e:
            print(f"[time_domain] image failed: {e}")
            gen_time_domain._slices = []

    slices = gen_time_domain._slices
    amp = []
    if slices and frame < len(slices):
        for v in slices[frame]:
            amp.append(round(v + random.gauss(0, 0.005), 4))
    else:
        for i in range(total_pts):
            amp.append(round(0.5 * math.sin(i * 0.6 + frame) ** 2 + 0.2 + rng.uniform(0, 0.5), 4))

    return t, amp


def build_status(snr, mode_name, mod, carrier, frame):
    return {
        "data_rec_valid": "有效" if snr > 12 else "弱信号保持",
        "rx_mode_name": "AI Agent通信",
        "current_send_mode": mode_name,
        "current_mod": mod,
        "center_frequency": carrier * 1e9,
        "samp_rate": 390625.0,
        "snr": f"{snr + random.gauss(0, 0.3):.2f} dB",
        "mes_valid": f"判决: {mode_name}",
        "mes_rate": round(120000.0 + random.gauss(0, 8000) + 5000 * math.sin(frame * 0.07), 1),
        "power_gain": "14 dB",
        "carrier_gain": f"{carrier:.2f} GHz",
        "ber": f"{max(1e-6, 10 ** (-snr / 4.5)):.2e}",
        "fb_health": f"{(1 - max(0, (15 - snr) / 25)) * 100:.1f}%",
        "current_time": f"{frame * 0.12:.1f}s",
        "received_text": f"通信正常 | {mode_name}",
    }


def main():
    rng = random.Random(2026060301)
    timeline = []
    global_frame = 0

    # ═══════════════════════════════════════════════════════════
    # Phase 0: 正常通信 (ch5, no jam)
    # ═══════════════════════════════════════════════════════════
    base = base_spectrum(5, [], rng)
    for f in range(60):
        timeline.append(_frame("normal", f, base, 5, rng, 25.0, "常规模式", "QPSK", 3.0))
        global_frame += 1

    # ═══════════════════════════════════════════════════════════
    # Phase 1: 基站退服 — ch5坚守, jam ch0-5 (8MHz宽带)
    #           ch5=黑色重叠, 低速抗扰硬扛
    # ═══════════════════════════════════════════════════════════
    base = base_spectrum(5, list(range(10)), rng)
    for f in range(200):
        timeline.append(_frame("base_station", f, base, 5, rng, 8.5, "低速抗扰模式", "BPSK+扩频", 3.0))
        global_frame += 1

    # ═══════════════════════════════════════════════════════════
    # Phase 2: 救援拥塞 — jam ch4-5 (4MHz多音)
    #   A: 决策延迟 ch5绿→黑 ch4-5干扰 通信仍在ch5硬撑10帧
    #   B: 切频完成 ch7绿 ch4-5红
    # ═══════════════════════════════════════════════════════════
    jam_ch = [4, 5]
    base_overlap = base_spectrum(5, jam_ch, rng)
    for f in range(12):
        timeline.append(_frame("rescue_overlap", f, base_overlap, 5, rng, 10.0, "常规模式", "QPSK", 3.0))
        global_frame += 1

    base_hop = base_spectrum(7, jam_ch, rng)
    for f in range(188):
        timeline.append(_frame("rescue_recovery", f, base_hop, 7, rng, 24.0, "切频模式", "QPSK", 3.5))
        global_frame += 1

    # ═══════════════════════════════════════════════════════════
    # Phase 3: 无人机压测 — jam ch5-8 (6MHz宽带 20dB)
    #   A: 决策延迟 ch7绿→黑 ch5-8干扰 通信仍在ch7硬撑10帧
    #   B: 切频完成 ch2绿 ch5-8红
    # ═══════════════════════════════════════════════════════════
    jam_ch = [5, 6, 7, 8]
    base_overlap = base_spectrum(7, jam_ch, rng)
    for f in range(12):
        timeline.append(_frame("uav_overlap", f, base_overlap, 7, rng, 6.0, "常规模式", "QPSK", 3.5))
        global_frame += 1

    base_hop = base_spectrum(2, jam_ch, rng)
    for f in range(198):
        timeline.append(_frame("uav_recovery", f, base_hop, 2, rng, 24.0, "切频模式", "QPSK", 3.0))
        global_frame += 1

    OUT.parent.mkdir(parents=True, exist_ok=True)
    with OUT.open("w") as f:
        json.dump(timeline, f, ensure_ascii=False)

    total = len(timeline)
    print(f"[OK] {OUT} — {total} frames ({total * 0.12:.0f}s total)")
    # Print scene boundaries
    scenes = {}
    for i, fd in enumerate(timeline):
        s = fd["scene"]
        if s not in scenes: scenes[s] = [i, i]
        scenes[s][1] = i + 1
    for s, (a, b) in scenes.items():
        print(f"  {s}: frames {a}..{b-1} ({b-a}f)")


def _frame(scene_id, local_f, base, comm_ch, rng, snr, mode, mod, carrier):
    spectrum = perturb(base, local_f, rng)
    ci, cq = gen_constellation(snr, local_f, rng)
    t_vals, t_amp = gen_time_domain(local_f, rng)
    status = build_status(snr, mode, mod, carrier, local_f)
    return {
        "scene": scene_id, "frame": local_f,
        "spectrum": spectrum,
        "waterfall_line": spectrum,
        "waterfall_linear": [10 ** (v / 20.0) for v in spectrum],
        "constellation_i": ci, "constellation_q": cq,
        "time_domain_t": t_vals, "time_domain_amp": t_amp,
        "status": status,
    }


if __name__ == "__main__":
    main()
