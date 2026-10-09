# Mirror · daily feed

Every morning this collects **student jobs in Berlin** (Werkstudent, working student,
internship, Praktikum, thesis) from companies' **own career pages**, and **student
events** from universities' **own calendars**. New items wait for you in
Mirror → Settings → Admin → **Review feed**. Nothing is shown to students until you approve it.

- `sources.json`: the companies and calendars. Add or pause sources here.
- `mirror_feed.py`: the script (Python, no extra packages).
- `.github/workflows/daily.yml`: runs it every morning on GitHub, for free.

The two secrets live in this repository's Settings → Secrets and variables → Actions:
`SUPABASE_URL` and `SUPABASE_SERVICE_KEY` (the secret key from Supabase → Project Settings → API Keys).
Never put the secret key in the extension or anywhere public.
