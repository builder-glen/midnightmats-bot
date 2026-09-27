"""MidnightMats 클라우드 수집기 — GitHub Actions에서 매시간 실행.
블리자드 경매장 API → 한밤 직업 재료 시세 계산 → Supabase 적재 → (선택) Discord 알림.

설정은 환경변수(GitHub Secrets)에서 읽고, 없으면 ../companion/config.json 으로 대체한다(로컬 실행용).
  BLIZZARD_CLIENT_ID / BLIZZARD_CLIENT_SECRET / SUPABASE_DSN / DISCORD_WEBHOOK(선택)
"""
import base64
import json
import os
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path

import psycopg

BASE = Path(__file__).resolve().parent
REGION, LOCALE = "kr", "ko_KR"
MIN_ID = 236000          # 한밤(12.0) 아이템 ID 대역 시작
TRADESKILL_CLASS = 7     # 직업용품
# 서브클래스 → 직업 코드. Discord 메시지·대시보드 탭이 이 값으로 나뉜다
PROFESSION = {
    1: "eng", 4: "gem", 5: "cloth", 6: "leather", 7: "ore", 8: "cook", 9: "herb",
    10: "elem", 11: "other", 12: "ench", 16: "inscr", 18: "optional", 19: "finish",
}
MEDIA_PER_RUN = 60       # 아이콘·설명 조회는 실행당 이만큼만 (실행 시간 제한)
RETENTION_DAYS = 90      # 시간별 원본 보관 기간. 지나면 일별 요약으로 압축


def log(msg):
    print(f"[{datetime.now():%Y-%m-%d %H:%M:%S}] {msg}", flush=True)


def load_config():
    env = {k: os.environ.get(k) for k in ("BLIZZARD_CLIENT_ID", "BLIZZARD_CLIENT_SECRET", "SUPABASE_DSN", "DISCORD_WEBHOOK")}
    if env["BLIZZARD_CLIENT_ID"] and env["SUPABASE_DSN"]:
        return env
    local = BASE.parent / "companion" / "config.json"
    if not local.exists():
        sys.exit("설정 없음: 환경변수 또는 companion/config.json 이 필요합니다")
    c = json.load(open(local))
    return {"BLIZZARD_CLIENT_ID": c["client_id"], "BLIZZARD_CLIENT_SECRET": c["client_secret"],
            "SUPABASE_DSN": c["supabase_dsn"], "DISCORD_WEBHOOK": c.get("discord_webhook")}


def with_retry(fn, what, attempts=3, delay=30):
    for i in range(1, attempts + 1):
        try:
            return fn()
        except urllib.error.HTTPError as e:
            if 400 <= e.code < 500:
                raise  # 키 오류 등은 재시도해도 소용없음
            log(f"{what} 실패({i}/{attempts}, HTTP {e.code}) — {delay}초 후 재시도")
        except Exception as e:
            log(f"{what} 실패({i}/{attempts}, {e}) — {delay}초 후 재시도")
        if i == attempts:
            raise RuntimeError(f"{what} 최종 실패")
        time.sleep(delay)


def http_json(url, headers=None, data=None, timeout=120):
    req = urllib.request.Request(url, headers=headers or {}, data=data)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def get_token(cfg):
    cred = base64.b64encode(f"{cfg['BLIZZARD_CLIENT_ID']}:{cfg['BLIZZARD_CLIENT_SECRET']}".encode()).decode()
    d = http_json("https://oauth.battle.net/token", headers={"Authorization": f"Basic {cred}"},
                  data=b"grant_type=client_credentials", timeout=30)
    return d["access_token"]


def api(token, path):
    return http_json(f"https://{REGION}.api.blizzard.com{path}", headers={"Authorization": f"Bearer {token}"}, timeout=60)


def fetch_commodities(token):
    """경매장 스냅샷과 그 생성 시각(Last-Modified). 수집 시각이 아니라 이 값을 기준 시각으로 쓴다."""
    url = f"https://{REGION}.api.blizzard.com/data/wow/auctions/commodities?namespace=dynamic-{REGION}&locale={LOCALE}"
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}"})
    with urllib.request.urlopen(req, timeout=300) as resp:
        lm = resp.headers.get("Last-Modified")
        snap_ts = parsedate_to_datetime(lm) if lm else datetime.now(timezone.utc)
        data = json.loads(resp.read().decode("utf-8"))
    return data.get("auctions", []), snap_ts


def market_price(listings):
    """시장가 = 가격 오름차순으로 누적 수량이 전체의 1%에 도달하는 지점의 호가.
    (1개짜리 초저가 낚시 매물에 휘둘리지 않기 위함). (시장가, 최저가, 총수량, 매물건수) 반환."""
    listings.sort(key=lambda x: x[0])
    total = sum(q for _, q in listings)
    threshold = max(1, total // 100)
    acc = 0
    for price, qty in listings:
        acc += qty
        if acc >= threshold:
            return price, listings[0][0], total, len(listings)
    return listings[-1][0], listings[0][0], total, len(listings)


def sync_items(token, pg, by_item):
    """검색 API로 한밤 직업 재료 전체를 받아 items를 갱신한다.
    - 경매장에 한 번이라도 매물이 잡힌 아이템만 tracked=true (퀘스트/귀속템 제외)
    - 품질 등급: data/tiers.json(Wowhead 검증값) 우선, 없으면 'ID 오름차순 = 품질 오름차순'
    - 연금(alch) 등 이 함수 범위 밖의 행은 건드리지 않는다"""
    found = []
    for sid in PROFESSION:
        page = 1
        while True:
            r = api(token, f"/data/wow/search/item?namespace=static-{REGION}&item_class.id={TRADESKILL_CLASS}"
                           f"&item_subclass.id={sid}&id=[{MIN_ID},]&orderby=id&_pageSize=100&_page={page}")
            for x in r.get("results", []):
                d = x["data"]
                if d["id"] >= MIN_ID:
                    found.append((d["id"], d.get("name", {}).get(LOCALE), TRADESKILL_CLASS, sid,
                                  PROFESSION[sid], d.get("quality", {}).get("type", "COMMON")))
            if page >= r.get("pageCount", 1):
                break
            page += 1

    tiers_file = BASE / "data" / "tiers.json"
    tiers = {int(k): v for k, v in json.load(open(tiers_file)).items()} if tiers_file.exists() else {}
    by_name = {}
    for row in found:
        by_name.setdefault(row[1], []).append(row[0])
    rows = []
    for iid, name, cls, sid, prof, rarity in found:
        ids = sorted(by_name[name])
        tier = tiers.get(iid) or (ids.index(iid) + 1 if len(ids) > 1 else None)
        rows.append((iid, name, cls, sid, prof, tier, rarity, iid in by_item))
    with pg.cursor() as cur:
        cur.executemany(
            """insert into items (id, name, class_id, subclass_id, profession, tier, rarity, tracked)
               values (%s,%s,%s,%s,%s,%s,%s,%s)
               on conflict (id) do update set
                 name = excluded.name, subclass_id = excluded.subclass_id, profession = excluded.profession,
                 tier = excluded.tier, rarity = excluded.rarity,
                 tracked = items.tracked or excluded.tracked, updated_at = now()""",
            rows,
        )
    n = pg.execute("select count(*) from items where tracked").fetchone()[0]
    log(f"아이템 갱신: 한밤 직업 재료 {len(rows)}개 조회, 추적 중 {n}개")


def sync_media(token, pg):
    """추적 아이템의 아이콘·설명을 아이템당 최초 1회 채운다 (실행당 MEDIA_PER_RUN개까지)."""
    ids = [r[0] for r in pg.execute(
        "select id from items where tracked and (icon_url is null or description is null) order by id limit %s",
        (MEDIA_PER_RUN,))]
    for iid in ids:
        icon, text = "", ""
        try:
            m = api(token, f"/data/wow/media/item/{iid}?namespace=static-{REGION}")
            icon = next((a["value"] for a in m.get("assets", []) if a.get("key") == "icon"), "")
            d = api(token, f"/data/wow/item/{iid}?namespace=static-{REGION}&locale={LOCALE}")
            spells = [s.get("description", "") for s in d.get("preview_item", {}).get("spells", [])]
            text = (" ".join(t for t in spells if t) or d.get("description", "") or "")
            text = text.replace("경매장에서 거래할 수 있습니다.", "").strip()
        except Exception:
            pass  # 실패해도 빈 값으로 채워 매번 재시도하지 않음
        pg.execute("update items set icon_url = %s, description = %s where id = %s", (icon, text, iid))
    if ids:
        log(f"아이콘·설명 {len(ids)}개 채움")


def store_snapshot(pg, snap_ts, auctions, by_item):
    tracked = {r[0] for r in pg.execute("select id from items where tracked")}
    rows = []
    for iid, listings in by_item.items():
        if iid in tracked:
            price, min_price, qty, n = market_price(listings)
            rows.append((snap_ts, iid, price, min_price, qty, n))
    with pg.cursor() as cur:
        cur.execute("insert into snapshots (ts, auction_count) values (%s, %s)", (snap_ts, len(auctions)))
        with cur.copy("copy prices (ts, item_id, price, min_price, qty, listings) from stdin") as cp:
            for r in rows:
                cp.write_row(r)
    log(f"스냅샷 저장: {snap_ts.astimezone():%m/%d %H:%M} 기준 추적 아이템 {len(rows)}개")


def rollup_old(pg):
    """RETENTION_DAYS 지난 시간별 원본을 일별 요약으로 옮기고 삭제한다."""
    cutoff = f"now() - interval '{RETENTION_DAYS} days'"
    pg.execute(f"""
        insert into prices_daily (day, item_id, price_min, price_avg, price_max, qty_avg)
        select (ts at time zone 'Asia/Seoul')::date, item_id, min(price), avg(price)::bigint, max(price), avg(qty)::int
        from prices where ts < {cutoff}
        group by 1, 2
        on conflict (item_id, day) do nothing""")
    n = pg.execute(f"delete from prices where ts < {cutoff}").rowcount
    if n:
        log(f"{RETENTION_DAYS}일 지난 시간별 데이터 {n}행을 일별 요약으로 압축")


def top_movers(pg, min_listings=20, n=5):
    """직업별 등락 Top N: 현재가 vs 7일 평균. 매물 min_listings건 미만은 제외 (얇은 시장 잡음 방지)."""
    rows = pg.execute("""
        with latest as (select max(ts) as ts from snapshots),
        hist as (  -- 최근 7일간 시세 이력 개수. 하루치(24회) 미만이면 평균이 의미 없어 제외
            select p.item_id, count(*) as n from prices p, latest
            where p.ts >= latest.ts - interval '7 days' group by p.item_id)
        select s.profession, s.name, s.tier, s.cur, s.a7, p.listings,
               round((s.cur - s.a7) * 100.0 / s.a7, 1) as pct
        from v_item_stats s
        join prices p on p.item_id = s.id and p.ts = (select ts from latest)
        join hist h on h.item_id = s.id
        where s.a7 > 0 and p.listings >= %s and h.n >= 24
        order by s.profession, pct desc""", (min_listings,)).fetchall()
    out = {}
    for prof in sorted({r[0] for r in rows}):
        g = [r for r in rows if r[0] == prof]
        up = [r for r in g if r[6] > 0][:n]
        down = [r for r in reversed(g) if r[6] < 0][:n]
        if up or down:
            out[prof] = {"up": up, "down": down}
    return out


def gold(copper):
    g = copper / 10000
    return f"{g:,.2f}g" if g < 10 else f"{g:,.0f}g"  # 물고기처럼 1g 미만인 것은 소수점까지


def preview_movers(movers):
    for prof, d in movers.items():
        log(f"── {prof} ──")
        for label, rows in (("▲", d["up"]), ("▼", d["down"])):
            for _, name, tier, cur, a7, listings, pct in rows:
                t = f"★{tier}" if tier else ""
                log(f"  {label} {name}{t} {gold(cur)} ({pct:+.1f}% vs 7일, 매물 {listings}건)")


def main():
    cfg = load_config()
    log("액세스 토큰 발급")
    token = with_retry(lambda: get_token(cfg), "토큰 발급")
    log("경매장 스냅샷 다운로드")
    auctions, snap_ts = with_retry(lambda: fetch_commodities(token), "경매장 다운로드")
    log(f"매물 {len(auctions):,}건 (스냅샷 {snap_ts.astimezone():%m/%d %H:%M})")

    by_item = {}
    for a in auctions:
        by_item.setdefault(a["item"]["id"], []).append((a["unit_price"], a["quantity"]))

    with psycopg.connect(cfg["SUPABASE_DSN"], connect_timeout=30) as pg:
        # 아이템 목록·아이콘 갱신은 스냅샷이 새것이 아니어도 매번 진행 (아이콘 채우기가 여러 실행에 걸쳐 진행됨)
        sync_items(token, pg, by_item)
        sync_media(token, pg)
        if pg.execute("select 1 from snapshots where ts = %s", (snap_ts,)).fetchone():
            pg.commit()
            log("이미 저장된 스냅샷 — 시세 저장 생략")
            return
        store_snapshot(pg, snap_ts, auctions, by_item)
        rollup_old(pg)
        pg.commit()
        preview_movers(top_movers(pg))
    log("완료")


if __name__ == "__main__":
    main()
