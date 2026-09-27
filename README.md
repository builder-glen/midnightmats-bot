# midnightmats-bot

WoW 한밤(Midnight) 직업 재료 시세 수집기. GitHub Actions가 매시간 블리자드 경매장 API를 읽어 Supabase에 적재하고 Discord에 등락 요약을 올린다.

- `collect.py` — 수집 본체. 로컬 실행 시 `../companion/config.json`을 읽는다
- `data/tiers.json` — Wowhead로 검증한 제작 품질 등급 (같은 이름의 ID 여러 개 구분용)
- `.github/workflows/hourly.yml` — 매시 40분 스케줄

필요한 GitHub Secrets: `BLIZZARD_CLIENT_ID`, `BLIZZARD_CLIENT_SECRET`, `SUPABASE_DSN`, `DISCORD_WEBHOOK`(선택)
