# UmaPS

A working, feature rich private server implementation for the English (Global) version of Umamusume: Pretty Derby. 

> **Disclaimer**
>
> - This is an unofficial fan project for research and personal use. It has no connection to Cygames.
> - No proprietary assets are shipped in this project.
> - This project is freeware. If you paid for access to it, you have been scammed.
> - This software comes with **NO WARRANTY**. The developers are not responsible for any loss of data that results from using it.

---

## Features

- **Wire protocol:** msgpack payloads with AES-CBC encryption, no modification to the game client is required to play.
- **Careers:** Career mode supporting all 4 current released scenarios in global: URA, Unity Cup, Trackblazer, Grand Concert. While career should be playable front start to end, there may be bugs and constants that are yet to be sorted out.
- **Gacha:** A full reimplementation of the gacha system with relevant odds sourced from the game's `master.mdb`.
- **Race Mechanics:** A reimplementation of race mechanics. Check sources below for logic.
- **Account Management:** System for running multiple accounts, along with clubs, following, and state.
- **So much more:** There is a lot of miscellaneous things that I simply can't list because the list will be too long. Like Archive, Team Rank, Practice Matches, (Team races are not implemented), Ephitets, Shop logic, Concerts, Independent training (The real logic is not implemented, instead it yields a maxed out uma).

## Requirements

- Windows 10/11 or Linux
- Python 3.12+
- The game installed through Steam, launched at least once so that
  `master.mdb` exists
- *(Optional)* A Rust toolchain to build the native race engine

## Quick start (Windows)

### 1. Install dependencies

```powershell
cd server
python -m venv .venv
.venv\Scripts\python -m pip install -r requirements.txt
```

### 2. Build the race engine (optional)

```powershell
cd server\race_engine_rs
cargo build --release
```

If `target/release/race_engine_rs.exe` is missing, races will be ran on python (expect MUCH slower results).

### 3. Trust the TLS certificate (once per machine)

The server presents a self-signed certificate for `api.games.umamusume.com`.
Import `server/certs/server.cert.der` into **Current User → Trusted Root
Certification Authorities** (`Cert:\CurrentUser\Root`).

### 4. Point the client at your machine

```powershell
.\enable-redirect.ps1
.\toggle-redirect.ps1 status
```

Use the following command to return to the real server:
```powershell
.\disable-redirect.ps1
```

### 5. Start the server

```powershell
.\start-server.ps1
```
### 6. Launch the game through Steam

## Linux

```bash
./toggle-redirect.sh on
./start-server.sh [--reload] [--force]
```

## Admin tools

```bash
# Account
python admin.py show
python admin.py set-fcoin 999999
python admin.py give-item 45 10
python admin.py give-all-cards
python admin.py max-support-cards

# Career
python career.py show
python career.py set-turn 45 --fill-races
python career.py set speed 900
python career.py events --fired
python career.py event-undo --turn 30
```

Run either script with no arguments to see the full command list.

## Development

### Adding or upgrading an endpoint

1. Add a handler module under [server/app/handlers/](server/app/handlers/).
2. Start from the captured response (`fixtures.store.find(endpoint, ...)`),
   then read and write state through [server/app/state.py](server/app/state.py).
3. Register it in the handler table in [server/app/main.py](server/app/main.py).

Every response's `data_headers` must have a `viewer_id`, `sid` and
`servertime`. op.

### Notes:
- Artifical Intellegence was used in the development of this project, hopefully this project can serve as a guide for those make a server on their own.

## Credits

### Game data and research

| Source | Used for |
|---|---|
| [GameTora](https://gametora.com/umamusume) | Trainee and support card event data, [Unity Cup](https://gametora.com/umamusume/unity-cup), [Team Trials scoring](https://gametora.com/umamusume/team-trials-pvp-scoring), character pages |
| [Umamusume Wiki](https://umamusu.wiki) | [Game Mechanics](https://umamusu.wiki/Game:Mechanics), [Trackblazer](https://umamusu.wiki/Game:Trackblazer) and [Concert Theater](https://umamusu.wiki/Game:Concert_Theater) pages |
| [uma.guide](https://uma.guide/guides/trackblazer) | Trackblazer (MANT) guide |
| [Hakuraku](https://hakuraku.moe/notes) | Research notes |
| **KuromiAK**, *Uma Musume Race Mechanics* | Race mechanics reference, building on reverse engineering by @umamusu_reveng, @kak_eng and @hoffe_33 |
| **Crazyfellow**, *Parenting & Gene guide* ([Ko-fi](https://ko-fi.com/crazyfellow), [race planner](https://uma.pwnation.net/)) | Inheritance and gene mechanics, with data from [@shoppo_ura](https://x.com/shoppo_ura), [Aoneko](https://aoneko-uma.fanbox.cc), [@BourBon_Polaris](https://x.com/BourBon_Polaris), NottinTV, [umamusustation](https://umamusustation.com) and [mee1080's compatibility table](https://mee1080.github.io/umaishow/) |
| Grand Live scenario guide (`docs/grand-live-guide (1).md`) | Compiled from kamigame, [VIPでウマ娘 Wiki](https://wikiwiki.jp/vip_umamusu), GameWith, Game8, Altema, Famitsu, umamusumelabo (@4mutaaaN), gamewiki.jp, GameTora, Namuwiki and umamusu.wiki |

Umamusume: Pretty Derby and all related names, characters and data belong to
Cygames, Inc. This project is not affiliated with or endorsed by Cygames.

## License

This project is licenced under MIT.
