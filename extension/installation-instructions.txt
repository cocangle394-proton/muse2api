Muse2API Cookie Import Extension — Installation Guide
=====================================

This extension does one thing: it syncs the muse.ai login cookies from your browser
to your muse2api service. No Python, no terminal needed.


1. Extract
--------
Extract muse2api-extension.zip into a folder you will not delete, for example:

    Windows : D:\muse2api-extension
    macOS   : ~/Documents/muse2api-extension

After extracting, you should see three files: manifest.json, popup.html, and popup.js.


2. Load it in Chrome / Edge
---------------------------
Chrome：
  1. In the address bar, type chrome://extensions and press Enter
  2. Turn on the "Developer mode" switch in the top right
  3. Click "Load unpacked" in the top left
  4. Select the folder you extracted in step 1 (not a single file inside it)
  5. A puzzle-piece icon appears in the toolbar; pin it to the toolbar (optional but handy)

Edge：
  1. In the address bar, type edge://extensions and press Enter
  2. Turn on "Developer mode" in the bottom left
  3. Click "Load unpacked"
  4. The rest is the same as above

Other Chromium-based browsers (Brave / Vivaldi / 360 Speed, etc.) follow similar steps.


3. Use
--------
1. First open https://muse.ai/ in this browser and log in,
   until you can see the chat interface.
2. On the muse2api admin page, copy the BASE URL and API Key from the top of the "Account Pool" tab.
3. Click the extension icon in the browser toolbar and enter both values (only once — they are remembered).
4. Click "Read and import" in the same window where you are logged in to Muse. Before using Incognito/InPrivate, enable "Allow in Incognito mode" in the extension details.
5. Check that the "Cookie source" in the result is the expected window, then test the account on the admin page. A successful import only means it was saved, not that the session has been verified.


4. FAQ
------------
Q: I am logged in in an Incognito window, but it imported the regular window's account, or testing shows /api/session HTTP 401
A: Version 1.0.2 and earlier did not select the cookie store by the current window, so they could read an old session from the regular window.
   Download 1.0.3 from your Muse2API admin page, replace the old extension folder, and reload it; then retry in the same window where you are logged in to Muse.
   1.0.3 stops the import if it cannot find the matching store instead of falling back to another window. A logged-out session still needs to be logged in again on the official site.

Q: It says "No muse.ai cookies found"
A: This browser is not logged in to muse.ai yet. Open https://muse.ai/ and log in first.

Q: It says "Missing core items such as hatch_sess"
A: Same as above — login is not complete. Confirm you can see the chat interface, then click again.

Q: It says "Invalid API Key (the service returned 401)"
A: Copy the API Key from the admin page again. Note that keys start with m2a_.

Q: It reports a cross-origin / network error
A: Check that the service URL is https and that the service itself opens
   (Try opening the service URL/admin directly in the browser).

Q: Will my cookies be sent somewhere else?
A: No. The extension uses the cookies, storage, and activeTab permissions, and the code is right there in popup.js.
   You can read it yourself: it sends a single POST to the service URL you entered, and no other requests.


5. Uninstall
--------
chrome://extensions → find "Muse2API Cookie Import" → Remove.
