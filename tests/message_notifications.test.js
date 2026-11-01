// Run with node, or macOS JavaScriptCore's jsc from the repository root.
const read = path => typeof readFile === 'function' ? readFile(path) : require('fs').readFileSync(path, 'utf8');
const source = read('static/js/message-notifications.js');
const workerSource = read('static/js/message-notifications-worker.js');
const log = typeof print === 'function' ? print : console.log;
function assert(value, message) { if (!value) throw new Error(message); }
async function settle() { for (let i = 0; i < 30; i++) await Promise.resolve(); }

function browser(options = {}) {
  const requests = [];
  const storage = options.storage || new Map();
  const elements = {};
  for (const id of ['message-notifications', 'notification-prompt', 'message-notifications-toggle',
                    'message-notifications-test',
                    'notification-prompt-enable', 'message-notifications-status',
                    'notification-prompt-description', 'notification-prompt-dismiss']) {
    elements[id] = { hidden: id === 'notification-prompt', disabled: false, textContent: '', handlers: {},
      replaceChildren(...children) { this.textContent = children.map(child => typeof child === 'string' ? child : child.textContent).join(''); },
      addEventListener(type, fn) { this.handlers[type] = fn; } };
  }
  elements['message-notifications'].dataset = {
    userId: '3', csrf: 'test-token', configUrl: '/config', statusUrl: '/status',
    promptDescription: 'Enable notifications to receive a notification when you have received a message from your mentor.',
    subscribeUrl: '/subscribe', unsubscribeUrl: '/unsubscribe', workerUrl: '/worker.js',
  };
  const state = { subscriptions: 0, removed: 0, saveFails: false, testAlerts: [], testFails: false };
  const Notification = { permission: options.permission || 'default' };
  const device = { endpoint: 'https://web.push.apple.com/device', options: { applicationServerKey: new Uint8Array([1, 2, 3]).buffer },
    unsubscribe: async () => { state.removed++; return true; },
    toJSON: () => ({ endpoint: device.endpoint, keys: { p256dh: 'test', auth: 'test' } }) };
  const registration = { showNotification: async (title, data) => {
    if (state.testFails) throw new Error('Notification permission denied');
    state.testAlerts.push({ title, ...data });
  }, pushManager: {
    getSubscription: async () => options.existing ? device : null,
    subscribe: async settings => {
      state.subscriptions++;
      assert(settings.userVisibleOnly, 'Subscription must request visible notifications');
      Notification.permission = 'granted';
      return device;
    },
  } };
  const navigator = {
    userAgent: options.ios ? 'iPhone' : 'Desktop', platform: options.ios ? 'iPhone' : 'MacIntel',
    maxTouchPoints: options.ios ? 5 : 0,
    serviceWorker: { register: async () => registration, ready: Promise.resolve(registration) },
  };
  const window = { Notification, PushManager: {}, isSecureContext: true,
    matchMedia: () => ({ matches: !!options.standalone }) };
  const fetch = async (path, request = {}) => {
    requests.push({ path, ...request });
    const data = path === '/config'
      ? { user_id: options.wrongAccount ? 4 : 3, available: options.configured !== false, public_key: 'AQID' }
      : { enabled: path === '/status' ? !!options.enabled : path === '/subscribe' };
    const fails = path === '/subscribe' && state.saveFails;
    return { ok: !fails, status: fails ? 503 : 200, redirected: false,
      json: async () => fails ? { error: 'Could not save. Try again.' } : data };
  };
  const sessionStorage = { getItem: key => storage.get(key) || null, setItem: (key, value) => storage.set(key, value) };
  new Function('document', 'window', 'navigator', 'Notification', 'fetch', 'sessionStorage', 'atob', source)(
    { getElementById: id => elements[id], createElement: () => ({ textContent: '' }) }, window, navigator, Notification, fetch, sessionStorage,
    () => String.fromCharCode(1, 2, 3));
  return { elements, state, requests, storage, click: id => elements[id].handlers.click() };
}

(async () => {
  const tab = browser();
  await settle();
  assert(tab.state.subscriptions === 0, 'Never subscribe automatically');
  assert(!tab.elements['notification-prompt'].hidden, 'Returning unregistered user sees the prompt');
  assert(tab.elements['notification-prompt-description'].textContent === 'Enable notifications to receive a notification when you have received a message from your mentor. Tap Enable notifications, then allow notifications in your browser.', 'Prompt explains mentor message alerts');
  const enabled = tab.click('notification-prompt-enable');
  assert(tab.state.subscriptions === 1, 'Subscribe is called immediately in the user gesture');
  await enabled;
  assert(tab.elements['notification-prompt'].hidden, 'Successful enable hides the prompt');
  const requestCount = tab.requests.length;
  await tab.click('message-notifications-test');
  assert(tab.state.testAlerts.length === 1, 'Local test requests a browser alert');
  await tab.click('message-notifications-test');
  await tab.click('message-notifications-test');
  assert(tab.state.testAlerts.length === 3 && new Set(tab.state.testAlerts.map(alert => alert.tag)).size === 3,
    'Repeated clicks create separate alerts instead of replacing the first notification');
  assert(!tab.elements['message-notifications-test'].disabled, 'Test button remains available after repeated clicks');
  assert(tab.requests.length === requestCount, 'Display test does not send messages or contact the push service');
  tab.state.testFails = true;
  await tab.click('message-notifications-test');
  assert(tab.elements['message-notifications-status'].textContent.includes('could not display'), 'Display failure explains permission checks');
  const save = tab.requests.find(request => request.path === '/subscribe');
  assert(save.headers['X-CSRF-Token'] === 'test-token' && JSON.parse(save.body).user_id === 3, 'Saving is authenticated and CSRF protected');
  await tab.click('message-notifications-toggle');
  assert(tab.state.removed === 1 && tab.requests.some(request => request.path === '/unsubscribe'), 'Turning off removes server and browser subscriptions');
  assert(tab.elements['message-notifications-test'].hidden, 'Display test is hidden when notifications are disabled');

  const registered = browser({ existing: true, enabled: true, permission: 'granted' });
  await settle();
  assert(registered.elements['notification-prompt'].hidden, 'Previously enabled device is not prompted again');
  assert(registered.state.subscriptions === 0, 'Existing registration is not duplicated');

  const ios = browser({ ios: true });
  await settle();
  assert(ios.elements['notification-prompt-description'].textContent.includes('Add to Home Screen'), 'iPhone tab explains installation');
  assert(ios.elements['notification-prompt-enable'].hidden, 'No unusable enable button in iPhone browser tab');
  const installed = browser({ ios: true, standalone: true });
  await settle();
  assert(!installed.elements['notification-prompt-enable'].hidden, 'Installed iPhone app can enable push');

  const denied = browser({ permission: 'denied' });
  await settle();
  assert(denied.elements['message-notifications-status'].textContent.includes('blocked'), 'Blocked permission explains settings');
  assert(denied.elements['message-notifications-toggle'].disabled && denied.state.subscriptions === 0, 'Blocked permission does not loop prompts');

  const unavailable = browser({ configured: false });
  await settle();
  assert(unavailable.elements['message-notifications-toggle'].disabled, 'Unconfigured server cannot advertise working delivery');
  const stale = browser({ wrongAccount: true });
  await settle();
  assert(stale.elements['message-notifications-toggle'].disabled, 'Stale account cannot register a device');

  const retry = browser();
  await settle();
  retry.state.saveFails = true;
  await retry.click('notification-prompt-enable');
  assert(!retry.elements['notification-prompt'].hidden, 'Failed server save does not claim success');
  retry.state.saveFails = false;
  await retry.click('notification-prompt-enable');
  assert(retry.state.subscriptions === 1 && retry.elements['notification-prompt'].hidden, 'Retry reuses browser subscription');

  const later = browser();
  await settle();
  later.click('notification-prompt-dismiss');
  const sameVisit = browser({ storage: later.storage });
  await settle();
  assert(sameVisit.elements['notification-prompt'].hidden, 'Not now is remembered for the visit');
  const nextVisit = browser();
  await settle();
  assert(!nextVisit.elements['notification-prompt'].hidden, 'A new visit shows the reminder');

  // The service worker receives push with no page or document running.
  const handlers = {};
  const notices = [];
  const opened = [];
  class TestURL {
    constructor(value, base) {
      const absolute = /^https?:/.test(value) ? value : base + value;
      const match = absolute.match(/^(https?:\/\/[^/]+)([^?#]*)(#.*)?$/);
      this.origin = match[1]; this.pathname = match[2]; this.hash = match[3] || ''; this.href = absolute;
    }
  }
  const self = { location: { origin: 'https://kynnected.test' },
    addEventListener: (type, fn) => { handlers[type] = fn; },
    registration: { showNotification: async (title, data) => notices.push({ title, ...data }) },
    clients: { matchAll: async () => [], openWindow: async url => opened.push(url) } };
  new Function('self', 'URL', workerSource)(self, typeof URL === 'function' ? URL : TestURL);
  let finished;
  handlers.push({ data: { json: () => ({ url: '/pairings/1/3#chat', tag: 'pair' }) },
    waitUntil: promise => { finished = promise; } });
  await finished;
  assert(notices.length === 1 && notices[0].data.url === '/pairings/1/3#chat', 'Background push displays without an open tab');
  assert(notices[0].renotify === true && notices[0].tag, 'Later messages in the same pairing must alert again');
  handlers.notificationclick({ notification: { close() {}, data: notices[0].data },
    waitUntil: promise => { finished = promise; } });
  await finished;
  assert(opened[0] === 'https://kynnected.test/pairings/1/3#chat', 'Tapping opens its pairing chat');
  handlers.notificationclick({ notification: { close() {}, data: { url: 'https://other.test/pairings/1/3#chat' } },
    waitUntil() { throw new Error('Unsafe notification link'); } });
  log('Web Push prompt and service-worker JavaScript checks passed.');
})().catch(error => { log(error.stack); if (typeof quit === 'function') quit(1); else process.exitCode = 1; });
