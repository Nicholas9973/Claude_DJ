# Setting up the Discord bot

About 10 minutes. You can do this now; the code that uses it arrives in phase 4.
Discord's developer site changes its layout from time to time, so labels may
differ slightly from what's written here.

## 1. Make a private server

1. In the Discord app, click **+** ("Add a Server") at the bottom of the server list.
2. Choose **Create My Own** -> **For me and my friends**, name it (e.g. "Home Music").
3. Create a text channel for requests, e.g. `#music`.

## 2. Create the bot application

1. Go to <https://discord.com/developers/applications> and log in.
2. **New Application**, name it (e.g. "Claude DJ"), accept the terms.
3. Open the **Bot** page in the left menu.
4. Under **Privileged Gateway Intents**, turn on **Message Content Intent** and
   **Save Changes**. Without it the bot receives messages with empty text.
5. Turn **Public Bot** off, so only you can add it to servers. If Discord
   refuses, first go to the **Installation** page, set **Install Link** to
   **None**, save, then come back and turn Public Bot off.
6. Click **Reset Token**, confirm, and copy the token.
   **Treat it like a password.** Anyone with it can act as your bot.
   If it ever leaks (pasted in chat, committed to git), reset it again here.

## 3. Invite the bot to your server

1. Open **OAuth2** in the left menu, then the **URL Generator**.
2. Scopes: tick **bot** and **applications.commands**.
3. Bot permissions: tick **View Channels**, **Send Messages**,
   **Read Message History**, **Add Reactions**, **Embed Links**.
4. Copy the generated URL at the bottom, open it in your browser, pick your
   server, **Authorize**. The bot appears in the member list (offline until
   our code runs).

## 4. Collect the IDs

1. In Discord: **User Settings** (gear) -> **Advanced** -> turn on **Developer Mode**.
2. Right-click your own name -> **Copy User ID**.
3. Right-click the `#music` channel -> **Copy Channel ID**.

## 5. Store them for the project

Copy `.env.example` to a file named `.env` in `C:\dev\Claude_music` and fill
it in (`.env` is in `.gitignore`, so git will not pick it up):

```
DISCORD_TOKEN=paste-the-bot-token-here
DISCORD_ALLOWED_USER_IDS=123456789012345678
DISCORD_CHANNEL_ID=123456789012345678
```

Don't send me the token in chat; the code reads it from this file. The two
IDs are not secret, so you can share those if something needs debugging.

## 6. Run it

```powershell
python -m music_agent.discord_bot
```

When it says "Logged in as ...", write in your channel: `!help`, then try
"play something upbeat".

Troubleshooting:
- "Discord rejected the token": reset the token on the Bot page and update `.env`.
- "Turn on Message Content Intent": step 2.4 above.
- The bot is online but never answers: check that `DISCORD_CHANNEL_ID` is the
  channel you write in and that your ID is in `DISCORD_ALLOWED_USER_IDS`.
  Messages from anyone else are ignored on purpose, without a reply.
