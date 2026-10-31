"""Собирает керри-игры 7000+ MMR (Immortal) за последние 24 часа через OpenDota API.
Результат: data.json (для сайта) и cache.json (накопление между запусками).
Переменные окружения: OPENDOTA_KEY (необязательно), MAX_FETCH (матчей за запуск, по умолчанию 120).
"""
import json, os, time, urllib.error, urllib.request, urllib.parse
from collections import Counter, defaultdict
from statistics import median

API = "https://api.opendota.com/api"
KEY = os.environ.get("OPENDOTA_KEY", "")
MAX_FETCH = int(os.environ.get("MAX_FETCH", "120"))
PAUSE = 0.15 if KEY else 1.1
DAY = 86400
START = time.time()
BUDGET = int(os.environ.get("BUDGET", "480"))  # секунд на запуск


def out_of_time():
    return time.time() - START > BUDGET
STRATZ = os.environ.get("STRATZ_TOKEN", "")
ITEMS_BY_ID = {}
MIN_RANK = 80          # Immortal
MIN_GAMES = 1          # минимум игр на героя для показа
BIG_ITEM = 1400        # порог стоимости "значимого" предмета


def call(path, params=None, post=False):
    p = dict(params or {})
    if KEY:
        p["api_key"] = KEY
    url = API + path + ("?" + urllib.parse.urlencode(p) if p else "")
    for attempt in range(2):
        try:
            print("запрос", path, flush=True)
            req = urllib.request.Request(url, data=b"" if post else None,
                                         method="POST" if post else "GET",
                                         headers={"User-Agent": "carry-hub"})
            with urllib.request.urlopen(req, timeout=20) as r:
                body = r.read().decode()
            time.sleep(PAUSE)
            return json.loads(body) if body else None
        except Exception as e:
            print("ошибка", path, e, flush=True)
            time.sleep(3)
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


def lane_cells(p, radiant):
    cells = []
    for xs, ys in (p.get("lane_pos") or {}).items():
        for y, c in (ys or {}).items():
            x, yy = int(xs), int(y)
            if not radiant:                     # зеркалим Dire в сторону Radiant
                x, yy = 256 - x, 256 - yy
            cells.append([x, yy, c])
    return cells


def extract(m, p):
    first = {}
    for e in p.get("purchase_log") or []:
        k, t = e.get("key"), e.get("time", 0)
        if k not in first or t < first[k]:
            first[k] = t
    return {
        "t": m["start_time"], "hero": p["hero_id"], "mid": m["match_id"],
        "k": p.get("kills"), "d": p.get("deaths"), "a": p.get("assists"),
        "win": bool(m["radiant_win"]) == (p["player_slot"] < 128),
        "items": {k: round(t / 60, 1) for k, t in first.items()},
        "gpm": p.get("gold_per_min"), "xpm": p.get("xp_per_min"),
        "lh": [at(p.get("lh_t"), i) for i in (10, 20, 30)],
        "nw": [at(p.get("gold_t"), i) for i in (10, 20, 30)],
        "neutral": p.get("neutral_kills"), "lane": p.get("lane_efficiency_pct"),
        "total_lh": p.get("last_hits"), "dur": m.get("duration"),
        "pos": lane_cells(p, p["player_slot"] < 128),
        "neut": [[e.get("item_key"), round((e.get("time") or 0) / 60, 1),
                  {k: v for k, v in e.items() if k not in ("time", "item_key")}]
                 for e in (p.get("neutral_item_history") or []) if isinstance(e, dict)],
    }


def pick_carry(m, radiant):
    c = [p for p in m["players"]
         if (p["player_slot"] < 128) == radiant and p.get("lane_role") == 1
         and not p.get("is_roaming")]
    return max(c, key=lambda p: p.get("last_hits") or 0) if c else None


Q = """query($id: Long!){match(id:$id){durationSeconds didRadiantWin startDateTime
players{heroId isRadiant isVictory position kills deaths assists goldPerMinute experiencePerMinute numLastHits
stats{lastHitsPerMinute networthPerMinute itemPurchases{time itemId}}
playbackData{playerUpdatePositionEvents{time x y}}}}}"""
_errs = [0]


def gql(query, variables=None):
    body = json.dumps({"query": query, "variables": variables or {}}).encode()
    req = urllib.request.Request("https://api.stratz.com/graphql", data=body, method="POST",
                                 headers={"Authorization": "Bearer " + STRATZ,
                                          "Content-Type": "application/json",
                                          "User-Agent": "STRATZ_API"})
    try:
        print("запрос stratz", flush=True)
        with urllib.request.urlopen(req, timeout=60) as r:
            res = json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        print("stratz HTTP", e.code, e.read()[:300], flush=True)
        time.sleep(3)
        return None
    except Exception as e:
        print("stratz ошибка", e, flush=True)
        time.sleep(3)
        return None
    time.sleep(0.4)
    if res.get("errors") and _errs[0] < 3:
        _errs[0] += 1
        print("stratz errors:", json.dumps(res["errors"], ensure_ascii=False)[:600], flush=True)
    return res.get("data")


def cum(arr):
    arr = [a or 0 for a in (arr or [])]
    if all(arr[i] <= arr[i + 1] for i in range(len(arr) - 1)):
        return arr
    out, tot = [], 0
    for a in arr:
        tot += a
        out.append(tot)
    return out


def stratz_records(mid, st, m):
    recs = []
    for p in m["players"]:
        if not str(p.get("position", "")).upper().replace("_", "").endswith("1"):
            continue
        radiant = bool(p["isRadiant"])
        cnt = Counter()
        pb = ((p.get("playbackData") or {}).get("playerUpdatePositionEvents")) or []
        for e in pb:
            x, y = e.get("x"), e.get("y")
            if x is None or y is None:
                continue
            if not radiant:
                x, y = 256 - x, 256 - y
            cnt[(x, y, min(3, int((e.get("time") or 0) // 600)))] += 1
        stt = p.get("stats") or {}
        first = {}
        for e in stt.get("itemPurchases") or []:
            k = ITEMS_BY_ID.get(e.get("itemId"))
            if k and (k not in first or e["time"] < first[k]):
                first[k] = e["time"]
        lh, nw = cum(stt.get("lastHitsPerMinute")), stt.get("networthPerMinute") or []
        recs.append({
            "t": st, "hero": p["heroId"], "mid": mid, "k": p.get("kills"), "d": p.get("deaths"),
            "a": p.get("assists"), "win": bool(p.get("isVictory")),
            "items": {k: round(t / 60, 1) for k, t in first.items()},
            "gpm": p.get("goldPerMinute"), "xpm": p.get("experiencePerMinute"),
            "lh": [at(lh, i) for i in (10, 20, 30)], "nw": [at(nw, i) for i in (10, 20, 30)],
            "neutral": None, "lane": None, "total_lh": p.get("numLastHits"), "dur": m.get("durationSeconds"),
            "pos": [[x, y, c, ph] for (x, y, ph), c in cnt.most_common(400)], "neut": [],
        })
    return recs


def collect_stratz(cache, cands):
    seen, now = cache["seen"], int(time.time())
    for mid, st in cands[:MAX_FETCH]:
        if out_of_time():
            print("лимит времени, сохраняю что есть", flush=True)
            break
        m = ((gql(Q, {"id": mid}) or {}).get("match"))
        ok = m and m.get("players") and any(((p.get("stats") or {}).get("itemPurchases")) for p in m["players"])
        if not ok:
            if now - st > 3 * 3600:
                seen[str(mid)] = {"s": "skip", "t": st}
            continue
        cache["records"].extend(stratz_records(mid, st, m))
        seen[str(mid)] = {"s": "done", "t": st}


def collect(cache, patch_id):
    now = int(time.time())
    seen, cands, last = cache["seen"], [], None
    for _ in range(5):
        if out_of_time():
            break
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
    print("кандидатов:", len(cands), flush=True)
    if STRATZ:
        collect_stratz(cache, cands)
        cands = []
    blocked = False
    for mid, st in cands[:MAX_FETCH]:
        if out_of_time():
            print("лимит времени, сохраняю что есть", flush=True)
            break
        m = call(f"/matches/{mid}")
        sid = str(mid)
        if not m or not m.get("players"):
            continue
        if not any(p.get("purchase_log") for p in m["players"]):
            if seen.get(sid, {}).get("s") != "req" and not blocked:
                if call(f"/request/{mid}", post=True) is None:
                    blocked = True
                    print("лимит на разбор реплеев, больше не запрашиваю", flush=True)
                else:
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
            item_rows.append({"name": info.get("dname", k), "img": (info.get("img") or "").split("?")[0], "share": round(100 * len(v) / n),
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
                       "imgs": [((items.get(k) or {}).get("img") or "").split("?")[0] for k in key],
                       "games": c, "wr": round(100 * bw[key] / c)}
                      for key, c in builds.most_common(3)]
        def meta(k):
            info = items.get(k) or {}
            return info.get("dname", k), (info.get("img") or "").split("?")[0]
        games = []
        for r in sorted(rs, key=lambda r: -r["t"])[:30]:
            tl = sorted((t, k) for k, t in r["items"].items()
                        if (items.get(k) or {}).get("cost", 0) >= 500 and not k.startswith("recipe_"))
            cells = sorted(r.get("pos") or [], key=lambda c: -c[2])[:400]
            games.append({"id": r.get("mid"), "t": r["t"], "win": r["win"],
                          "dur": round((r.get("dur") or 0) / 60), "gpm": r["gpm"],
                          "kda": [r.get("k"), r.get("d"), r.get("a")], "lh": r["lh"],
                          "items": [[meta(k)[0], round(t, 1), meta(k)[1]] for t, k in tl],
                          "neut": [[meta(k)[0], t, meta(k)[1], ex] for k, t, ex in (r.get("neut") or []) if k],
                          "pos": cells})
        heat = Counter()
        for r in rs:
            for cell in r.get("pos") or []:
                heat[(cell[0], cell[1], cell[3] if len(cell) > 3 else 0)] += cell[2]
        heat_list = [[x, y, c, ph] for (x, y, ph), c in heat.most_common(1500)]
        nc = Counter()
        for r in rs:
            for name in {k for k, _, _ in (r.get("neut") or []) if k}:
                nc[name] += 1
        neutrals = [{"name": meta(k)[0], "img": meta(k)[1], "share": round(100 * c / n)}
                    for k, c in nc.most_common(8)]
        out.append({
            "img": ((heroes.get(str(hid)) or {}).get("img") or "").split("?")[0], "list": games,
            "heat": heat_list, "neutrals": neutrals,
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
    ITEMS_BY_ID.update({v["id"]: k for k, v in items.items() if isinstance(v, dict) and "id" in v})
    if STRATZ:
        d = gql('{__type(name:"MatchPlayerType"){fields{name}}}')
        names = [f["name"] for f in (((d or {}).get("__type") or {}).get("fields") or [])]
        print("поля игрока Stratz:", ", ".join(names), flush=True)
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
