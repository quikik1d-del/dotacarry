"""Собирает керри-игры 7000+ MMR (Immortal) за последние 24 часа через OpenDota API.
Результат: data.json (для сайта) и cache.json (накопление между запусками).
Переменные окружения: OPENDOTA_KEY (необязательно), MAX_FETCH (матчей за запуск, по умолчанию 120).
"""
import json, os, time, urllib.request, urllib.parse
from collections import Counter, defaultdict
from statistics import median

API = "https://api.opendota.com/api"
KEY = os.environ.get("OPENDOTA_KEY", "")
MAX_FETCH = int(os.environ.get("MAX_FETCH", "120"))
PAUSE = 0.15 if KEY else 1.1
DAY = 86400
MIN_RANK = 80          # Immortal
MIN_GAMES = 3          # минимум игр на героя для показа
BIG_ITEM = 1400        # порог стоимости "значимого" предмета


def call(path, params=None, post=False):
    p = dict(params or {})
    if KEY:
        p["api_key"] = KEY
    url = API + path + ("?" + urllib.parse.urlencode(p) if p else "")
    for attempt in range(3):
        try:
            req = urllib.request.Request(url, data=b"" if post else None,
                                         method="POST" if post else "GET",
                                         headers={"User-Agent": "carry-hub"})
            with urllib.request.urlopen(req, timeout=40) as r:
                body = r.read().decode()
            time.sleep(PAUSE)
            return json.loads(body) if body else None
        except Exception as e:
            print("retry", path, e)
            time.sleep(3 * (attempt + 1))
    return None


def load(name, default):
    try:
        with open(name, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default


def at(arr, i):
    return arr[i] if isinstance(arr, list) and len(arr) > i else None


def med(vals, nd=1):
    vals = [v for v in vals if v is not None]
    return round(median(vals), nd) if vals else None


def extract(m, p):
    first = {}
    for e in p.get("purchase_log") or []:
        k, t = e.get("key"), e.get("time", 0)
        if k not in first or t < first[k]:
            first[k] = t
    return {
        "t": m["start_time"], "hero": p["hero_id"],
        "win": bool(m["radiant_win"]) == (p["player_slot"] < 128),
        "items": {k: round(t / 60, 1) for k, t in first.items()},
        "gpm": p.get("gold_per_min"), "xpm": p.get("xp_per_min"),
        "lh": [at(p.get("lh_t"), i) for i in (10, 20, 30)],
        "nw": [at(p.get("gold_t"), i) for i in (10, 20, 30)],
        "neutral": p.get("neutral_kills"), "lane": p.get("lane_efficiency_pct"),
        "total_lh": p.get("last_hits"), "dur": m.get("duration"),
    }


def pick_carry(m, radiant):
    c = [p for p in m["players"]
         if (p["player_slot"] < 128) == radiant and p.get("lane_role") == 1
         and not p.get("is_roaming")]
    return max(c, key=lambda p: p.get("last_hits") or 0) if c else None


def collect(cache, patch_id):
    now = int(time.time())
    seen, cands, last = cache["seen"], [], None
    for _ in range(5):
        params = {"min_rank": MIN_RANK}
        if last:
            params["less_than_match_id"] = last
        page = call("/publicMatches", params) or []
        if not page:
            break
        last = min(x["match_id"] for x in page)
        for x in page:
            sid = str(x["match_id"])
            if x["start_time"] < now - DAY or x.get("lobby_type") != 7 or x.get("game_mode") != 22:
                continue
            if seen.get(sid, {}).get("s") in ("done", "skip"):
                continue
            cands.append((x["match_id"], x["start_time"]))
    # сначала те, что уже запрошены на парсинг
    cands.sort(key=lambda c: seen.get(str(c[0]), {}).get("s") != "req")
    for mid, st in cands[:MAX_FETCH]:
        m = call(f"/matches/{mid}")
        sid = str(mid)
        if not m or not m.get("players"):
            continue
        if not any(p.get("purchase_log") for p in m["players"]):
            if seen.get(sid, {}).get("s") != "req":
                call(f"/request/{mid}", post=True)
                seen[sid] = {"s": "req", "t": st}
            continue
        if m.get("patch") != patch_id:
            seen[sid] = {"s": "skip", "t": st}
            continue
        for side in (True, False):
            p = pick_carry(m, side)
            if p:
                cache["records"].append(extract(m, p))
        seen[sid] = {"s": "done", "t": st}
    cutoff = now - DAY
    cache["records"] = [r for r in cache["records"] if r["t"] >= cutoff]
    cache["seen"] = {k: v for k, v in seen.items() if v["t"] >= cutoff}


def aggregate(records, heroes, items):
    by = defaultdict(list)
    for r in records:
        by[r["hero"]].append(r)
    out = []
    for hid, rs in by.items():
        if len(rs) < MIN_GAMES:
            continue
        n = len(rs)
        stats = defaultdict(list)
        for r in rs:
            for k, t in r["items"].items():
                stats[k].append((t, r["win"]))
        item_rows = []
        for k, v in stats.items():
            info = items.get(k) or {}
            if k.startswith("recipe_") or (info.get("cost") or 0) < BIG_ITEM or len(v) / n < 0.2:
                continue
            item_rows.append({"name": info.get("dname", k), "share": round(100 * len(v) / n),
                              "time": round(median(t for t, _ in v), 1),
                              "wr": round(100 * sum(w for _, w in v) / len(v))})
        item_rows.sort(key=lambda x: x["time"])
        builds = Counter()
        bw = defaultdict(int)
        for r in rs:
            big = sorted([(t, k) for k, t in r["items"].items()
                          if (items.get(k) or {}).get("cost", 0) >= BIG_ITEM and not k.startswith("recipe_")])
            key = tuple(k for _, k in big[:4])
            if len(key) >= 3:
                builds[key] += 1
                bw[key] += r["win"]
        build_rows = [{"items": [(items.get(k) or {}).get("dname", k) for k in key],
                       "games": c, "wr": round(100 * bw[key] / c)}
                      for key, c in builds.most_common(3)]
        out.append({
            "hero": (heroes.get(str(hid)) or {}).get("localized_name", str(hid)),
            "games": n, "wr": round(100 * sum(r["win"] for r in rs) / n, 1),
            "builds": build_rows, "items": item_rows,
            "farm": {
                "lh": [med([r["lh"][i] for r in rs], 0) for i in range(3)],
                "nw": [med([r["nw"][i] for r in rs], 0) for i in range(3)],
                "gpm": med([r["gpm"] for r in rs], 0), "xpm": med([r["xpm"] for r in rs], 0),
                "neutral": med([r["neutral"] for r in rs], 0),
                "lane": med([r["lane"] for r in rs], 0),
            },
        })
    out.sort(key=lambda h: -h["games"])
    return out


def main():
    patches = call("/constants/patch") or []
    heroes = call("/constants/heroes") or {}
    items = call("/constants/items") or {}
    if not patches:
        raise SystemExit("нет данных о патче")
    patch_id, patch_name = patches[-1]["id"], patches[-1]["name"]
    cache = load("cache.json", {"patch": None, "seen": {}, "records": []})
    if cache.get("patch") != patch_id:          # новый патч — начинаем с нуля
        cache = {"patch": patch_id, "seen": {}, "records": []}
    collect(cache, patch_id)
    data = {"updated": int(time.time()), "patch": patch_name,
            "total": len(cache["records"]), "heroes": aggregate(cache["records"], heroes, items)}
    with open("data.json", "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False)
    with open("cache.json", "w", encoding="utf-8") as f:
        json.dump(cache, f, ensure_ascii=False)
    print("керри-игр:", data["total"], "героев:", len(data["heroes"]))


if __name__ == "__main__":
    main()
