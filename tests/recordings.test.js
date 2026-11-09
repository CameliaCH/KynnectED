// Run with node or macOS JavaScriptCore's jsc from the repository root.
const read = path => typeof readFile === 'function' ? readFile(path) : require('fs').readFileSync(path, 'utf8');
const source = read('static/js/recordings.js');
const log = typeof print === 'function' ? print : console.log;
function assert(value, message) { if (!value) throw new Error(message); }
class Element {
  constructor() { this.handlers = {}; this.children = []; this.hidden = false; this.value = ''; }
  addEventListener(type, handler) { this.handlers[type] = handler; }
  append(...items) { this.children.push(...items); }
  replaceChildren(...items) { this.children = items; }
  setAttribute(name, value) { this[name] = value; }
  set value(value) { this._value = value; if (value === '' && this.files) this.files = []; }
  get value() { return this._value; }
  closest() { return this; }
}
class Controller {
  constructor() { this.signal = {aborted: false, addEventListener() {}, removeEventListener() {}}; }
  abort() { this.signal.aborted = true; }
}
class Exception extends Error {
  constructor(message, name) { super(message); this.name = name; }
}
const file = (name, size = 20) => ({name, size, slice: (start, end) => ({size: Math.min(size, end) - start})});
function browser(files, version = '', remaining = '') {
  const selectors = ['[type="file"]', '[name="confirmed"]', '[type="submit"]', '.upload-cancel',
    '.upload-status', 'progress', '.upload-watch', '.upload-selection', '.upload-file-list', '[name="_csrf_token"]'];
  const elements = Object.fromEntries(selectors.map(selector => [selector, new Element()]));
  const form = new Element();
  form.action = '/start';
  form.dataset = {recordingVersion: version, remainingFiles: String(remaining)};
  form.reportValidity = () => !input.required || input.files.length > 0;
  form.querySelector = selector => elements[selector];
  const input = elements['[type="file"]'];
  input.files = files;
  elements['[name="confirmed"]'].checked = true;
  elements['[name="_csrf_token"]'].value = 'csrf';
  const state = {requests: [], starts: [], completes: [], next: 0, confirmations: 0, fail: '', cancelAt: 0,
    savedSeconds: version ? 1800 : 0, savedCount: version ? 1 : 0, batchSize: version ? Number(remaining) + 1 : null};
  const counters = Object.fromEntries(['hours', 'completed', 'upcoming', 'pairHours', 'pairCompleted', 'pairDuration', 'duration', 'otherPairHours']
    .map(name => [name, new Element()]));
  counters.otherPairHours.textContent = '7.00';
  const userPanel = {querySelectorAll: selector => ({
    '[data-learning-hours]': [counters.hours], '[data-learning-completed]': [counters.completed],
    '[data-learning-upcoming]': [counters.upcoming]
  }[selector] || [])};
  const pairPanel = {querySelectorAll: selector => ({
    '[data-learning-hours]': [counters.pairHours], '[data-learning-completed]': [counters.pairCompleted],
    '[data-learning-duration]': [counters.pairDuration]
  }[selector] || [])};
  const document = {querySelectorAll: selector => ({
    '.recording-upload': [form], '[data-learning-user="1"]': [userPanel],
    '[data-learning-pair="1-3"]': [pairPanel], '[data-lesson-duration="lesson"]': [counters.duration],
    '[data-learning-pair="1-4"]': [{querySelectorAll: () => [counters.otherPairHours]}]
  }[selector] || []), createElement: () => new Element()};
  const fetch = async (url, options) => {
    if (options.signal?.aborted) throw new Exception('Cancelled', 'AbortError');
    const data = JSON.parse(options.body);
    state.requests.push({url, data});
    let body = {};
    let status = 200;
    if (url === '/start') {
      state.starts.push(data);
      if (data.file_count !== undefined) state.batchSize = data.file_count;
      const id = String(++state.next);
      body = {upload_id: id, part_size: 8, part_url: `/part/${id}`, complete_url: `/complete/${id}`, abort_url: `/abort/${id}`};
      if (state.cancelAt === state.next) elements['.upload-cancel'].handlers.click();
    } else if (url.startsWith('/part/')) body = {url: 'https://storage.invalid/part'};
    else if (url.startsWith('/complete/')) {
      if (state.fail === url) { status = 400; body = {error: 'Invalid video'}; }
      else {
        state.completes.push(url);
        const id = Number(url.split('/').pop());
        state.savedSeconds += 1800;
        state.savedCount++;
        body = {watch_url: `/watch/${id}`, lesson_saved: state.savedCount === state.batchSize,
          remaining_files: state.batchSize - state.savedCount, progress: {
          user_id: 1, hours: (3 + state.savedSeconds / 3600).toFixed(2), completed: 2, upcoming: 0,
          pair_key: '1-3', pair_hours: (state.savedSeconds / 3600).toFixed(2), pair_completed: 1,
          pair_duration: `${state.savedSeconds / 60} min`,
          lesson_id: 'lesson', lesson_duration: `${state.savedSeconds / 60} min`
        }};
      }
    }
    return {ok: status === 200, status, json: async () => body};
  };
  class XHR {
    constructor() { this.upload = {}; this.status = 200; }
    open() {}
    getResponseHeader() { return 'etag'; }
    send(blob) { this.upload.onprogress({lengthComputable: true, loaded: blob.size}); this.onload(); this.onloadend(); }
  }
  new Function('document', 'window', 'fetch', 'XMLHttpRequest', 'AbortController', 'DOMException', 'setTimeout', source)(
    document, {confirm: () => { state.confirmations++; return true; }}, fetch, XHR, Controller, Exception, fn => fn());
  return {state, elements, form, input, counters, submit: () => form.handlers.submit({preventDefault() {}})};
}
(async () => {
  const accumulated = browser([]);
  const choose = files => { accumulated.input.files = files; accumulated.input.handlers.change(); };
  choose([file('first.mp4')]);
  choose([file('second.mov')]);
  choose([]);
  choose([file('first.mp4'), file('third.webm')]);
  assert(accumulated.elements['.upload-file-list'].children.length === 3,
    'Repeated picker choices accumulate, cancelling keeps the selection, and duplicates are skipped');
  assert(accumulated.elements['.upload-selection'].textContent === '3 videos selected · 0 saved.',
    'The count includes videos from every picker choice');
  accumulated.elements['.upload-file-list'].children[1].children[1].handlers.click();
  assert(accumulated.elements['.upload-file-list'].children.length === 2, 'Videos can be removed before uploading');
  choose([file('second.mov')]);
  await accumulated.submit();
  assert(accumulated.state.starts.map(upload => upload.filename).join(',') === 'first.mp4,third.webm,second.mov',
    'Upload uses the full accumulated list in order, including re-added files');
  assert(accumulated.state.starts[0].file_count === 3 && accumulated.state.completes.length === 3,
    'All accumulated videos belong to one batch before the lesson locks');

  const removed = browser([file('remove-me.mp4')]);
  removed.elements['.upload-file-list'].children[0].children[1].handlers.click();
  await removed.submit();
  assert(removed.elements['[type="submit"]'].disabled && removed.state.starts.length === 0,
    'Removing every video prevents an empty upload');

  const batch = browser([file('one.mp4'), file('two.webm'), file('three.mov')]);
  assert(batch.elements['.upload-selection'].textContent === '3 videos selected · 0 saved.', 'All selected videos are counted before uploading');
  await batch.submit();
  assert(batch.state.starts.length === 3 && batch.state.completes.length === 3, 'Every selected video uploads');
  assert(batch.state.starts[0].file_count === 3 && batch.state.starts.slice(1).every(upload => upload.file_count === undefined), 'The first request declares the whole batch and later files preserve its size');
  assert(batch.state.starts.map(upload => upload.recording_version).join(',') === ',1,2', 'Each file uses the latest saved version');
  assert(batch.elements['.upload-watch'].children.length === 3, 'Every saved video has a watch link');
  assert(batch.elements['.upload-selection'].textContent === '3 videos selected · 3 saved.', 'Every completed video is counted');
  assert(batch.counters.pairDuration.textContent === '90 min', 'Pairing duration updates alongside learning hours');
  assert(batch.counters.hours.textContent === '4.50' && batch.counters.pairHours.textContent === '1.50',
    'Personal and pairing hours update from saved server totals after uploading');
  assert(batch.counters.completed.textContent === '2' && batch.counters.pairCompleted.textContent === '1',
    'Attendance counters update without counting each video as a separate session');
  assert(batch.counters.upcoming.textContent === '0' && batch.counters.duration.textContent === '90 min',
    'Upcoming count and lesson duration update after saving');
  assert(batch.counters.otherPairHours.textContent === '7.00', 'Uploading never overwrites totals for another pairing');
  await batch.submit();
  assert(batch.state.starts.length === 3, 'Submitting again never uploads saved files twice');
  batch.input.files = [file('four.mp4'), file('five.mp4')];
  batch.input.handlers.change();
  await batch.submit();
  assert(batch.state.starts.length === 3, 'A saved lesson rejects further uploads even if the file selection is changed');
  assert(batch.elements['[type="submit"]'].disabled && batch.input.disabled, 'The completed form is locked');
  assert(batch.elements['.upload-status'].textContent.includes('Lesson saved.'), 'The saved lesson is clearly confirmed');

  const invalid = browser([file('valid.mp4'), file('too-large.mp4', 2 * 1024 ** 3 + 1)]);
  await invalid.submit();
  assert(invalid.state.starts.length === 0, 'All file sizes are checked before starting the batch');
  const invalidType = browser([file('valid.mp4'), file('invalid.txt')]);
  await invalidType.submit();
  assert(invalidType.state.starts.length === 0, 'All file types are checked before starting the batch');

  const partial = browser([file('one.mp4'), file('two.mp4'), file('three.mp4')]);
  partial.state.fail = '/complete/2';
  await partial.submit();
  assert(partial.state.starts.length === 2 && partial.state.completes.length === 1, 'Failure stops the queue and preserves saved files');
  assert(partial.elements['.upload-status'].textContent.includes('1 of 3'), 'Failure reports how many videos were saved');
  assert(partial.state.requests.some(request => request.url === '/abort/2'), 'Failed upload is cancelled');
  assert(partial.counters.hours.textContent === '3.50', 'Saved hours remain visible when a later file fails');
  partial.input.files = [file('extra.mp4')];
  partial.input.handlers.change();
  partial.elements['.upload-file-list'].children[0].children[1].handlers.click();
  assert(partial.elements['.upload-file-list'].children.length === 3 && partial.input.disabled,
    'A partial batch preserves the original selection and saved videos during retries');
  partial.state.fail = '';
  await partial.submit();
  assert(partial.state.starts.map(upload => upload.filename).join(',') === 'one.mp4,two.mp4,two.mp4,three.mp4', 'Retry skips previously saved videos');
  assert(partial.state.completes.length === 3, 'All videos can finish after a partial failure');

  const resumed = browser([file('remaining-two.mp4'), file('remaining-three.mp4')], 'saved-first', 2);
  await resumed.submit();
  assert(resumed.state.completes.length === 2 && resumed.input.disabled, 'A partial batch can finish after reopening the page');
  assert(resumed.state.starts[0].recording_version === 'saved-first', 'Resuming preserves the saved first video');
  const wrongCount = browser([file('only-one.mp4')], 'saved-first', 2);
  await wrongCount.submit();
  assert(wrongCount.state.starts.length === 0, 'A partial batch must retain the original remaining file count');

  const cancelled = browser([file('one.mp4'), file('two.mp4'), file('three.mp4')]);
  cancelled.state.cancelAt = 2;
  await cancelled.submit();
  assert(cancelled.state.completes.length === 1 && cancelled.state.starts.length === 2, 'Cancellation preserves completed videos and stops the queue');
  assert(cancelled.state.requests.some(request => request.url === '/abort/2'), 'Cancellation aborts the active multipart upload');
  cancelled.state.cancelAt = 0;
  await cancelled.submit();
  assert(cancelled.state.completes.length === 3, 'Cancelled batches can retry remaining files');
  log('Recording upload browser tests passed.');
})().catch(error => { log(error.stack); if (typeof process !== 'undefined') process.exitCode = 1; else throw error; });
