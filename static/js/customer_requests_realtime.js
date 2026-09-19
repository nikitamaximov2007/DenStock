(function () {
  const root = document.querySelector('[data-workspace-realtime]');
  if (!root || !window.fetch) return;
  let cursor = Number(root.dataset.cursor || 0);
  let audioReady = false;
  let sounds = localStorage.getItem('denstock-request-sounds') !== 'off';
  const control = document.querySelector('[data-sound-control]');
  const updateControl = () => { if (control) control.textContent = sounds ? '🔊 Звуки: вкл.' : '🔇 Звуки: выкл.'; };
  updateControl();
  document.addEventListener('pointerdown', () => { audioReady = true; }, { once: true });
  if (control) control.addEventListener('click', () => { sounds = !sounds; localStorage.setItem('denstock-request-sounds', sounds ? 'on' : 'off'); updateControl(); });
  const composer = document.querySelector('[data-reply-form] textarea[name="text"]');
  const replyForm = document.querySelector('[data-reply-form]');
  if (composer && replyForm) {
    composer.addEventListener('keydown', (event) => {
      if (event.key !== 'Enter' || event.shiftKey || event.isComposing) return;
      event.preventDefault();
      if (composer.value.trim() && !replyForm.dataset.submitting) {
        replyForm.dataset.submitting = '1';
        replyForm.requestSubmit();
      }
    });
  }
  function beep(kind) {
    if (!sounds || !audioReady) return;
    try {
      const ctx = new (window.AudioContext || window.webkitAudioContext)();
      const osc = ctx.createOscillator(); const gain = ctx.createGain();
      osc.frequency.value = kind === 'request' ? 740 : 520;
      gain.gain.setValueAtTime(0.035, ctx.currentTime); gain.gain.exponentialRampToValueAtTime(0.001, ctx.currentTime + 0.16);
      osc.connect(gain).connect(ctx.destination); osc.start(); osc.stop(ctx.currentTime + 0.16);
    } catch (_) { /* autoplay or audio device unavailable */ }
  }
  async function poll() {
    try {
      const response = await fetch(root.dataset.endpoint + '?after=' + encodeURIComponent(cursor), { credentials: 'same-origin', headers: { Accept: 'application/json' } });
      if (!response.ok) return;
      const body = await response.json(); const events = body.events || [];
      if (!events.length) return;
      cursor = Number(body.cursor || cursor);
      let refresh = false;
      events.forEach((event) => {
        if (event.type === 'request_created') beep('request');
        if (event.type === 'customer_message_created') beep('message');
        if (event.type === 'request_deleted' && event.payload && String(event.payload.request_id) === root.dataset.requestId) {
          const notice = document.createElement('div'); notice.className = 'messages';
          notice.innerHTML = 'Заявка была удалена. <a href="/customer-requests/">Вернуться к заявкам</a>';
          document.querySelector('#content').prepend(notice); refresh = false;
        } else refresh = true;
      });
      if (refresh) window.location.reload();
    } catch (_) { /* transient network failure; next poll catches up from cursor */ }
  }
  setInterval(poll, document.hidden ? 4000 : 1500);
  document.addEventListener('visibilitychange', () => { if (!document.hidden) poll(); });
})();
