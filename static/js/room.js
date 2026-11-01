(() => {
  for (const form of document.querySelectorAll('.missing-recording-form')) {
    const reason = form.querySelector('[name="reason"]');
    const explanation = form.querySelector('[data-other-explanation]');
    const note = form.querySelector('[name="note"]');
    const updateExplanation = () => {
      const other = reason.value === 'other';
      explanation.hidden = !other;
      note.required = other;
      note.disabled = !other;
    };
    reason.addEventListener('change', updateExplanation);
    updateExplanation();
  }
  const openMissingRecording = () => {
    const target = document.getElementById(window.location.hash.slice(1));
    if (target?.matches('.missing-recording-details')) target.open = true;
  };
  window.addEventListener('hashchange', openMissingRecording);
  openMissingRecording();

  const chat = document.getElementById('chat');
  if (!chat) return;
  const list = document.getElementById('chat-messages');
  const form = document.getElementById('chat-form');
  const older = document.getElementById('chat-older');
  const status = document.getElementById('chat-status');
  const empty = document.getElementById('chat-empty');
  const seen = new Set([...list.children].map(item => Number(item.dataset.messageId)));
  const deleted = new Set();
  let deletionCursor = 0;
  let last = Math.max(0, ...seen);
  let oldest = Math.min(Infinity, ...seen);
  let stopped = false;
  let pending = null;
  let sendError = '';
  list.scrollTop = list.scrollHeight;

  async function fetchMessages(url, options, action = 'load messages') {
    const response = await fetch(url, options);
    const data = await response.json().catch(() => null);
    if ([401, 403].includes(response.status) || response.redirected) {
      stopped = true;
      if (form) form.querySelector('button').disabled = true;
      throw new Error(data?.error || 'Chat access has ended. Reload the page or log in again.');
    }
    if (!response.ok) {
      throw new Error(data?.error || `Could not ${action} (error ${response.status}). Please try again.`);
    }
    if (!data || !Array.isArray(data.messages)) throw new Error('Could not read messages. Reload the page and try again.');
    return data;
  }

  function applyDeletions(data) {
    for (const id of data.deleted_ids || []) {
      deleted.add(id);
      seen.delete(id);
      list.querySelector(`[data-message-id="${id}"]`)?.remove();
    }
    deletionCursor = Math.max(deletionCursor, data.deletion_cursor || 0);
    empty.hidden = seen.size > 0;
  }

  function add(messages, prepend = false) {
    const nearBottom = list.scrollHeight - list.scrollTop - list.clientHeight < 80;
    const oldHeight = list.scrollHeight;
    const fragment = document.createDocumentFragment();
    for (const message of messages) {
      last = Math.max(last, message.id);
      oldest = Math.min(oldest, message.id);
      if (seen.has(message.id) || deleted.has(message.id)) continue;
      seen.add(message.id);
      const item = document.createElement('li');
      item.dataset.messageId = message.id;
      item.className = 'chat-message' + (String(message.sender_id) === chat.dataset.userId ? ' chat-own' : '');
      const meta = document.createElement('span');
      meta.className = 'chat-meta';
      meta.textContent = `${message.sender} · ${message.time}`;
      const body = document.createElement('p');
      body.textContent = message.body;
      item.append(meta, body);
      if (chat.dataset.canDelete === 'true') {
        const button = document.createElement('button');
        button.type = 'button';
        button.className = 'room-link-button chat-delete';
        button.dataset.deleteMessage = message.id;
        button.setAttribute('aria-label', `Delete message from ${message.sender}`);
        button.textContent = 'Delete';
        item.append(button);
      }
      fragment.append(item);
    }
    if (prepend) {
      list.prepend(fragment);
      list.scrollTop += list.scrollHeight - oldHeight;
    } else {
      list.append(fragment);
      if (nearBottom) list.scrollTop = list.scrollHeight;
    }
    empty.hidden = seen.size > 0;
  }

  function poll() {
    if (pending || stopped) return pending;
    pending = fetchMessages(`${chat.dataset.messagesUrl}?after=${last}&deletions_after=${deletionCursor}`)
      .then(data => { applyDeletions(data); add(data.messages); status.textContent = sendError; })
      .catch(error => { status.textContent = error.message; })
      .finally(() => { pending = null; });
    return pending;
  }

  older.addEventListener('click', async () => {
    older.disabled = true;
    try {
      const data = await fetchMessages(`${chat.dataset.messagesUrl}?before=${oldest}&deletions_after=${deletionCursor}`);
      applyDeletions(data);
      add(data.messages, true);
      older.hidden = data.messages.length < 50;
    } catch (error) { status.textContent = error.message; }
    finally { older.disabled = false; }
  });

  if (form) form.addEventListener('submit', async event => {
    event.preventDefault();
    const button = form.querySelector('button');
    if (button.disabled || stopped) return;
    button.disabled = true;
    try {
      await fetchMessages(form.action, { method: 'POST', body: new URLSearchParams(new FormData(form)) }, 'send your message');
      sendError = '';
      status.textContent = '';
      document.getElementById('chat-message').value = '';
      await poll();
      list.scrollTop = list.scrollHeight;
    } catch (error) { sendError = error.message; status.textContent = sendError; }
    finally { button.disabled = stopped; }
  });
  if (chat.dataset.canDelete === 'true') list.addEventListener('click', async event => {
    const button = event.target.closest('[data-delete-message]');
    if (!button || !list.contains(button) || button.disabled || stopped) return;
    if (!window.confirm('Delete this message for everyone? This cannot be undone.')) return;
    button.disabled = true;
    try {
      const data = await fetchMessages(`${chat.dataset.deleteMessagesUrl}/${button.dataset.deleteMessage}/delete`, {
        method: 'POST', headers: { 'X-CSRF-Token': chat.dataset.csrf },
      }, 'delete this message');
      applyDeletions(data);
      status.textContent = 'Message deleted.';
    } catch (error) { status.textContent = error.message; }
    finally { button.disabled = stopped; }
  });
  setInterval(() => { if (!document.hidden) poll(); }, 5000);
  document.addEventListener('visibilitychange', () => { if (!document.hidden) poll(); });
  poll();
})();
