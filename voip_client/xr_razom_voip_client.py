#!/usr/bin/env python3
"""
xrRazom VOIP Client & Host
--------------------------
Companion 3D Proximity + Walkie-Talkie VOIP application for
S.T.A.L.K.E.R. Anomaly 1.5.3 / G.A.M.M.A. with xrRazom Co-op.

Reads atomic telemetry from `$app_data_root$/voip_telemetry.json` written by
`xr_razom_voip_telemetry.script`:
  - Automatic 3D spatial proximity voice (no PTT required) with distance
    attenuation, stereo azimuth panning + ITD, air absorption, and wall occlusion.
  - Walkie-talkie radio voice when holding the in-game PTT key (default Z),
    featuring classic bandpass compression, radio static hiss, PTT pling chirp,
    and squelch tail.
  - Simultaneous proximity + walkie-talkie mixing ("double audio") when a
    nearby player transmits on radio.
"""

from __future__ import annotations

import argparse
from collections import deque
import ctypes
import glob
import json
import math
import os
import queue
import socket
import struct
import sys
import threading
import time
import zlib
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

import numpy as np
from scipy import signal

try:
    import sounddevice as sd
except Exception:
    sd = None

try:
    import tkinter as tk
    from tkinter import filedialog, ttk
except Exception:
    tk = None
    ttk = None
    filedialog = None


# ---------------------------------------------------------------------------
# Audio & Network Constants
# ---------------------------------------------------------------------------
SAMPLE_RATE = 24000
FRAME_SAMPLES = 480  # 20 ms per packet
CHANNELS_IN = 1
CHANNELS_OUT = 2
DEFAULT_PORT = 27016

# UDP Packet types
PKT_HELLO = 1
PKT_VOICE = 2
PKT_PING = 3
PKT_HELLO_ACK = 4
PKT_VOICE_RELAYED = 5

FLAG_RADIO_TX = 1 << 0
FLAG_HAS_RADIO = 1 << 1


def clamp(val: float, lo: float, hi: float) -> float:
    return lo if val < lo else (hi if val > hi else val)


def normalize(v: np.ndarray) -> np.ndarray:
    norm = float(np.linalg.norm(v))
    if norm < 1e-6:
        return np.zeros(3, dtype=np.float32)
    return v / norm


# ---------------------------------------------------------------------------
# PulseAudio / PipeWire Simple Backend (Zero pip dependencies on Linux)
# ---------------------------------------------------------------------------
class PulseSimpleBackend:
    """Thin ctypes wrapper around libpulse-simple.so.0 for Linux PipeWire/PulseAudio."""

    PA_SAMPLE_S16LE = 3
    PA_STREAM_PLAYBACK = 1
    PA_STREAM_RECORD = 2

    class _SampleSpec(ctypes.Structure):
        _fields_ = [
            ("format", ctypes.c_int),
            ("rate", ctypes.c_uint32),
            ("channels", ctypes.c_uint8),
        ]

    class _BufferAttr(ctypes.Structure):
        _fields_ = [
            ("maxlength", ctypes.c_uint32),
            ("tlength", ctypes.c_uint32),
            ("prebuf", ctypes.c_uint32),
            ("minreq", ctypes.c_uint32),
            ("fragsize", ctypes.c_uint32),
        ]

    def __init__(self, rate: int = SAMPLE_RATE, frame_samples: int = FRAME_SAMPLES):
        self.pa = ctypes.CDLL("libpulse-simple.so.0")
        self.pa.pa_simple_new.restype = ctypes.c_void_p
        self.pa.pa_simple_new.argtypes = [
            ctypes.c_char_p,
            ctypes.c_char_p,
            ctypes.c_int,
            ctypes.c_char_p,
            ctypes.c_char_p,
            ctypes.POINTER(self._SampleSpec),
            ctypes.c_void_p,
            ctypes.POINTER(self._BufferAttr),
            ctypes.POINTER(ctypes.c_int),
        ]
        self.pa.pa_simple_read.restype = ctypes.c_int
        self.pa.pa_simple_read.argtypes = [
            ctypes.c_void_p,
            ctypes.c_void_p,
            ctypes.c_size_t,
            ctypes.POINTER(ctypes.c_int),
        ]
        self.pa.pa_simple_write.restype = ctypes.c_int
        self.pa.pa_simple_write.argtypes = [
            ctypes.c_void_p,
            ctypes.c_void_p,
            ctypes.c_size_t,
            ctypes.POINTER(ctypes.c_int),
        ]
        self.pa.pa_simple_free.argtypes = [ctypes.c_void_p]
        self.pa.pa_strerror.restype = ctypes.c_char_p
        self.pa.pa_strerror.argtypes = [ctypes.c_int]

        self.rate = rate
        self.frame_samples = frame_samples
        self._lock = threading.Lock()
        self._rec = None
        self._play = None
        self._open_streams()

    def _open_streams(self) -> None:
        err = ctypes.c_int(0)
        in_bytes = self.frame_samples * 2  # mono int16
        out_bytes = self.frame_samples * 4  # stereo int16

        rec_spec = self._SampleSpec(self.PA_SAMPLE_S16LE, self.rate, CHANNELS_IN)
        rec_attr = self._BufferAttr(
            in_bytes * 6,
            0xFFFFFFFF,
            0xFFFFFFFF,
            0xFFFFFFFF,
            in_bytes,
        )
        self._rec = self.pa.pa_simple_new(
            None,
            b"xrRazomVOIP",
            self.PA_STREAM_RECORD,
            None,
            b"Microphone Capture",
            ctypes.byref(rec_spec),
            None,
            ctypes.byref(rec_attr),
            ctypes.byref(err),
        )
        if not self._rec:
            msg = self.pa.pa_strerror(err.value).decode("utf-8", "ignore")
            raise RuntimeError(f"PulseAudio record init failed: {msg}")

        play_spec = self._SampleSpec(self.PA_SAMPLE_S16LE, self.rate, CHANNELS_OUT)
        play_attr = self._BufferAttr(
            out_bytes * 8,
            out_bytes * 2,
            out_bytes,
            out_bytes,
            0xFFFFFFFF,
        )
        self._play = self.pa.pa_simple_new(
            None,
            b"xrRazomVOIP",
            self.PA_STREAM_PLAYBACK,
            None,
            b"3D & Radio VOIP Playback",
            ctypes.byref(play_spec),
            None,
            ctypes.byref(play_attr),
            ctypes.byref(err),
        )
        if not self._play:
            self.close()
            msg = self.pa.pa_strerror(err.value).decode("utf-8", "ignore")
            raise RuntimeError(f"PulseAudio playback init failed: {msg}")

    def read_frame(self) -> np.ndarray:
        buf = (ctypes.c_int16 * self.frame_samples)()
        err = ctypes.c_int(0)
        rc = self.pa.pa_simple_read(self._rec, buf, ctypes.sizeof(buf), ctypes.byref(err))
        if rc < 0:
            msg = self.pa.pa_strerror(err.value).decode("utf-8", "ignore")
            raise RuntimeError(f"PulseAudio read error: {msg}")
        return np.ctypeslib.as_array(buf).astype(np.float32) / 32768.0

    def write_frame(self, stereo_float: np.ndarray) -> None:
        clipped = np.clip(stereo_float, -1.0, 1.0)
        pcm = (clipped * 32767.0).astype(np.int16)
        err = ctypes.c_int(0)
        rc = self.pa.pa_simple_write(self._play, pcm.ctypes.data, pcm.nbytes, ctypes.byref(err))
        if rc < 0:
            msg = self.pa.pa_strerror(err.value).decode("utf-8", "ignore")
            raise RuntimeError(f"PulseAudio write error: {msg}")

    def close(self) -> None:
        with self._lock:
            if self._rec:
                self.pa.pa_simple_free(self._rec)
                self._rec = None
            if self._play:
                self.pa.pa_simple_free(self._play)
                self._play = None


class SoundDeviceBackend:
    """Cross-platform sounddevice backend (used primarily on Windows)."""

    def __init__(self, rate: int = SAMPLE_RATE, frame_samples: int = FRAME_SAMPLES):
        if sd is None:
            raise RuntimeError("sounddevice is not installed")
        self.rate = rate
        self.frame_samples = frame_samples
        self._in_stream = sd.InputStream(
            samplerate=rate,
            channels=CHANNELS_IN,
            dtype="float32",
            blocksize=frame_samples,
        )
        self._out_stream = sd.OutputStream(
            samplerate=rate,
            channels=CHANNELS_OUT,
            dtype="float32",
            blocksize=frame_samples,
        )
        self._in_stream.start()
        self._out_stream.start()

    def read_frame(self) -> np.ndarray:
        data, _ = self._in_stream.read(self.frame_samples)
        return data[:, 0].copy()

    def write_frame(self, stereo_float: np.ndarray) -> None:
        clipped = np.clip(stereo_float, -1.0, 1.0).astype(np.float32)
        self._out_stream.write(clipped)

    def close(self) -> None:
        for stream in (self._in_stream, self._out_stream):
            try:
                stream.stop()
                stream.close()
            except Exception:
                pass


def create_audio_backend(rate: int = SAMPLE_RATE, frame_samples: int = FRAME_SAMPLES):
    if sys.platform.startswith("linux"):
        try:
            return PulseSimpleBackend(rate, frame_samples)
        except Exception as exc:
            if sd is not None:
                return SoundDeviceBackend(rate, frame_samples)
            raise exc
    if sd is not None:
        return SoundDeviceBackend(rate, frame_samples)
    raise RuntimeError(
        "No audio backend available. On Windows, install: pip install numpy scipy sounddevice"
    )


# ---------------------------------------------------------------------------
# Radio Sound Synthesis & DSP Pipeline
# ---------------------------------------------------------------------------
def build_ptt_pling(sample_rate: int = SAMPLE_RATE) -> np.ndarray:
    """Synthesizes the crisp walkie-talkie PTT key-up 'pling' chirp + click."""
    dur = 0.072
    n = int(sample_rate * dur)
    t = np.arange(n, dtype=np.float32) / sample_rate

    # Two-step crisp digital walkie-talkie chirp (1280 Hz -> 1840 Hz)
    split = int(n * 0.44)
    tone = np.zeros(n, dtype=np.float32)
    tone[:split] = np.sin(2.0 * np.pi * 1280.0 * t[:split])
    tone[split:] = np.sin(2.0 * np.pi * 1840.0 * t[split:])

    env = np.ones(n, dtype=np.float32)
    attack = max(1, int(0.004 * sample_rate))
    decay = max(1, int(0.025 * sample_rate))
    env[:attack] = np.linspace(0.0, 1.0, attack, dtype=np.float32)
    env[-decay:] = np.linspace(1.0, 0.0, decay, dtype=np.float32)

    # Subtle key-switch mechanical transient at t=0
    rng = np.random.default_rng(42)
    click = rng.normal(0.0, 0.16, size=min(n, int(0.010 * sample_rate))).astype(np.float32)
    click *= np.linspace(1.0, 0.0, len(click), dtype=np.float32)

    out = tone * env * 0.28
    out[: len(click)] += click
    return out.astype(np.float32)


def build_squelch_tail(sample_rate: int = SAMPLE_RATE) -> np.ndarray:
    """Synthesizes the classic walkie-talkie squelch tail hiss + release click."""
    dur = 0.115
    n = int(sample_rate * dur)
    rng = np.random.default_rng(1337)
    noise = rng.normal(0.0, 1.0, size=n).astype(np.float32)
    sos = signal.butter(3, [450.0, 3100.0], btype="bandpass", fs=sample_rate, output="sos")
    filtered = signal.sosfilt(sos, noise).astype(np.float32)

    env = np.linspace(1.0, 0.0, n, dtype=np.float32) ** 1.4
    # Tiny PTT button release tone
    t = np.arange(min(n, int(0.028 * sample_rate)), dtype=np.float32) / sample_rate
    off_blip = np.sin(2.0 * np.pi * 940.0 * t) * np.linspace(0.18, 0.0, len(t), dtype=np.float32)

    out = filtered * env * 0.24
    out[: len(off_blip)] += off_blip
    return out.astype(np.float32)


# Precomputed shared SOS filter banks (24 kHz)
SOS_RADIO_BP = signal.butter(3, [380.0, 2750.0], btype="bandpass", fs=SAMPLE_RATE, output="sos")
SOS_OCCLUDE_LP = signal.butter(2, 1200.0, btype="lowpass", fs=SAMPLE_RATE, output="sos")
SOS_DIST_MID_LP = signal.butter(2, 3400.0, btype="lowpass", fs=SAMPLE_RATE, output="sos")
SOS_DIST_FAR_LP = signal.butter(2, 1850.0, btype="lowpass", fs=SAMPLE_RATE, output="sos")
SOS_STATIC_BP = signal.butter(2, [420.0, 3200.0], btype="bandpass", fs=SAMPLE_RATE, output="sos")

SFX_PTT_PLING = build_ptt_pling(SAMPLE_RATE)
SFX_SQUELCH_TAIL = build_squelch_tail(SAMPLE_RATE)


@dataclass
class PeerAudioState:
    """Per-speaker jitter queue and persistent DSP filter states."""

    uid: int
    name: str
    level: str = ""
    pos: np.ndarray = field(default_factory=lambda: np.zeros(3, dtype=np.float32))
    radio_tx: bool = False
    has_radio: bool = True
    last_seen: float = field(default_factory=time.monotonic)
    queue: deque = field(default_factory=lambda: deque(maxlen=10))
    prebuffering: bool = True
    empty_ticks: int = 0

    # Persistent filter states so 20ms frames join seamlessly without clicks
    zi_radio: np.ndarray = field(default_factory=lambda: signal.sosfilt_zi(SOS_RADIO_BP) * 0.0)
    zi_occlude: np.ndarray = field(default_factory=lambda: signal.sosfilt_zi(SOS_OCCLUDE_LP) * 0.0)
    zi_dist: np.ndarray = field(default_factory=lambda: signal.sosfilt_zi(SOS_DIST_MID_LP) * 0.0)
    zi_static: np.ndarray = field(default_factory=lambda: signal.sosfilt_zi(SOS_STATIC_BP) * 0.0)

    # Interaural time delay (ITD) & radio transit delay buffers
    itd_buf_l: np.ndarray = field(default_factory=lambda: np.zeros(16, dtype=np.float32))
    itd_buf_r: np.ndarray = field(default_factory=lambda: np.zeros(16, dtype=np.float32))
    radio_delay_buf: np.ndarray = field(default_factory=lambda: np.zeros(FRAME_SAMPLES, dtype=np.float32))

    # Edge-triggered radio sound overlay (pling / squelch tail)
    sfx_buf: np.ndarray = field(default_factory=lambda: np.zeros(0, dtype=np.float32))
    prev_radio_tx: bool = False
    rng: np.random.Generator = field(default_factory=lambda: np.random.default_rng())

    def enqueue_sfx(self, clip: np.ndarray) -> None:
        if len(self.sfx_buf) == 0:
            self.sfx_buf = clip.copy()
        else:
            max_len = max(len(self.sfx_buf), len(clip))
            merged = np.zeros(max_len, dtype=np.float32)
            merged[: len(self.sfx_buf)] += self.sfx_buf
            merged[: len(clip)] += clip
            self.sfx_buf = merged

    def pop_sfx_frame(self, n: int) -> np.ndarray:
        if len(self.sfx_buf) == 0:
            return np.zeros(n, dtype=np.float32)
        if len(self.sfx_buf) <= n:
            out = np.zeros(n, dtype=np.float32)
            out[: len(self.sfx_buf)] = self.sfx_buf
            self.sfx_buf = np.zeros(0, dtype=np.float32)
            return out
        out = self.sfx_buf[:n].copy()
        self.sfx_buf = self.sfx_buf[n:]
        return out


@dataclass
class ListenerState:
    name: str = "Player"
    level: str = ""
    is_host: bool = False
    in_session: bool = False
    last_ip: str = ""
    pos: np.ndarray = field(default_factory=lambda: np.zeros(3, dtype=np.float32))
    dir: np.ndarray = field(default_factory=lambda: np.array([0.0, 0.0, 1.0], dtype=np.float32))
    top: np.ndarray = field(default_factory=lambda: np.array([0.0, 1.0, 0.0], dtype=np.float32))
    has_radio: bool = True
    radio_tx: bool = False
    peers: List[dict] = field(default_factory=list)
    file_mtime: float = 0.0
    file_ok: bool = False


class SpatialRadioMixer:
    """Computes 3D proximity spatialization, walkie-talkie DSP, and simultaneous dual-path mixing."""

    def __init__(self):
        self.prox_volume = 1.0
        self.radio_volume = 0.85
        self.radio_static_level = 0.045
        self.prox_max_dist = 75.0
        self.prox_ref_dist = 5.5
        self.local_ptt_feedback = True

        self._local_sfx_buf = np.zeros(0, dtype=np.float32)
        self._local_prev_tx = False
        self._local_zi_static = signal.sosfilt_zi(SOS_STATIC_BP) * 0.0
        self._local_rng = np.random.default_rng(777)

    def update_local_tx_edges(self, radio_tx: bool, has_radio: bool) -> np.ndarray:
        active = bool(radio_tx and has_radio)
        if self.local_ptt_feedback:
            if active and not self._local_prev_tx:
                self._queue_local_sfx(SFX_PTT_PLING * 0.75)
            elif not active and self._local_prev_tx:
                self._queue_local_sfx(SFX_SQUELCH_TAIL * 0.70)
        self._local_prev_tx = active

        out = self._pop_local_sfx(FRAME_SAMPLES)
        if active and self.local_ptt_feedback and self.radio_static_level > 0.0:
            # Subtle comfort static while holding PTT
            noise = self._local_rng.normal(0.0, 1.0, size=FRAME_SAMPLES).astype(np.float32)
            hiss, self._local_zi_static = signal.sosfilt(SOS_STATIC_BP, noise, zi=self._local_zi_static)
            out += hiss.astype(np.float32) * (self.radio_static_level * 0.35 * self.radio_volume)

        return np.column_stack((out, out)).astype(np.float32)

    def _queue_local_sfx(self, clip: np.ndarray) -> None:
        if len(self._local_sfx_buf) == 0:
            self._local_sfx_buf = clip.copy()
        else:
            m = np.zeros(max(len(self._local_sfx_buf), len(clip)), dtype=np.float32)
            m[: len(self._local_sfx_buf)] += self._local_sfx_buf
            m[: len(clip)] += clip
            self._local_sfx_buf = m

    def _pop_local_sfx(self, n: int) -> np.ndarray:
        if len(self._local_sfx_buf) == 0:
            return np.zeros(n, dtype=np.float32)
        if len(self._local_sfx_buf) <= n:
            out = np.zeros(n, dtype=np.float32)
            out[: len(self._local_sfx_buf)] = self._local_sfx_buf
            self._local_sfx_buf = np.zeros(0, dtype=np.float32)
            return out
        out = self._local_sfx_buf[:n].copy()
        self._local_sfx_buf = self._local_sfx_buf[n:]
        return out

    @staticmethod
    def resolve_peer_telemetry(peer_state: PeerAudioState, listener: ListenerState) -> Tuple[np.ndarray, bool, bool]:
        """
        Resolves best position and occlusion for a remote speaker.
        Returns (target_pos, occluded, same_level).
        """
        if not listener.level:
            return peer_state.pos, False, False

        candidates = []
        target_name = peer_state.name.strip().lower()
        for entry in listener.peers:
            pname = str(entry.get("name", "")).strip().lower()
            if pname == target_name:
                pvec = entry.get("pos") or {}
                pos = np.array(
                    [float(pvec.get("x", 0.0)), float(pvec.get("y", 0.0)), float(pvec.get("z", 0.0))],
                    dtype=np.float32,
                )
                candidates.append((pos, bool(entry.get("occluded", False))))

        # If only 1 peer is in the local world and speaker name is still default "Player", match that peer
        if not candidates and len(listener.peers) == 1 and target_name in ("player", ""):
            entry = listener.peers[0]
            pvec = entry.get("pos") or {}
            pos = np.array(
                [float(pvec.get("x", 0.0)), float(pvec.get("y", 0.0)), float(pvec.get("z", 0.0))],
                dtype=np.float32,
            )
            candidates.append((pos, bool(entry.get("occluded", False))))

        pkt_same_level = bool(peer_state.level == listener.level and float(np.linalg.norm(peer_state.pos)) > 0.01)

        if candidates:
            if len(candidates) == 1:
                avatar_pos, best_occ = candidates[0]
            else:
                avatar_pos, best_occ = candidates[0]
                best_d = float(np.linalg.norm(avatar_pos - peer_state.pos))
                for pos, occ in candidates[1:]:
                    d = float(np.linalg.norm(pos - peer_state.pos))
                    if d < best_d:
                        best_d = d
                        avatar_pos, best_occ = pos, occ

            # Use 50 Hz packet cam_pos when it agrees closely with in-game avatar position;
            # otherwise trust the listener's own in-game avatar position so stale remote telemetry never locks audio.
            if pkt_same_level and float(np.linalg.norm(peer_state.pos - avatar_pos)) < 5.0:
                return peer_state.pos, best_occ, True
            if float(np.linalg.norm(avatar_pos)) > 0.01:
                return avatar_pos, best_occ, True

        if peer_state.level and peer_state.level != listener.level:
            return peer_state.pos, False, False

        if pkt_same_level:
            return peer_state.pos, False, True

        return peer_state.pos, False, False

    def compute_proximity_params(
        self, listener: ListenerState, speaker_pos: np.ndarray, occluded: bool, same_level: bool
    ) -> Tuple[float, float, float, int, int, float]:
        """Returns (gain_l, gain_r, dist, delay_l_samples, delay_r_samples, lowpass_blend)."""
        if not same_level:
            return 0.0, 0.0, 9999.0, 0, 0, 0.0

        delta = speaker_pos - listener.pos
        dist = float(np.linalg.norm(delta))
        if dist >= self.prox_max_dist:
            return 0.0, 0.0, dist, 0, 0, 0.0

        # Distance rolloff (gentler falloff so voices carry naturally outdoors)
        if dist <= self.prox_ref_dist:
            dist_gain = 1.0
        else:
            norm_d = (dist - self.prox_ref_dist) / max(1.0, self.prox_max_dist - self.prox_ref_dist)
            dist_gain = ((self.prox_ref_dist / dist) ** 0.75) * max(0.0, 1.0 - (norm_d ** 2.4))

        # X-Ray left-handed basis: +X right, +Y up, +Z forward => right = cross(top, dir)
        fwd = normalize(listener.dir)
        up = normalize(listener.top)
        right = normalize(np.cross(up, fwd))
        if float(np.linalg.norm(right)) < 1e-4:
            right = np.array([1.0, 0.0, 0.0], dtype=np.float32)

        if dist < 0.35:
            pan = 0.0
            front_dot = 1.0
        else:
            unit = delta / dist
            pan = clamp(float(np.dot(unit, right)), -1.0, 1.0)
            front_dot = clamp(float(np.dot(unit, fwd)), -1.0, 1.0)

        # Equal-power stereo panning with subtle ear crosstalk (8%)
        angle = (pan + 1.0) * 0.25 * math.pi
        raw_l = math.cos(angle)
        raw_r = math.sin(angle)
        pan_l = raw_l * 0.92 + raw_r * 0.08
        pan_r = raw_r * 0.92 + raw_l * 0.08

        # Rear-facing gentle dampening (behind your head sounds slightly softer)
        rear_gain = 1.0 if front_dot >= 0.0 else (1.0 + 0.12 * front_dot)

        # Wall occlusion gain penalty (moderate, keeps voices intelligible behind walls)
        occ_gain = 0.68 if occluded else 1.0

        total_gain = dist_gain * rear_gain * occ_gain * self.prox_volume

        # Interaural time delay (up to 12 samples = 0.5 ms)
        itd_samples = int(round(abs(pan) * 12.0))
        delay_l = itd_samples if pan > 0.0 else 0
        delay_r = itd_samples if pan < 0.0 else 0

        # Distance air absorption blend (0 = bright close, 1 = muffled distant)
        lp_blend = clamp((dist - 12.0) / max(1.0, self.prox_max_dist - 12.0), 0.0, 1.0)
        if front_dot < -0.2:
            lp_blend = max(lp_blend, 0.18)

        return pan_l * total_gain, pan_r * total_gain, dist, delay_l, delay_r, lp_blend

    @staticmethod
    def _apply_delay(frame: np.ndarray, buf: np.ndarray, delay: int) -> np.ndarray:
        if delay <= 0:
            buf[-16:] = frame[-16:]
            return frame
        combined = np.concatenate((buf[-delay:], frame[:-delay]))
        buf[-16:] = frame[-16:]
        return combined

    def render_peer_frame(
        self,
        peer: PeerAudioState,
        mono_frame: Optional[np.ndarray],
        listener: ListenerState,
        force_pos_override: Optional[Tuple[np.ndarray, bool, bool]] = None,
    ) -> np.ndarray:
        """
        Renders one 480-sample stereo frame for `peer`.
        Simultaneously mixes 3D proximity audio and walkie-talkie radio audio when both apply.
        """
        n = FRAME_SAMPLES
        dry = mono_frame if mono_frame is not None else np.zeros(n, dtype=np.float32)

        # Check PTT transitions for pling / squelch tail
        rx_active = bool(peer.radio_tx and peer.has_radio and listener.has_radio)
        if rx_active and not peer.prev_radio_tx:
            peer.enqueue_sfx(SFX_PTT_PLING)
        elif not rx_active and peer.prev_radio_tx:
            peer.enqueue_sfx(SFX_SQUELCH_TAIL)
        peer.prev_radio_tx = rx_active

        stereo_out = np.zeros((n, 2), dtype=np.float32)

        # --- Path 1: 3D Proximity Voice (always active when in range & same map) ---
        if mono_frame is not None:
            if force_pos_override is not None:
                tpos, occluded, same_level = force_pos_override
            else:
                tpos, occluded, same_level = self.resolve_peer_telemetry(peer, listener)

            gain_l, gain_r, _, delay_l, delay_r, lp_blend = self.compute_proximity_params(
                listener, tpos, occluded, same_level
            )

            if gain_l > 0.0005 or gain_r > 0.0005:
                prox_sig = dry
                if occluded:
                    prox_sig, peer.zi_occlude = signal.sosfilt(
                        SOS_OCCLUDE_LP, prox_sig, zi=peer.zi_occlude
                    )
                    prox_sig = prox_sig.astype(np.float32)
                elif lp_blend > 0.05:
                    sos_lp = SOS_DIST_FAR_LP if lp_blend > 0.55 else SOS_DIST_MID_LP
                    filtered, peer.zi_dist = signal.sosfilt(sos_lp, prox_sig, zi=peer.zi_dist)
                    prox_sig = (prox_sig * (1.0 - lp_blend) + filtered * lp_blend).astype(np.float32)
                else:
                    # Keep filter state warm without altering close signal
                    _, peer.zi_dist = signal.sosfilt(SOS_DIST_MID_LP, prox_sig, zi=peer.zi_dist)

                sig_l = self._apply_delay(prox_sig, peer.itd_buf_l, delay_l) * gain_l
                sig_r = self._apply_delay(prox_sig, peer.itd_buf_r, delay_r) * gain_r
                stereo_out[:, 0] += sig_l
                stereo_out[:, 1] += sig_r

        # --- Path 2: Walkie-Talkie Radio Voice (when speaker presses PTT) ---
        sfx_frame = peer.pop_sfx_frame(n)
        if rx_active or float(np.max(np.abs(sfx_frame))) > 1e-5:
            radio_mix = np.zeros(n, dtype=np.float32)
            if rx_active:
                # 20ms radio transit delay so nearby radio+proximity gives authentic double sound
                delayed_input = peer.radio_delay_buf.copy()
                peer.radio_delay_buf[:] = dry

                bp, peer.zi_radio = signal.sosfilt(SOS_RADIO_BP, delayed_input, zi=peer.zi_radio)
                compressed = np.tanh(bp.astype(np.float32) * 2.6) * 0.52

                # Classic walkie-talkie continuous static hiss
                noise = peer.rng.normal(0.0, 1.0, size=n).astype(np.float32)
                hiss, peer.zi_static = signal.sosfilt(SOS_STATIC_BP, noise, zi=peer.zi_static)

                radio_mix += compressed + hiss.astype(np.float32) * self.radio_static_level
            else:
                peer.radio_delay_buf.fill(0.0)

            radio_mix = (radio_mix + sfx_frame) * self.radio_volume
            stereo_out[:, 0] += radio_mix
            stereo_out[:, 1] += radio_mix
        else:
            peer.radio_delay_buf.fill(0.0)

        return stereo_out


# ---------------------------------------------------------------------------
# User Config & Telemetry Reader
# ---------------------------------------------------------------------------
CONFIG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "voip_config.json")


def load_user_config() -> dict:
    if not os.path.isfile(CONFIG_PATH):
        return {}
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8", errors="replace") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def save_user_config(cfg: dict) -> None:
    try:
        with open(CONFIG_PATH, "w", encoding="utf-8") as f:
            json.dump(cfg, f, indent=2)
    except OSError:
        pass


def discover_telemetry_path() -> str:
    cfg = load_user_config()
    saved = cfg.get("telemetry_path")
    if isinstance(saved, str) and saved.strip() and os.path.isfile(saved.strip()):
        return saved.strip()

    script_dir = os.path.dirname(os.path.abspath(__file__))
    candidates = [
        os.path.join(os.getcwd(), "appdata", "voip_telemetry.json"),
        os.path.join(os.getcwd(), "Anomaly", "appdata", "voip_telemetry.json"),
        os.path.normpath(os.path.join(script_dir, "..", "Anomaly", "appdata", "voip_telemetry.json")),
        os.path.normpath(os.path.join(script_dir, "..", "..", "Anomaly", "appdata", "voip_telemetry.json")),
        "../Anomaly/appdata/voip_telemetry.json",
        "../../Anomaly/appdata/voip_telemetry.json",
        "../../../Anomaly/appdata/voip_telemetry.json",
    ]
    if sys.platform.startswith("win"):
        candidates.extend(
            [
                r"C:\Anomaly\appdata\voip_telemetry.json",
                r"C:\Games\Anomaly\appdata\voip_telemetry.json",
                r"C:\STALKER\Anomaly\appdata\voip_telemetry.json",
                r"D:\Anomaly\appdata\voip_telemetry.json",
                r"D:\Games\Anomaly\appdata\voip_telemetry.json",
                r"D:\STALKER\Anomaly\appdata\voip_telemetry.json",
            ]
        )
    else:
        candidates.extend(
            [
                os.path.expanduser("~/Games/Anomaly/appdata/voip_telemetry.json"),
                os.path.expanduser("~/Games/STALKER/Anomaly/appdata/voip_telemetry.json"),
                os.path.expanduser("~/STALKER/Anomaly/appdata/voip_telemetry.json"),
            ]
        )

    for path in candidates:
        if os.path.isfile(path):
            return path
    return ""


def read_telemetry_file(path: str, state: ListenerState) -> None:
    if not path or not os.path.isfile(path):
        state.file_ok = False
        return
    try:
        st = os.stat(path)
        if st.st_mtime == state.file_mtime and state.file_ok:
            return
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            raw = f.read()
        if not raw:
            return
        data = json.loads(raw)
    except (FileNotFoundError, PermissionError, OSError, json.JSONDecodeError):
        return

    state.file_mtime = st.st_mtime
    state.file_ok = True
    state.name = str(data.get("name") or "Player")
    state.level = str(data.get("level") or "")
    state.is_host = bool(data.get("is_host", False))
    state.in_session = bool(data.get("in_session", False))
    state.last_ip = str(data.get("last_ip") or "")

    pos = data.get("pos") or {}
    dvec = data.get("dir") or {}
    tvec = data.get("top") or {}
    state.pos = np.array(
        [float(pos.get("x", 0.0)), float(pos.get("y", 0.0)), float(pos.get("z", 0.0))],
        dtype=np.float32,
    )
    state.dir = np.array(
        [float(dvec.get("x", 0.0)), float(dvec.get("y", 0.0)), float(dvec.get("z", 1.0))],
        dtype=np.float32,
    )
    state.top = np.array(
        [float(tvec.get("x", 0.0)), float(tvec.get("y", 1.0)), float(tvec.get("z", 0.0))],
        dtype=np.float32,
    )
    state.has_radio = bool(data.get("has_radio", True))
    state.radio_tx = bool(data.get("radio_tx", False))
    peers = data.get("peers")
    state.peers = peers if isinstance(peers, list) else []


# ---------------------------------------------------------------------------
# UDP Protocol & Transport Engine
# ---------------------------------------------------------------------------
def encode_voice_packet(
    seq: int,
    flags: int,
    name: str,
    level_name: str,
    pos: np.ndarray,
    pcm_float: np.ndarray,
) -> bytes:
    name_b = name.encode("utf-8", "replace")[:64]
    lvl_b = level_name.encode("utf-8", "replace")[:64]
    pcm_i16 = (np.clip(pcm_float, -1.0, 1.0) * 32767.0).astype("<i2").tobytes()
    header = struct.pack(
        "<BIBH",
        PKT_VOICE,
        seq & 0xFFFFFFFF,
        flags & 0xFF,
        len(name_b),
    )
    mid = struct.pack("<H", len(lvl_b))
    pos_b = struct.pack("<fff", float(pos[0]), float(pos[1]), float(pos[2]))
    return header + name_b + mid + lvl_b + pos_b + pcm_i16


def decode_voice_payload(payload: bytes) -> Optional[Tuple[int, int, str, str, np.ndarray, np.ndarray]]:
    """Decodes PKT_VOICE payload (starting after pkt_type byte)."""
    if len(payload) < 7:
        return None
    seq, flags, name_len = struct.unpack_from("<IBH", payload, 0)
    off = 7
    if len(payload) < off + name_len + 2:
        return None
    name = payload[off : off + name_len].decode("utf-8", "replace")
    off += name_len
    lvl_len = struct.unpack_from("<H", payload, off)[0]
    off += 2
    expected_pcm = FRAME_SAMPLES * 2
    if len(payload) < off + lvl_len + 12 + expected_pcm:
        return None
    lvl = payload[off : off + lvl_len].decode("utf-8", "replace")
    off += lvl_len
    px, py, pz = struct.unpack_from("<fff", payload, off)
    off += 12
    pcm = np.frombuffer(payload, dtype="<i2", count=FRAME_SAMPLES, offset=off).astype(np.float32) / 32768.0
    pos = np.array([px, py, pz], dtype=np.float32)
    return seq, flags, name, lvl, pos, pcm


class VoipEngine:
    """Manages audio threads, telemetry polling, UDP Host/Peer relay, and DSP mixing."""

    def __init__(self, telemetry_path: str):
        self.telemetry_path = telemetry_path
        self.listener = ListenerState()
        self.mixer = SpatialRadioMixer()

        self.mode = "host"  # "host" or "peer"
        self.host_ip = "127.0.0.1"
        self.port = DEFAULT_PORT

        self.mic_gain = 1.0
        self.noise_gate_rms = 0.012
        self.muted = False
        self.manual_ptt = False
        self.loopback_test = False
        self.loopback_distance = 8.0
        self.loopback_angle_deg = 45.0
        self.loopback_occluded = False

        self.running = False
        self.status_text = "Ready (not started)"
        self.mic_level = 0.0
        self.vad_open = False

        self._sock: Optional[socket.socket] = None
        self._backend = None
        self._peers: Dict[int, PeerAudioState] = {}
        self._clients: Dict[Tuple[str, int], Tuple[int, float]] = {}
        self._state_lock = threading.Lock()
        self._threads: List[threading.Thread] = []
        self._seq = 0
        self._vad_hold_frames = 0
        self._loopback_peer = PeerAudioState(uid=999999, name="LoopbackTest")

    def start(self, mode: str, host_ip: str, port: int) -> None:
        if self.running:
            self.stop()
        self.mode = mode
        self.host_ip = host_ip.strip() or "127.0.0.1"
        self.port = int(port)

        self._backend = create_audio_backend(SAMPLE_RATE, FRAME_SAMPLES)
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self._sock.settimeout(0.1)

        if self.mode == "host":
            self._sock.bind(("0.0.0.0", self.port))
            self.status_text = f"Host active on UDP :{self.port}"
        else:
            self._sock.bind(("0.0.0.0", 0))
            self.status_text = f"Connected to {self.host_ip}:{self.port}"

        self.running = True
        self._threads = [
            threading.Thread(target=self._telemetry_loop, daemon=True, name="voip-telemetry"),
            threading.Thread(target=self._net_rx_loop, daemon=True, name="voip-net-rx"),
            threading.Thread(target=self._audio_capture_loop, daemon=True, name="voip-capture"),
            threading.Thread(target=self._audio_playback_loop, daemon=True, name="voip-playback"),
        ]
        for t in self._threads:
            t.start()

    def stop(self) -> None:
        self.running = False
        if self._sock:
            try:
                self._sock.close()
            except Exception:
                pass
            self._sock = None
        for t in self._threads:
            t.join(timeout=0.5)
        self._threads.clear()
        if self._backend:
            self._backend.close()
            self._backend = None
        with self._state_lock:
            self._peers.clear()
            self._clients.clear()
        self.status_text = "Stopped"

    def _telemetry_loop(self) -> None:
        last_hello = 0.0
        while self.running:
            with self._state_lock:
                read_telemetry_file(self.telemetry_path, self.listener)
                name = self.listener.name
            now = time.monotonic()
            if self.mode == "peer" and now - last_hello >= 1.0 and self._sock:
                last_hello = now
                name_b = name.encode("utf-8", "replace")[:64]
                pkt = struct.pack("<BH", PKT_HELLO, len(name_b)) + name_b
                try:
                    self._sock.sendto(pkt, (self.host_ip, self.port))
                except OSError:
                    pass
            time.sleep(0.025)

    def _uid_for_addr(self, addr: Tuple[str, int]) -> int:
        key = f"{addr[0]}:{addr[1]}".encode("utf-8")
        return zlib.crc32(key) & 0xFFFFFFFF or 1

    @staticmethod
    def _parse_hello_name(payload: bytes, fallback: str) -> str:
        if len(payload) >= 2:
            name_len = struct.unpack_from("<H", payload, 0)[0]
            if len(payload) >= 2 + name_len:
                parsed = payload[2 : 2 + name_len].decode("utf-8", "replace").strip()
                if parsed:
                    return parsed
        return fallback

    def _touch_hello_peer(self, uid: int, name: str, now: float) -> None:
        with self._state_lock:
            peer = self._peers.get(uid)
            if peer is None:
                peer = PeerAudioState(uid=uid, name=name)
                self._peers[uid] = peer
            elif name:
                peer.name = name
            peer.last_seen = now

    def _net_rx_loop(self) -> None:
        while self.running and self._sock:
            try:
                data, addr = self._sock.recvfrom(4096)
            except socket.timeout:
                continue
            except OSError:
                break

            if not data:
                continue
            pkt_type = data[0]
            now = time.monotonic()

            if self.mode == "host":
                uid = self._uid_for_addr(addr)
                with self._state_lock:
                    self._clients[addr] = (uid, now)
                    host_name = self.listener.name

                if pkt_type == PKT_VOICE:
                    # Relay to all OTHER connected peers
                    relayed = struct.pack("<BI", PKT_VOICE_RELAYED, uid) + data[1:]
                    with self._state_lock:
                        targets = [c for c in self._clients.keys() if c != addr]
                    for target in targets:
                        try:
                            self._sock.sendto(relayed, target)
                        except OSError:
                            pass
                    decoded = decode_voice_payload(data[1:])
                    if decoded:
                        self._push_peer_audio(uid, decoded, now)
                elif pkt_type == PKT_HELLO:
                    hello_name = self._parse_hello_name(data[1:], "Peer")
                    self._touch_hello_peer(uid, hello_name, now)
                    host_name_b = host_name.encode("utf-8", "replace")[:64]
                    ack = struct.pack("<BH", PKT_HELLO_ACK, len(host_name_b)) + host_name_b
                    try:
                        self._sock.sendto(ack, addr)
                    except OSError:
                        pass
            else:
                # Peer mode
                if pkt_type == PKT_VOICE_RELAYED and len(data) > 5:
                    uid = struct.unpack_from("<I", data, 1)[0]
                    decoded = decode_voice_payload(data[5:])
                    if decoded:
                        self._push_peer_audio(uid, decoded, now)
                elif pkt_type == PKT_VOICE:
                    decoded = decode_voice_payload(data[1:])
                    if decoded:
                        self._push_peer_audio(0, decoded, now)
                elif pkt_type == PKT_HELLO_ACK:
                    host_name = self._parse_hello_name(data[1:], "Host")
                    self._touch_hello_peer(0, host_name, now)

    def _push_peer_audio(
        self, uid: int, decoded: Tuple[int, int, str, str, np.ndarray, np.ndarray], now: float
    ) -> None:
        _, flags, name, lvl, pos, pcm = decoded
        radio_tx = bool(flags & FLAG_RADIO_TX)
        has_radio = bool(flags & FLAG_HAS_RADIO)
        with self._state_lock:
            peer = self._peers.get(uid)
            if peer is None:
                peer = PeerAudioState(uid=uid, name=name)
                self._peers[uid] = peer
            peer.name = name
            peer.level = lvl
            peer.pos = pos
            peer.radio_tx = radio_tx
            peer.has_radio = has_radio
            peer.last_seen = now
            peer.queue.append(pcm)

    def _audio_capture_loop(self) -> None:
        prev_tx_radio = False
        while self.running and self._backend:
            try:
                frame = self._backend.read_frame()
            except Exception as exc:
                self.status_text = f"Mic error: {exc}"
                time.sleep(0.1)
                continue

            frame = frame * self.mic_gain
            rms = float(np.sqrt(np.mean(frame * frame)))
            self.mic_level = rms

            with self._state_lock:
                tx_radio = (self.listener.radio_tx or self.manual_ptt) and self.listener.has_radio
                has_radio = self.listener.has_radio
                name = self.listener.name
                lvl = self.listener.level
                pos = self.listener.pos.copy()

            if self.muted:
                tx_radio = False
                self.vad_open = False
                self._vad_hold_frames = 0
                if prev_tx_radio:
                    prev_tx_radio = False
                    # Send immediate PTT-release edge frame
                    self._send_voice_frame(False, has_radio, name, lvl, pos, np.zeros(FRAME_SAMPLES, dtype=np.float32))
                continue

            if rms >= self.noise_gate_rms:
                self._vad_hold_frames = 14  # ~280 ms hangover
            elif self._vad_hold_frames > 0:
                self._vad_hold_frames -= 1

            self.vad_open = self._vad_hold_frames > 0
            ptt_released = prev_tx_radio and not tx_radio
            prev_tx_radio = tx_radio
            should_send = tx_radio or self.vad_open or ptt_released

            if self.loopback_test and should_send:
                with self._state_lock:
                    self._loopback_peer.radio_tx = tx_radio
                    self._loopback_peer.has_radio = has_radio
                    self._loopback_peer.queue.append(frame.copy() if (tx_radio or self.vad_open) else np.zeros(FRAME_SAMPLES, dtype=np.float32))

            if not should_send:
                continue

            tx_frame = frame if (tx_radio or self.vad_open) else np.zeros(FRAME_SAMPLES, dtype=np.float32)
            self._send_voice_frame(tx_radio, has_radio, name, lvl, pos, tx_frame)

    def _send_voice_frame(
        self,
        tx_radio: bool,
        has_radio: bool,
        name: str,
        lvl: str,
        pos: np.ndarray,
        frame: np.ndarray,
    ) -> None:
        self._seq += 1
        flags = (FLAG_RADIO_TX if tx_radio else 0) | (FLAG_HAS_RADIO if has_radio else 0)
        pkt = encode_voice_packet(self._seq, flags, name, lvl, pos, frame)

        if self._sock:
            if self.mode == "host":
                relayed = struct.pack("<BI", PKT_VOICE_RELAYED, 0) + pkt[1:]
                with self._state_lock:
                    targets = list(self._clients.keys())
                for target in targets:
                    try:
                        self._sock.sendto(relayed, target)
                    except OSError:
                        pass
            else:
                try:
                    self._sock.sendto(pkt, (self.host_ip, self.port))
                except OSError:
                    pass

    def _audio_playback_loop(self) -> None:
        next_tick = time.monotonic()
        frame_dt = FRAME_SAMPLES / float(SAMPLE_RATE)
        while self.running and self._backend:
            now = time.monotonic()
            with self._state_lock:
                listener_snap = ListenerState(
                    name=self.listener.name,
                    level=self.listener.level,
                    is_host=self.listener.is_host,
                    in_session=self.listener.in_session,
                    last_ip=self.listener.last_ip,
                    pos=self.listener.pos.copy(),
                    dir=self.listener.dir.copy(),
                    top=self.listener.top.copy(),
                    has_radio=self.listener.has_radio,
                    radio_tx=self.listener.radio_tx,
                    peers=list(self.listener.peers),
                )
                # Drop stale clients (>15s silence/no hello)
                stale_clients = [c for c, (_, ts) in self._clients.items() if now - ts > 15.0]
                for c in stale_clients:
                    del self._clients[c]
                # Drop stale peers (>15s inactive, matching HELLO / HELLO_ACK keepalive)
                stale_peers = [uid for uid, p in self._peers.items() if now - p.last_seen > 15.0]
                for uid in stale_peers:
                    del self._peers[uid]
                active_peers = list(self._peers.values())

            effective_tx = (listener_snap.radio_tx or self.manual_ptt) and listener_snap.has_radio
            mixed = self.mixer.update_local_tx_edges(effective_tx, listener_snap.has_radio)

            for peer in active_peers:
                if peer.prebuffering:
                    if len(peer.queue) >= 2:
                        peer.prebuffering = False
                        peer.empty_ticks = 0
                        pcm = peer.queue.popleft()
                    else:
                        pcm = None
                elif peer.queue:
                    pcm = peer.queue.popleft()
                    peer.empty_ticks = 0
                else:
                    pcm = None
                    peer.empty_ticks += 1
                    if peer.empty_ticks >= 4:
                        peer.prebuffering = True
                # Reset radio_tx if no packet received in >200ms
                if pcm is None and (now - peer.last_seen > 0.20):
                    peer.radio_tx = False
                mixed += self.mixer.render_peer_frame(peer, pcm, listener_snap)

            if self.loopback_test:
                lb_pcm = self._loopback_peer.queue.popleft() if self._loopback_peer.queue else None
                if lb_pcm is None:
                    self._loopback_peer.radio_tx = effective_tx
                rad = math.radians(self.loopback_angle_deg)
                fwd = normalize(listener_snap.dir)
                up = normalize(listener_snap.top)
                right = normalize(np.cross(up, fwd))
                if float(np.linalg.norm(right)) < 1e-4:
                    right = np.array([1.0, 0.0, 0.0], dtype=np.float32)
                virtual_pos = (
                    listener_snap.pos
                    + fwd * (math.cos(rad) * self.loopback_distance)
                    + right * (math.sin(rad) * self.loopback_distance)
                )
                mixed += self.mixer.render_peer_frame(
                    self._loopback_peer,
                    lb_pcm,
                    listener_snap,
                    force_pos_override=(virtual_pos, self.loopback_occluded, True),
                )

            # Soft limiter to protect ears against summing peaks
            mixed = np.tanh(mixed * 0.95).astype(np.float32)

            try:
                self._backend.write_frame(mixed)
            except Exception as exc:
                self.status_text = f"Playback error: {exc}"
                time.sleep(0.05)

            next_tick += frame_dt
            sleep_for = next_tick - time.monotonic()
            if sleep_for > 0:
                time.sleep(sleep_for)
            else:
                next_tick = time.monotonic()


# ---------------------------------------------------------------------------
# GUI (STALKER Dark Theme)
# ---------------------------------------------------------------------------
class VoipGui:
    def __init__(
        self,
        engine: VoipEngine,
        mode: str = "host",
        ip: str = "127.0.0.1",
        port: int = DEFAULT_PORT,
        auto_start: bool = False,
    ):
        self.engine = engine
        self._ip_autofilled = False
        self._key_ptt_held = False
        self._btn_ptt_held = False
        self._z_release_after = None
        self.root = tk.Tk()
        self.root.title("xrRazom VOIP — Proximity & Walkie-Talkie")
        self.root.geometry("620x710")
        self.root.configure(bg="#171916")

        style = ttk.Style()
        style.theme_use("clam")
        style.configure("TFrame", background="#171916")
        style.configure("TLabelframe", background="#1d201c", foreground="#d2c7a5")
        style.configure("TLabelframe.Label", background="#1d201c", foreground="#d7aa4c", font=("Sans", 10, "bold"))
        style.configure("TLabel", background="#1d201c", foreground="#d6d6d0")
        style.configure("Root.TLabel", background="#171916", foreground="#d6d6d0")
        style.configure("TRadiobutton", background="#1d201c", foreground="#d6d6d0")
        style.configure("TCheckbutton", background="#1d201c", foreground="#d6d6d0")
        style.configure("TButton", background="#2d332a", foreground="#eae6d8")

        self.var_telemetry = tk.StringVar(value=engine.telemetry_path)
        self.var_mode = tk.StringVar(value=mode)
        self.var_ip = tk.StringVar(value=ip)
        self.var_port = tk.StringVar(value=str(port))
        self.var_status = tk.StringVar(value="Status: Ready")
        self.var_game = tk.StringVar(value="Game telemetry: Waiting for STALKER...")
        self.var_ptt = tk.StringVar(value="Walkie-Talkie (Z): STANDBY")
        self.var_mute = tk.BooleanVar(value=False)
        self.var_local_pling = tk.BooleanVar(value=True)
        self.var_loopback = tk.BooleanVar(value=False)
        self.var_lb_occluded = tk.BooleanVar(value=False)
        self.var_lb_ptt_lock = tk.BooleanVar(value=False)

        self.var_mic_gain = tk.DoubleVar(value=1.0)
        self.var_gate = tk.DoubleVar(value=0.012)
        self.var_prox_vol = tk.DoubleVar(value=1.0)
        self.var_radio_vol = tk.DoubleVar(value=0.85)
        self.var_static = tk.DoubleVar(value=0.045)
        self.var_max_dist = tk.DoubleVar(value=75.0)
        self.var_lb_dist = tk.DoubleVar(value=8.0)
        self.var_lb_angle = tk.DoubleVar(value=45.0)

        self._build_ui()
        self._bind_ptt_keys()
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

        if auto_start:
            self._toggle_start()
        self._refresh_ui()

    def _build_ui(self) -> None:
        pad = {"padx": 10, "pady": 6}

        # Telemetry Path
        lf_tel = ttk.LabelFrame(self.root, text="1. STALKER Telemetry (voip_telemetry.json)")
        lf_tel.pack(fill="x", **pad)
        row_tel = ttk.Frame(lf_tel)
        row_tel.pack(fill="x", padx=8, pady=6)
        ttk.Entry(row_tel, textvariable=self.var_telemetry).pack(side="left", fill="x", expand=True)
        ttk.Button(row_tel, text="Browse...", command=self._browse_telemetry).pack(side="left", padx=(6, 0))
        ttk.Label(lf_tel, textvariable=self.var_game, foreground="#9ec48e").pack(anchor="w", padx=8, pady=(0, 6))

        # Network
        lf_net = ttk.LabelFrame(self.root, text="2. Co-op Network (UDP)")
        lf_net.pack(fill="x", **pad)
        row_mode = ttk.Frame(lf_net)
        row_mode.pack(fill="x", padx=8, pady=4)
        ttk.Radiobutton(row_mode, text="Host (Server + Client)", variable=self.var_mode, value="host").pack(side="left")
        ttk.Radiobutton(row_mode, text="Peer (Connect to Host IP)", variable=self.var_mode, value="peer").pack(side="left", padx=(14, 0))

        row_addr = ttk.Frame(lf_net)
        row_addr.pack(fill="x", padx=8, pady=4)
        ttk.Label(row_addr, text="Host IP:").pack(side="left")
        ttk.Entry(row_addr, textvariable=self.var_ip, width=20).pack(side="left", padx=(4, 10))
        ttk.Label(row_addr, text="UDP Port:").pack(side="left")
        ttk.Entry(row_addr, textvariable=self.var_port, width=8).pack(side="left", padx=(4, 10))
        self.btn_start = ttk.Button(row_addr, text="Start VOIP", command=self._toggle_start)
        self.btn_start.pack(side="right")

        row_st = ttk.Frame(lf_net)
        row_st.pack(fill="x", padx=8, pady=(0, 6))
        ttk.Label(row_st, textvariable=self.var_status).pack(side="left")
        self.lbl_ptt = ttk.Label(row_st, textvariable=self.var_ptt, font=("Sans", 9, "bold"))
        self.lbl_ptt.pack(side="right")

        # Audio Mix
        lf_mix = ttk.LabelFrame(self.root, text="3. Audio, Proximity (3D) & Walkie-Talkie")
        lf_mix.pack(fill="x", **pad)

        self._slider(lf_mix, "Microphone Gain", self.var_mic_gain, 0.2, 3.0)
        self._slider(lf_mix, "Noise Gate (Proximity Voice)", self.var_gate, 0.002, 0.08)
        self._slider(lf_mix, "Proximity Volume (3D Voice)", self.var_prox_vol, 0.0, 2.5)
        self._slider(lf_mix, "Walkie-Talkie Volume", self.var_radio_vol, 0.0, 2.0)
        self._slider(lf_mix, "Walkie-Talkie Static", self.var_static, 0.0, 0.14)
        self._slider(lf_mix, "Max Proximity Distance (m)", self.var_max_dist, 20.0, 120.0)

        row_chk = ttk.Frame(lf_mix)
        row_chk.pack(fill="x", padx=8, pady=6)
        ttk.Checkbutton(row_chk, text="Mute Microphone", variable=self.var_mute).pack(side="left")
        ttk.Checkbutton(row_chk, text="Play local radio beep/static on PTT (Z)", variable=self.var_local_pling).pack(side="left", padx=(14, 0))

        # Self-Test Loopback
        lf_lb = ttk.LabelFrame(self.root, text="4. Audio Self-Test (Loopback in 3D / Walkie-Talkie)")
        lf_lb.pack(fill="x", **pad)
        row_lb_top = ttk.Frame(lf_lb)
        row_lb_top.pack(fill="x", padx=8, pady=4)
        ttk.Checkbutton(row_lb_top, text="Enable Loopback Self-Test", variable=self.var_loopback).pack(side="left")
        ttk.Checkbutton(row_lb_top, text="Simulate wall occlusion", variable=self.var_lb_occluded).pack(side="left", padx=(14, 0))
        row_lb_ptt = ttk.Frame(lf_lb)
        row_lb_ptt.pack(fill="x", padx=8, pady=(0, 4))
        ttk.Checkbutton(
            row_lb_ptt,
            text="Lock Walkie-Talkie PTT On",
            variable=self.var_lb_ptt_lock,
            command=self._sync_manual_ptt,
        ).pack(side="left")
        btn_ptt = ttk.Button(row_lb_ptt, text="Hold for Walkie-Talkie (or hold Z in window)")
        btn_ptt.pack(side="right")
        btn_ptt.bind("<ButtonPress-1>", self._on_btn_ptt_press)
        btn_ptt.bind("<ButtonRelease-1>", self._on_btn_ptt_release)
        self._slider(lf_lb, "Test Distance (m)", self.var_lb_dist, 0.5, 80.0)
        self._slider(lf_lb, "Direction (-90° Left / +90° Right)", self.var_lb_angle, -180.0, 180.0)

        self.lbl_mic = ttk.Label(self.root, text="Mic Level: [----------]", style="Root.TLabel")
        self.lbl_mic.pack(anchor="w", padx=12, pady=(4, 8))

    def _slider(self, parent, label: str, var: tk.DoubleVar, lo: float, hi: float) -> None:
        row = ttk.Frame(parent)
        row.pack(fill="x", padx=8, pady=2)
        ttk.Label(row, text=label, width=30).pack(side="left")
        val_lbl = ttk.Label(row, width=7, anchor="e")
        val_lbl.pack(side="right")

        def _upd(*_):
            val_lbl.configure(text=f"{var.get():.3f}" if hi < 0.5 else f"{var.get():.1f}")

        var.trace_add("write", _upd)
        _upd()
        ttk.Scale(row, from_=lo, to=hi, variable=var, orient="horizontal").pack(side="left", fill="x", expand=True, padx=6)

    def _save_config(self) -> None:
        raw_port = self.var_port.get().strip()
        try:
            port = int(raw_port) if raw_port else DEFAULT_PORT
        except ValueError:
            port = DEFAULT_PORT
        save_user_config(
            {
                "telemetry_path": self.var_telemetry.get().strip(),
                "mode": self.var_mode.get(),
                "host_ip": self.var_ip.get().strip() or "127.0.0.1",
                "port": port,
            }
        )

    def _browse_telemetry(self) -> None:
        if not filedialog:
            return
        p = filedialog.askopenfilename(
            title="Select voip_telemetry.json in Anomaly/appdata",
            filetypes=[("JSON", "*.json"), ("All files", "*.*")],
        )
        if p:
            self.var_telemetry.set(p)
            self.engine.telemetry_path = p
            self._save_config()

    def _toggle_start(self) -> None:
        if self.engine.running:
            self.engine.stop()
            self.btn_start.configure(text="Start VOIP")
        else:
            self.engine.telemetry_path = self.var_telemetry.get().strip()
            try:
                port = int(self.var_port.get().strip() or DEFAULT_PORT)
                self.engine.start(self.var_mode.get(), self.var_ip.get(), port)
                self.btn_start.configure(text="Stop VOIP")
                self._save_config()
            except Exception as exc:
                self.var_status.set(f"Error: {exc}")

    def _bind_ptt_keys(self) -> None:
        self.root.bind("<KeyPress-z>", self._on_key_z_press)
        self.root.bind("<KeyPress-Z>", self._on_key_z_press)
        self.root.bind("<KeyRelease-z>", self._on_key_z_release)
        self.root.bind("<KeyRelease-Z>", self._on_key_z_release)
        self.root.bind("<FocusOut>", self._on_focus_out)

    def _sync_manual_ptt(self) -> None:
        self.engine.manual_ptt = bool(
            self.var_lb_ptt_lock.get() or self._key_ptt_held or self._btn_ptt_held
        )

    def _on_btn_ptt_press(self, _event) -> None:
        self._btn_ptt_held = True
        self._sync_manual_ptt()

    def _on_btn_ptt_release(self, _event) -> None:
        self._btn_ptt_held = False
        self._sync_manual_ptt()

    def _on_key_z_press(self, event) -> None:
        if isinstance(event.widget, (tk.Entry, ttk.Entry)):
            return
        if self._z_release_after is not None:
            self.root.after_cancel(self._z_release_after)
            self._z_release_after = None
        self._key_ptt_held = True
        self._sync_manual_ptt()

    def _on_key_z_release(self, event) -> None:
        if isinstance(event.widget, (tk.Entry, ttk.Entry)):
            return
        if self._z_release_after is not None:
            self.root.after_cancel(self._z_release_after)
        self._z_release_after = self.root.after(35, self._finish_z_release)

    def _finish_z_release(self) -> None:
        self._z_release_after = None
        self._key_ptt_held = False
        self._sync_manual_ptt()

    def _on_focus_out(self, event) -> None:
        if event.widget is self.root:
            if self._z_release_after is not None:
                self.root.after_cancel(self._z_release_after)
                self._z_release_after = None
            self._key_ptt_held = False
            self._btn_ptt_held = False
            self._sync_manual_ptt()

    def _refresh_ui(self) -> None:
        # Sync controls to engine
        self.engine.telemetry_path = self.var_telemetry.get().strip()
        self.engine.mic_gain = float(self.var_mic_gain.get())
        self.engine.noise_gate_rms = float(self.var_gate.get())
        self.engine.muted = bool(self.var_mute.get())
        self.engine.mixer.prox_volume = float(self.var_prox_vol.get())
        self.engine.mixer.radio_volume = float(self.var_radio_vol.get())
        self.engine.mixer.radio_static_level = float(self.var_static.get())
        self.engine.mixer.prox_max_dist = float(self.var_max_dist.get())
        self.engine.mixer.local_ptt_feedback = bool(self.var_local_pling.get())
        self.engine.loopback_test = bool(self.var_loopback.get())
        self.engine.loopback_distance = float(self.var_lb_dist.get())
        self.engine.loopback_angle_deg = float(self.var_lb_angle.get())
        self.engine.loopback_occluded = bool(self.var_lb_occluded.get())
        self._sync_manual_ptt()

        with self.engine._state_lock:
            l = self.engine.listener
            file_ok = l.file_ok
            lname = l.name
            llevel = l.level or "-"
            lradio = l.has_radio
            ltx = l.radio_tx
            peer_cnt = len(l.peers)
            last_ip = l.last_ip
            net_cnt = len(self.engine._peers)

        if not self.engine.running:
            read_telemetry_file(self.engine.telemetry_path, self.engine.listener)

        if not self._ip_autofilled and last_ip and self.var_ip.get() == "127.0.0.1":
            self.var_ip.set(last_ip.split(":")[0] if ":" in last_ip else last_ip)
            self._ip_autofilled = True

        if file_ok:
            self.var_game.set(f"Active: {lname} | Level: {llevel} | Nearby peers: {peer_cnt} | VOIP peers: {net_cnt}")
        else:
            self.var_game.set("Game telemetry: Waiting for game world to load...")

        self.var_status.set(f"Status: {self.engine.status_text}")

        if (ltx or self.engine.manual_ptt) and lradio:
            self.var_ptt.set("Walkie-Talkie: TRANSMITTING (TX)")
            self.lbl_ptt.configure(foreground="#ff7744")
        elif lradio:
            self.var_ptt.set("Walkie-Talkie: READY")
            self.lbl_ptt.configure(foreground="#7ecb75")
        else:
            self.var_ptt.set("Walkie-Talkie: NO RADIO")
            self.lbl_ptt.configure(foreground="#aa6666")

        bars = int(clamp(self.engine.mic_level * 120.0, 0, 16))
        meter = "#" * bars + "-" * (16 - bars)
        vad_tag = " [SPEAKING]" if self.engine.vad_open else ""
        self.lbl_mic.configure(text=f"Mic Level: [{meter}]{vad_tag}")

        self.root.after(60, self._refresh_ui)

    def _on_close(self) -> None:
        self._save_config()
        self.engine.stop()
        self.root.destroy()

    def run(self) -> None:
        self.root.mainloop()


# ---------------------------------------------------------------------------
# Entry Point
# ---------------------------------------------------------------------------
def main() -> int:
    parser = argparse.ArgumentParser(description="xrRazom Proximity & Walkie-Talkie VOIP")
    parser.add_argument("--telemetry", default="", help="Path to Anomaly/appdata/voip_telemetry.json")
    parser.add_argument("--mode", choices=["host", "peer"], default=None)
    parser.add_argument("--ip", default=None, help="Host IP (for peer mode)")
    parser.add_argument("--port", type=int, default=None, help="UDP port (default 27016)")
    parser.add_argument("--headless", action="store_true", help="Run without GUI")
    parser.add_argument("--autostart", action="store_true", help="Start VOIP immediately on launch")
    args = parser.parse_args()

    cfg = load_user_config()
    mode = args.mode or (cfg.get("mode") if cfg.get("mode") in ("host", "peer") else "host")
    ip = (args.ip if args.ip is not None else str(cfg.get("host_ip") or "")).strip() or "127.0.0.1"
    try:
        cfg_port = int(cfg.get("port", DEFAULT_PORT))
    except (TypeError, ValueError):
        cfg_port = DEFAULT_PORT
    port = args.port if args.port is not None else cfg_port

    tel_path = args.telemetry or discover_telemetry_path()
    engine = VoipEngine(tel_path)

    if args.headless or tk is None:
        engine.start(mode, ip, port)
        print(f"[xrRazom VOIP] Headless {mode} started on UDP :{port} | telemetry={tel_path}")
        try:
            while True:
                time.sleep(0.5)
        except KeyboardInterrupt:
            engine.stop()
            return 0

    gui = VoipGui(engine, mode=mode, ip=ip, port=port, auto_start=args.autostart)
    gui.run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
