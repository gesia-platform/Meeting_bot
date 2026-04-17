# ZOOM_MEETING_BOT

`zoom-meeting-bot` is a menu-driven CLI kit for running the Zoom Meeting Bot system on your own PC.

This package is meant for real end users, not only for people working directly inside this repository. If you install from npm, you do not need to clone the GitHub repo first.

## What This Package Does

- Joins Zoom meetings with your own config
- Captures meeting audio locally
- Summarizes the meeting with local AI pipelines
- Generates meeting outputs as HTML/CSS-based PDFs by default
- Lets you manage reusable meeting-output styles (`SKILL`)

The packaged default reference skill is:

- `skills/meeting-output-default/SKILL.md`

Generated custom skills are stored in your own workspace, not inside the npm package.

## Quick Start

### Requirements

- Node.js 18+ and npm
- Python 3.11+

### Install

```bash
npm install -g zoom-meeting-bot
```

### Run

```bash
zoom-meeting-bot
```

Running `zoom-meeting-bot` with no arguments opens the Korean menu UI.

## First-Run Experience

On the first run, the CLI prepares the user workspace automatically. That includes:

- creating a dedicated workspace folder
- creating a local `.venv`
- installing the Python runtime dependencies
- creating the first config file
- guiding you through `quickstart`

Typical first-run flow:

1. `zoom-meeting-bot`
2. Choose `[1] 처음 설정하기`
3. Finish the setup/config prompts
4. Choose `[4] 회의 참가`

Internally, the quickstart flow corresponds to:

- `init`
- `configure`
- `setup`
- `doctor`
- `start`

## Typical User Flow

The simplest user path is:

```bash
zoom-meeting-bot
```

Then inside the menu:

- `[1] 처음 설정하기`
- `[2] 런처 시작`
- `[4] 회의 참가`
- `[5] 현재 상태 보기`
- `[6] 결과물 스타일 관리`

For most end users, this menu flow is now the main entry point.

## Direct CLI Commands

If you prefer direct commands instead of the menu, these are the current equivalents:

### First-time setup

```bash
zoom-meeting-bot quickstart --preset launcher_dm --yes
```

### Join a meeting

```bash
zoom-meeting-bot create-session "회의링크" --passcode "암호" --open
```

### Runtime control

```bash
zoom-meeting-bot start
zoom-meeting-bot status
zoom-meeting-bot stop
```

### Diagnostics

```bash
zoom-meeting-bot show-config
zoom-meeting-bot doctor
zoom-meeting-bot support-bundle
```

### Skill management

```bash
zoom-meeting-bot skill --help
```

## Presets

### `runtime_only`

Use this if you want to validate the Zoom meeting engine and local export flow first.

- runs the Zoom runtime without launcher routing
- good for basic `meeting -> capture -> summarize -> PDF export` testing

### `launcher_dm`

This is the most practical first preset for end users.

- enables launcher mode
- keeps Telegram artifact delivery ready
- uses `personal_dm` as the default PDF artifact route

### `launcher_metheus`

Use this when you are integrating with a Metheus project route.

## What Users Need To Prepare

During setup/configuration, users may need:

- Zoom Client ID
- Zoom Client Secret
- Hugging Face token
- optional Telegram bot token and route information

Telegram is not required for every flow. If you only want local exports first, start with `runtime_only`.

## Output Defaults

Current packaged defaults are:

- base reference skill: `meeting-output-default`
- generated skills: stored in the user workspace
- PDF renderer: `html`
- meeting outputs: HTML/CSS-first PDF flow

The old DOCX-first path is no longer the default user experience.

## whisper.cpp And Model Preparation

`whisper.cpp` is treated as a core part of the runtime path.

The npm package includes the bundled runtime assets needed for the packaged flow, while heavy model files are prepared during setup as needed.

In practice, users should think of it like this:

- the npm package installs the CLI entry point
- first-run setup prepares the local runtime environment
- model and tool preparation still happens during setup/quickstart

## Workspace Location

By default, the package uses a user-level workspace instead of writing mutable data into the npm install directory.

Typical workspace roots:

- Windows: `%LOCALAPPDATA%\zoom-meeting-bot`
- macOS: `~/Library/Application Support/zoom-meeting-bot`
- Linux: `~/.local/share/zoom-meeting-bot`

This workspace holds things like:

- config files
- `.venv`
- exports
- audio archives
- generated skills
- runtime state files

## Important Note About Python

Although the package is installed through npm, the core runtime is Python-based.

That means:

- Node.js/npm is needed to install the package
- Python 3.11+ is needed to actually run the full system

If `zoom-meeting-bot` reports that Python 3.11+ was not found, install Python first and run the command again.

## Troubleshooting

### Show the effective config

```bash
zoom-meeting-bot show-config
```

### Check prerequisites and blocking issues

```bash
zoom-meeting-bot doctor
```

### Check runtime status

```bash
zoom-meeting-bot status
```

### Generate a support bundle

```bash
zoom-meeting-bot support-bundle
```

## Repository Workflow vs npm Workflow

There are now two different ways to use this project:

### 1. End-user npm workflow

Recommended for normal users:

```bash
npm install -g zoom-meeting-bot
zoom-meeting-bot
```

### 2. Source-checkout workflow

Still useful if you are developing directly inside this repository:

Windows:

```powershell
.\scripts\bootstrap.ps1
.\scripts\zoom-meeting-bot.ps1
```

macOS:

```bash
./scripts/bootstrap.sh
./scripts/zoom-meeting-bot.sh
```

The old script-based flow still exists, but it is no longer the main end-user onboarding path for the packaged CLI.

## Project Direction

The goal of this project is not to hardcode one single `WooBIN_bot` instance forever.

The goal is to let other users run the same Zoom meeting bot system on their own machines with:

- their own Zoom app credentials
- their own Hugging Face token
- their own Telegram routing
- their own local environment
- their own meeting-output style

That is why the product direction is now centered on:

- npm installation
- menu-driven CLI onboarding
- packaged default reference skill
- user-workspace-based runtime state
