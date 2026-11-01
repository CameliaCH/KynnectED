self.addEventListener('install', event => event.waitUntil(self.skipWaiting()));
self.addEventListener('activate', event => event.waitUntil(self.clients.claim()));

self.addEventListener('push', event => {
  let notice = {};
  try { notice = event.data?.json() || {}; } catch (_) {}
  const url = /^\/pairings\/\d+\/\d+#chat$/.test(notice.url || '') ? notice.url : '/dashboard';
  event.waitUntil(self.registration.showNotification('New KynnectED message', {
    body: 'You have a new message in your pairing chat.',
    tag: typeof notice.tag === 'string' && notice.tag ? notice.tag : 'kynnected-message',
    renotify: true,
    data: { url },
  }));
});

self.addEventListener('notificationclick', event => {
  event.notification.close();
  const url = new URL(event.notification.data?.url || '/', self.location.origin);
  if (url.origin !== self.location.origin || !(url.pathname === '/dashboard' || /^\/pairings\/\d+\/\d+#chat$/.test(url.pathname + url.hash))) return;
  event.waitUntil((async () => {
    const windows = await self.clients.matchAll({ type: 'window', includeUncontrolled: true });
    const existing = windows.find(client => new URL(client.url).pathname === url.pathname);
    if (existing) {
      await existing.navigate(url.href);
      await existing.focus();
    } else {
      await self.clients.openWindow(url.href);
    }
  })());
});
