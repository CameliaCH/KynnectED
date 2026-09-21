(() => {
  for (const form of document.querySelectorAll('.recording-upload')) {
    form.addEventListener('submit', event => {
      event.preventDefault();
      const button = form.querySelector('button');
      if (button.disabled) return;
      const progress = form.querySelector('progress');
      const status = form.querySelector('.upload-status');
      const request = new XMLHttpRequest();
      request.open('POST', form.action);
      request.setRequestHeader('X-CSRF-Token', form.querySelector('[name="_csrf_token"]').value);
      request.setRequestHeader('X-Requested-With', 'XMLHttpRequest');
      button.disabled = true;
      progress.hidden = false;
      status.textContent = 'Uploading recording…';
      request.upload.addEventListener('progress', event => {
        if (event.lengthComputable) {
          progress.value = event.loaded / event.total * 100;
          status.textContent = progress.value === 100 ? 'Checking the recording and updating attendance…' : `Uploading: ${Math.round(progress.value)}%`;
        }
      });
      const failed = message => {
        button.disabled = false;
        progress.hidden = true;
        status.textContent = message;
      };
      request.addEventListener('load', () => {
        if (request.status >= 200 && request.status < 300 && !request.responseURL.includes('/login')) {
          // Changing only the anchor does not load the newly saved recording.
          window.location.hash = new URL(form.dataset.returnUrl, window.location.href).hash;
          window.location.reload();
        } else {
          const page = new DOMParser().parseFromString(request.responseText, 'text/html');
          failed(page.querySelector('.form-error')?.textContent || 'Upload failed. Please reload the page, check your login, and try again.');
        }
      });
      request.addEventListener('error', () => failed('The upload did not finish. Check your connection and try again.'));
      request.send(new FormData(form));
    });
  }

  const chat = document.getElementById('chat');
  if (!chat) return;
  const list = document.getElementById('chat-messages');
  const form = document.getElementById('chat-form');
  const older = document.getElementById('chat-older');
  const status = document.getElementById('chat-status');
  const empty = document.getElementById('chat-empty');
  const seen = new Set([...list.children].map(item => Number(item.dataset.messageId)));
  let last = Math.max(0, ...seen);
  let stopped = false;
  let pending = null;
  list.scrollTop = list.scrollHeight;

  async function fetchMessages(url, options) {
    const response = await fetch(url, options);
    if ([401, 403].includes(response.status) || response.redirected) {
      stopped = true;
      if (form) form.querySelector('button').disabled = true;
      throw new Error('Chat access has ended. Reload the page or log in again.');
    }
    if (!response.ok) throw new Error('Could not load or send messages. Please try again.');
    return response.json();
  }

  function add(messages, prepend = false) {
    const nearBottom = list.scrollHeight - list.scrollTop - list.clientHeight < 80;
    const oldHeight = list.scrollHeight;
    const fragment = document.createDocumentFragment();
    for (const message of messages) {
      if (seen.has(message.id)) continue;
      seen.add(message.id);
      last = Math.max(last, message.id);
      const item = document.createElement('li');
      item.dataset.messageId = message.id;
      item.className = 'chat-message' + (String(message.sender_id) === chat.dataset.userId ? ' chat-own' : '');
      const meta = document.createElement('span');
      meta.className = 'chat-meta';
      meta.textContent = `${message.sender} · ${message.time}`;
      const body = document.createElement('p');
      body.textContent = message.body;
      item.append(meta, body);
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
    pending = fetchMessages(`${chat.dataset.messagesUrl}?after=${last}`)
      .then(data => { add(data.messages); status.textContent = ''; })
      .catch(error => { status.textContent = error.message; })
      .finally(() => { pending = null; });
    return pending;
  }

  older.addEventListener('click', async () => {
    older.disabled = true;
    try {
      const first = Math.min(...seen);
      const data = await fetchMessages(`${chat.dataset.messagesUrl}?before=${first}`);
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
      await fetchMessages(form.action, { method: 'POST', body: new URLSearchParams(new FormData(form)) });
      document.getElementById('chat-message').value = '';
      await poll();
      list.scrollTop = list.scrollHeight;
    } catch (error) { status.textContent = error.message; }
    finally { button.disabled = stopped; }
  });
  setInterval(() => { if (!document.hidden) poll(); }, 5000);
  document.addEventListener('visibilitychange', () => { if (!document.hidden) poll(); });
  poll();
})();
