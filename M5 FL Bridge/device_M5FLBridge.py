# name=M5 FL Bridge
# url=https://github.com/LIBCSYS
"""fl-bridge-mcp — FL Studio side. v0.2

Decodes SysEx commands arriving on the 'M5 FL Bridge' virtual MIDI port
(created by ~/dev/flstudio-mcp/server.py) and drives FL through its
scripting API. Plain note traffic is deliberately left UNhandled so FL
routes it to the selected channel like any hardware keyboard — that is
what makes play_notes audible and recordable with zero code here.

v0.2 talks back: state reports and channel names go out via
device.midiOutSysex, which reaches the server only if this controller's
OUTPUT in FL's MIDI settings is pointed at 'M5 FL Bridge Return' (and
given the same port number as the input). Without that wiring the send
is a silent no-op — everything else still works.

Wire protocol (must match server.py byte-for-byte):
  F0 7D <cmd> <payload...> F7
  in:  0x01 transport: 0=stop 1=play 2=record
       0x02 tempo:     bpm*10 as two 7-bit bytes, MSB first
       0x03 pattern:   pattern number, same 14-bit split
       0x04 grid:      channel(14bit) step(1) value(1)
       0x05 mixer:     sub(1: 0=vol 1=pan 2=mute) track(14bit) value(1)
       0x06 channel:   sub(1: 0=mute 1=solo 2=select) channel(14bit)
       0x10 status poll (no payload)
       0x11 name request: channel(14bit)
  out: 0x10 status: playing(1) tempo*10(14bit) pattern(14bit) channel(14bit)
       0x11 name:   channel(14bit) + ASCII bytes (empty = no such channel)
"""

import channels
import device
import general
import midi
import mixer
import patterns
import transport

CMD_TRANSPORT = 0x01
CMD_TEMPO = 0x02
CMD_PATTERN = 0x03
CMD_GRID = 0x04
CMD_MIXER = 0x05
CMD_CHANNEL = 0x06
CMD_STATUS = 0x10
CMD_NAME = 0x11

# Last status tuple that went out — reports are sent only on change, so the
# idle/refresh hooks can call _push_status() as often as FL likes.
_last_sent = None


def OnInit():
    # Shows up in FL's script output window — the one cheap proof the
    # script actually loaded after the user wires up the MIDI input.
    print("M5 FL Bridge v0.2 online — awaiting orders from the MCP server")


def _u14(hi, lo):
    """Rejoin the server's MSB-first 7-bit split."""
    return (hi << 7) | lo


def _s14(value):
    """The other direction, for replies."""
    return [(value >> 7) & 0x7F, value & 0x7F]


def _snapshot():
    # getCurrentTempo() returns BPM*1000; /100 keeps the wire format's
    # 0.1-BPM resolution without overflowing 14 bits (522 BPM -> 5220)
    tempo10 = int(mixer.getCurrentTempo() / 100)
    chan = channels.selectedChannel()
    return (1 if transport.isPlaying() else 0,
            max(0, min(tempo10, 16383)),
            max(0, min(patterns.patternNumber(), 16383)),
            max(0, min(chan if chan >= 0 else 0, 16383)))


def _send(cmd, payload):
    # isAssigned() is false until the user points this controller's OUTPUT
    # at the return port; sending without it would throw, not queue.
    if device.isAssigned():
        device.midiOutSysex(bytes([0xF0, 0x7D, cmd] + payload + [0xF7]))


def _push_status(force=False):
    global _last_sent
    snap = _snapshot()
    if force or snap != _last_sent:
        playing, tempo10, pat, chan = snap
        _send(CMD_STATUS, [playing] + _s14(tempo10) + _s14(pat) + _s14(chan))
        _last_sent = snap


def OnRefresh(flags):
    # FL calls this on most state changes (transport, pattern, selection);
    # the change-gate in _push_status keeps the return port quiet otherwise.
    _push_status()


def OnUpdateBeatIndicator(value):
    # Fires on every beat while playing — cheap way to catch tempo rides
    # and the play flag without polling from OnIdle.
    _push_status()


def OnSysEx(event):
    # event.sysex is the whole frame including F0/F7; our payload starts
    # after F0 7D. Anything not ours stays unhandled for other scripts.
    data = list(event.sysex)
    if len(data) < 4 or data[1] != 0x7D:
        return
    cmd = data[2]
    body = data[3:-1]  # strip trailing F7

    if cmd == CMD_TRANSPORT and body:
        if body[0] == 0:
            transport.stop()
            print("bridge: stop")
        elif body[0] == 1:
            transport.start()
            print("bridge: play")
        elif body[0] == 2:
            transport.record()
            print("bridge: record toggle")
        event.handled = True

    elif cmd == CMD_TEMPO and len(body) >= 2:
        bpm = _u14(body[0], body[1]) / 10.0
        # FL stores tempo as BPM*1000; REC_Control routes it through the
        # same path the tempo nudge wheel uses, so it's undo-able.
        general.processRECEvent(
            midi.REC_Tempo,
            int(bpm * 1000),
            midi.REC_Control | midi.REC_UpdateControl,
        )
        print("bridge: tempo ->", bpm)
        event.handled = True

    elif cmd == CMD_PATTERN and len(body) >= 2:
        num = _u14(body[0], body[1])
        patterns.jumpToPattern(num)
        print("bridge: pattern ->", num)
        event.handled = True

    elif cmd == CMD_GRID and len(body) >= 4:
        chan, step, val = _u14(body[0], body[1]), body[2], body[3]
        if chan < channels.channelCount():
            channels.setGridBit(chan, step, val)
            event.handled = True
        # no print: program_steps bursts dozens of these and the script
        # output window would drown out anything useful

    elif cmd == CMD_MIXER and len(body) >= 4:
        sub, track, val = body[0], _u14(body[1], body[2]), body[3]
        if track <= 125:
            if sub == 0:
                # setTrackVolume wants 0..1; 100/127 lands near FL's 80% default
                mixer.setTrackVolume(track, val / 127.0)
                print("bridge: mixer vol", track, "->", val)
            elif sub == 1:
                mixer.setTrackPan(track, (val - 64) / 64.0)
                print("bridge: mixer pan", track, "->", val)
            elif sub == 2:
                mixer.muteTrack(track)
                print("bridge: mixer mute toggle", track)
            event.handled = True

    elif cmd == CMD_CHANNEL and len(body) >= 3:
        sub, chan = body[0], _u14(body[1], body[2])
        if chan < channels.channelCount():
            if sub == 0:
                channels.muteChannel(chan)
                print("bridge: channel mute toggle", chan)
            elif sub == 1:
                channels.soloChannel(chan)
                print("bridge: channel solo toggle", chan)
            elif sub == 2:
                channels.selectOneChannel(chan)
                print("bridge: channel select", chan)
            event.handled = True

    elif cmd == CMD_STATUS:
        _push_status(force=True)
        event.handled = True

    elif cmd == CMD_NAME and len(body) >= 2:
        idx = _u14(body[0], body[1])
        if idx < channels.channelCount():
            # 7-bit-safe ASCII only: a high byte inside SysEx would end the
            # frame early on the wire and corrupt the reply
            name = channels.getChannelName(idx)[:24]
            payload = _s14(idx) + [b for b in name.encode("ascii", "replace")
                                   if 0 < b < 128]
        else:
            payload = _s14(idx)  # empty name = "no channel here", by contract
        _send(CMD_NAME, payload)
        event.handled = True


def OnMidiMsg(event):
    # Notes and CCs fall through on purpose: unhandled events reach FL's
    # normal input path (selected channel, recording, typing-keyboard
    # rules). Handling them here would mute the whole point of the bridge.
    event.handled = False
