# GD Claude: an AI level builder for the Geometry Dash Create tab

You chat with Claude about your Geometry Dash levels. GD Claude reads the levels
in your **Create tab**, reviews them, and **builds new levels or sections
directly into your save file**. Before it writes anything it shows you an ASCII
preview and waits for your OK.

```
you> make a short easy cube level called Sunrise with a ship part in the middle

--- New level 'Sunrise' (212 objects) ---
  4 |                    ########################
  3 |                    v  v      v
  2 |
  1 |          #       S     ^          ^     C
  0 |   ^     ##  ^^   S  #######  ###########C   ^
    +==============================================
Write this to your save? [y = yes / n = no / or type feedback]
```

## What it can do
- **Build**: "make an easy 30-second cube level", "add a hard wave section to the end of *Spike Party*"
- **Review**: "look at my level *Neon Rush* and tell me what's unfair"
- **Continue**: "keep going from where *My Level* ends, same style"
- **Get inspiration**: "what does today's daily level use?" (this downloads from the official GD servers)

If you type feedback instead of `y` at the preview, Claude redesigns the part based on what you said.

## Setup
1. Install Python 3.10 or newer, then run `pip install -r requirements.txt`
2. Get an API key at https://console.anthropic.com and set it:
   - Windows: `setx ANTHROPIC_API_KEY "sk-ant-..."` (open a new terminal afterwards)
   - macOS/Linux: `export ANTHROPIC_API_KEY=sk-ant-...`
3. **Close Geometry Dash.** The game rewrites its save file when it exits, so it would wipe out anything GD Claude added while it was open.
4. Run it:
   ```
   python gdclaude.py                 # chat
   python gdclaude.py --list          # just list your Create tab levels
   python gdclaude.py "make me a level called Test"
   ```
5. Open GD. The new level appears at the top of your Create tab. Open it in the editor and playtest it.

GD Claude finds the save file on its own on Windows, macOS, and Steam/Proton.
If it can't, add `--save "path/to/CCLocalLevels.dat"`. Before its first write
in each session it copies your save to `CCLocalLevels.dat.backup-<time>`.

## How it works
| File | What it does |
|---|---|
| `gdclaude.py` | Chat loop. Claude (`claude-opus-5-5`) uses tools to list and inspect your levels, create levels, add sections, and look up online levels |
| `save_file.py` | Decrypts and re-encrypts `CCLocalLevels.dat` (XOR+base64+gzip on Windows, AES on macOS) |
| `level_builder.py` | Claude designs on a grid (1 cell = 1 block, row 0 = ground). This file turns that grid into real GD objects (`1,8,2,315,3,15`) and draws the previews |
| `gd_api.py` | Talks to the official boomlings.com servers for searching and downloading levels |

Claude can place blocks, spikes, saws, orbs, pads, every gamemode, gravity,
size, dual and speed portals, user coins, and colour triggers. It is
better at layouts than at decoration, so playtest everything it makes.
Pads and small spikes may sit a few units off where the editor would snap
them, so nudge them if needed.

Run the tests with `python -m pytest tests`.
