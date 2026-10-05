"""Очередь автосохранения быстрых действий, проверенная под node без браузера.

Что обязано держаться (static/js/quick_actions_autosave.js):
* быстрый ввод 1 -> 12 -> 123 сохраняет 123, а не промежуточное значение;
* запросы одной корзины идут строго по одному, старый ответ не обгоняет новый;
* «Провести» (flush) ждёт и отложенное, и уже отправленное сохранение;
* неудачное сохранение не даёт провести, пока не сохранится.
"""
import json
import shutil
import subprocess
from pathlib import Path

import pytest

JS_PATH = Path(__file__).resolve().parents[1] / "static" / "js" / "quick_actions_autosave.js"

HARNESS = r"""
const { createAutosaveQueue } = require(process.argv[1]);
const timers = [];
const schedule = (fn) => { timers.push(fn); return timers.length - 1; };
const cancel = (handle) => { timers[handle] = null; };
const fireTimers = () => timers.splice(0).forEach((fn) => fn && fn());
const tick = () => new Promise((resolve) => setImmediate(resolve));

function makeServer() {
  const server = { value: {}, inFlight: 0, maxInFlight: 0, sent: [], pending: [] };
  server.send = (key, field, reply) => {
    const body = { key, value: field.value };
    server.sent.push(body);
    server.inFlight += 1;
    server.maxInFlight = Math.max(server.maxInFlight, server.inFlight);
    return new Promise((resolve) => {
      server.pending.push(() => {
        server.inFlight -= 1;
        const ok = reply ? reply(body) : true;
        if (ok) server.value[key] = body.value;
        resolve({ ok });
      });
    });
  };
  // Отвечает на запросы в ОБРАТНОМ порядке, если их больше одного: так сеть
  // переставила бы ответы, если бы очередь слала параллельно.
  server.answer = async () => {
    while (server.pending.length) {
      server.pending.pop()();
      await tick();
    }
  };
  return server;
}

const scenarios = {
  async debouncedTypingSavesTheLastValue() {
    const field = { value: "" };
    const server = makeServer();
    const queue = createAutosaveQueue({ send: (k) => server.send(k, field), schedule, cancel });
    for (const value of ["1", "12", "123"]) { field.value = value; queue.touch("q", false); }
    fireTimers(); await tick(); await server.answer();
    return { sent: server.sent.map((s) => s.value), saved: server.value.q };
  },

  async requestsNeverOverlapAndTheNewestWins() {
    const field = { value: "1" };
    const server = makeServer();
    const queue = createAutosaveQueue({ send: (k) => server.send(k, field), schedule, cancel });
    queue.touch("q", true); await tick();            // "1" в полёте
    field.value = "12"; queue.touch("q", true);       // ждёт, пока ответит первый
    field.value = "123"; queue.touch("q", true);
    await tick();
    const inFlightWhileFirstPending = server.inFlight;
    while (server.pending.length) { await server.answer(); await tick(); }
    return {
      inFlightWhileFirstPending, maxInFlight: server.maxInFlight,
      sent: server.sent.map((s) => s.value), saved: server.value.q,
    };
  },

  async flushSendsTheDebouncedEditAndWaitsForIt() {
    const field = { value: "1" };
    const server = makeServer();
    const queue = createAutosaveQueue({ send: (k) => server.send(k, field), schedule, cancel });
    field.value = "3"; queue.touch("q", false);       // ещё в задержке ввода
    let flushed = null;
    queue.flush().then((ok) => { flushed = ok; });
    await tick();
    const resolvedBeforeServerAnswered = flushed !== null;
    await server.answer(); await tick();
    return {
      resolvedBeforeServerAnswered, flushed, saved: server.value.q,
      timersLeft: timers.filter(Boolean).length,
    };
  },

  async flushWaitsForARequestAlreadyInFlight() {
    const field = { value: "7" };
    const server = makeServer();
    const queue = createAutosaveQueue({ send: (k) => server.send(k, field), schedule, cancel });
    queue.touch("customer", true); await tick();
    let flushed = null;
    queue.flush().then((ok) => { flushed = ok; });
    await tick();
    const before = flushed;
    await server.answer(); await tick();
    return { before, flushed, saved: server.value.customer };
  },

  async aFailedSaveBlocksUntilItSucceeds() {
    const field = { value: "abc" };
    const server = makeServer();
    const reply = (body) => body.value !== "abc";
    const queue = createAutosaveQueue({
      send: (k) => server.send(k, field, reply), schedule, cancel,
    });
    queue.touch("q", true); await tick(); await server.answer(); await tick();
    let first = null;
    queue.flush().then((ok) => { first = ok; });
    await tick(); await server.answer(); await tick();     // повтор: снова отказ
    field.value = "3";
    let second = null;
    queue.flush().then((ok) => { second = ok; });
    await tick(); await server.answer(); await tick();     // повтор с исправленным значением
    return { first, second, saved: server.value.q, sends: server.sent.length };
  },

  async aNetworkErrorBlocksToo() {
    let calls = 0;
    const send = () => {
      calls += 1;
      return calls === 1 ? Promise.reject(new Error("offline")) : Promise.resolve({ ok: true });
    };
    const queue = createAutosaveQueue({ send, schedule, cancel });
    queue.touch("q", true);
    await tick(); await tick();
    const pendingAfterError = queue.pending();
    const flushed = await queue.flush();
    return { pendingAfterError, flushed, calls };
  },

  async nothingToSaveFlushesAtOnce() {
    const queue = createAutosaveQueue({
      send: () => Promise.resolve({ ok: true }), schedule, cancel,
    });
    return { flushed: await queue.flush(), pending: queue.pending() };
  },
};

scenarios[process.argv[2]]().then((result) => process.stdout.write(JSON.stringify(result)));
"""


def _run(scenario):
    node = shutil.which("node")
    if node is None:  # pragma: no cover - зависит от машины
        pytest.skip("node не установлен: очередь автосохранения проверяется отдельно")
    result = subprocess.run(
        [node, "-e", HARNESS, str(JS_PATH), scenario],
        capture_output=True, text=True, timeout=30, check=True,
    )
    return json.loads(result.stdout)


def test_the_module_exposes_the_pure_queue_to_tests():
    text = JS_PATH.read_text(encoding="utf-8")
    assert "module.exports" in text
    assert "keepalive: true" in text
    assert "—" not in text


def test_debounced_typing_sends_one_request_with_the_last_value():
    assert _run("debouncedTypingSavesTheLastValue") == {"sent": ["123"], "saved": "123"}


def test_requests_never_overlap_so_an_older_answer_cannot_win():
    result = _run("requestsNeverOverlapAndTheNewestWins")
    assert result["inFlightWhileFirstPending"] == 1
    assert result["maxInFlight"] == 1
    assert result["sent"] == ["1", "123"]  # промежуточное 12 слито в последнее значение
    assert result["saved"] == "123"


def test_completion_flush_sends_the_debounced_edit_and_waits_for_the_server():
    result = _run("flushSendsTheDebouncedEditAndWaitsForIt")
    assert result == {
        "resolvedBeforeServerAnswered": False, "flushed": True, "saved": "3", "timersLeft": 0,
    }


def test_completion_flush_waits_for_a_request_already_in_flight():
    assert _run("flushWaitsForARequestAlreadyInFlight") == {
        "before": None, "flushed": True, "saved": "7",
    }


def test_a_rejected_value_blocks_completion_until_it_is_corrected():
    result = _run("aFailedSaveBlocksUntilItSucceeds")
    assert result["first"] is False
    assert result["second"] is True
    assert result["saved"] == "3"


def test_a_network_failure_blocks_completion_until_the_retry_succeeds():
    result = _run("aNetworkErrorBlocksToo")
    assert result == {"pendingAfterError": False, "flushed": True, "calls": 2}


def test_flush_with_nothing_pending_does_not_wait():
    assert _run("nothingToSaveFlushesAtOnce") == {"flushed": True, "pending": False}


def test_the_completion_resubmit_runs_in_a_new_task():
    """Проверено в Chromium: если сохранять было нечего, flush завершается ещё
    внутри исходного события submit, и requestSubmit там молча игнорируется
    (флаг «firing submission events»). Повторная отправка идёт через setTimeout,
    а защиту от двойной отправки (app_shell.js) опережает перехват на document."""
    text = JS_PATH.read_text(encoding="utf-8")
    resubmit = text.split("queue.flush().then(function (ok) {\n      delete form.dataset")[1]
    assert resubmit.index("setTimeout(") < resubmit.index("requestSubmit(")
    assert 'document.addEventListener("submit", function (event) {' in text
    assert "}, true);" in text  # фаза перехвата
