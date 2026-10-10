(() => {
  const sleep = milliseconds => new Promise(resolve => setTimeout(resolve, milliseconds));
  async function api(url, {csrf, data, signal} = {}) {
    const options = {credentials: 'same-origin', cache: 'no-store', signal};
    if (data !== undefined) {
      options.method = 'POST';
      options.headers = {'Content-Type': 'application/json', 'X-CSRF-Token': csrf};
      options.body = JSON.stringify(data);
    }
    const response = await fetch(url, options);
    const body = await response.json().catch(() => ({}));
    if (!response.ok || response.redirected) {
      const error = new Error(body.error || 'Please reload the page, check your login, and try again.');
      error.status = response.status;
      throw error;
    }
    return body;
  }

  function uploadChunk(url, blob, signal, progress) {
    return new Promise((resolve, reject) => {
      const xhr = new XMLHttpRequest();
      const cancel = () => xhr.abort();
      xhr.open('PUT', url);
      xhr.timeout = 900000;
      // No app cookies, CSRF token or credentials are sent to storage.
      xhr.upload.onprogress = event => { if (event.lengthComputable) progress(event.loaded); };
      xhr.onload = () => {
        if (xhr.status >= 200 && xhr.status < 300 && xhr.getResponseHeader('ETag')) resolve();
        else reject(new Error('A chunk could not be uploaded. Check your connection and try again.'));
      };
      xhr.onerror = xhr.ontimeout = () => reject(new Error('The connection was interrupted.'));
      xhr.onabort = () => reject(new DOMException('Upload cancelled.', 'AbortError'));
      xhr.onloadend = () => signal.removeEventListener('abort', cancel);
      signal.addEventListener('abort', cancel, {once: true});
      if (signal.aborted) { reject(new DOMException('Upload cancelled.', 'AbortError')); return; }
      xhr.send(blob);
    });
  }

  for (const form of document.querySelectorAll('.recording-upload')) {
    const fileInput = form.querySelector('[type="file"]');
    const consent = form.querySelector('[name="confirmed"]');
    const button = form.querySelector('[type="submit"]');
    const cancel = form.querySelector('.upload-cancel');
    const status = form.querySelector('.upload-status');
    const progress = form.querySelector('progress');
    const watch = form.querySelector('.upload-watch');
    const csrf = form.querySelector('[name="_csrf_token"]').value;
    let busy = false;
    let saved = false;
    let controller;
    const updateButton = () => { button.disabled = busy || saved || !consent.checked || !fileInput.files.length; };
    consent.addEventListener('change', updateButton);
    fileInput.addEventListener('change', updateButton);
    cancel.addEventListener('click', () => controller?.abort());
    updateButton();
    form.addEventListener('submit', async event => {
      event.preventDefault();
      if (busy || saved || !form.reportValidity() || !consent.checked) return;
      const file = fileInput.files[0];
      if (!file || file.size <= 0 || file.size > 2 * 1024 ** 3) {
        status.textContent = 'Choose a recording up to 2 GB.'; return;
      }
      const extension = file.name.split('.').pop().toLowerCase();
      const contentType = {mp4: 'video/mp4', webm: 'video/webm', mov: 'video/quicktime'}[extension];
      if (!contentType) { status.textContent = 'Choose an MP4, WebM or MOV video.'; return; }
      if (form.dataset.recordingVersion && !window.confirm('Replace this session’s recording? This updates the existing attendance entry. Retained safeguarding versions will remain saved.')) return;
      busy = true;
      controller = new AbortController();
      updateButton();
      consent.disabled = fileInput.disabled = true;
      cancel.hidden = false;
      progress.hidden = false;
      progress.value = 0;
      status.textContent = 'Preparing upload…';
      let upload;
      try {
        // Keep start uncancelled so an issued upload can always be aborted.
        upload = await api(form.action, {csrf, data: {filename: file.name, content_type: contentType,
          file_size: file.size, confirmed: true, replace: Boolean(form.dataset.recordingVersion),
          recording_version: form.dataset.recordingVersion}});
        const chunks = Math.ceil(file.size / upload.part_size);
        const loaded = Array(chunks).fill(0);
        let next = 0;
        let failure;
        const signal = controller.signal;
        const report = (index, bytes) => {
          loaded[index] = bytes;
          progress.value = loaded.reduce((sum, value) => sum + value, 0) / file.size * 100;
          status.textContent = `Uploading: ${Math.floor(progress.value)}%`;
        };
        const worker = async () => {
          while (!failure && next < chunks) {
            const index = next++;
            const blob = file.slice(index * upload.part_size, (index + 1) * upload.part_size);
            for (let attempt = 0; attempt < 4; attempt++) {
              try {
                const part = await api(upload.part_url, {csrf, signal, data: {part_number: index + 1}});
                await uploadChunk(part.url, blob, signal, bytes => report(index, bytes));
                report(index, blob.size);
                break;
              } catch (error) {
                if (signal.aborted || [400, 401, 403, 409, 410].includes(error.status) || attempt === 3) {
                  failure = error; controller.abort(); throw error;
                }
                report(index, 0);
                status.textContent = 'Connection interrupted. Retrying a chunk…';
                await sleep(1000 * 2 ** attempt);
              }
            }
          }
        };
        await Promise.all(Array.from({length: Math.min(3, chunks)}, worker));
        if (signal.aborted) throw new DOMException('Upload cancelled.', 'AbortError');
        cancel.hidden = true;
        status.textContent = 'Checking the video and saving attendance…';
        let result;
        for (let attempt = 0; attempt < 3; attempt++) {
          try { result = await api(upload.complete_url, {csrf, data: {}}); break; }
          catch (error) {
            if (attempt === 2 || [400, 401, 403, 409, 410].includes(error.status)) throw error;
            await sleep(1500 * 2 ** attempt);
          }
        }
        saved = true;
        progress.value = 100;
        status.textContent = result.message;
        if (result.watch_url) { watch.href = result.watch_url; watch.hidden = false; }
      } catch (error) {
        controller.abort();
        if (upload) await api(upload.abort_url, {csrf, data: {}}).catch(() => {});
        status.textContent = error.name === 'AbortError' ? 'Upload cancelled. You can start again.' : error.message;
        progress.hidden = true;
      } finally {
        busy = false;
        cancel.hidden = true;
        consent.disabled = fileInput.disabled = saved;
        updateButton();
      }
    });
  }

  for (const video of document.querySelectorAll('.private-recording-video')) {
    const panel = video.closest('.lesson-card');
    const status = panel.querySelector('.video-status');
    const retry = panel.querySelector('.video-retry');
    let pending = false;
    let timer;
    let automaticRetry = true;
    async function loadVideo(keepPosition = true) {
      if (pending) return;
      pending = true;
      clearTimeout(timer);
      const position = keepPosition ? video.currentTime : 0;
      const playing = !video.paused;
      try {
        const data = await api(video.dataset.playbackUrl);
        retry.hidden = true;
        status.textContent = '';
        video.addEventListener('loadedmetadata', () => {
          if (position) video.currentTime = position;
          if (playing) video.play().catch(() => {});
        }, {once: true});
        video.src = data.url;
        timer = setTimeout(() => loadVideo(), Math.max(5, data.expires_in - 60) * 1000);
      } catch (error) {
        video.removeAttribute('src');
        video.load();
        status.textContent = error.message;
        retry.hidden = [401, 403, 410].includes(error.status);
      } finally { pending = false; }
    }
    video.addEventListener('error', () => {
      if (pending) return;
      if (automaticRetry) { automaticRetry = false; loadVideo(); return; }
      status.textContent = 'The video could not be played. Try reloading it or contact a coordinator.';
      retry.hidden = false;
    });
    retry.addEventListener('click', () => { automaticRetry = true; loadVideo(); });
    loadVideo(false);
  }
})();
