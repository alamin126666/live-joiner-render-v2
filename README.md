# Vc Joiner Bot Improve — Render + cron-job.org Setup

## 1) GitHub e upload
1. GitHub e notun **Private** repo banan (session data ase, tai Private rakhun).
2. Zip extract kore shob file repo te upload/push korun.
   (`.env` ba `accounts.json` kokhono upload korben na.)

## 2) my.telegram.org theke API_ID & API_HASH nin
`API development tools` e giye app create korun.

## 3) Render e deploy
1. render.com -> **New +** -> **Blueprint** -> repo select korun (`render.yaml` auto-detect hobe).
   Ba manually: **New Web Service** -> Build: `pip install -r requirements.txt` -> Start: `python bot.py`
2. **Environment** tab e ei 4ta set korun:

| Key | Value |
|---|---|
| BOT_TOKEN | @BotFather er token |
| API_ID | my.telegram.org theke |
| API_HASH | my.telegram.org theke |
| OWNER_ID | apnar Telegram numeric ID |

3. Deploy dile Logs e `✅ Bot is running!` dekhben.
4. Service URL copy korun, jemon: `https://vc-joiner-bot.onrender.com`

## 4) cron-job.org diye 24/7 alive rakha
Render Free plan e 15 min request na pele service ghumiye jay. Tai:
1. cron-job.org e account khulun -> **Create cronjob**
2. **URL:** `https://YOUR-APP.onrender.com/health`
3. **Schedule:** Every **5 minutes** (14 min er niche rakhben)
4. **Request method:** GET -> Save
5. Test run korle `✅ Bot is alive!` response asbe.

Alternative lightweight URL: `/ping` (response: `pong`)

## 5) Gurutwopurno shotorkota (accounts.json)
Render Free plan er disk **ephemeral** — restart/redeploy hole `accounts.json` mushe jay.
- Prottek deploy er age bot er **📤 BOT EXPORT** diye session file backup rakhun,
  porer bar **📥 BOT IMPORT** diye ferot nin.
- Permanent korte chaile paid plan e Render **Disk** (mount path `/data`) add kore
  env var `DATA_DIR=/data` din.

## 6) Notes
- Free plan e RAM 512MB; onek account dile memory sesh hote pare.
- Session string = account er full access. Keu er sathe share korben na.
- Environment variable missing thakle bot crash na kore Logs e error dekhabe.

## Ki ki improve kora hoyeche
- `DATA_DIR` support (persistent disk er jonno)
- Atomic JSON save (file corrupt hoy na)
- `/health` e uptime, notun `/ping` endpoint
- Missing env var check + global error handler
- Render Blueprint (`render.yaml`) + Python 3.11 pin

## Notun Admin System
**➕ ADD ADMIN** → User ID din → `ADMIN SETTING ON/OFF` message (red = OFF, green = ON, NEXT sobsomoy green):
- 🪖 **ADMIN SETTING ACCESS** ON: oi admin Owner er moto admin add/remove korte parbe (nijer add kora admin-ei dekhbe). OFF: button o thakbe na, access o na.
- ♻️ **BOT IMPORT & EXPORT** ON: import/export button + access. OFF: button thakbe na, access o na.
- **NORMAL** (default) / **PREMIUM** (ekta-i green thakbe).
- NEXT → confirm message → **YES** (green) / **NOO** (red).
- PREMIUM + YES → "kotogula account dibe?" → number dile admin add hobe.

### Premium account dewa
- 20 ta account, 5 ta dile pool 15 hoye jay; porer admin ke dewar somoy 15 ta dekhabe.
- Jei account ekjon pelo seta onno admin ke dewa jabe na.
- Receiver: shudhu **use** (Join Live / Join CH/GRP) korte parbe. ALL ACCOUNT e number masked (🎁), remove/export nei. Nijer add kora account full dekhbe, remove/export korte parbe.
- Giver: ALL ACCOUNT e sob dekhbe (🔒), export korte parbe, kintu oi account use/remove korte parbe na — jotokhon admin remove na kore.
- Admin remove korle account gulo giver er kache ferot ashe (chalu VC thakle bondho hoy).
- Ager `admins.json` (list format) auto-migrate hoy: settings OFF, import/export ON, NORMAL.
- Notun permission/keyboard refresh er jonno admin ke `/start` dite hobe.

## Premium Emoji (Bot API 9.4+)
Bot jei Telegram account diye BotFather e banano, oi account e **Telegram Premium** thakte hobe.
- Beshi emoji soriye shudhu dorkari **28 ta** emoji rakha hoyeche. Baki jaygay **text + 𝗳𝗼𝗻𝘁** (𝗠𝗮𝘁𝗵 𝗦𝗮𝗻𝘀 𝗕𝗼𝗹𝗱) use kora hoyeche.
- `bot.py` er `PREMIUM_EMOJI_IDS` e **1 emoji = 1 line**, sobar pashe `"GET_PREMIUM_ID"` bosano ache (comment e lekha ache kothay use hoy).
- Oi `GET_PREMIUM_ID` er jaygay premium emoji er ID (shudhu songkha) boshan. ID na boshale normal emoji thakbe, error hoy na.
- Notun emoji use korle dict e ekta line add korun: `"😀": "123456789",`
- Message ar button duitei auto premium hoy. Button er **shurur** emoji button icon hoy.
- Limit: inline mode ar channel e kaj kore na.

## UI / Animation
- **/start** e dashboard (ID, Role, Accounts, Lent/Received, Admins) + ❤️ message effect.
- **ALL ACCOUNT / ALL ADMIN** text-header table: `NO ┃ NUMBER ┃ DATE ┃ LIVE` / `NO ┃ ID ┃ TYPE ┃ SET ┃ I/E ┃ ACC`, stat line, PRV/NXT + 🔄 REFRESH + REMOVE.
- Table load howar somoy **bouncing bar + spinner** animation (`⠋ Loading... ▱▱▰▰▰▰▱▱▱▱▱▱`) — jotokkhon load hoy totokkhon cholte thake.
- Join Live / Join CH/GRP / Import / Leave VC / Add Account e **live ticker**: spinner ghorte thake, progress bar er matha blink kore,
  account process hoy ar count update hoy. Edit flood-safe (same text hole edit hoy na, `RetryAfter` e auto thame).
- Success message (login, join, admin add, import) e message effect. Effect support na korle auto effect chara jay.
- Prompt gulote `/cancel` hint.

## Mini App — ALL ACCOUNT / ALL ADMIN panel (white premium UI)
`🗄 ALL ACCOUNT` ar `🗄 ALL ADMIN` button e click korle bot e message ashbe na — Telegram er vitorei ekta panel khule jabe.
- Account table: search, filter (All / Live / Offline / Mine / Lent / Received), live status (progressively check hoy), page, **remove**.
- Admin table: search, filter, flags (Settings / Import-Export / accounts given / added by), page, **remove**.
- Import / Export panel e nei (bot e ager moto-i ache). Baki sob bot e ager moto.
- UI shudhu white (light) theme, shudhu SVG icon (kono emoji nei), smooth animation + haptic.

### Setup
Render e `RENDER_EXTERNAL_URL` auto set thake, tai kichu korte hobe na. Custom domain hole env var din:
`WEBAPP_URL=https://your-app.onrender.com`  (https hote hobe)
Deploy er por bot e `/start` din — keyboard e notun button ashbe (puraton keyboard e chaple "Open Panel" button ashe).
WEBAPP_URL / https na thakle bot ager moto message-table diye cholbe (fallback).

### Keyboard button kivabe login hoy
Telegram reply-keyboard button theke khulle `initData` pathay na. Tai bot prottek user er keyboard e **tar nijer signed token** URL e boshay
(`uid + meyad + HMAC`). Token 3 din chole, ar bot jotobar keyboard pathay (prottek reply te) notun token boshe. Keyboard onek din purono hole
panel "Link expired" dekhay — `/start` dile notun token ashe. Inline "OPEN PANEL" ar Menu button e Telegram er signed `initData` use hoy.

### Security
- Panel link browser e (ba onno keu) khulle kichui dekhabe na — shudhu "Private panel" lock screen.
- Server prottek request e Telegram er signed `initData` ba bot er signed keyboard-token (HMAC-SHA256, bot token theke banano key) verify kore; fake/tampered/meyad-sesh hole 401.
- Token apnar nijer — keyboard er link kauke share korben na. Admin remove hole token thakleo access sathe sathe bondho (prottek request e admin check hoy).
- Tarpor bot er admin list check kore (admin na hole 403). Admin permission (ADMIN SETTING ACCESS) server e abar check hoy.
- Session string kokhono browser e jay na. Account id gulo opaque (HMAC), "Received" account er number masked.
- Lent account remove kora jay na (bot er motoi). Admin shudhu nijer add kora admin remove korte pare.
