# Century Bot

Opens Century Tech in a browser window, picks lessons by subject, answers the
tasks with an AI model (Groq), and can report stats to your phone via Telegram.

Use it at your own risk: automation may break your school's or Century's rules.

## Requirements

- Windows 10 or newer (macOS / Linux also work, colours may differ)
- Python 3.12 or newer
- A Groq API key (https://console.groq.com -> API Keys)
- Optional: a Telegram bot token from @BotFather

## Install

Open PowerShell or Windows Terminal in the folder with the files:

```powershell
pip install -r requirements.txt
playwright install chromium
```

## Run

Run it in a real terminal (not IDLE, which cannot show the colours):

```powershell
python bot_century.py
```

1. The menu opens. Set subjects, number of lessons, speed, human mode and
   Telegram, then press `1` to start.
2. A browser window opens. Log in to Century there, then press Enter in the
   terminal. The login is saved in `century_state.json`.
3. The bot opens a random lesson in your chosen subjects and works through it.

### Menu

| Key | What it does |
|-----|--------------|
| 1 | Start the bot |
| 2 | Choose subjects (type `1,3` to switch on/off, `A` for all) |
| 3 | Number of lessons |
| 4 | Speed: Normal / Fast / Slow |
| 5 | Human mode on / off (random delays, mouse movement, typing) |
| 6 | Record my clicks (debug: shows what you click so selectors can be fixed) |
| 7 | Telegram stats on / off |
| 8 | Choose AI model (lists the models your Groq key can use) |
| 9 | Auto-clear the console between lessons on / off |
| L | Detailed logs on / off (off = clean console, on = every technical step) |
| D | Delete debug files (keeps login, settings and history) |
| 0 | Quit |

Your menu choices are saved and loaded again next time (`century_settings.json`).

English only uses Reading and SPaG lessons. Anything with "Landmarks" or
"Legends" is skipped.

## Settings

Set these as environment variables. Never put a key or token in the source
file or commit it to the repository.

| Variable | Purpose | Default |
|----------|---------|---------|
| `LLM_API_KEY` | Groq API key | empty (required) |
| `LLM_MODEL` | Text model | `openai/gpt-oss-120b` |
| `LLM_VISION_MODEL` | Model for tasks with images | `meta-llama/llama-4-scout-17b-16e-instruct` (auto-switches if unavailable) |
| `LLM_BASE_URL` | API address | `https://api.groq.com/openai/v1` |
| `TG_TOKEN` | Telegram bot token | empty (Telegram off) |
| `TG_CHAT_ID` | Skip pairing, use this chat | empty |

PowerShell example:

```powershell
$env:LLM_API_KEY="your_key"
python bot_century.py
```

Never share the script with your key inside, and never post keys or tokens in
chats or on GitHub. If a key was exposed, delete it in the Groq console and
create a new one.

## Telegram stats

1. In Telegram, message **@BotFather**, send `/newbot`, copy the token.
2. Put the token into `TG_TOKEN` (in the file or as an environment variable).
   Use a new bot, not one you already run elsewhere.
3. Start the bot. The terminal shows a 4-digit code. Send `/start 1234`
   (your code) to your bot. Only that chat is accepted from then on.

Commands: `/stats`, `/history`, `/pause`, `/resume`, `/shot` (screenshot), `/stop`.

It reports lessons, questions, time and how often it needed you. It does not
track scores or XP. Commands only work while the script is running.

## Extra features

- **Live progress line** after every question: progress bar, lessons done,
  questions answered, running time and an estimated finish time.
- **Lesson history** in `century_history.csv` (time, subject, questions,
  duration). `/history` in Telegram shows the last lessons and all-time totals.
- **Sound alert** on the PC (three beeps on Windows) when the bot needs you.
- **Automatic retry** when Groq is busy or you hit a rate limit (it waits as
  long as Groq asks, up to 5 tries). A wrong model name is not retried.
- **Saved settings and model picker**: pick a model from your own Groq list
  with menu option 8, and it is remembered. `LLM_MODEL` in the environment
  overrides the saved model.

## Files it creates (next to the script)

| File | What it is |
|------|------------|
| `century_state.json` | Saved Century login |
| `century_settings.json` | Your menu choices and chosen model |
| `century_history.csv` | One row per finished lesson |
| `century_telegram.json` | Your paired Telegram chat |
| `century_debug.txt` / `.png` | What the bot saw when it could not open a lesson |
| `century_clicks.txt` | Output of the click recorder (menu option 6) |
| `century_unknown_N.png` / `.html` | A task type the bot could not solve |

## Troubleshooting

- **Menu is cut off or old text is above it:** the console is cleared every time the
  menu is drawn, and in a short window (the default cmd is 30 rows) a compact menu
  is used so it fits. Maximise the window if you want the big banner. Inside IDLE or
  an IDE the console cannot be wiped, so the script pushes old text out of view with
  blank lines. Run it in cmd, PowerShell or Windows Terminal for a real clear.
- **Too many technical lines in the console:** they are hidden by default. The console
  shows only the lesson header, a progress line after each question, a line when a
  lesson finishes, and real problems. Turn on Detailed logs (menu L) when you need to
  debug something.
- **Console too full while running:** with Auto-clear on (menu 9) the console is
  wiped before every lesson after the first, so you only see the current lesson.
  The full record stays in `century_history.csv`.

- **Syntax error after copy-paste:** download the file instead of copying it,
  then run `python -m py_compile bot_century.py`.
- **404 model does not exist:** change `LLM_MODEL`. List your models with
  `OpenAI(api_key=..., base_url="https://api.groq.com/openai/v1").models.list()`.
- **Lessons do not open:** use menu option 6, click through a lesson by hand,
  and send the printed `CLICK` lines, or send `century_debug.txt`.
- **A task type is not solved:** the bot asks you to do it by hand and saves
  `century_unknown_N.html`. Send that file's contents to get support added.
- **Matching and drag-and-drop tasks** are best effort and may need adjusting.
- **Telegram silent:** check the token, make sure you sent `/start CODE` with
  the code from the terminal, and that no other program uses the same bot.
