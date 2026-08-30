"""seen.json doubles as the dedupe index AND the application tracker."""
from __future__ import annotations

import csv
import json
import re
from datetime import datetime, timezone
from pathlib import Path

from .fetch import Job


class Store:
    def __init__(self, path: str | Path = "seen.json"):
        self.path = Path(path)
        self.data: dict[str, dict] = {}
        if self.path.exists():
            for enc in ("utf-8", "utf-8-sig", "cp1252", "latin-1"):
                try:
                    raw = self.path.read_text(encoding=enc)
                    self.data = json.loads(raw)
                    break
                except (json.JSONDecodeError, UnicodeDecodeError):
                    continue
            else:
                print(f"  ! {self.path} corrupt, starting fresh")

    def unseen(self, jobs: list[Job]) -> list[Job]:
        seen_urls = {v.get("url", "").split("?")[0] for v in self.data.values() if v.get("url")}
        seen_combos = {(re.sub(r'[^a-zA-Z0-9]', '', (v.get("title") or "").lower()),
                        re.sub(r'[^a-zA-Z0-9]', '', (v.get("company") or "").lower()))
                       for v in self.data.values()}

        out = []
        for j in jobs:
            if j.job_id in self.data:
                continue
            clean_url = j.url.split("?")[0] if j.url else ""
            if clean_url and clean_url in seen_urls:
                continue
            combo = (re.sub(r'[^a-zA-Z0-9]', '', j.title.lower()),
                     re.sub(r'[^a-zA-Z0-9]', '', j.company.lower()))
            if combo in seen_combos:
                continue
            if clean_url:
                seen_urls.add(clean_url)
            seen_combos.add(combo)
            out.append(j)
        return out

    def record(self, jobs: list[Job], emailed: bool) -> None:
        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        for j in jobs:
            self.data.setdefault(j.job_id, {
                "first_seen": now,
                "company": j.company,
                "title": j.title,
                "location": j.location,
                "url": j.url,
                "score": j.score,
                "reason": j.reason,
                "emailed": emailed,
                "applied": False,
                "applied_on": None,
            })
        self.save()

    def mark_applied(self, job_id: str) -> bool:
        if job_id not in self.data:
            return False
        self.data[job_id]["applied"] = True
        self.data[job_id]["applied_on"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
        self.save()
        return True

    def stats(self) -> dict:
        return {
            "tracked": len(self.data),
            "emailed": sum(1 for v in self.data.values() if v.get("emailed")),
            "applied": sum(1 for v in self.data.values() if v.get("applied")),
        }

    def export_csv(self, path: str | Path = "out/tracker.csv") -> Path:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        cols = ["first_seen", "company", "title", "location", "score",
                "reason", "applied", "applied_on", "url"]
        with path.open("w", newline="", encoding="utf-8") as fh:
            w = csv.DictWriter(fh, fieldnames=["job_id"] + cols, extrasaction="ignore")
            w.writeheader()
            for jid, row in sorted(self.data.items(),
                                   key=lambda kv: kv[1].get("first_seen", ""), reverse=True):
                w.writerow({"job_id": jid, **row})
        return path

    def save(self) -> None:
        self.path.write_text(json.dumps(self.data, indent=2, ensure_ascii=False), encoding="utf-8")
