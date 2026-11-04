"""God's Eye collector. Runs on GitHub Actions, writes data.json and state.json.
Needs env vars LASTFM_KEY (required) and YOUTUBE_KEY (optional)."""
import os, re, json, time, difflib, statistics, urllib.request, urllib.parse
from datetime import datetime, timezone, timedelta

LFM = os.environ.get("LASTFM_KEY", "")
YT = os.environ.get("YOUTUBE_KEY", "")
W = {"g": .22, "pv": .14, "yt": .12, "sent": .16, "disc": .14, "tt": .22}
POS = set("fire goat hard slaps banger bangers crazy insane best love loved legend legendary cold underrated wave heat clean goated peak blessing vibes".split())
NEG = set("trash mid flop washed boring bad worst hate cap ass garbage overrated wack dead dry skip".split())


def get(url):
    for _ in range(2):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "gods-eye/1.0"})
            with urllib.request.urlopen(req, timeout=20) as r:
                return json.loads(r.read().decode())
        except Exception as e:
            err = e
            time.sleep(1)
    print("request failed:", str(err)[:120])
    return None


def norm(s): return re.sub(r"[^a-z0-9]", "", s.lower())
def sim(a, b): return difflib.SequenceMatcher(None, norm(a), norm(b)).ratio()
def q(**p): return urllib.parse.urlencode(p)


def lastfm(name):
    j = get("https://ws.audioscrobbler.com/2.0/?" + q(method="artist.search", artist=name, api_key=LFM, format="json", limit=10))
    res = (((j or {}).get("results") or {}).get("artistmatches") or {}).get("artist") or []
    c = [x for x in res if sim(x.get("name", ""), name) >= .9]
    pick = max(c, key=lambda x: int(x.get("listeners", 0) or 0))["name"] if c else name
    j = get("https://ws.audioscrobbler.com/2.0/?" + q(method="artist.getinfo", artist=pick, api_key=LFM, format="json", autocorrect=1))
    a = (j or {}).get("artist")
    if not a or sim(a.get("name", ""), name) < .8:
        return None
    s = a.get("stats", {})
    l = int(s.get("listeners", 0))
    if l < 1000:
        return None
    return {"lf": a["name"], "l": l, "p": int(s.get("playcount", 0))}


def deezer(name):
    j = get("https://api.deezer.com/search/artist?" + q(q=name, limit=5))
    c = [x for x in (j or {}).get("data", []) if sim(x.get("name", ""), name) >= .9]
    return max(c, key=lambda x: x.get("nb_fan", 0))["nb_fan"] if c else None


def ytapi(path, **p):
    return get("https://www.googleapis.com/youtube/v3/" + path + "?" + q(key=YT, **p))


def channel_id(name, cache):
    if name in cache:
        return cache[name]
    j = ytapi("search", part="snippet", type="channel", q=name, maxResults=3)
    if j is None:
        return ""
    best = None
    for it in j.get("items", []):
        s = sim(it["snippet"]["channelTitle"], name)
        if s >= .8 and (not best or s > best[0]):
            best = (s, it["id"]["channelId"])
    cache[name] = best[1] if best else ""
    return cache[name]


def youtube(name, cache):
    out = {}
    cid = channel_id(name, cache)
    if not cid:
        return out
    j = ytapi("channels", part="statistics,contentDetails", id=cid)
    it = ((j or {}).get("items") or [None])[0]
    if not it:
        return out
    st = it["statistics"]
    out["subs"] = int(st.get("subscriberCount", 0))
    out["views"] = int(st.get("viewCount", 0))
    up = it["contentDetails"]["relatedPlaylists"]["uploads"]
    pl = ytapi("playlistItems", part="contentDetails", playlistId=up, maxResults=8)
    ids = [x["contentDetails"]["videoId"] for x in (pl or {}).get("items", [])]
    if not ids:
        return out
    vs = (ytapi("videos", part="statistics", id=",".join(ids)) or {}).get("items", [])
    views = [int(v["statistics"].get("viewCount", 0)) for v in vs]
    likes = [int(v["statistics"].get("likeCount", 0)) for v in vs]
    if sum(views):
        out["yt"] = sum(likes) / sum(views) * 100
    cm = ytapi("commentThreads", part="snippet", videoId=ids[0], maxResults=50, order="relevance")
    pos = neg = 0
    for c in (cm or {}).get("items", []):
        for w in re.findall(r"[a-z']+", c["snippet"]["topLevelComment"]["snippet"]["textDisplay"].lower()):
            pos += w in POS
            neg += w in NEG
    if pos + neg:
        out["sent"] = (pos - neg) / (pos + neg + 5)
    return out


def growth(hist, idx, now):
    """% change vs ~30 days ago (or the oldest snapshot if newer). Returns (pct, age_days)."""
    val = lambda e: e[idx] if len(e) > idx else None
    cur = val(hist[-1])
    old = next((h for h in hist if h[0] >= (now - timedelta(days=30)).isoformat()), hist[0])
    age = (now - datetime.fromisoformat(old[0])).total_seconds() / 86400
    if age < 1 or not val(old) or cur is None:
        return 0.0, age
    return max(-100, min(1000, (cur / val(old) - 1) * 100)), age


def score(rows):
    mn, mx = {}, {}
    for k in W:
        v = [r[k] for r in rows]
        mn[k], mx[k] = min(v), max(v)
    for r in rows:
        s = 0
        for k in W:
            p = .5 if mx[k] == mn[k] else (r[k] - mn[k]) / (mx[k] - mn[k])
            s += p * W[k]
        r["sc"] = round(s * 100)


def main():
    if not LFM:
        raise SystemExit("Missing LASTFM_KEY secret")
    names = [x.strip() for x in open("artists.txt") if x.strip()]
    state = json.load(open("state.json")) if os.path.exists("state.json") else {}
    if state.get("v") != 2:
        state = {"v": 2, "channels": state.get("channels", {})}
    chans, hist, peaks = state.setdefault("channels", {}), state.setdefault("hist", {}), state.setdefault("peak", {})
    now = datetime.now(timezone.utc)
    rows, unmatched, maxage = [], [], 0
    for n in names:
        lf = lastfm(n)
        if not lf:
            unmatched.append(n)
            continue
        dz = deezer(n)
        yt = youtube(n, chans) if YT else {}
        h = hist.setdefault(n, [])
        if not h or (now - datetime.fromisoformat(h[-1][0])).total_seconds() > 5 * 3600:
            h.append([now.isoformat(), lf["l"], lf["p"], dz, yt.get("views")])
        cutoff = (now - timedelta(days=60)).isoformat()
        hist[n] = h = [x for x in h if x[0] >= cutoff]
        g, a1 = growth(h, 1, now)
        tt, _ = growth(h, 2, now)
        pv, _ = growth(h, 3, now) if dz else (0.0, 0)
        dc, _ = growth(h, 4, now)
        maxage = max(maxage, a1)
        rows.append({"n": n, "lf": lf["lf"], "ml": round(lf["l"] / 1e6, 3), "g": g, "pv": pv, "tt": tt,
                     "yt": yt.get("yt"), "sent": yt.get("sent"), "disc": dc})
        time.sleep(.2)
    if len(rows) < 3:
        raise SystemExit("Fewer than 3 artists matched. Check LASTFM_KEY and artists.txt")
    for k in ("yt", "sent"):  # missing values become the group median
        have = [r[k] for r in rows if r[k] is not None]
        med = statistics.median(have) if have else 0
        for r in rows:
            if r[k] is None:
                r[k] = med
    score(rows)
    for r in rows:
        peaks[r["n"]] = max(peaks.get(r["n"], 0), r["sc"])
        r["peak"] = peaks[r["n"]]
        for k in ("g", "pv", "tt", "yt", "disc"):
            r[k] = round(r[k], 1)
        r["sent"] = round(r["sent"], 2)
        del r["sc"]
    json.dump({"updated": now.isoformat(), "history_days": round(maxage, 1), "artists": rows, "unmatched": unmatched},
              open("data.json", "w"), separators=(",", ":"))
    json.dump(state, open("state.json", "w"), separators=(",", ":"))
    print(f"ok: {len(rows)} artists, {len(unmatched)} unmatched")


if __name__ == "__main__":
    main()
