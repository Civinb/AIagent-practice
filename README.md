# Multi-Gmail Mail Triage Tool

Fetch new mail from several Gmail accounts → use Gemini to categorize each email, set its priority and write "what to do" → in a local web UI, tick the ones you need to add them to your to-dos and discard the rest.

- Runs only on this machine; the UI listens only on `127.0.0.1`, so other devices on the same network cannot open it.
- Reads Gmail only: it never sends, deletes or modifies any Gmail message.

## First-time setup

### 0. Preparation (once)

1. The project folder already has the virtual environment `.venv`, with dependencies installed:
   ```bash
   .venv\Scripts\python.exe -m pip install -r requirements.txt
   ```
2. `credentials.json` and `accounts.json` are in the project folder.
3. Set the environment variable `GEMINI_API_KEY` (for double-clicking `start.bat` it must be a "user variable"; setting it temporarily in one terminal does not count):
   Search the Start menu for "Edit environment variables for your account" → User variables → New → variable name `GEMINI_API_KEY`, variable value: your key → OK.
   After setting it, reopen the terminal or double-click `start.bat` again for it to take effect.

### 1. Authorize

Open a terminal in the project folder:

```bash
.venv\Scripts\activate
```

```bash
python authorize.py
```

Your browser opens the authorization page for each account in turn; follow the terminal prompts and choose the right mailbox. On success the token is saved to `tokens/<alias>.json`.

### 2. Start

Double-click `start.bat`. It changes to the project folder, checks `GEMINI_API_KEY`, activates `.venv`, then runs `python app.py`.
When an address appears in the window, open it in your browser (usually http://127.0.0.1:7860 ; if the port is taken, Gradio picks another one, so go by what the window shows).

To close the UI: just close the `start.bat` window; or press Ctrl+C, and when "Terminate batch job (Y/N)?" appears, press N to keep the window open and read the output.

## ⚠ Reauthorize every 7 days

While the Google Cloud project's publishing status is "Testing", authorization tokens expire after about **7 days** and fetching reports an authorization error.
Every 7 days (or whenever you see an authorization error), run this once for each account:

```bash
python authorize.py work
```

Replace `work` with each alias in `accounts.json` and run them one at a time. The last authorization time of each account is recorded in `tokens/authorized_dates.json`.
Note: `python authorize.py` without an alias only handles accounts that "have no token yet"; accounts that already have an (expired) token need the alias.
