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
Jump check: beatable (found a way through)
Write this to your save? [y = yes / n = no / or type feedback]
```

## What it can do
- **Build**: "make an easy 30-second cube level", "add a hard wave section to the end of *Spike Party*"
- **Review**: "look at my level *Neon Rush* and tell me what's unfair"
- **Continue**: "keep going from where *My Level* ends, same style"
- **Get inspiration**: "what does today's daily level use?" (this downloads from the official GD servers)

If you type feedback instead of `y` at the preview, Claude redesigns the part based on what you said.

## Jump checker
Claude can't play the game, so GD Claude includes a physics simulation (`physics.py`).
It plays a design by trying every possible click timing at once. It tells Claude:
- whether the design can be beaten at all
- where the best attempt died, and what killed it ("hit the spike at col 13, row 0")
- one sequence of inputs that beats it ("jump @ col 9.2, yellow orb @ col 14, ...")

Claude has to run it before it saves anything, and it redesigns until the level is
beatable. The preview shows you the result too. It covers the cube, ship, ball, UFO and
wave (the robot is checked as a cube), plus gravity, mini and speed portals, pads, orbs,
blocks, spikes and saws. Spider, swing and dual mode aren't simulated yet, so it says
when it stops checking. The physics numbers are close to the real game but not exact,
so still playtest, especially jumps that are only barely possible.

## Study mode
```
python gdclaude.py --vault "path/to/vault" --study "easy featured levels"
```
This downloads rated levels from the official servers and measures how they're built:
section lengths, how dense the hazards are, longest spike runs, the breaks between
obstacles, orbs and pads per 100 columns, and how much is decoration. Claude turns the
numbers into rules, such as "rated easy levels: longest spike run 2, about 6 empty columns
between obstacles", and saves them to `Design Rules` in your vault. Then every level it
builds afterwards follows them. You can also ask for it in chat: "study some insane demons
and compare them to my level".

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

## Memory: making it learn (Obsidian)
Without memory, GD Claude forgets everything when you quit. Point it at your Obsidian vault:

```
python gdclaude.py --vault "C:/Users/you/Documents/MyVault"
```
(or set `GD_CLAUDE_VAULT` once and skip the flag). It creates a `GD Claude/` folder in your vault:

| Note | What goes in it |
|---|---|
| `My Style` | Your taste: favourite gamemodes, difficulty, things you hate |
| `Design Rules` | Lessons it learned, e.g. "2-cell ship corridors are too tight for easy levels" |
| `Feedback Log` | Filled in automatically every time you reject a preview and say why |
| `Levels/<name>` | One note per level: the idea, sections built so far, what's left to do |

GD Claude reads all of these at the start of every session and updates them as you
work, so the more you use it and the more feedback you give, the better it fits
you. The notes are normal Markdown, so you can read and edit them in Obsidian.
Writing your own rules into `My Style` is the fastest way to teach it.

## How it works
| File | What it does |
|---|---|
| `gdclaude.py` | Chat loop. Claude (`claude-opus-5-5`) uses tools to list and inspect your levels, create levels, add sections, and look up online levels |
| `save_file.py` | Decrypts and re-encrypts `CCLocalLevels.dat` (XOR+base64+gzip on Windows, AES on macOS) |
| `level_builder.py` | Claude designs on a grid (1 cell = 1 block, row 0 = ground). This file turns that grid into real GD objects (`1,8,2,315,3,15`) and draws the previews |
| `physics.py` | Jump checker: simulates the player and searches for a way through |
| `study.py` | Study mode: measures real levels downloaded from the servers |
| `vault.py` | Memory: reads and writes notes in your Obsidian vault (only inside its `GD Claude/` folder) |
| `gd_api.py` | Talks to the official boomlings.com servers for searching and downloading levels |

Claude can place blocks, spikes, saws, orbs, pads, every gamemode, gravity,
size, dual and speed portals, user coins, and colour triggers. It is
better at layouts than at decoration, so playtest everything it makes.
Pads and small spikes may sit a few units off where the editor would snap
them, so nudge them if needed.

Run the tests with `python -m pytest tests`.
