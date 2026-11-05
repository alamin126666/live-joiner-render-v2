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
- `bot.py` er bhitor `PREMIUM_EMOJI_IDS` e sob normal emoji er pashe `"GET_PREMIUM_ID"` bosano ache.
- Oi jaygay premium emoji er ID boshan (sudhu songkha). ID na boshale oi emoji normal-ই thakbe, error hoy na.
- Message ar button duitei auto premium hoy. Button er **shurur** emoji button icon hoy.
- Limit: inline mode ar channel e kaj kore na.
