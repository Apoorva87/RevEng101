const DASHBOARD_URL = 'http://localhost:9081/api/chat/browser-context';

document.getElementById('send-btn').addEventListener('click', async () => {
  const statusEl = document.getElementById('status');
  const btn = document.getElementById('send-btn');

  btn.disabled = true;
  statusEl.textContent = 'Gathering context...';
  statusEl.className = 'status';

  try {
    // Get the current active tab
    const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
    if (!tab) throw new Error('No active tab');

    const url = tab.url;

    // Get cookies for this tab's URL
    let cookies = [];
    try {
      const tabUrl = new URL(url);
      cookies = await chrome.cookies.getAll({ domain: tabUrl.hostname });
    } catch (e) {
      // May fail for special URLs
    }

    // Get localStorage via content script
    let localStorageData = {};
    try {
      const results = await chrome.scripting.executeScript({
        target: { tabId: tab.id },
        func: () => {
          const data = {};
          for (let i = 0; i < localStorage.length; i++) {
            const key = localStorage.key(i);
            data[key] = localStorage.getItem(key);
          }
          return data;
        },
      });
      if (results && results[0]) {
        localStorageData = results[0].result || {};
      }
    } catch (e) {
      // May fail on restricted pages
    }

    // Send to dashboard
    const response = await fetch(DASHBOARD_URL, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        url,
        cookies: cookies.map(c => ({
          name: c.name,
          value: c.value,
          domain: c.domain,
          path: c.path,
          secure: c.secure,
          httpOnly: c.httpOnly,
        })),
        localStorage: localStorageData,
      }),
    });

    if (!response.ok) throw new Error(`HTTP ${response.status}`);

    statusEl.textContent = `Sent: ${cookies.length} cookies, ${Object.keys(localStorageData).length} localStorage keys`;
    statusEl.className = 'status success';
  } catch (err) {
    statusEl.textContent = `Error: ${err.message}`;
    statusEl.className = 'status error';
  } finally {
    btn.disabled = false;
  }
});
