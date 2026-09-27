# fl-bridge-mcp

An **unofficial** MCP (Model Context Protocol) bridge for FL Studio.
Not affiliated with or endorsed by Image-Line. FL Studio is a trademark
of Image-Line Software.

Talk to FL Studio from Claude (or any MCP client): transport, tempo,
pattern jumps, step-sequencer programming, mixer moves, live notes,
chord progressions — and, unlike a fire-and-forget MIDI blaster, it can
**read FL's state back** over a return port.

## How it works

```
MCP client ── stdio ──> server.py ── virtual MIDI "M5 FL Bridge" ──> FL Studio
                            ^                                          │
                            └── virtual MIDI "M5 FL Bridge Return" <───┘
```

- `server.py` opens two virtual CoreMIDI ports and exposes 18 MCP tools.
- `device_M5FLBridge.py` (an FL MIDI controller script) decodes commands
  and drives FL's scripting API; it reports state back via SysEx.
- Commands ride in SysEx under manufacturer id `0x7D` (the dev/educational
  id) so they can never collide with note traffic. Notes ride as plain
  `note_on`/`note_off` that the script deliberately leaves unhandled — FL
  routes them to the selected channel exactly like a hardware keyboard,
  which is what makes them audible and recordable.

## Install

1. `python3 -m venv venv && ./venv/bin/pip install -r requirements.txt`
   (pin matters: `mcp<2` — the 2.x SDK renamed FastMCP)
2. Copy the `M5 FL Bridge` folder into FL's controller-script directory:
   - macOS: `~/Documents/Image-Line/FL Studio/Settings/Hardware/`
   - Windows: `%USERPROFILE%\Documents\Image-Line\FL Studio\Settings\Hardware\`
3. Register the server with your MCP client, e.g. for Claude Code
   (`~/.claude.json` → `mcpServers`):
   ```json
   "flstudio": {"type": "stdio",
                "command": "/path/to/venv/bin/python",
                "args": ["/path/to/server.py"]}
   ```

## Wire it up in FL (once)

With the MCP server running (the ports only exist while it lives):

1. **Options → MIDI settings**
2. Input list → **M5 FL Bridge** → Enable, controller type **M5 FL Bridge (user)**
3. *(for state read-back)* Output list → **M5 FL Bridge Return** → Enable,
   and give it the **same port number** as the input — that links it to the
   controller so the script's replies have somewhere to go
4. **View → Script output** should greet you: `M5 FL Bridge v0.2 online`

Skipping step 3 costs you `get_status` and `list_channels`; everything else
still works.

## Tools

| Tool | Does |
|---|---|
| `transport` | play / stop / record |
| `set_tempo` | 10–522 BPM, 0.1 resolution, undo-able |
| `select_pattern` | jump to pattern (creates it if missing) |
| `get_status` | playing? tempo? pattern? channel? — from FL's own report |
| `list_channels` | channel-rack names by index |
| `set_grid_bit` / `program_steps` / `program_drums` | step-sequencer programming |
| `mixer_volume` / `mixer_pan` / `mixer_mute_toggle` | mixer track control |
| `channel_select` / `channel_mute_toggle` / `channel_solo` | channel-rack control |
| `play_notes` | timed notes/chords into the selected channel |
| `play_progression` | chord symbols (`Dm7 G7 Cmaj7`, slash bass OK), blocked or arpeggiated |
| `play_scale` | any mode/pentatonic/blues run |
| `panic` | all notes off |
| `bridge_status` | are the ports up, has FL ever answered |

## Wire protocol

`F0 7D <cmd> <payload> F7` — multi-byte values are two 7-bit bytes, MSB first.

| cmd | direction | payload |
|---|---|---|
| 0x01 | →FL | transport: 0 stop · 1 play · 2 record |
| 0x02 | →FL | tempo×10 (14-bit) |
| 0x03 | →FL | pattern number (14-bit) |
| 0x04 | →FL | grid: channel (14-bit), step, value |
| 0x05 | →FL | mixer: sub (0 vol · 1 pan · 2 mute), track (14-bit), value |
| 0x06 | →FL | channel: sub (0 mute · 1 solo · 2 select), channel (14-bit) |
| 0x10 | both | →FL: status poll · FL→: playing, tempo×10, pattern, channel |
| 0x11 | both | →FL: name request (channel) · FL→: channel + ASCII name |

## Limits (honest ones)

- The FL MIDI-scripting API is a controller surface, not a project editor:
  no playlist arrangement, no plugin-parameter surgery, no piano-roll
  writing. Recording `play_notes` while transport records is the way to
  get notes into a pattern.
- Notes land on the **selected** channel; `list_channels` then
  `channel_select` is the aiming sequence.
- Built and tested on macOS (CoreMIDI). Windows needs a virtual-MIDI
  driver (loopMIDI) instead of `virtual=True` ports — untested.

MIT licensed (see LICENSE); scratches my itch, PRs welcome, no SLA.
