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
