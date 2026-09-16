#!/usr/bin/env python3
"""Rehearse the MAX V1 production release locally, end to end, with fakes only.

Builds a production-shaped stack from docker-compose.yml (PostgreSQL 16, web,
catalog-web with the restricted public role, telegram-bot, Caddy with the
production Caddyfile mounted as a single file) on the PRE-MAX base commit, seeds
it, proves Telegram works, then follows `max_release.py plan` step by step to the
candidate: images, CA and secrets, identity, migrations through web, public role,
telegram-bot, max-bot, edge route, subscription, username, catalog-web. It then
runs the first-time and returning MAX customer, a Telegram regression, webhook
fail-closed checks and the forensics script, and finally rehearses the rollback
back to the base code on the migrated database.

MAX and Telegram are local fakes (tests/max_fake.py over HTTPS with a throwaway
CA named like the production one, tests/telegram_fake.py). The only differences
from production are explicit and printed: plain HTTP in the Caddy test copy, the
fake API base URLs, rehearsal-only tool flags and fake credentials.

    python3 scripts/qualification/max_release_rehearsal.py --workdir /tmp/maxreh \\
        --base ee89760d914511967adb54366052e59292b2a711 --candidate HEAD

Needs Docker, git and the project's virtualenv Python (for the throwaway CA).
"""

from __future__ import annotations

import argparse
import http.cookiejar
import json
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "scripts" / "qualification"))

from max_edge_check import plain_http_copy  # noqa: E402

PROJECT = "maxreh"
PROXY_PORT = 18880
MAX_API_PORT = 18780
TELEGRAM_PORT = 18790
PUBLIC_HOST = "pro-brp.ru"
INTERNAL_HOST = "185-250-44-206.sslip.io"
WEBHOOK = "/customer-requests/max/webhook/"
MAX_USER, MAX_CHAT = 7001, 9001
TG_CUSTOMER = 5001
OPERATORS = {"denis": 810001, "masha": 810002}
FAKE_TELEGRAM_TOKEN = "123456789:AAFakeRehearsalTokenNotRealAAAAAAAAAAAA"
FAKE_MAX_TOKEN = "fake-max-token-for-local-tests-only"
ACK = "Сообщение передано менеджеру PRO-STOR."


class Rehearsal:
    def __init__(self, workdir: Path, base: str, candidate: str, keep: bool):
        self.work = workdir
        self.opt = workdir / "opt"
        self.etc = workdir / "etc"
        self.base = base
        self.candidate = candidate
        self.keep = keep
        self.failures = 0
        self.log = open(workdir / "rehearsal.log", "a", encoding="utf-8")  # noqa: SIM115
        self.tool = workdir / "tool" / "max_release.py"

    # --- plumbing ------------------------------------------------------------------------

    def step(self, title):
        print(f"\n== {title}", flush=True)
        self.log.write(f"\n== {title}\n")

    def check(self, ok, text):
        self.failures += not ok
        line = f"{'PASS' if ok else 'FAIL'}  {text}"
        print(line, flush=True)
        self.log.write(line + "\n")
        return ok

    def run(self, argv, *, cwd=None, input_text=None, env=None, check=True, quiet=False):
        self.log.write(f"$ {' '.join(str(a) for a in argv)}\n")
        result = subprocess.run(
            [str(a) for a in argv],
            cwd=cwd or self.opt,
            input=input_text,
            text=True,
            capture_output=True,
            env={**os.environ, **(env or {})},
        )
        self.log.write(result.stdout[-4000:] + result.stderr[-4000:])
        if check and result.returncode != 0:
            tail = (result.stderr or result.stdout).strip().splitlines()[-15:]
            raise SystemExit(f"STOP: {' '.join(str(a) for a in argv[:8])}\n" + "\n".join(tail))
        if not quiet and result.stdout.strip():
            print(result.stdout.strip()[-1500:], flush=True)
        return result

    def compose(self, *args, **kwargs):
        return self.run(["docker", "compose", *args], **kwargs)

    def tool_run(self, *args, **kwargs):
        rehearsal = [
            "--root",
            self.opt,
            "--caddyfile",
            self.etc / "caddy/Caddyfile",
            "--ca-dir",
            self.etc / "max",
            "--rehearsal",
            "--pre-max-caddyfile",
            self.etc / "caddy/Caddyfile.pre-max.http",
            "--candidate-caddyfile",
            self.etc / "caddy/Caddyfile.max.http",
        ]
        return self.run([sys.executable, self.tool, *rehearsal, *args], **kwargs)

    def shell(self, code, service="web"):
        return self.compose(
            "exec", "-T", service, "python", "manage.py", "shell", "-c", code, quiet=True
        )

    def wait_json(self, url, predicate, timeout=90):
        deadline = time.monotonic() + timeout
        last = None
        while time.monotonic() < deadline:
            try:
                with urllib.request.urlopen(url, timeout=5) as response:
                    last = json.loads(response.read())
                if predicate(last):
                    return last
            except (OSError, ValueError):
                pass
            time.sleep(1)
        return last

    # --- environment ---------------------------------------------------------------------

    def prepare(self):
        self.step("prepare an isolated production-shaped checkout on the base commit")
        if self.opt.exists():
            raise SystemExit(f"STOP: {self.opt} exists; use a fresh --workdir")
        (self.etc / "caddy").mkdir(parents=True)
        (self.etc / "max").mkdir(parents=True)
        (self.work / "tool").mkdir(parents=True)
        self.run(["git", "worktree", "add", "--detach", self.opt, self.base], cwd=REPO)
        for name in ("max_release.py", "max_release_plan.txt"):
            content = self.run(
                ["git", "show", f"{self.candidate}:scripts/operations/{name}"], cwd=REPO, quiet=True
            ).stdout
            (self.work / "tool" / name).write_text(content)
        pre = (REPO / "deploy/caddy/Caddyfile.production.pre-max").read_text()
        post = self.run(
            ["git", "show", f"{self.candidate}:deploy/caddy/Caddyfile.production"],
            cwd=REPO,
            quiet=True,
        ).stdout
        (self.etc / "caddy/Caddyfile.pre-max.http").write_text(plain_http_copy(pre))
        (self.etc / "caddy/Caddyfile.max.http").write_text(plain_http_copy(post))
        shutil.copy(self.etc / "caddy/Caddyfile.pre-max.http", self.etc / "caddy/Caddyfile")
        (self.opt / ".env").write_text(f"""COMPOSE_PROJECT_NAME={PROJECT}
COMPOSE_FILE=docker-compose.yml:docker-compose.signing.yml
DJANGO_SECRET_KEY=rehearsal-only-not-a-secret-{uuid.uuid4().hex}
DJANGO_SETTINGS_MODULE=config.settings.prod
DJANGO_DEBUG=false
DJANGO_ALLOWED_HOSTS=localhost,127.0.0.1,{INTERNAL_HOST}
DJANGO_CSRF_TRUSTED_ORIGINS=http://{INTERNAL_HOST}
DJANGO_SECURE_COOKIES=false
POSTGRES_DB=denstock
POSTGRES_USER=denstock
POSTGRES_PASSWORD=rehearsal_db_pw
DATABASE_URL=postgres://denstock:rehearsal_db_pw@db:5432/denstock
CADDY_SITE_ADDRESS=http://{INTERNAL_HOST}
DENSTOCK_APP_COMMIT={self.base}
AI_SUPPORT_ENABLED=false
TELEGRAM_BOT_USERNAME=sim_telegram_bot
TELEGRAM_INTERNAL_BASE_URL=http://{INTERNAL_HOST}
TELEGRAM_API_PROXY_URL=
DENSTOCK_MAX_CA_DIR={self.etc / "max"}
""")
        (
            self.opt / ".env.public"
        ).write_text(f"""DJANGO_SECRET_KEY=rehearsal-public-{uuid.uuid4().hex}
DJANGO_PUBLIC_ALLOWED_HOSTS={PUBLIC_HOST},localhost
DJANGO_ALLOWED_HOSTS={PUBLIC_HOST},localhost
PUBLIC_DATABASE_URL=postgres://denstock_public:rehearsal_public_pw@db:5432/denstock
DATABASE_URL=postgres://denstock_public:rehearsal_public_pw@db:5432/denstock
DJANGO_SECURE_COOKIES=false
PUBLIC_CATALOG_BASE_URL=https://{PUBLIC_HOST}
PUBLIC_CATALOG_INDEXING=false
TELEGRAM_BOT_USERNAME=sim_telegram_bot
""")
        (self.opt / ".env.telegram").write_text(
            f"TELEGRAM_BOT_TOKEN={FAKE_TELEGRAM_TOKEN}\n"
            f"TELEGRAM_API_BASE_URL=http://fake-telegram:{TELEGRAM_PORT}\n"
        )
        for name in (".env", ".env.public", ".env.telegram"):
            (self.opt / name).chmod(0o600)
        # Stands in for the untracked production signing overlay: the proxy
        # serves an /etc Caddyfile through a single-file bind mount.
        (self.opt / "docker-compose.signing.yml").write_text(f"""services:
  proxy:
    ports: !override
      - "127.0.0.1:{PROXY_PORT}:80"
    volumes:
      - {self.etc / "caddy/Caddyfile"}:/etc/caddy/Caddyfile:ro
""")
        self.check(
            True,
            "differences from production: plain-HTTP Caddy copy, fake API URLs, "
            "fake credentials, rehearsal tool flags, no signed backups",
        )

    def start_fakes(self):
        self.step("start fake Telegram and fake MAX (HTTPS, throwaway root CA)")
        sys.path.insert(0, str(REPO))
        from tests.max_tls import make_local_ca

        local = make_local_ca(
            self.etc / "max-src", label="Fake Russian Trusted Root CA", hostnames=("fake-max",)
        )
        shutil.copy(local.ca_file, self.etc / "max/russian-trusted-root-ca.pem")
        self.ca_sha256 = local.ca_sha256
        network = f"{PROJECT}_default"
        self.run(["docker", "network", "inspect", network], check=False, quiet=True)
        for name in ("fake-telegram", "fake-max"):
            self.run(["docker", "rm", "-f", f"{PROJECT}-{name}"], check=False, quiet=True)
        common = [
            "docker",
            "run",
            "-d",
            "--network",
            network,
            "-v",
            f"{REPO / 'tests'}:/fakes/tests:ro",
            "-w",
            "/fakes",
            "python:3.12.13-slim",
        ]
        self.run(
            common[:3]
            + [
                "--name",
                f"{PROJECT}-fake-telegram",
                "--network-alias",
                "fake-telegram",
                "-p",
                f"127.0.0.1:{TELEGRAM_PORT}:{TELEGRAM_PORT}",
            ]
            + common[3:]
            + [
                "python",
                "-m",
                "tests.telegram_fake",
                "--port",
                str(TELEGRAM_PORT),
                "--bind",
                "0.0.0.0",
            ]
        )
        self.run(
            common[:3]
            + [
                "--name",
                f"{PROJECT}-fake-max",
                "--network-alias",
                "fake-max",
                "-p",
                f"127.0.0.1:{MAX_API_PORT}:{MAX_API_PORT}",
                "-v",
                f"{self.etc / 'max-src'}:/tls:ro",
            ]
            + common[3:]
            + [
                "python",
                "-m",
                "tests.max_fake",
                "serve",
                "--api-port",
                str(MAX_API_PORT),
                "--bind",
                "0.0.0.0",
                "--tls-cert",
                "/tls/server-chain.pem",
                "--tls-key",
                "/tls/server-key.pem",
            ]
        )

    # --- HTTP through the proxy ----------------------------------------------------------

    def browser(self):
        jar = http.cookiejar.CookieJar()

        class NoRedirect(urllib.request.HTTPRedirectHandler):
            def redirect_request(self, *args, **kwargs):
                return None

        return urllib.request.build_opener(
            NoRedirect(), urllib.request.HTTPCookieProcessor(jar), urllib.request.ProxyHandler({})
        )

    def http(self, opener, method, path, *, host=PUBLIC_HOST, form=None, body=None, headers=None):
        data = urllib.parse.urlencode(form).encode() if form is not None else body
        request = urllib.request.Request(
            f"http://127.0.0.1:{PROXY_PORT}{path}",
            data=data,
            method=method,
            headers={
                "Host": host,
                **(
                    {"Content-Type": "application/x-www-form-urlencoded"}
                    if form is not None
                    else {}
                ),
                **(headers or {}),
            },
        )
        try:
            with opener.open(request, timeout=30) as response:
                return response.status, response.headers, response.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as exc:
            return exc.code, exc.headers, exc.read().decode("utf-8", "replace")

    def customer_request(self, messenger):
        """Search, add to cart, send the request, open the success page, continue."""
        opener = self.browser()
        status, _, page = self.http(opener, "GET", "/search/?q=" + urllib.parse.quote("ремень"))
        action = re.search(r'action="(/cart/[0-9a-f-]{36}/add/)"', page)
        csrf = re.search(r'name="csrfmiddlewaretoken" value="([^"]+)"', page)
        if not self.check(status == 200 and action and csrf, f"catalog search ({status})"):
            raise SystemExit("STOP: catalog search failed")
        self.http(
            opener,
            "POST",
            action.group(1),
            form={"csrfmiddlewaretoken": csrf.group(1), "quantity": "1"},
        )
        _, _, form = self.http(opener, "GET", "/request/")
        csrf = re.search(r'name="csrfmiddlewaretoken" value="([^"]+)"', form).group(1)
        key = re.search(r'name="submission_key" value="([^"]+)"', form).group(1)
        status, headers, _ = self.http(
            opener,
            "POST",
            "/request/submit/",
            form={
                "csrfmiddlewaretoken": csrf,
                "submission_key": key,
                "customer_name": "Проверка MAX",
                "customer_phone": "+7 900 000-00-00",
                "preferred_messenger": messenger,
                "comment": "Тестовая заявка релизной репетиции",
                "consent": "1",
            },
        )
        success = headers.get("Location", "")
        self.check(status == 302 and "/request/success/" in success, f"{messenger} request sent")
        status, headers, page = self.http(opener, "GET", success)
        policy = headers.get("Content-Security-Policy", "")
        csrf = re.search(r'name="csrfmiddlewaretoken" value="([^"]+)"', page)
        public_id = success.rstrip("/").rsplit("/", 1)[-1]
        return opener, public_id, page, policy, (csrf.group(1) if csrf else "")

    def handoff(self, messenger, expected_origin):
        opener, public_id, page, policy, csrf = self.customer_request(messenger)
        label = "Продолжить в MAX" if messenger == "max" else "Продолжить в Telegram"
        self.check(
            label in page and "?start=" not in page,
            f"{messenger} success page offers the handoff without a token",
        )
        self.check(
            f"form-action 'self' {expected_origin}" in policy,
            f"{messenger} success page CSP allows exactly {expected_origin}",
        )
        status, headers, _ = self.http(
            opener,
            "POST",
            f"/request/success/{public_id}/{messenger}/",
            form={"csrfmiddlewaretoken": csrf},
        )
        location = headers.get("Location", "")
        self.check(
            status == 303 and location.startswith(expected_origin + "/"),
            f"{messenger} handoff 303 -> {location.split('?')[0]}",
        )
        token = urllib.parse.parse_qs(urllib.parse.urlparse(location).query).get("start", [""])[0]
        reference = public_id.split("-")[0].upper()
        return reference, token

    def webhook(self, kind, *extra, secret=None, expect=200):
        env = {"MAX_WEBHOOK_SECRET": self.webhook_secret if secret is None else secret}
        result = self.run(
            [
                sys.executable,
                "-m",
                "tests.max_fake",
                "send",
                "--url",
                f"http://127.0.0.1:{PROXY_PORT}{WEBHOOK}",
                "--host",
                PUBLIC_HOST,
                kind,
                *extra,
            ],
            cwd=REPO,
            env=env,
            check=False,
            quiet=True,
        )
        return result.stdout.strip().split()[0] if result.stdout.strip() else "?"

    def max_sent(self, chat=MAX_CHAT):
        with urllib.request.urlopen(f"http://127.0.0.1:{MAX_API_PORT}/_fake/sent", timeout=5) as r:
            data = json.loads(r.read())
        return [item["text"] for item in data["sent"] if item["chat_id"] == chat], data

    def wait_max_texts(self, count, chat=MAX_CHAT, timeout=60):
        deadline = time.monotonic() + timeout
        texts = []
        while time.monotonic() < deadline:
            texts, _ = self.max_sent(chat)
            if len(texts) >= count:
                return texts
            time.sleep(1)
        return texts

    def telegram(self, update):
        request = urllib.request.Request(
            f"http://127.0.0.1:{TELEGRAM_PORT}/_fake/update",
            data=json.dumps(update, ensure_ascii=False).encode(),
            method="POST",
            headers={"Content-Type": "application/json"},
        )
        urllib.request.urlopen(request, timeout=5).read()

    def telegram_sent(self, chat):
        with urllib.request.urlopen(
            f"http://127.0.0.1:{TELEGRAM_PORT}/_fake/sent", timeout=5
        ) as response:
            return [i["text"] for i in json.loads(response.read())["sent"] if i["chat_id"] == chat]

    def wait_telegram(self, chat, predicate, timeout=60):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            texts = self.telegram_sent(chat)
            if predicate(texts):
                return texts
            time.sleep(1)
        return self.telegram_sent(chat)

    def tg_message(self, user, text):
        return {
            "message": {
                "message_id": int(time.time() * 1000) % 10**9,
                "chat": {"id": user, "type": "private"},
                "from": {"id": user, "is_bot": False},
                "text": text,
            }
        }

    # --- phases --------------------------------------------------------------------------

    def pre_max_state(self):
        self.step("PRE-MAX production state on the base commit")
        self.compose(
            "--profile",
            "public-catalog",
            "--profile",
            "telegram-bot",
            "build",
            "web",
            "catalog-web",
            "telegram-bot",
            quiet=True,
        )
        self.compose("up", "-d", "--wait", "db", quiet=True)
        self.start_fakes()
        self.compose("up", "-d", "--no-deps", "--wait", "web", quiet=True)
        self.compose(
            "exec",
            "-T",
            "db",
            "psql",
            "-U",
            "denstock",
            "-d",
            "denstock",
            "-qc",
            "CREATE ROLE denstock_public LOGIN PASSWORD 'rehearsal_public_pw'",
            quiet=True,
        )
        script = (self.opt / "scripts/operations/create_public_catalog_role.sql").read_text()
        self.compose(
            "exec",
            "-T",
            "db",
            "psql",
            "-v",
            "ON_ERROR_STOP=1",
            "-v",
            "public_role=denstock_public",
            "-q",
            "-U",
            "denstock",
            "-d",
            "denstock",
            input_text=script,
            quiet=True,
        )
        self.compose(
            "exec",
            "-T",
            "web",
            "python",
            "manage.py",
            "seed_public_catalog_demo",
            "--confirm-isolated",
            quiet=True,
        )
        self.shell(
            "from django.contrib.auth import get_user_model; from django.contrib.auth.models "
            "import Group; from apps.accounts import roles; from apps.customer_requests.models "
            "import TelegramOperator\n"
            + "".join(
                f"u=get_user_model().objects.create_user(username='{name}', password='x'*16, "
                f"first_name='{name.title()}'); u.groups.add(Group.objects.get(name=roles.SELLER));"
                f" TelegramOperator.objects.create(user=u, telegram_user_id={tg})\n"
                for name, tg in OPERATORS.items()
            )
        )
        self.compose(
            "--profile",
            "telegram-bot",
            "up",
            "-d",
            "--no-deps",
            "--wait",
            "telegram-bot",
            quiet=True,
        )
        self.compose(
            "--profile",
            "public-catalog",
            "up",
            "-d",
            "--no-deps",
            "--wait",
            "catalog-web",
            quiet=True,
        )
        self.compose("up", "-d", "--no-deps", "proxy", quiet=True)
        time.sleep(3)
        _, token = self.handoff("telegram", "https://t.me")
        self.telegram(self.tg_message(TG_CUSTOMER, f"/start {token}"))
        texts = self.wait_telegram(TG_CUSTOMER, lambda t: any("подключён" in x for x in t))
        self.check(
            any("Готово. Telegram подключён" in t for t in texts),
            "base: Telegram start summary delivered",
        )
        status, _, _ = self.http(self.browser(), "POST", WEBHOOK, body=b"{}")
        self.check(status == 404, f"base: MAX webhook on {PUBLIC_HOST} is catalog-web ({status})")

    def release(self):
        c = self.candidate
        self.step("A. gates")
        self.tool_run("preflight", "--expect-head", self.base, "--candidate", c)
        self.step("C. candidate code and images")
        self.run(["git", "checkout", "--detach", c])
        env = (self.opt / ".env").read_text()
        (self.opt / ".env").write_text(
            re.sub(r"^DENSTOCK_APP_COMMIT=.*$", f"DENSTOCK_APP_COMMIT={c}", env, flags=re.M)
        )
        self.compose(
            "--profile",
            "public-catalog",
            "--profile",
            "telegram-bot",
            "--profile",
            "max-bot",
            "build",
            "web",
            "catalog-web",
            "telegram-bot",
            "max-bot",
            quiet=True,
        )
        self.check(True, "images built for web, catalog-web, telegram-bot, max-bot")
        self.step("D. CA, secrets and identity (no database change yet)")
        self.tool_run(
            "install-secrets",
            "--ca-sha256",
            self.ca_sha256,
            "--token-from-stdin",
            "--execute",
            input_text=FAKE_MAX_TOKEN + "\n",
        )
        with open(self.opt / ".env.max", "a") as handle:  # rehearsal only: the fake MAX
            handle.write(f"MAX_API_BASE_URL=https://fake-max:{MAX_API_PORT}\n")
        self.webhook_secret = dict(
            line.split("=", 1) for line in (self.opt / ".env.max-webhook").read_text().split()
        )["MAX_WEBHOOK_SECRET"]
        modes = [
            oct((self.opt / n).stat().st_mode & 0o777) for n in (".env.max", ".env.max-webhook")
        ]
        self.check(modes == ["0o600", "0o600"], f"secret files are 0600 {modes}")
        identity = self.compose(
            "--profile", "max-bot", "run", "--rm", "--no-deps", "max-bot", "max_bot_identity"
        )
        self.check(
            "is_bot: true" in identity.stdout and FAKE_MAX_TOKEN not in identity.stdout,
            "GET /me through the pinned CA; token not printed",
        )
        self.step("E. schema, grants, services MAX depends on")
        self.compose("up", "-d", "--no-deps", "--wait", "web", quiet=True)
        self.tool_run("public-role", "--execute")
        self.compose(
            "--profile",
            "telegram-bot",
            "up",
            "-d",
            "--no-deps",
            "--wait",
            "telegram-bot",
            quiet=True,
        )
        self.tool_run("verify", "--candidate", c, "--services", "web,telegram-bot")
        status, _, _ = self.http(self.browser(), "POST", WEBHOOK, body=b"{}")
        self.check(
            status == 404, f"before edge-install the public webhook is catalog-web ({status})"
        )
        self.step("F. MAX live, customer entry point last")
        self.compose(
            "--profile", "max-bot", "up", "-d", "--no-deps", "--wait", "max-bot", quiet=True
        )
        self.tool_run("edge-install", "--execute")
        for label, secret, expect in (
            ("no secret", "", "404"),
            ("wrong secret", "wrong_secret_x", "404"),
        ):
            self.check(
                self.webhook("bot_started", "--user", "1", "--chat", "1", secret=secret) == expect,
                f"webhook through the edge with {label} -> {expect}",
            )
        bad = self.http(
            self.browser(),
            "POST",
            WEBHOOK,
            body=b"{not json",
            headers={
                "X-Max-Bot-Api-Secret": self.webhook_secret,
                "Content-Type": "application/json",
            },
        )[0]
        self.check(bad == 400, f"malformed body with the right secret -> 400 ({bad})")
        self.compose(
            "--profile",
            "max-bot",
            "run",
            "--rm",
            "--no-deps",
            "-v",
            f"{self.opt / '.env.max-webhook'}:/run/max-webhook.env:ro",
            "max-bot",
            "max_webhook",
            "subscribe",
            "--confirm",
            "--secret-file",
            "/run/max-webhook.env",
        )
        self.compose(
            "--profile",
            "max-bot",
            "run",
            "--rm",
            "--no-deps",
            "max-bot",
            "max_webhook",
            "status",
            "--require-subscribed",
        )
        line = (
            self.compose(
                "--profile",
                "max-bot",
                "run",
                "--rm",
                "--no-deps",
                "max-bot",
                "max_bot_identity",
                "--env-line",
                quiet=True,
            )
            .stdout.strip()
            .splitlines()[-1]
        )
        self.tool_run("set-username", line, "--execute")
        self.compose(
            "--profile",
            "public-catalog",
            "up",
            "-d",
            "--no-deps",
            "--wait",
            "catalog-web",
            quiet=True,
        )
        self.tool_run(
            "verify",
            "--candidate",
            c,
            "--services",
            "web,telegram-bot,max-bot,catalog-web",
            "--require-subscribed",
        )
        logs = self.compose(
            "logs", "--no-color", "web", "max-bot", "catalog-web", "proxy", quiet=True
        ).stdout
        self.check(
            FAKE_MAX_TOKEN not in logs and self.webhook_secret not in logs,
            "no token or webhook secret in any service log",
        )

    def acceptance(self):
        self.step("G. first-time MAX customer")
        before = self.forensics("")
        ref_a, token_a = self.handoff("max", "https://max.ru")
        self.webhook(
            "bot_started", "--user", str(MAX_USER), "--chat", str(MAX_CHAT), "--payload", token_a
        )
        texts = self.wait_max_texts(1)
        self.check(
            bool(texts) and texts[0].startswith(f"Готово. MAX подключён к заявке {ref_a}."),
            "start summary for the right request",
        )
        self.webhook(
            "message_created",
            "--user",
            str(MAX_USER),
            "--chat",
            str(MAX_CHAT),
            "--text",
            "первое тестовое сообщение",
        )
        self.wait_max_texts(2)
        self.webhook(
            "message_created",
            "--user",
            str(MAX_USER),
            "--chat",
            str(MAX_CHAT),
            "--text",
            "второе тестовое сообщение",
        )
        time.sleep(4)
        self.check(self.max_sent()[0].count(ACK) == 1, "exactly one ACK for two messages")
        self.shell(
            "from django.contrib.auth import get_user_model; from apps.customer_requests import "
            "max_service; from apps.customer_requests.models import CustomerRequest\n"
            f"r=[x for x in CustomerRequest.objects.all() if x.reference=='{ref_a}'][0]\n"
            "u=get_user_model().objects.get(username='denis')\n"
            "max_service.submit_operator_reply(request_id=r.pk, user=u, "
            f"text='тестовый ответ менеджера', submission_key='{uuid.uuid4().hex}')"
        )
        texts = self.wait_max_texts(3)
        self.check(texts.count("тестовый ответ менеджера") == 1, "operator reply delivered once")
        masha = self.wait_telegram(
            OPERATORS["masha"], lambda t: any("ответ клиенту" in x for x in t)
        )
        self.check(
            any("MAX · сообщение клиента" in t for t in masha)
            and any("ответ клиенту отправлен" in t for t in masha),
            "the other employee heard the MAX messages and the reply via Telegram",
        )
        self.step("G. returning MAX customer, same account")
        ref_b, token_b = self.handoff("max", "https://max.ru")
        self.webhook(
            "message_created",
            "--user",
            str(MAX_USER),
            "--chat",
            str(MAX_CHAT),
            "--text",
            f"/start {token_b}",
        )
        texts = self.wait_max_texts(4)
        self.check(
            texts[-1].startswith(f"Готово. MAX подключён к заявке {ref_b}."),
            "second request bound in the existing dialog",
        )
        hexes = json.loads(
            self.shell(
                "import json; from apps.customer_requests.models import MaxConversation\n"
                "print(json.dumps({c.request.reference: c.public_id.hex for c in "
                "MaxConversation.objects.select_related('request')}))"
            )
            .stdout.strip()
            .splitlines()[-1]
        )
        for ref, text in ((ref_b, "сообщение для B"), (ref_a, "сообщение для A")):
            count = len(self.max_sent()[0])
            self.webhook(
                "message_callback",
                "--user",
                str(MAX_USER),
                "--chat",
                str(MAX_CHAT),
                "--payload",
                f"s:{hexes[ref]}",
            )
            self.wait_max_texts(count + 1)
            self.webhook(
                "message_created", "--user", str(MAX_USER), "--chat", str(MAX_CHAT), "--text", text
            )
        time.sleep(4)
        routed = json.loads(
            self.shell(
                "import json; from apps.customer_requests.models import MaxMessage\n"
                "print(json.dumps([[m.conversation.request.reference, m.text] for m in "
                "MaxMessage.objects.filter(direction='customer_to_operator').select_related("
                "'conversation__request').order_by('pk')]))"
            )
            .stdout.strip()
            .splitlines()[-1]
        )
        self.check(
            [ref_b, "сообщение для B"] in routed
            and [ref_a, "сообщение для A"] in routed
            and [ref_a, "сообщение для B"] not in routed,
            "selection routes B to B, A to A",
        )
        self.step("G. Telegram regression after the MAX release")
        _, token = self.handoff("telegram", "https://t.me")
        customer = TG_CUSTOMER + 1
        self.telegram(self.tg_message(customer, f"/start {token}"))
        self.wait_telegram(customer, lambda t: any("подключён" in x for x in t))
        self.telegram(self.tg_message(customer, "первое тестовое сообщение"))
        texts = self.wait_telegram(customer, lambda t: ACK in t)
        self.telegram(self.tg_message(customer, "второе тестовое сообщение"))
        time.sleep(15)
        texts = self.telegram_sent(customer)
        self.check(
            any("Готово. Telegram подключён" in t for t in texts) and texts.count(ACK) == 1,
            "Telegram: start summary and exactly one ACK",
        )
        self.step("G. forensics and cancellation through the normal workflow")
        after = self.forensics(f"{ref_a},{ref_b}", operators="denis,masha")
        self.check("MAX FORENSICS PASS" in after, "forensics PASS for both MAX requests")
        counts = [re.search(r"business_counts=(.*)", text).group(1) for text in (before, after)]
        self.check(counts[0] == counts[1], f"business counts unchanged {counts[1]}")
        self.shell(
            "from django.contrib.auth import get_user_model; from apps.customer_requests.models "
            "import CustomerRequest; from apps.customer_requests.services import "
            "change_request_status\nu=get_user_model().objects.get(username='denis')\n"
            f"refs=('{ref_a}','{ref_b}')\n"
            "for r in [x for x in CustomerRequest.objects.all() if x.reference in refs]:"
            "\n    change_request_status(request_id=r.pk, target_status='canceled', by=u)"
        )
        kept = self.shell(
            "from apps.customer_requests.models import MaxMessage, CustomerRequest\n"
            "cancelled = CustomerRequest.objects.filter(status='canceled').count()\n"
            "print(MaxMessage.objects.count(), cancelled)"
        ).stdout.split()
        self.check(int(kept[-1]) >= 2 and int(kept[-2]) > 0, f"cancelled, history kept {kept[-2:]}")

    def forensics(self, references, operators=""):
        script = (self.opt / "scripts/operations/max_forensics.py").read_text()
        result = self.compose(
            "exec",
            "-T",
            "-e",
            f"MAX_FORENSICS_REFERENCES={references}",
            "-e",
            f"MAX_FORENSICS_OPERATORS={operators}",
            "web",
            "python",
            "manage.py",
            "shell",
            input_text=script,
            quiet=True,
        )
        print("\n".join(x for x in result.stdout.splitlines() if x.startswith(("FAIL", "MAX F"))))
        return result.stdout

    def rollback(self):
        b = self.base
        self.step("ROLLBACK rehearsal: stop MAX traffic, then code back to base on the new schema")
        self.compose(
            "--profile",
            "max-bot",
            "run",
            "--rm",
            "--no-deps",
            "max-bot",
            "max_webhook",
            "unsubscribe",
            "--confirm",
        )
        _, data = self.max_sent()
        self.compose("--profile", "max-bot", "stop", "max-bot", quiet=True)
        self.tool_run("unset-username", "--execute")
        self.compose(
            "--profile",
            "public-catalog",
            "up",
            "-d",
            "--no-deps",
            "--wait",
            "catalog-web",
            quiet=True,
        )
        self.tool_run("edge-rollback", "--execute")
        for name in (".env.max-webhook", ".env.max"):
            (self.opt / name).rename(self.work / f"{name}.disabled")
        self.run(["git", "checkout", "--detach", b])
        env = (self.opt / ".env").read_text()
        (self.opt / ".env").write_text(
            re.sub(r"^DENSTOCK_APP_COMMIT=.*$", f"DENSTOCK_APP_COMMIT={b}", env, flags=re.M)
        )
        self.compose(
            "--profile",
            "public-catalog",
            "--profile",
            "telegram-bot",
            "build",
            "web",
            "catalog-web",
            "telegram-bot",
            quiet=True,
        )
        self.compose("up", "-d", "--no-deps", "--wait", "web", quiet=True)
        self.compose(
            "--profile",
            "telegram-bot",
            "up",
            "-d",
            "--no-deps",
            "--wait",
            "telegram-bot",
            quiet=True,
        )
        self.compose(
            "--profile",
            "public-catalog",
            "up",
            "-d",
            "--no-deps",
            "--wait",
            "catalog-web",
            quiet=True,
        )
        self.check(True, "base web, telegram-bot and catalog-web healthy on the migrated database")
        status, _, _ = self.http(self.browser(), "POST", WEBHOOK, body=b"{}")
        self.check(
            status == 404, f"after rollback the public webhook is catalog-web again ({status})"
        )
        applied = (
            self.shell(
                "from django.db.migrations.recorder import MigrationRecorder\n"
                "print(MigrationRecorder.Migration.objects.filter(name='0009_max_public_link_guard').exists())"
            )
            .stdout.strip()
            .splitlines()[-1]
        )
        self.check(applied == "True", "MAX migrations stay applied; nothing reversed")
        _, token = self.handoff("telegram", "https://t.me")
        customer = TG_CUSTOMER + 2
        self.telegram(self.tg_message(customer, f"/start {token}"))
        texts = self.wait_telegram(customer, lambda t: any("подключён" in x for x in t))
        self.check(
            any("Готово. Telegram подключён" in t for t in texts),
            "base code: Telegram still works after rollback",
        )

    def teardown(self):
        if self.keep:
            print(f"kept: {self.opt} (docker compose down -v there to remove)")
            return
        self.compose(
            "--profile",
            "public-catalog",
            "--profile",
            "telegram-bot",
            "down",
            "-v",
            check=False,
            quiet=True,
        )
        for name in ("fake-telegram", "fake-max"):
            self.run(["docker", "rm", "-f", f"{PROJECT}-{name}"], check=False, quiet=True)
        self.run(["git", "worktree", "remove", "--force", self.opt], cwd=REPO, check=False)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--workdir", type=Path, required=True)
    parser.add_argument("--base", required=True)
    parser.add_argument("--candidate", default="HEAD")
    parser.add_argument("--keep", action="store_true")
    args = parser.parse_args(argv)
    candidate = subprocess.run(
        ["git", "rev-parse", args.candidate], cwd=REPO, text=True, capture_output=True, check=True
    ).stdout.strip()
    args.workdir.mkdir(parents=True, exist_ok=True)
    rehearsal = Rehearsal(args.workdir.resolve(), args.base, candidate, args.keep)
    try:
        rehearsal.prepare()
        rehearsal.pre_max_state()
        rehearsal.release()
        rehearsal.acceptance()
        rehearsal.rollback()
    finally:
        rehearsal.teardown()
    print(json.dumps({"base": args.base, "candidate": candidate, "failures": rehearsal.failures}))
    return 1 if rehearsal.failures else 0


if __name__ == "__main__":
    sys.exit(main())
