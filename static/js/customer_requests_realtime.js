(function () {
  const root = document.querySelector('[data-workspace-realtime]');
  if (!window.fetch) return;
  let cursor = Number(root.dataset.cursor || 0);
  let composing = false;
  let audioReady = false;
  let sounds = localStorage.getItem('denstock-request-sounds') !== 'off';
  const control = document.querySelector('[data-sound-control]');
  const updateControl = () => { if (control) control.textContent = sounds ? '🔊 Звуки: вкл.' : '🔇 Звуки: выкл.'; };
  updateControl();
  document.addEventListener('pointerdown', () => { audioReady = true; }, { once: true });
  document.addEventListener('compositionstart', () => { composing = true; }, true);
  document.addEventListener('compositionend', () => { composing = false; }, true);
  if (control) control.addEventListener('click', () => { sounds = !sounds; localStorage.setItem('denstock-request-sounds', sounds ? 'on' : 'off'); updateControl(); });
  const submitComposer = (event, composer) => {
    if (!(composer instanceof HTMLTextAreaElement) || !composer.matches('[name="text"]')) return;
    const replyForm = composer.closest('[data-reply-form]');
    if (!replyForm || !composer.value.trim() || replyForm.dataset.submitting) return;
    event.preventDefault();
    replyForm.dataset.submitting = '1';
    replyForm.requestSubmit();
  };
  const submitOnEnter = (event) => {
    const isEnter = event.key === 'Enter' || event.key === 'Return' || event.code === 'Enter' || event.code === 'NumpadEnter' || event.keyCode === 13 || event.which === 13;
    if (!isEnter || event.shiftKey || composing || event.isComposing || event.keyCode === 229) return;
    submitComposer(event, event.target);
  };
  const submitBeforeLineBreak = (event) => {
    if (event.inputType !== 'insertLineBreak' || event.shiftKey || composing || event.isComposing) return;
    submitComposer(event, event.target);
  };
  // Delegation keeps the shortcut working after partial navigation replaces
  // the detail form without re-running this bootstrap script.
  document.addEventListener('keydown', submitOnEnter, true);
  document.addEventListener('keypress', submitOnEnter, true);
  document.addEventListener('beforeinput', submitBeforeLineBreak, true);
  if (!root) return;
  const composer = document.querySelector('[data-reply-form] textarea[name="text"]');
  const fileInput = document.querySelector('[data-attachment-input]');
  const fileName = document.querySelector('[data-attachment-name]');
  const removeFile = document.querySelector('[data-attachment-remove]');
  const setFile = (file) => {
    if (!file || !fileInput) return;
    const transfer = new DataTransfer(); transfer.items.add(file); fileInput.files = transfer.files;
    if (fileName) fileName.textContent = `${file.name} (${Math.ceil(file.size / 1024)} КБ)`;
    if (removeFile) removeFile.hidden = false;
  };
  if (fileInput) fileInput.addEventListener('change', () => setFile(fileInput.files[0]));
  if (removeFile) removeFile.addEventListener('click', () => { fileInput.value = ''; fileName.textContent = ''; removeFile.hidden = true; });
  if (composer) composer.addEventListener('paste', (event) => {
    const image = Array.from(event.clipboardData.files || []).find((file) => file.type.startsWith('image/'));
    if (image) { event.preventDefault(); setFile(image); }
  });
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
