#!/usr/bin/env python3
"""Jun-paper-tracker: arXiv 논문 트래커.

config.toml 의 주제별로 arXiv 최신 논문을 수집해 data/papers.json 에 누적하고,
README.md 와 lists/<주제>/ 아래 연도별 목록을 생성한다.

- 외부 패키지 없음 (Python 3.11+ 표준 라이브러리만 사용).
- 무료 API만 사용: arXiv API, Hugging Face papers API (코드 링크).

사용 예:
    python tracker.py                 # 수집 + 코드 링크 확인 + 문서 생성
    python tracker.py --days 30       # 최근 30일을 다시 훑기 (처음 채울 때)
    python tracker.py --topic slam    # 특정 주제만 수집
    python tracker.py --render-only   # 수집 없이 문서만 다시 생성
    python tracker.py --skip-code     # 코드 링크 확인 생략
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import logging
import re
import sys
import time
import tomllib
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from pathlib import Path

ROOT = Path(__file__).resolve().parent
CONFIG_PATH = ROOT / "config.toml"
DATA_PATH = ROOT / "data" / "papers.json"
STATE_PATH = ROOT / "data" / "state.json"
README_PATH = ROOT / "README.md"
LISTS_DIR = ROOT / "lists"

ARXIV_API = "https://export.arxiv.org/api/query"
HF_API = "https://huggingface.co/api/papers/"
USER_AGENT = "jun-paper-tracker/1.0"
NS = {
    "a": "http://www.w3.org/2005/Atom",
    "arxiv": "http://arxiv.org/schemas/atom",
    "os": "http://a9.com/-/spec/opensearch/1.1/",
}

GITHUB_RE = re.compile(r"https?://(?:www\.)?github\.com/([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+)", re.I)
PAGE_RE = re.compile(r"https?://[A-Za-z0-9_.-]+\.github\.io(?:/[^\s,;)\]}>\"']*)?", re.I)
NOT_REPO_OWNERS = {"features", "topics", "orgs", "sponsors", "marketplace", "about", "settings", "login"}

for _stream in (sys.stdout, sys.stderr):  # Windows 콘솔/리다이렉트에서도 한글이 깨지지 않게
    if hasattr(_stream, "reconfigure"):
        _stream.reconfigure(encoding="utf-8", errors="replace")

logging.basicConfig(format="[%(asctime)s %(levelname)s] %(message)s", datefmt="%H:%M:%S", level=logging.INFO)
log = logging.getLogger("tracker")


# ---------------------------------------------------------------- 공통
def today_utc() -> dt.date:
    return dt.datetime.now(dt.timezone.utc).date()


def http_get(url: str, timeout: float = 40) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def gh_annotation(level: str, msg: str) -> None:
    """GitHub Actions 화면에 경고/오류로 표시 (로컬에서는 그냥 한 줄 출력)."""
    print(f"::{level}::{msg}", flush=True)


def load_config() -> dict:
    with open(CONFIG_PATH, "rb") as f:
        cfg = tomllib.load(f)
    slugs = [t["slug"] for t in cfg["topics"]]
    if len(slugs) != len(set(slugs)):
        raise SystemExit("config.toml: topics 의 slug 가 중복됩니다.")
    return cfg


def load_db() -> dict:
    if not DATA_PATH.exists():
        return {}
    with open(DATA_PATH, encoding="utf-8") as f:
        return json.load(f)


def save_db(db: dict) -> None:
    """한 줄에 논문 하나씩 저장 (git diff 를 읽기 쉽게)."""
    DATA_PATH.parent.mkdir(parents=True, exist_ok=True)
    ids = sorted(db, reverse=True)
    lines = [f"{json.dumps(i)}: {json.dumps(db[i], ensure_ascii=False, sort_keys=True)}" for i in ids]
    text = "{\n" + ",\n".join(lines) + "\n}\n" if lines else "{}\n"
    tmp = DATA_PATH.with_suffix(".tmp")
    tmp.write_text(text, encoding="utf-8", newline="\n")
    tmp.replace(DATA_PATH)


def load_state() -> dict:
    if STATE_PATH.exists():
        return json.loads(STATE_PATH.read_text(encoding="utf-8"))
    return {}


def save_state(state: dict) -> None:
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    STATE_PATH.write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8", newline="\n")


# ---------------------------------------------------------------- 링크 추출
def find_links(text: str) -> tuple[str | None, str | None]:
    """초록/코멘트에서 GitHub 저장소와 프로젝트 페이지(github.io) 주소를 찾는다."""
    code = None
    for owner, repo in GITHUB_RE.findall(text):
        repo = re.sub(r"(\.git)?[.,;:]*$", "", repo)
        if owner.lower() in NOT_REPO_OWNERS or not repo:
            continue
        code = f"https://github.com/{owner}/{repo}"
        break
    page = None
    m = PAGE_RE.search(text)
    if m:
        page = m.group(0).rstrip(".,;:")
    return code, page


# ---------------------------------------------------------------- arXiv
class RateLimited(RuntimeError):
    """arXiv 가 요청 과다(429/503)로 계속 거절함."""


def retry_after_seconds(err: urllib.error.HTTPError) -> float:
    """Retry-After 헤더(초 단위 숫자)를 읽는다. 없거나 날짜 형식이면 0."""
    try:
        return float(err.headers.get("Retry-After", 0) or 0)
    except (TypeError, ValueError):
        return 0.0


class ArxivClient:
    def __init__(self, delay: float, s: dict | None = None):
        s = s or {}
        self.delay = delay
        self.attempts = int(s.get("rate_limit_retries", 4))
        self.limit_wait = float(s.get("rate_limit_wait_seconds", 60))
        self.limit_max_wait = float(s.get("rate_limit_max_wait_seconds", 600))
        self._last = 0.0

    def _wait(self) -> None:
        gap = time.monotonic() - self._last
        if gap < self.delay:
            time.sleep(self.delay - gap)
        self._last = time.monotonic()

    def page(self, params: dict) -> tuple[list, int]:
        url = ARXIV_API + "?" + urllib.parse.urlencode(params)
        attempts = self.attempts
        last_err: Exception | None = None
        for attempt in range(1, attempts + 1):
            self._wait()
            try:
                root = ET.fromstring(http_get(url))
                total = int(root.findtext("os:totalResults", "0", NS))
                entries = root.findall("a:entry", NS)
                # arXiv API 는 가끔 결과가 남아 있는데도 빈 페이지를 돌려준다 -> 재시도
                if not entries and params["start"] < total:
                    raise RuntimeError(f"빈 페이지 (start={params['start']}, total={total})")
                return entries, total
            except (urllib.error.URLError, TimeoutError, ET.ParseError, RuntimeError, ValueError) as e:
                last_err = e
                if attempt == attempts:
                    break
                wait = 10 * attempt
                if isinstance(e, urllib.error.HTTPError) and e.code in (429, 503):
                    # arXiv 가 알려 준 대기 시간과 우리 대기 시간 중 긴 쪽을 지킨다
                    wait = min(max(retry_after_seconds(e), self.limit_wait * attempt), self.limit_max_wait)
                log.warning(f"arXiv 요청 실패 ({attempt}/{attempts}): {e} -> {wait:.0f}초 후 재시도")
                time.sleep(wait)
        if isinstance(last_err, urllib.error.HTTPError) and last_err.code in (429, 503):
            raise RateLimited(f"arXiv 요청 과다 거절 (HTTP {last_err.code}), {attempts}회 시도")
        raise RuntimeError(f"arXiv 요청 최종 실패: {last_err}")


def build_query(topic: dict) -> str:
    terms = " OR ".join(f'{field}:"{p}"' for p in topic["phrases"] for field in ("ti", "abs"))
    query = f"({terms})"
    cats = topic.get("categories") or []
    if cats:
        query += " AND (" + " OR ".join(f"cat:{c}" for c in cats) + ")"
    for p in topic.get("exclude") or []:
        query += f' ANDNOT (ti:"{p}" OR abs:"{p}")'
    return query


def parse_entry(e: ET.Element) -> dict:
    raw_id = e.findtext("a:id", "", NS).rsplit("/abs/", 1)[-1]
    m = re.match(r"^(.+?)v(\d+)$", raw_id)
    pid, version = (m.group(1), int(m.group(2))) if m else (raw_id, 1)
    clean = lambda s: " ".join((s or "").split())  # noqa: E731
    summary = clean(e.findtext("a:summary", "", NS))
    comment = clean(e.findtext("arxiv:comment", "", NS))
    prim = e.find("arxiv:primary_category", NS)
    code, page = find_links(summary + " " + comment)
    rec = {
        "id": pid,
        "version": version,
        "title": clean(e.findtext("a:title", "", NS)),
        "authors": [clean(a.findtext("a:name", "", NS)) for a in e.findall("a:author", NS)],
        "published": e.findtext("a:published", "", NS)[:10],
        "updated": e.findtext("a:updated", "", NS)[:10],
        "primary_category": prim.get("term") if prim is not None else "",
        "categories": [c.get("term") for c in e.findall("a:category", NS)],
        "comment": comment,
        "code_url": code,
        "code_source": "arxiv" if code else None,
        "page_url": page,
    }
    return rec


def fetch_topic(client: ArxivClient, topic: dict, cutoff: dt.date, s: dict) -> dict:
    query = build_query(topic)
    found: dict = {}
    size = s["page_size"]
    for n in range(s["max_pages_per_topic"]):
        params = {"search_query": query, "sortBy": "submittedDate", "sortOrder": "descending",
                  "start": n * size, "max_results": size}
        entries, total = client.page(params)
        reached_cutoff = False
        for e in entries:
            rec = parse_entry(e)
            if not rec["published"] or dt.date.fromisoformat(rec["published"]) < cutoff:
                reached_cutoff = True
                continue
            found[rec["id"]] = rec
        if reached_cutoff or not entries or (n + 1) * size >= total:
            break
    else:
        gh_annotation("warning", f"[{topic['name']}] 최대 페이지({s['max_pages_per_topic']})에 도달해 "
                                 f"{cutoff} 이후 논문 일부가 빠졌을 수 있습니다.")
    return found


def topic_cutoff(db: dict, slug: str, s: dict, days: int | None) -> dt.date:
    today = today_utc()
    if days is not None:
        return today - dt.timedelta(days=days)
    dates = [p["published"] for p in db.values() if slug in p.get("topics", [])]
    if not dates:
        return today - dt.timedelta(days=s["initial_days"])
    return dt.date.fromisoformat(max(dates)) - dt.timedelta(days=s["overlap_days"])


def merge(db: dict, slug: str, found: dict, today: str) -> tuple[int, set]:
    new_ids = set()
    for pid, rec in found.items():
        old = db.get(pid)
        if old is None:
            rec["topics"] = [slug]
            rec["added"] = today
            db[pid] = rec
            new_ids.add(pid)
            continue
        if slug not in old["topics"]:
            old["topics"].append(slug)
        for k in ("version", "title", "authors", "updated", "primary_category", "categories", "comment"):
            old[k] = rec[k]
        if not old.get("code_url") and rec.get("code_url"):
            old["code_url"], old["code_source"] = rec["code_url"], rec["code_source"]
        if not old.get("page_url") and rec.get("page_url"):
            old["page_url"] = rec["page_url"]
    return len(new_ids), new_ids


# ---------------------------------------------------------------- 코드 링크 (Hugging Face)
def hf_lookup(pid: str) -> tuple[str | None, int | None]:
    try:
        d = json.loads(http_get(HF_API + urllib.parse.quote(pid), timeout=20))
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return None, None
        raise
    repo = d.get("githubRepo")
    if repo and GITHUB_RE.match(repo):
        return repo.rstrip("/"), d.get("githubStars")
    return None, None


def refresh_code_links(db: dict, new_ids: set, s: dict) -> int:
    today = today_utc()
    ages = set(s["code_check_ages"])
    cands = []
    for pid, p in db.items():
        if p.get("code_url"):
            continue
        age = (today - dt.date.fromisoformat(p["published"])).days
        if pid in new_ids or age in ages:
            cands.append(p)
    cands.sort(key=lambda p: (p["published"], p["id"]), reverse=True)
    cands = cands[: s["code_check_max"]]
    log.info(f"코드 링크 확인 대상: {len(cands)}편")
    found = errors = 0
    for p in cands:
        time.sleep(s["hf_delay_seconds"])
        try:
            url, stars = hf_lookup(p["id"])
        except Exception as e:  # 네트워크 오류 등: 다음 확인일에 다시 시도
            errors += 1
            if errors >= 20:
                gh_annotation("warning", f"Hugging Face 오류가 계속되어 코드 링크 확인을 중단합니다: {e}")
                break
            continue
        if url:
            p["code_url"], p["code_source"] = url, "huggingface"
            found += 1
    log.info(f"코드 링크 새로 찾음: {found}편 (오류 {errors}건)")
    return found


# ---------------------------------------------------------------- 문서 생성
def md_anchor(heading: str) -> str:
    a = heading.strip().lower()
    a = re.sub(r"[^\w\- ]", "", a)
    return a.replace(" ", "-")


def esc(s: str) -> str:
    return s.replace("|", "\\|").replace("\n", " ")


def paper_row(p: dict) -> str:
    authors = p.get("authors") or []
    who = authors[0] + (" 외" if len(authors) > 1 else "") if authors else ""
    links = []
    if p.get("code_url"):
        links.append(f"[code]({p['code_url']})")
    if p.get("page_url"):
        links.append(f"[page]({p['page_url']})")
    return (f"|{p['published']}|**{esc(p['title'])}**|{esc(who)}|"
            f"[{p['id']}](https://arxiv.org/abs/{p['id']})|{' '.join(links) or '-'}|")


TABLE_HEAD = "|제출일|제목|저자|arXiv|코드|\n|---|---|---|---|---|"


def sort_papers(papers: list) -> list:
    return sorted(papers, key=lambda p: (p["published"], p["id"]), reverse=True)


def write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8", newline="\n")


def render(db: dict, cfg: dict, state: dict) -> None:
    s = cfg["settings"]
    tz = dt.timezone(dt.timedelta(hours=s["timezone_offset_hours"]))
    stamp = state.get("last_change")
    stamp_txt = (dt.datetime.fromisoformat(stamp).astimezone(tz).strftime("%Y-%m-%d %H:%M")
                 + f" (UTC{s['timezone_offset_hours']:+d})") if stamp else "-"
    week_ago = (today_utc() - dt.timedelta(days=7)).isoformat()

    by_topic = {t["slug"]: sort_papers([p for p in db.values() if t["slug"] in p.get("topics", [])])
                for t in cfg["topics"]}

    out = ["# Jun-paper-tracker", "",
           f"> 마지막 데이터 변경: {stamp_txt} · arXiv 최신 논문을 매일 자동 수집합니다.", "",
           "|주제|전체|최근 7일|코드 공개|", "|---|---:|---:|---:|"]
    for t in cfg["topics"]:
        ps = by_topic[t["slug"]]
        recent = sum(p["published"] >= week_ago for p in ps)
        with_code = sum(bool(p.get("code_url")) for p in ps)
        ratio = f"{with_code} ({with_code * 100 // len(ps)}%)" if ps else "0"
        out.append(f"|[{t['name']}](#{md_anchor(t['name'])})|{len(ps)}|{recent}|{ratio}|")
    out.append("")

    for t in cfg["topics"]:
        ps = by_topic[t["slug"]]
        out += [f"## {t['name']}", "",
                f"최근 {min(len(ps), s['readme_latest'])}편 · [전체 목록 →](lists/{t['slug']}/README.md)", ""]
        if ps:
            out.append(TABLE_HEAD)
            out += [paper_row(p) for p in ps[: s["readme_latest"]]]
        else:
            out.append("_아직 수집된 논문이 없습니다._")
        out.append("")
    out += ["---", "", "검색 조건은 [config.toml](config.toml)에서 바꿀 수 있습니다 "
            "([사용법](docs/USAGE.md)). 코드 링크는 논문 초록·코멘트, 그리고 Hugging Face Papers에서 찾습니다.", ""]
    write_text(README_PATH, "\n".join(out))

    # 주제별 전체 목록: lists/<slug>/README.md (연도 목차) + <연도>.md (월별)
    for t in cfg["topics"]:
        ps = by_topic[t["slug"]]
        tdir = LISTS_DIR / t["slug"]
        years: dict[str, list] = {}
        for p in ps:
            years.setdefault(p["published"][:4], []).append(p)
        idx = [f"# {t['name']}", "", f"[← 처음으로](../../README.md) · 전체 {len(ps)}편", "",
               "|연도|논문 수|코드 공개|", "|---|---:|---:|"]
        for y in sorted(years, reverse=True):
            yp = years[y]
            idx.append(f"|[{y}]({y}.md)|{len(yp)}|{sum(bool(p.get('code_url')) for p in yp)}|")
            body = [f"# {t['name']} — {y}", "", f"[← {t['name']} 목록](README.md)", ""]
            months: dict[str, list] = {}
            for p in yp:
                months.setdefault(p["published"][:7], []).append(p)
            for m in sorted(months, reverse=True):
                body += [f"## {m} ({len(months[m])}편)", "", TABLE_HEAD]
                body += [paper_row(p) for p in months[m]]
                body.append("")
            write_text(tdir / f"{y}.md", "\n".join(body))
        idx.append("")
        write_text(tdir / "README.md", "\n".join(idx))


# ---------------------------------------------------------------- main
def main() -> int:
    ap = argparse.ArgumentParser(description="Jun-paper-tracker (arXiv 논문 트래커)")
    ap.add_argument("--days", type=int, help="최근 N일을 다시 수집 (기본: 마지막 수집 날짜 기준 자동)")
    ap.add_argument("--topic", action="append", help="이 slug 의 주제만 수집 (여러 번 지정 가능)")
    ap.add_argument("--render-only", action="store_true", help="수집 없이 문서만 다시 생성")
    ap.add_argument("--skip-code", action="store_true", help="Hugging Face 코드 링크 확인 생략")
    args = ap.parse_args()

    cfg = load_config()
    s = cfg["settings"]
    db = load_db()
    state = load_state()
    before = json.dumps(db, sort_keys=True)
    failed: list[str] = []
    topics = cfg["topics"]

    if args.topic:
        unknown = set(args.topic) - {t["slug"] for t in topics}
        if unknown:
            raise SystemExit(f"없는 slug: {', '.join(sorted(unknown))}")
        topics = [t for t in topics if t["slug"] in args.topic]

    rate_limited: list[str] = []  # arXiv 요청 과다로 못 한 주제 (실패가 아니라 '다음에 따라잡기')
    if not args.render_only:
        client = ArxivClient(s["request_delay_seconds"], s)
        today = today_utc().isoformat()
        all_new: set = set()
        for t in topics:
            if rate_limited:
                # 이미 막혔으면 남은 주제로 arXiv 를 더 두드리지 않는다
                rate_limited.append(t["name"])
                continue
            cutoff = topic_cutoff(db, t["slug"], s, args.days)
            log.info(f"[{t['name']}] {cutoff} 이후 제출 논문 검색")
            try:
                found = fetch_topic(client, t, cutoff, s)
            except RateLimited as e:
                rate_limited.append(t["name"])
                log.warning(f"[{t['name']}] {e} -> 남은 주제는 건너뛰고 다음 실행 때 따라잡습니다")
                continue
            except Exception as e:
                failed.append(t["name"])
                gh_annotation("error", f"[{t['name']}] 수집 실패: {e}")
                continue
            n, new_ids = merge(db, t["slug"], found, today)
            all_new |= new_ids
            log.info(f"[{t['name']}] 검색 {len(found)}편, 신규 {n}편")
        if not args.skip_code:
            refresh_code_links(db, all_new, s)

    # 요청 과다로 막힌 날이 며칠 연속인지 기록 (막히지 않은 날 0으로 초기화)
    old_streak = int(state.get("rate_limited_days", 0))
    streak = old_streak + 1 if rate_limited else 0
    state_changed = streak != old_streak
    state["rate_limited_days"] = streak

    changed = json.dumps(db, sort_keys=True) != before
    if changed:
        state["last_change"] = dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat()
        save_db(db)
    if changed or state_changed:
        save_state(state)
    log.info("데이터 변경 있음" if changed else "데이터 변경 없음")
    render(db, cfg, state)

    if rate_limited:
        limit_days = int(s.get("rate_limit_fail_after_days", 3))
        msg = (f"arXiv 요청 과다(429)로 {len(rate_limited)}개 주제를 건너뜀: {', '.join(rate_limited)} "
               f"(연속 {streak}일째, 다음 실행 때 자동으로 따라잡습니다)")
        if streak >= limit_days:
            gh_annotation("error", msg + f" - {limit_days}일 연속이라 실패로 알립니다.")
            return 1
        gh_annotation("warning", msg)
    if failed and len(failed) == len(topics):
        log.error("모든 주제 수집 실패")
        return 1
    if failed:
        log.warning(f"일부 주제 수집 실패: {', '.join(failed)} (다음 실행 때 자동으로 따라잡습니다)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
