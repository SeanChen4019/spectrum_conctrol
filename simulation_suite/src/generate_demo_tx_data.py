"""
Generate pre-baked TX-side data matching the 3-scene demo.
- TX time domain: same image-pixel waveform as RX (600 pts)
- Signalling time domain: text-derived waveform (420 pts)
- TX spectrum: FFT of time-domain waveform
"""
from __future__ import annotations

import json, math, random, os, struct
from pathlib import Path

OUT = Path(__file__).resolve().parents[1] / "outputs" / "demo_tx_data.json"
N_SP = 512    # spectrum points
N_TD = 600    # time domain
N_SIG = 420   # signalling time domain

# ── Text payload for signalling waveform ────────────────────
SIGNAL_TEXT = (
    "应急通信保障请求：A区房屋倒塌严重，急需救援力量。"
    "B区可设临时指挥所。通信链路正在抗干扰切换中..."
)
SIGNAL_BYTES = list(SIGNAL_TEXT.encode("utf-8"))  # ~100 bytes
# Repeat to fill 420 points
SIGNAL_STREAM = (SIGNAL_BYTES * 5)[:N_SIG]  # 420 values

SCENE_FLOW = [
    ("normal",           60,  5,  [],      25.0, "常规模式",    "QPSK/BPSK自适应", 3.0, 14, "救援1.png"),
    ("base_station",    200,  5,  [0,1,2,3,4,5], 8.5, "低速抗扰模式", "BPSK+扩频",      3.0, 14, "救援2.png"),
    ("rescue_overlap",   10,  5,  [4,5],   10.0, "常规模式",    "QPSK/BPSK自适应", 3.0, 14, "救援3.png"),
    ("rescue_recovery", 190,  7,  [4,5],   24.0, "切频模式",    "QPSK跳频",        3.5, 16, "救援4.png"),
    ("uav_overlap",      10,  7,  [5,6,7,8], 6.0, "增益补偿模式", "QPSK增益提升",    3.5, 22, "救援5.png"),
    ("uav_recovery",    200,  2,  [5,6,7,8], 24.0, "切频模式",    "QPSK关键帧",      3.0, 16, "救援7.png"),
    ("epilogue",         30,  2,  [5,6,7,8], 24.0, "切频模式",    "QPSK关键帧",      3.0, 16, "救援7.png"),
]


def load_image_waveform():
    """Load image edge data, return list of N_TD-point slices."""
    try:
        from PIL import Image, ImageFilter
        img_path = os.path.join(
            os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
            "transmitter_and_receive", "发射机-http", "p2.jpg")
        img = Image.open(img_path).convert("L")
        edges = img.filter(ImageFilter.FIND_EDGES)
        flat = list(edges.getdata())
        total_needed = 700
        slices = []
        r = random.Random(2026060301)
        step = max(1, (len(flat) - N_TD) // total_needed)
        for idx in range(total_needed):
            start = idx * step + r.randint(0, max(1, step - N_TD))
            start = min(max(0, start), len(flat) - N_TD)
            vals = [flat[start + i] / 255.0 * 0.85 + 0.02 for i in range(N_TD)]
            slices.append(vals)
        return slices
    except Exception as e:
        print(f"[tx_data] image load: {e}")
        return None


def gen_time_domain(slices, frame, rng):
    t = [round(i * 0.002, 4) for i in range(N_TD)]
    if slices and frame < len(slices):
        amp = [round(v + rng.gauss(0, 0.005), 4) for v in slices[frame]]
    else:
        amp = [round(abs(math.sin(i * 0.08 + frame * 0.03)) * 0.7, 4) for i in range(N_TD)]
    return {"time": t, "amp": amp}


def gen_spectrum(td_amp):
    """Compute FFT magnitude of time-domain waveform → 512-point spectrum."""
    N = N_SP
    s = td_amp[:N] if len(td_amp) >= N else td_amp + [0] * (N - len(td_amp))
    re_vals = [0.0] * N
    im_vals = [0.0] * N
    for k in range(N):
        for n in range(N):
            angle = -2 * math.pi * k * n / N
            re_vals[k] += s[n] * math.cos(angle)
            im_vals[k] += s[n] * math.sin(angle)
    half = N // 2
    spectrum = []
    for k in range(half, N):
        mag = math.sqrt(re_vals[k] * re_vals[k] + im_vals[k] * im_vals[k]) / N * 200
        db_like = -75 + 20 * math.log10(max(0.001, mag * 10))
        spectrum.append(round(db_like, 3))
    for k in range(0, half):
        mag = math.sqrt(re_vals[k] * re_vals[k] + im_vals[k] * im_vals[k]) / N * 200
        db_like = -75 + 20 * math.log10(max(0.001, mag * 10))
        spectrum.append(round(db_like, 3))
    freq = [round(-195 + 390 * i / (N - 1), 3) for i in range(N)]
    return {"freq": freq, "amp": spectrum}


def gen_sig_time(frame, rng):
    """Signalling waveform from text bytes — 420 pts."""
    t = [round(i * 0.002, 4) for i in range(N_SIG)]
    amp = []
    for i in range(N_SIG):
        # Byte value 0-255 → amplitude 0.05-0.75
        val = SIGNAL_STREAM[i] / 255.0 * 0.70 + 0.05
        # Rotate bytes every 10 frames so the stream looks different over time
        offset = (frame * 2) % len(SIGNAL_STREAM)
        idx = (i + offset) % len(SIGNAL_STREAM)
        val2 = SIGNAL_STREAM[idx] / 255.0 * 0.70 + 0.05
        # Mix current + rotated for variety
        mixed = val * 0.7 + val2 * 0.3 + rng.gauss(0, 0.02)
        amp.append(round(mixed, 4))
    # Add carrier ripple
    for i in range(N_SIG):
        ripple = 0.08 * abs(math.sin(i * 1.6 + frame * 0.1))
        amp[i] = round(max(0.01, amp[i] + ripple), 4)
    return {"time": t, "amp": amp}


def gen_rx_const(frame, rng):
    const_n = 300
    noise = 0.12 + 0.03 * math.sin(frame * 0.05)
    ci, cq = [], []
    for i in range(const_n):
        ix = 1 if i % 4 in (0, 1) else -1
        qx = 1 if i % 4 in (0, 2) else -1
        ci.append(round(ix + noise * rng.gauss(0, 1), 4))
        cq.append(round(qx + noise * rng.gauss(0, 1), 4))
    return {"i": ci, "q": cq}


def build_status(scene, mode_name, mod, carrier, gain, frame, img_file):
    task_map = {
        "normal": "AI Agent通信 | 现场图片回传",
        "base_station": "AI Agent通信 | 短文本摘要回传（降级）",
        "rescue_overlap": "AI Agent通信 | 多终端图传与状态包",
        "rescue_recovery": "AI Agent通信 | 多终端图传（信道8）",
        "uav_overlap": "AI Agent通信 | 无人机视频回传",
        "uav_recovery": "AI Agent通信 | 关键帧图片回传（降级）",
        "epilogue": "AI Agent通信 | 灾情勘察完成",
    }
    progress = {
        "normal": 0.3, "base_station": 0.5, "rescue_overlap": 0.55,
        "rescue_recovery": 0.75, "uav_overlap": 0.78, "uav_recovery": 0.95,
        "epilogue": 1.0,
    }
    return {
        "tx_valid": "有效",
        "tx_mod": mod,
        "tx_mode": task_map.get(scene, "AI Agent通信"),
        "tx_carrier": f"{carrier:.2f} GHz",
        "tx_samp": "390.62 kHz",
        "tx_gain": f"{gain} dB",
        "tx_image": img_file,
        "rx_state": f"反向参数链路: {mode_name}",
        "rx_carrier": "1.45 GHz",
        "rx_tx_gain": f"{gain} dB",
        "rx_tx_carrier": f"{carrier:.2f} GHz",
        "fb_health": "99.2% 仿真反馈",
        "time": f"仿真时间: {frame * 0.12:.2f}s",
        "progress": progress.get(scene, 0.5),
    }


def main():
    rng = random.Random(2026060301)
    slices = load_image_waveform()
    print(f"[tx_data] image waveform slices: {len(slices) if slices else 'N/A'}")
    timeline = []
    global_frame = 0

    for scene_id, n_frames, comm_ch, jam_ch, snr, mode_name, mod, carrier, gain, img_file in SCENE_FLOW:
        for f in range(n_frames):
            td = gen_time_domain(slices, global_frame, rng)
            spec = gen_spectrum(td["amp"])
            fd = {
                "scene": scene_id,
                "frame": f,
                "tx_spec": spec,
                "tx_time": td,
                "rx_const": gen_rx_const(global_frame, rng),
                "rx_time": gen_sig_time(global_frame, rng),
                "status": build_status(scene_id, mode_name, mod, carrier, gain, f, img_file),
                "sending_image": "p2.jpg",
            }
            timeline.append(fd)
            global_frame += 1

    OUT.parent.mkdir(parents=True, exist_ok=True)
    with OUT.open("w") as f:
        json.dump(timeline, f, ensure_ascii=False)

    print(f"[OK] {OUT} — {len(timeline)} frames")
    scenes = {}
    for i, fd in enumerate(timeline):
        s = fd["scene"]
        if s not in scenes: scenes[s] = [i, i]
        scenes[s][1] = i + 1
    for s, (a, b) in scenes.items():
        print(f"  {s}: {a}..{b-1} ({b-a}f)")


if __name__ == "__main__":
    main()
