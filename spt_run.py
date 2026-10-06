#!/usr/bin/env python3
"""Runs the RPspt survey end to end for a list of login codes.

Usage:
    python3 spt_run.py LOGIN [LOGIN ...] [--seed N] [--age N] [--sex male|female]

With no login arguments the code is read from stdin.

Answer source is answers.json (a base profile on the 1-10 scale). A fraction
RANDOMIZE_FRAC of the answers is nudged by +/-1 so repeated runs differ slightly
while staying broadly consistent. The seed is random unless --seed is given.

Per login the script detects three states:
  new        - survey form is served, questionnaire is started from scratch
  partial    - server serves completionData listing unanswered questions,
               only those are answered and the survey is finished
  used       - login is already fully completed, it is skipped
"""
import argparse
import json
import os
import random
import re
import sys
from datetime import datetime, timezone

import requests

BASE = "https://61.rpspt.ru"
USER_AGENT = "Mozilla/5.0 (X11; Linux x86_64) Chrome/152.0.0.0"

AGE = 18
SEX = "male"

# Share of answers shifted by +/-1; the rest stay exactly as in the base profile.
RANDOMIZE_FRAC = 0.25

ANSWER_TIME_MS = 30000
TIMEOUT = 30

HERE = os.path.dirname(os.path.abspath(__file__))
ANSWERS_PATH = os.path.join(HERE, "answers.json")


def csrf_from(html):
    """CSRF token: the hidden _token input, falling back to the meta tag."""
    m = re.search(r'name="_token" value="([^"]+)"', html)
    if m:
        return m.group(1)
    m = re.search(r'csrf-token" content="([^"]+)"', html)
    return m.group(1) if m else None


def parse_test_page(html):
    """Pull test_id, sysId, csrf and (when resuming) the missing question list."""
    meta = {
        "test_id": re.search(r"var test_id\s*=\s*'?(\d+)'?", html).group(1),
        "sys_id": re.search(r"var sysId\s*=\s*'([^']+)'", html).group(1),
        "csrf": re.search(r"var csrf\s*=\s*'([^']+)'", html).group(1),
        "started_at": (
            re.search(r"var started_at\s*=\s*'([^']+)'", html)
            or re.search(r"started_at\s*=\s*'([^']+)'", html)
        ),
    }

    data = re.search(r"var testData\s*=\s*(\[.*?\]);", html, re.S)
    meta["questions"] = json.loads(data.group(1)) if data else []

    done = re.search(r"completionData\s*=\s*(\{.*?\});", html, re.S)
    meta["completion"] = json.loads(done.group(1)) if done else None
    return meta


def build_answers(question_nums, base, seed=None):
    """Base answers with a +/-1 nudge on a random subset, clamped to 1..10."""
    seed = random.randrange(2**31) if seed is None else seed
    rnd = random.Random(seed)
    answers = {}
    for num in question_nums:
        a = base.get(str(num), 5)
        if rnd.random() < RANDOMIZE_FRAC:
            a = max(1, min(10, a + rnd.choice((-1, 1))))
        answers[num] = a
    return answers, seed


class SurveyRunner:
    def __init__(self, login, age=AGE, sex=SEX, verbose=True):
        self.login = login
        self.age = age
        self.sex = sex
        self.verbose = verbose
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": USER_AGENT})

    def log(self, msg):
        if not self.verbose:
            return
        # The in-flight answer line rewrites itself; anything else is a plain line.
        if msg.endswith("\r"):
            sys.stdout.write(msg)
            sys.stdout.flush()
        else:
            print(msg, flush=True)

    def ajax_headers(self, token, referer):
        return {
            "X-CSRF-TOKEN": token,
            "X-Requested-With": "XMLHttpRequest",
            "Referer": referer,
            "Origin": BASE,
        }

    def resolve_state(self):
        """Walk the login flow until the questionnaire page is reached.

        Returns (status, payload) where status is one of
        'new', 'partial' (payload = questionnaire html) or 'used'.
        """
        self.session.get(f"{BASE}/", timeout=TIMEOUT)
        r = self.session.get(
            f"{BASE}/anketa", params={"oLogin": self.login}, timeout=TIMEOUT
        )

        # A partially completed test redirects straight to the questionnaire,
        # so the survey form is absent even though the test is still open.
        if "var testData" in r.text:
            status = "partial" if parse_test_page(r.text)["completion"] else "new"
            return status, r.text

        if "anketa_reg" not in r.text:
            # The login form is served again: either unknown or already spent.
            return "used", None

        token = csrf_from(r.text)
        referer = f"{BASE}/anketa?oLogin={self.login}"
        self.session.post(
            f"{BASE}/anketa_reg",
            data={
                "_token": token,
                "onetimeLogin": self.login,
                "oLogin": self.login,
                "age": self.age,
                "sex": self.sex,
            },
            headers=self.ajax_headers(token, referer),
            timeout=TIMEOUT,
        )
        self.log(f"  survey form submitted (age {self.age}, sex {self.sex})")

        r = self.session.get(
            f"{BASE}/startTest", params={"oLogin": self.login}, timeout=TIMEOUT
        )
        if "var testData" not in r.text:
            return "used", None
        return "partial" if parse_test_page(r.text)["completion"] else "new", r.text

    def run(self, base, seed=None):
        status, html = self.resolve_state()

        if status == "used":
            self.log("  already completed, skipping")
            return 0

        meta = parse_test_page(html)
        test_id = meta["test_id"]
        sys_id = meta["sys_id"]
        token = meta["csrf"]
        referer = f"{BASE}/startTest?oLogin={self.login}"
        completion = meta["completion"]

        if completion:
            pending = [q["num"] for q in completion["missing_questions"]]
            self.log(
                f"  resuming: {completion['answered_count']}/"
                f"{completion['total_questions']} already answered, "
                f"{len(pending)} to go"
            )
        else:
            pending = [q["num"] for q in meta["questions"]]
            self.log(f"  starting: {len(pending)} questions")

        start_payload = {"test_id": test_id, "sysId": sys_id}
        if completion:
            start_payload["completion_mode"] = "true"
        self.session.post(
            f"{BASE}/ajaxTestStart",
            data=start_payload,
            headers=self.ajax_headers(token, referer),
            timeout=TIMEOUT,
        )

        answers, used_seed = build_answers(pending, base, seed)
        self.log(f"  seed {used_seed}, ~{RANDOMIZE_FRAC:.0%} of answers nudged")

        recorded = []
        total = len(pending)
        for index, num in enumerate(pending, start=1):
            a = answers[num]
            r = self.session.post(
                f"{BASE}/ajaxAnswerSave",
                data={
                    "test_id": test_id,
                    "sysId": sys_id,
                    "qCurrent": num,
                    "answ": a,
                    "a_time": ANSWER_TIME_MS,
                },
                headers=self.ajax_headers(token, referer),
                timeout=TIMEOUT,
            )
            if r.status_code != 200:
                self.log(f"  question {num}: HTTP {r.status_code}, aborting")
                return 1
            self.log(
                f"  [{index:3d}/{total}] q{num:3d} -> {a:2d}"
            )
            recorded.append(
                {
                    "test_id": int(test_id),
                    "sysId": sys_id,
                    "num": num,
                    "answ": a,
                    "a_time": ANSWER_TIME_MS,
                }
            )

        finished_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")
        started_at = (
            (meta["started_at"].group(1) if meta["started_at"] else None)
            or self.session.cookies.get(f"spt_started_{self.login}")
            or finished_at
        )

        r = self.session.post(
            f"{BASE}/ajaxTestOver",
            data={
                "test_id": test_id,
                "sysId": sys_id,
                "type": "finish",
                "started_at": started_at,
                "finished_at": finished_at,
                "full_data": json.dumps(recorded, ensure_ascii=False),
            },
            headers=self.ajax_headers(token, referer),
            timeout=60,
        )
        try:
            res = r.json()
        except ValueError:
            self.log(f"  finish: HTTP {r.status_code}, body: {r.text[:300]}")
            return 1

        if str(res.get("success")) != "1":
            self.log(f"  finish rejected: {json.dumps(res, ensure_ascii=False)[:400]}")
            return 1

        self.log(f"  COMPLETED ({len(recorded)} answers sent), group {res.get('itog_group')}")
        feedback = (res.get("feedback") or "").strip()
        if feedback:
            self.log("  server feedback:")
            self.log(feedback)
        return 0


def main():
    ap = argparse.ArgumentParser(description="Completes the RPspt survey for login codes.")
    ap.add_argument("logins", nargs="*", help="login codes; read from stdin if omitted")
    ap.add_argument("--seed", type=int, help="randomisation seed (random by default)")
    ap.add_argument("--age", type=int, default=AGE)
    ap.add_argument("--sex", default=SEX, choices=["male", "female"])
    args = ap.parse_args()

    logins = [line.strip() for line in args.logins if line.strip()]
    if not logins:
        logins = [line.strip() for line in sys.stdin if line.strip()]
    if not logins:
        print("no login codes given", file=sys.stderr)
        return 2

    with open(ANSWERS_PATH) as f:
        base = json.load(f)

    rc = 0
    for raw in logins:
        login = raw.strip()
        if not login:
            continue
        print(f"=== {login} ===", flush=True)
        try:
            rc |= SurveyRunner(login, args.age, args.sex).run(base, args.seed)
        except requests.RequestException as e:
            print(f"  network error: {e}", file=sys.stderr)
            rc = 1
    return rc


if __name__ == "__main__":
    sys.exit(main())
