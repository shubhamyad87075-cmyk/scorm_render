# 🎓 SCORM Simulator

## Local Run
```bash
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
playwright install chromium
python main.py
```
Open: http://localhost:5000

---

## Deploy to Railway

### Step 1 — Push to GitHub
```bash
cd scorm-simulator-v2
git init
git add .
git commit -m "SCORM Simulator"
git branch -M main
git remote add origin https://github.com/YOUR_USERNAME/scorm-simulator.git
git push -u origin main
```

### Step 2 — Create Railway project
1. Go to railway.app
2. New Project → Deploy from GitHub
3. Select your repo
4. Railway auto-detects Dockerfile

### Step 3 — Set Environment Variables in Railway
Go to your Railway project → Variables tab → Add these:

| Variable | Value |
|----------|-------|
| `NORDVPN_USER` | Your NordVPN SOCKS5 username |
| `NORDVPN_PASS` | Your NordVPN SOCKS5 password |
| `SCORM_CONFIG` | Full JSON of your config (see below) |

### Step 4 — SCORM_CONFIG format
Paste this as the value (replace with real data):
```json
{"lms":{"url":"https://inco.docebosaas.com","learning_plan_id":41,"learning_plan_slug":"green-pathways"},"courses":[{"id":290,"name":"Module 1","lessons":6,"required":true,"slug":"module-1-foundations-of-sustainability"},{"id":291,"name":"Module 2","lessons":6,"required":true,"slug":"module-2-energy-transition"},{"id":292,"name":"Module 3","lessons":5,"required":true,"slug":"module-3-career-pathways"},{"id":293,"name":"Module 4","lessons":5,"required":false,"slug":"module-4-professional-skills"}],"simulation":{"min_minutes_per_lesson":6,"max_minutes_per_lesson":15,"score_min":82,"score_max":95,"include_optional_module":true,"stagger_start_seconds":60,"days_between_modules_min":1,"days_between_modules_max":4},"test_mode":{"enabled":false,"speed_multiplier":3600},"proxy":{"type":"socks5","port":1080,"username":"NORDVPN_USER","password":"NORDVPN_PASS","servers":{"in":"in.socks.nordhold.net","ae":"ae.socks.nordhold.net","gb":"gb.socks.nordhold.net","us":"us.socks.nordhold.net","de":"de.socks.nordhold.net","fr":"fr.socks.nordhold.net","sg":"sg.socks.nordhold.net","au":"au.socks.nordhold.net","ca":"ca.socks.nordhold.net","nl":"nl.socks.nordhold.net"}},"accounts":[{"email":"real@email.com","password":"realpass","proxy_country":"in","name":"User Name"}]}
```

### Step 5 — Get Railway URL
Railway gives you a URL like: `https://scorm-simulator-production.up.railway.app`
Open it → click ▶️ Start All

---

## Environment Variables Reference

| Variable | Required | Description |
|----------|----------|-------------|
| `PORT` | Auto | Set by Railway automatically |
| `NORDVPN_USER` | Yes | NordVPN SOCKS5 username |
| `NORDVPN_PASS` | Yes | NordVPN SOCKS5 password |
| `SCORM_CONFIG` | Optional | Full config JSON (overrides config.json) |
| `TEST_MODE` | Optional | `true` to enable test mode |
