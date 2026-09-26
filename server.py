#!/usr/bin/env python3
"""fl-bridge-mcp — MCP server side. v0.2

Claude tool calls land here over stdio; we translate them into MIDI on a
virtual CoreMIDI source named "M5 FL Bridge". FL Studio treats that source
as a controller and our device script (device_M5FLBridge.py) decodes the
commands. Commands ride in SysEx (mfr id 0x7D, the dev/educational id) so
they can never collide with real note traffic; notes ride as plain
note_on/note_off, which the FL script leaves unhandled so FL routes them
to the selected channel exactly like a hardware keyboard would.

v0.2 adds the return channel: a second virtual port ("M5 FL Bridge Return")
that FL's controller OUTPUT is pointed at, carrying state reports back —
so tools can read FL instead of flying blind.

The virtual ports only exist while this process lives — that's why they
are opened at import time and never closed: FL loses the controller the
moment the MCP server exits, which is the correct signal to the user that
the bridge is down.

Unofficial. Not affiliated with Image-Line.
"""

import re
import threading
import time

import mido
from mcp.server.fastmcp import FastMCP

# rtmidi is the only mido backend that can create *virtual* ports on macOS
mido.set_backend("mido.backends.rtmidi")

PORT_NAME = "M5 FL Bridge"
RETURN_PORT_NAME = "M5 FL Bridge Return"

# SysEx command bytes — must match device_M5FLBridge.py byte-for-byte
SYX = 0x7D          # manufacturer id: reserved for non-commercial/dev use
CMD_TRANSPORT = 0x01
CMD_TEMPO = 0x02
CMD_PATTERN = 0x03
CMD_GRID = 0x04
CMD_MIXER = 0x05
CMD_CHANNEL = 0x06
CMD_STATUS = 0x10   # server->FL: poll request; FL->server: status report
CMD_NAME = 0x11     # server->FL: name request;  FL->server: name reply

# Opened once for the life of the server (see module docstring for why).
_out = mido.open_output(PORT_NAME, virtual=True)

# --- return channel state ---------------------------------------------------
# Everything below _state_lock is written by the MIDI callback thread and
# read by tool calls, hence the lock; none of it is hot enough to need more.
_state_lock = threading.Lock()
_status = {"playing": None, "bpm": None, "pattern": None, "channel": None,
           "received_at": None}
_names: dict[int, str] = {}
_name_event = threading.Event()   # one outstanding name request at a time


def _on_return(msg) -> None:
    """MIDI callback for the return port. Runs on rtmidi's thread — keep it
    allocation-light and never block, or FL-side sends start backing up."""
    if msg.type != "sysex" or len(msg.data) < 2 or msg.data[0] != SYX:
        return
    cmd, body = msg.data[1], list(msg.data[2:])
    if cmd == CMD_STATUS and len(body) >= 7:
        with _state_lock:
            _status["playing"] = bool(body[0])
            _status["bpm"] = ((body[1] << 7) | body[2]) / 10.0
            _status["pattern"] = (body[3] << 7) | body[4]
            _status["channel"] = (body[5] << 7) | body[6]
            _status["received_at"] = time.time()
    elif cmd == CMD_NAME and len(body) >= 2:
        idx = (body[0] << 7) | body[1]
        with _state_lock:
            # empty name == FL says "no channel there"; keep the marker so
            # list_channels knows to stop rather than time out per index
            _names[idx] = bytes(body[2:]).decode("ascii", "replace")
        _name_event.set()


_ret = mido.open_input(RETURN_PORT_NAME, virtual=True, callback=_on_return)

mcp = FastMCP("flstudio")


def _sysex(cmd: int, payload: list[int]) -> None:
    """Fire one bridge command. Every payload byte must be 7-bit clean —
    a stray high bit would terminate the SysEx early inside CoreMIDI."""
    assert all(0 <= b < 128 for b in payload), "SysEx payload must be 7-bit"
    _out.send(mido.Message("sysex", data=[SYX, cmd] + payload))


def _14bit(value: int) -> list[int]:
    """Split an int into two 7-bit bytes, MSB first. 14 bits caps at 16383,
    enough for pattern numbers and for tempo*10 (FL tops out at 522 bpm)."""
    return [(value >> 7) & 0x7F, value & 0x7F]


# --- note scheduling (shared by play_notes / play_progression / play_scale) --

def _schedule(notes: list[dict]) -> str:
    """Validate note dicts, flatten to timestamped on/off events, play them.
    Blocking by design: the tool returns when the last note releases, so
    consecutive tool calls naturally sequence instead of piling up."""
    events: list[tuple[float, mido.Message]] = []
    for n in notes:
        note = int(n["note"])
        vel = int(n.get("velocity", 100))
        start = float(n.get("start_ms", 0)) / 1000.0
        dur = float(n.get("duration_ms", 250)) / 1000.0
        if not 0 <= note <= 127:
            return f"note {note} out of MIDI range"
        events.append((start, mido.Message("note_on", note=note, velocity=max(1, min(vel, 127)))))
        events.append((start + dur, mido.Message("note_off", note=note)))
    if max(t for t, _ in events) > 60:
        return "sequence longer than the 60s cap — split it into multiple calls"
    # note_off must win ties with note_on at the same instant, otherwise a
    # repeated note re-triggers then instantly kills itself
    events.sort(key=lambda e: (e[0], e[1].type != "note_off"))
    t0 = time.monotonic()
    for when, msg in events:
        delay = t0 + when - time.monotonic()
        if delay > 0:
            time.sleep(delay)
        _out.send(msg)
    return f"played {len(notes)} notes"


# --- chord / scale math -------------------------------------------------------

_NOTE_OFFSET = {"C": 0, "D": 2, "E": 4, "F": 5, "G": 7, "A": 9, "B": 11}

# Interval sets, root-relative in semitones. Order matters only for broken
# (arpeggiated) playback, so keep them low-to-high.
_QUALITIES = {
    "": (0, 4, 7), "maj": (0, 4, 7), "M": (0, 4, 7),
    "m": (0, 3, 7), "min": (0, 3, 7), "-": (0, 3, 7),
    "dim": (0, 3, 6), "aug": (0, 4, 8), "+": (0, 4, 8),
    "sus2": (0, 2, 7), "sus4": (0, 5, 7),
    "6": (0, 4, 7, 9), "m6": (0, 3, 7, 9),
    "7": (0, 4, 7, 10), "maj7": (0, 4, 7, 11), "M7": (0, 4, 7, 11),
    "m7": (0, 3, 7, 10), "min7": (0, 3, 7, 10),
    "dim7": (0, 3, 6, 9), "m7b5": (0, 3, 6, 10),
    "9": (0, 4, 7, 10, 14), "maj9": (0, 4, 7, 11, 14), "m9": (0, 3, 7, 10, 14),
    "add9": (0, 4, 7, 14), "madd9": (0, 3, 7, 14),
}

_CHORD_RE = re.compile(r"^([A-G])([#b]?)([^/]*)(?:/([A-G])([#b]?))?$")

# Roots live in the C3 octave (MIDI 48-59) so voicings sit around middle C;
# a slash bass drops a further octave below the root zone.
_ROOT_BASE = 48
_BASS_BASE = 36


def _pc(letter: str, accidental: str) -> int:
    off = _NOTE_OFFSET[letter]
    return (off + {"#": 1, "b": -1, "": 0}[accidental]) % 12


def parse_chord(symbol: str) -> list[int]:
    """Chord symbol -> MIDI notes. Raises ValueError on anything it can't
    read — better a loud error than a wrong chord played with confidence."""
    m = _CHORD_RE.match(symbol.strip())
    if not m:
        raise ValueError(f"can't parse chord '{symbol}'")
    letter, acc, quality, bass_letter, bass_acc = m.groups()
    if quality not in _QUALITIES:
        raise ValueError(f"unknown quality '{quality}' in '{symbol}' "
                         f"(known: {', '.join(sorted(q for q in _QUALITIES if q))})")
    root = _ROOT_BASE + _pc(letter, acc)
    notes = [root + iv for iv in _QUALITIES[quality]]
    if bass_letter:
        notes.insert(0, _BASS_BASE + _pc(bass_letter, bass_acc or ""))
    return notes


_MODES = {
    "major": (0, 2, 4, 5, 7, 9, 11), "ionian": (0, 2, 4, 5, 7, 9, 11),
    "dorian": (0, 2, 3, 5, 7, 9, 10), "phrygian": (0, 1, 3, 5, 7, 8, 10),
    "lydian": (0, 2, 4, 6, 7, 9, 11), "mixolydian": (0, 2, 4, 5, 7, 9, 10),
    "minor": (0, 2, 3, 5, 7, 8, 10), "aeolian": (0, 2, 3, 5, 7, 8, 10),
    "locrian": (0, 1, 3, 5, 6, 8, 10),
    "harmonic minor": (0, 2, 3, 5, 7, 8, 11),
    "pentatonic": (0, 2, 4, 7, 9), "minor pentatonic": (0, 3, 5, 7, 10),
    "blues": (0, 3, 5, 6, 7, 10), "chromatic": tuple(range(12)),
}


# --- tools -------------------------------------------------------------------

@mcp.tool()
def transport(action: str) -> str:
    """Control FL Studio transport. action: 'play', 'stop', or 'record'
    (record toggles the record arm, then starts playback if stopped)."""
    codes = {"stop": 0, "play": 1, "record": 2}
    if action not in codes:
        return f"unknown action '{action}' — use play/stop/record"
    _sysex(CMD_TRANSPORT, [codes[action]])
    return f"transport {action} sent"


@mcp.tool()
def set_tempo(bpm: float) -> str:
    """Set the project tempo in BPM. FL accepts 10-522; fractional tempos
    are honored to 0.1 BPM."""
    if not 10 <= bpm <= 522:
        return f"{bpm} out of FL's 10-522 BPM range"
    # wire format is bpm*10 so 0.1-BPM resolution survives the 7-bit split
    _sysex(CMD_TEMPO, _14bit(round(bpm * 10)))
    return f"tempo {bpm} BPM sent"


@mcp.tool()
def select_pattern(number: int) -> str:
    """Jump to a pattern by number (1-based, as shown in FL's pattern picker).
    FL creates the pattern if it doesn't exist yet."""
    if not 1 <= number <= 16383:
        return "pattern number must be 1-16383"
    _sysex(CMD_PATTERN, _14bit(number))
    return f"pattern {number} selected"


@mcp.tool()
def get_status() -> str:
    """Read FL's last-reported state: playing/stopped, tempo, pattern,
    selected channel, and how old the report is. Sends a fresh poll first
    and gives FL a beat to answer."""
    _sysex(CMD_STATUS, [])
    time.sleep(0.25)  # round-trip through FL's script thread is not instant
    with _state_lock:
        if _status["received_at"] is None:
            return ("no report yet — check FL return-port wiring "
                    f"(controller OUTPUT must point at '{RETURN_PORT_NAME}')")
        age = time.time() - _status["received_at"]
        return (f"{'playing' if _status['playing'] else 'stopped'} | "
                f"{_status['bpm']} BPM | pattern {_status['pattern']} | "
                f"channel {_status['channel']} | report {age:.1f}s old")


@mcp.tool()
def set_grid_bit(channel: int, step: int, on: bool) -> str:
    """Set one step-sequencer cell: channel-rack index (0-based), step 0-63."""
    if not 0 <= step <= 63:
        return "step must be 0-63"
    if not 0 <= channel <= 16382:
        return "channel out of range"
    _sysex(CMD_GRID, _14bit(channel) + [step, 1 if on else 0])
    return f"grid[{channel}][{step}] = {'on' if on else 'off'}"


@mcp.tool()
def program_steps(channel: int, steps: list[int], clear_first: bool = True,
                  pattern_length: int = 16) -> str:
    """Program a channel's step row in one call: clears steps
    0..pattern_length-1 (unless clear_first=False), then lights the given
    steps. Steps are 0-based; 16 steps = one bar of 16ths."""
    if not 0 <= channel <= 16382:
        return "channel out of range"
    if any(not 0 <= s <= 63 for s in steps):
        return "steps must be 0-63"
    sent = 0
    todo = ([(s, 0) for s in range(min(pattern_length, 64))] if clear_first else [])
    todo += [(s, 1) for s in steps]
    for step, val in todo:
        _sysex(CMD_GRID, _14bit(channel) + [step, val])
        sent += 1
        time.sleep(0.005)  # pace the burst so FL's script queue keeps up
    return f"programmed channel {channel}: {len(steps)} steps on ({sent} writes)"


@mcp.tool()
def program_drums(parts: list[dict], pattern_length: int = 16) -> str:
    """Program several channels' step rows at once. Each part:
    {"channel": <rack index>, "steps": [0,4,8,12]}. Clears each row first.
    Classic four-on-the-floor: kick [0,4,8,12], hats [2,6,10,14],
    snare [4,12]."""
    done = []
    for part in parts:
        ch, steps = int(part["channel"]), [int(s) for s in part["steps"]]
        result = program_steps(ch, steps, clear_first=True,
                               pattern_length=pattern_length)
        if not result.startswith("programmed"):
            return f"stopped at channel {ch}: {result}"
        done.append(f"ch{ch}:{len(steps)}")
    return f"drums programmed — {', '.join(done)}"


@mcp.tool()
def mixer_volume(track: int, value: int) -> str:
    """Set a mixer track's volume. track 0-125 (0 = master), value 0-127
    (100 ≈ FL's default 80%)."""
    if not 0 <= track <= 125:
        return "track must be 0-125"
    if not 0 <= value <= 127:
        return "value must be 0-127"
    _sysex(CMD_MIXER, [0] + _14bit(track) + [value])
    return f"mixer track {track} volume -> {value}/127"


@mcp.tool()
def mixer_pan(track: int, value: int) -> str:
    """Set a mixer track's pan. value 0-127; 64 is center, 0 hard left,
    127 hard right."""
    if not 0 <= track <= 125:
        return "track must be 0-125"
    if not 0 <= value <= 127:
        return "value must be 0-127"
    _sysex(CMD_MIXER, [1] + _14bit(track) + [value])
    return f"mixer track {track} pan -> {value} (64=center)"


@mcp.tool()
def mixer_mute_toggle(track: int) -> str:
    """Toggle mute on a mixer track (0-125)."""
    if not 0 <= track <= 125:
        return "track must be 0-125"
    _sysex(CMD_MIXER, [2] + _14bit(track) + [0])
    return f"mixer track {track} mute toggled"


@mcp.tool()
def channel_mute_toggle(channel: int) -> str:
    """Toggle mute on a channel-rack channel (0-based index)."""
    if not 0 <= channel <= 16382:
        return "channel out of range"
    _sysex(CMD_CHANNEL, [0] + _14bit(channel))
    return f"channel {channel} mute toggled"


@mcp.tool()
def channel_select(channel: int) -> str:
    """Select a channel-rack channel (0-based). Subsequent play_notes /
    play_progression / play_scale land on it."""
    if not 0 <= channel <= 16382:
        return "channel out of range"
    _sysex(CMD_CHANNEL, [2] + _14bit(channel))
    return f"channel {channel} selected"


@mcp.tool()
def channel_solo(channel: int) -> str:
    """Toggle solo on a channel-rack channel (0-based index)."""
    if not 0 <= channel <= 16382:
        return "channel out of range"
    _sysex(CMD_CHANNEL, [1] + _14bit(channel))
    return f"channel {channel} solo toggled"


@mcp.tool()
def list_channels(max_channels: int = 16) -> str:
    """List channel-rack channels by name, so notes and grid programming
    can target the right instrument. Needs the return port wired."""
    found = []
    for idx in range(max(1, min(max_channels, 64))):
        with _state_lock:
            _names.pop(idx, None)
        _name_event.clear()
        _sysex(CMD_NAME, _14bit(idx))
        # 300ms per index: generous for FL's script thread, short enough
        # that a dead return port fails the whole call in a few seconds
        if not _name_event.wait(0.3):
            if idx == 0:
                return ("no reply — check FL return-port wiring "
                        f"(controller OUTPUT must point at '{RETURN_PORT_NAME}')")
            break
        with _state_lock:
            name = _names.get(idx, "")
        if name == "":
            break  # FL says: no channel at this index — end of rack
        found.append(f"{idx}: {name}")
    return "\n".join(found) if found else "channel rack is empty"


@mcp.tool()
def play_notes(notes: list[dict]) -> str:
    """Play notes into FL's currently selected channel, live — audible
    immediately, and recorded into the pattern when transport is recording.

    Each note: {"note": 0-127 (60=C5 in FL), "velocity": 1-127 (default 100),
    "start_ms": offset from call start (default 0), "duration_ms": length
    (default 250)}. Notes sharing a start_ms sound as a chord.
    Blocks until the last note releases; total length capped at 60s."""
    if not notes:
        return "no notes given"
    return _schedule(notes)


@mcp.tool()
def play_progression(chords: list[str], bar_ms: int = 2000,
                     velocity: int = 96, broken: bool = False) -> str:
    """Play a chord progression on the selected channel, one chord per bar.
    Chord symbols: C, Dm7, G7, Fmaj7, Bdim, Esus4, Am7/G, etc. bar_ms is
    the length of each chord (2000ms = one bar at 120 BPM in 4/4).
    broken=True arpeggiates each chord low-to-high instead of blocking it."""
    if not chords:
        return "no chords given"
    try:
        voiced = [parse_chord(c) for c in chords]
    except ValueError as e:
        return str(e)
    notes = []
    for bar, chord in enumerate(voiced):
        base = bar * bar_ms
        # 95% duration leaves a gap so repeated chords re-articulate
        # instead of smearing into one long note
        for i, n in enumerate(chord):
            offset = i * (bar_ms // (len(chord) * 2)) if broken else 0
            notes.append({"note": n, "velocity": velocity,
                          "start_ms": base + offset,
                          "duration_ms": int(bar_ms * 0.95) - offset})
    result = _schedule(notes)
    return f"{' '.join(chords)}: {result}"


@mcp.tool()
def play_scale(root: str, mode: str = "major", octaves: int = 1,
               note_ms: int = 200, velocity: int = 96) -> str:
    """Run a scale up the selected channel. root like 'C' or 'F#'; mode:
    major, minor, dorian, phrygian, lydian, mixolydian, aeolian, locrian,
    harmonic minor, pentatonic, minor pentatonic, blues, chromatic."""
    m = re.match(r"^([A-G])([#b]?)$", root.strip())
    if not m:
        return f"can't parse root '{root}'"
    if mode not in _MODES:
        return f"unknown mode '{mode}' (known: {', '.join(sorted(_MODES))})"
    start = _ROOT_BASE + 12 + _pc(m.group(1), m.group(2))  # C4 zone reads as mid-keyboard
    degrees = [start + o * 12 + iv for o in range(max(1, min(octaves, 4)))
               for iv in _MODES[mode]]
    degrees.append(start + max(1, min(octaves, 4)) * 12)  # land on the octave
    notes = [{"note": n, "velocity": velocity, "start_ms": i * note_ms,
              "duration_ms": int(note_ms * 0.9)} for i, n in enumerate(degrees)]
    result = _schedule(notes)
    return f"{root} {mode}: {result}"


@mcp.tool()
def panic() -> str:
    """All-notes-off on every channel — the hung-note fire extinguisher."""
    for ch in range(16):
        _out.send(mido.Message("control_change", channel=ch, control=123, value=0))
    return "all notes off sent"


@mcp.tool()
def bridge_status() -> str:
    """Report the bridge's own health: both virtual ports and whether FL has
    ever reported back. Does not prove FL is listening — that needs ears
    or a get_status round-trip."""
    cmd_live = PORT_NAME in mido.get_input_names()   # our output = an input to others
    ret_live = RETURN_PORT_NAME in mido.get_output_names()
    with _state_lock:
        heard = _status["received_at"]
    return (f"command port '{PORT_NAME}': {'up' if cmd_live else 'MISSING'} | "
            f"return port '{RETURN_PORT_NAME}': {'up' if ret_live else 'MISSING'} | "
            f"FL last heard from: "
            f"{'never' if heard is None else f'{time.time() - heard:.0f}s ago'}")


if __name__ == "__main__":
    mcp.run()
