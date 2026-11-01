(() => {
  const panel = document.getElementById('message-notifications');
  if (!panel) return;
  const banner = document.getElementById('notification-prompt');
  const toggle = document.getElementById('message-notifications-toggle');
  const test = document.getElementById('message-notifications-test');
  const enable = document.getElementById('notification-prompt-enable');
  const status = document.getElementById('message-notifications-status');
  const explanation = document.getElementById('notification-prompt-description');
  const userId = Number(panel.dataset.userId);
  const dismissalKey = 'kynnected-push-prompt:' + userId;
  let registration;
  let subscription;
  let applicationServerKey;
  let enabled = false;
  let busy = true;
  let available = false;
  let testSequence = 0;

  function dismissed() {
    try { return sessionStorage.getItem(dismissalKey) === 'yes'; } catch (_) { return false; }
  }
  function show(message, canEnable = false) {
    status.textContent = message;
    explanation.textContent = message;
    banner.hidden = enabled || dismissed();
    enable.hidden = !canEnable;
    enable.disabled = busy || !available;
    toggle.disabled = busy || (!available && !enabled);
    toggle.textContent = enabled ? 'Turn off notifications' : 'Enable notifications';
    test.hidden = !enabled;
    test.disabled = busy || !available || !enabled;
  }
  function normalStatus() {
    show(enabled
      ? 'Notifications are on for this account on this device, even when KynnectED is closed.'
      : panel.dataset.promptDescription + ' Tap Enable notifications, then allow notifications in your browser.', !enabled);
    if (!enabled) {
      const action = document.createElement('strong');
      action.textContent = 'Enable notifications';
      explanation.replaceChildren(panel.dataset.promptDescription + ' Tap ', action, ', then allow notifications in your browser.');
    }
  }
  async function api(path, data) {
    const response = await fetch(path, data === undefined ? { cache: 'no-store' } : {
      method: 'POST', headers: { 'Content-Type': 'application/json', 'X-CSRF-Token': panel.dataset.csrf },
      body: JSON.stringify({ user_id: userId, ...data }),
    });
    if (response.redirected || [401, 403, 409].includes(response.status)) {
      available = false;
      throw new Error('Please reload this page and sign in to manage notifications.');
    }
    const result = await response.json().catch(() => null);
    if (!response.ok || !result) throw new Error(result?.error || 'Could not save notification settings. Please try again.');
    return result;
  }
  function decodeKey(value) {
    const padded = value.replace(/-/g, '+').replace(/_/g, '/') + '='.repeat((4 - value.length % 4) % 4);
    return Uint8Array.from(atob(padded), char => char.charCodeAt(0));
  }
  function sameKey(value) {
    const previous = value && new Uint8Array(value);
    return previous && previous.length === applicationServerKey.length && previous.every((byte, index) => byte === applicationServerKey[index]);
  }

  async function initialize() {
    busy = true;
    toggle.disabled = true;
    enable.disabled = true;
    try {
      const config = await api(panel.dataset.configUrl);
      if (config.user_id !== userId) throw new Error('Your account has changed. Reload this page.');
      if (!config.available) {
        show('Message notifications are not available yet. You can still read messages in your pairing chat.');
        return;
      }
      const ios = /iPad|iPhone|iPod/.test(navigator.userAgent) || (navigator.platform === 'MacIntel' && navigator.maxTouchPoints > 1);
      if (ios && !window.matchMedia('(display-mode: standalone)').matches && !navigator.standalone) {
        show('To receive notifications on iPhone or iPad, tap Share → Add to Home Screen. Open KynnectED from that icon, sign in, then tap Enable notifications.');
        return;
      }
      if (!window.isSecureContext || !('Notification' in window) || !('serviceWorker' in navigator) || !('PushManager' in window)) {
        show('This browser cannot receive push notifications here. Open KynnectED using HTTPS in a supported browser to enable alerts.');
        return;
      }
      await navigator.serviceWorker.register(panel.dataset.workerUrl, { updateViaCache: 'none' });
      registration = await navigator.serviceWorker.ready;
      applicationServerKey = decodeKey(config.public_key);
      subscription = await registration.pushManager.getSubscription();
      // A rotated application key requires a new browser subscription.
      if (subscription && !sameKey(subscription.options.applicationServerKey)) {
        await api(panel.dataset.unsubscribeUrl, { endpoint: subscription.endpoint });
        await subscription.unsubscribe();
        subscription = null;
      }
      if (subscription) {
        const result = await api(panel.dataset.statusUrl, { endpoint: subscription.endpoint });
        enabled = result.enabled && Notification.permission === 'granted';
      }
      if (Notification.permission === 'denied') {
        if (subscription) await api(panel.dataset.unsubscribeUrl, { endpoint: subscription.endpoint });
        enabled = false;
        show('Notifications are blocked. Allow notifications for KynnectED in your browser or device settings, then reload this page.');
        return;
      }
      available = true;
      busy = false;
      normalStatus();
    } catch (error) {
      show(error.message);
    } finally {
      busy = false;
      toggle.disabled = !available;
      enable.disabled = !available;
    }
  }

  async function changeNotifications() {
    if (busy || !available) return;
    busy = true;
    toggle.disabled = enable.disabled = true;
    try {
      if (enabled) {
        await api(panel.dataset.unsubscribeUrl, { endpoint: subscription.endpoint });
        enabled = false;
        await subscription.unsubscribe();
        subscription = null;
        try { sessionStorage.setItem(dismissalKey, 'yes'); } catch (_) {}
      } else {
        // Prepared before the tap so Safari receives subscribe during the user gesture.
        subscription = subscription || await registration.pushManager.subscribe({ userVisibleOnly: true, applicationServerKey });
        await api(panel.dataset.subscribeUrl, { subscription: subscription.toJSON() });
        enabled = true;
      }
      busy = false;
      normalStatus();
    } catch (error) {
      show(Notification.permission === 'denied'
        ? 'Notifications are blocked. Allow notifications for KynnectED in your browser or device settings, then reload this page.'
        : error.message || 'Could not enable notifications. Please try again.', true);
    } finally {
      busy = false;
      toggle.disabled = enable.disabled = !available;
    }
  }
  toggle.addEventListener('click', changeNotifications);
  test.addEventListener('click', async () => {
    if (busy || !enabled || !registration) return;
    busy = true;
    test.disabled = toggle.disabled = enable.disabled = true;
    try {
      await registration.showNotification('KynnectED notification test', {
        body: 'This is a test notification from KynnectED.',
        tag: `kynnected-notification-test-${Date.now()}-${++testSequence}`,
        data: { url: '/dashboard' },
      });
      const settings = /Mac/.test(navigator.platform) ? 'Mac System Settings → Notifications' : 'your device’s notification settings';
      status.textContent = 'A test alert was requested on this device. If it did not appear, check your browser’s notifications in ' + settings + ' and turn off Focus or Do Not Disturb. This checks alert display, not message delivery.';
    } catch (_) {
      status.textContent = 'Your browser could not display the test. Check notification permissions for this site and for your browser in your device settings, then reload.';
    } finally {
      busy = false;
      toggle.disabled = enable.disabled = !available;
      test.disabled = !available || !enabled;
    }
  });
  enable.addEventListener('click', changeNotifications);
  document.getElementById('notification-prompt-dismiss').addEventListener('click', () => {
    banner.hidden = true;
    try { sessionStorage.setItem(dismissalKey, 'yes'); } catch (_) {}
  });
  initialize();
})();
