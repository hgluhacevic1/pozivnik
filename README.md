# Pozivnik

Javni pozivi, grantovi i donatori u Bosni i Hercegovini na jednom mjestu.

- `index.html` – web stranica (GitHub Pages), povezana sa Supabase bazom.
- `pratilac.py` + `izvori.json` – program koji svaki dan obiđe 243 izvora i nove pozive upiše u bazu kao kandidate.
- `.github/workflows/pratilac.yml` – pokreće pratilac svaki dan u 06:17.

Tajne (Settings → Secrets and variables → Actions):
`SUPABASE_URL`, `SUPABASE_SERVICE_KEY`, po želji `TELEGRAM_TOKEN`, `TELEGRAM_CHAT_ID`, `ANTHROPIC_API_KEY`.
