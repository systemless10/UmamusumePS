# UmaPS

A working, feature rich private server implementation for the Global and JP version of Umamusume: Pretty Derby. 

> **Disclaimer**
>
> - This is an unofficial fan project for research and personal use. It has no connection to Cygames.
> - No proprietary assets are shipped in this project.
> - This project is freeware. If you paid for access to it, you have been scammed.
> - This software comes with **NO WARRANTY**. The developers are not responsible for any loss of data that results from using it.
> - It is recommended to use a alternative Steam account (that does not have a datalink to your real game data), when joining any instances of this server.
> - AI (Artificial Intelligence) was used in the creation of this project.

---

Join the [discord](https://discord.gg/E49vgNdRF7) for support and updates

## Features

- **Wire protocol:** msgpack payloads with AES-CBC encryption, no modification to the game client is required to play.
- **Careers:** Career mode supporting all 4 current released scenarios in global, and all 14 scenarios released in JP.
- **Gacha:** A full reimplementation of the gacha system with relevant odds sourced from the game's `master.mdb`.
- **Race Mechanics:** A full 1:1 reimplementation of Global's racing mechanics tested against ~2500 races, (while this is a substantial amount, there may be some rare edge cases where races do diverge, feel free to report this as a issue). On JP, the racing mechanics are not exact due to missing logic. JP has the ability to run races with more then 18 umas (up to 255) as long as the option to support it is enabled in the plugin. In Global this seems to be broken at the moment.
- **Admin Pannel** a pannel for modifying user saves within the private server, and supports things like the creation of custom races which can be sent staight to your Practice Race menu, custom support cards, career modifiers, and more.
- **Account Management:** System for running multiple accounts, along with clubs, following, and state.
- **Custom Support Cards** Allows creation of custom support cards and usage of them throughout the game.
- **So much more:** There is a lot of miscellaneous things that I simply can't list because the list will be too long. Like Archive, Team Rank, Practice Matches, (Team races are not implemented), Ephitets, Shop logic, Concerts, Independent training (The real logic is not implemented, instead it yields a maxed out uma).

## Requirements

- Windows 10/11 or Linux/MacOS
- Python 3.12+
- The game installed through Steam, launched at least once so that
  `master.mdb` exists
- Hachimi Edge

## Setup
1. Download/Clone this repository
2. Run the launcher for your version:
   | | Global | JP |
   |---|---|---|
   | **Windows** | right-click `start-global.ps1` → **Run with PowerShell** | right-click `start-jp.ps1` → **Run with PowerShell** |
   | **macOS / Linux** | `./start-global.sh` | `./start-jp.sh` |
   First run will take a while since it will setup everything
3. Leave this window open, when the server starts successfully a admin pannel will open in your browser.
4. Install the Hachimi plugin so the game can communicate with the server.

# Plugin install
1. Download the hachimi_redirect.dll plugin from the release, or build it (see plugin folder).
2. Make sure you have ran the game once with Hachimi installed.
3. Copy `hachimi_redirect.dll` into the game's `hachimi` folder.
4. Open `hachimi/config.json`, locate the "load libraries" field, and change it to include the plugin:
```json
   "load_libraries": [
      "hachimi\\hachimi_redirect.dll"
   ]
```
5. Start the game, press the right arrow key to open the Hachimi menu, and change the redirect server to:
   - Global: `https://127.0.0.1:8443`
   - JP: `https://127.0.0.1:8444`

# Switching back to real servers.
- Turn off the redirect in the Hachimi menu, (turn it back on to redirect back to the private server).

## Admin panel

The admin panel lets you edit your save from the browser. It runs at
<http://localhost:8189> for Global and <http://localhost:8190> for JP.

Umamusume: Pretty Derby and all related names, characters and data belong to
Cygames, Inc. This project is not affiliated with or endorsed by Cygames.

## Limitations
A lot of data used in the server are approximations, they should not be considered as fact. Some career mechanics and other game mechanics differ from how they are in the real server, while I do my best to get these as close as possible, ultimately a good chunk will remain different. Unless you have real concreate data that could fix something that is wrong, please do not report a difference from the real server as a issue. (This does not apply to real core mechanics bugs and softlocks, feel free to report those).

Most PvP modes and limited time events are not implemented (and probably won't ever be), please don't make a issue about that. Additonal features include independent careers, and procurement.

## License

This project is licenced under AGPL-3.0.

[GNU Affero General Public License v3.0](LICENSE).
