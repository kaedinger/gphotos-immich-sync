// Run this in the DevTools Console while logged in at:
//   https://photos.google.com/albums
//
// Steps:
//   1. Open https://photos.google.com/albums in your browser.
//   2. Open DevTools (F12) -> Console tab.
//   3. Paste this whole file and press Enter.
//   4. The script does THREE full top-to-bottom scroll passes (fast/medium/
//      slow), then up to 5 remediation passes targeting any anchors that
//      stayed empty. Expect ~1-3 minutes for many hundreds of albums.
//   5. Album list is copied to your clipboard as JSON, and printed.
//   6. Save the clipboard contents as `albums.json` in the project root.
//
// What's hard about this: Google Photos virtualizes the albums list
// (cards outside the viewport are unmounted from the DOM), nests its
// real scroll container inside the page (so `window.scroll` does
// nothing), and sometimes leaves placeholder <a> elements with no inner
// content even after we scroll back to them. Owned albums use
// /album/<hash>; shared albums use /share/<hash>. data-media-key is the
// canonical identity (stable across both). data-shared="true" is the
// cleanest shared flag.
//
// Selectors are best-effort and may need updating if Google changes the UI.

(async () => {
  const wait = (ms) => new Promise((r) => setTimeout(r, ms));

  const ALBUM_SELECTOR = 'a[href*="/album/"], a[href*="/share/"]';

  // Identity: data-media-key is stable across owned + shared albums and
  // doesn't depend on the href format. Fall back to href just in case.
  const idOf = (a) =>
    a.getAttribute("data-media-key") || a.getAttribute("href") || "";

  // Pull y from `style.transform: translate3d(Xpx, Ypx, 0)`.
  const transformY = (a) => {
    const m = (a.style?.transform || "").match(
      /translate3d\([^,]+,\s*(-?\d+(?:\.\d+)?)px/,
    );
    return m ? parseFloat(m[1]) : null;
  };

  // Recognises meta text like "31 items", "31 items  ·  Shared", "1 item".
  // Permissive contains-match so combined-meta nodes don't get mistaken
  // for titles.
  const parseMetaText = (t) => {
    const countMatch = t.match(/(\d+)\s+items?/);
    if (!countMatch) return null;
    return {
      count: parseInt(countMatch[1], 10),
      shared: /\bShared\b/.test(t),
    };
  };

  // Parse a comma-separated label like "Album Name, 12 items, Shared".
  const parseAlbumLabel = (lbl) => {
    let count = null;
    let shared = false;
    const parts = lbl.split(",").map((s) => s.trim());
    while (parts.length > 1) {
      const last = parts[parts.length - 1];
      const m = last.match(/^(\d+)\s+items?$/);
      if (m) {
        count = parseInt(m[1], 10);
        parts.pop();
        continue;
      }
      if (last === "Shared") {
        shared = true;
        parts.pop();
        continue;
      }
      break;
    }
    return { name: parts.join(", ").trim(), count, shared };
  };

  const extractAlbum = (a) => {
    const sharedAttr = a.getAttribute("data-shared") === "true";

    const anchorLbl = (a.getAttribute("aria-label") || "").trim();
    if (anchorLbl) {
      const parsed = parseAlbumLabel(anchorLbl);
      if (parsed.name) {
        return { ...parsed, shared: parsed.shared || sharedAttr };
      }
    }
    for (const node of a.querySelectorAll("[aria-label], [title]")) {
      const lbl = (
        node.getAttribute("aria-label") ||
        node.getAttribute("title") ||
        ""
      ).trim();
      if (!lbl || lbl === "More options") continue;
      const parsed = parseAlbumLabel(lbl);
      if (parsed.name) {
        return { ...parsed, shared: parsed.shared || sharedAttr };
      }
    }

    let name = "";
    let count = null;
    let shared = sharedAttr;
    a.querySelectorAll("*").forEach((node) => {
      if (node.children.length > 0) return;
      const t = (node.textContent || "").trim();
      if (!t) return;
      const meta = parseMetaText(t);
      if (meta) {
        if (meta.count !== null) count = meta.count;
        if (meta.shared) shared = true;
        return;
      }
      if (t === "More options") return;
      if (t === "Shared") {
        shared = true;
        return;
      }
      if (/^[·•\s]+$/.test(t)) return;
      name = name ? `${name} ${t}` : t;
    });
    return { name, count, shared };
  };

  // State.
  const known = new Map(); // mediaKey -> {name, count, shared}
  const placeholderY = new Map(); // mediaKey -> last-seen y (cards we've seen but couldn't extract)
  const hrefByKey = new Map(); // mediaKey -> href, for the URL in the final report
  const allKeys = new Set();

  const collect = () => {
    let added = 0;
    document.querySelectorAll(ALBUM_SELECTOR).forEach((a) => {
      const key = idOf(a);
      if (!key) return;
      allKeys.add(key);
      const href = a.getAttribute("href") || "";
      if (href) hrefByKey.set(key, href);
      if (known.has(key)) return;
      const album = extractAlbum(a);
      if (album.name) {
        known.set(key, album);
        placeholderY.delete(key);
        added++;
      } else {
        const y = transformY(a);
        if (y !== null) placeholderY.set(key, y);
      }
    });
    return added;
  };

  // Scroll container detection.
  const isScrollable = (el) => {
    const s = getComputedStyle(el);
    const oy = s.overflowY;
    const ox = s.overflow;
    const can = oy === "auto" || oy === "scroll" || ox === "auto" || ox === "scroll";
    return can && el.scrollHeight > el.clientHeight + 50;
  };
  const findFromAnchor = () => {
    const a = document.querySelector(ALBUM_SELECTOR);
    if (!a) return null;
    let el = a.parentElement;
    while (el && el !== document.body) {
      if (isScrollable(el)) return el;
      el = el.parentElement;
    }
    return null;
  };
  const findByScan = () => {
    let best = null;
    let bestExtra = 0;
    document.querySelectorAll("*").forEach((el) => {
      if (!isScrollable(el)) return;
      const extra = el.scrollHeight - el.clientHeight;
      if (extra > bestExtra) {
        bestExtra = extra;
        best = el;
      }
    });
    return best;
  };
  const scroller =
    findFromAnchor() ||
    findByScan() ||
    document.scrollingElement ||
    document.documentElement;
  console.log(
    "Using scroller:",
    scroller,
    `scrollHeight=${scroller.scrollHeight}  clientHeight=${scroller.clientHeight}`,
  );

  const fireWheel = (deltaY) => {
    const evt = new WheelEvent("wheel", {
      deltaY,
      deltaMode: 0,
      bubbles: true,
      cancelable: true,
    });
    scroller.dispatchEvent(evt);
    document.dispatchEvent(evt);
  };

  const scrollPass = async ({ stepPx, waitMs, label }) => {
    console.log(`-- Pass "${label}": step=${stepPx}px wait=${waitMs}ms --`);
    scroller.scrollTop = 0;
    await wait(600);
    collect();

    const STABLE_LIMIT = 15;
    const MAX_STEPS = 8000;
    let stable = 0;
    let step = 0;

    while (step < MAX_STEPS && stable < STABLE_LIMIT) {
      const beforeTop = scroller.scrollTop;
      const beforeHeight = scroller.scrollHeight;
      scroller.scrollTop = beforeTop + stepPx;
      if (scroller.scrollTop === beforeTop) fireWheel(stepPx);
      await wait(waitMs);
      const added = collect();
      const grew = scroller.scrollHeight > beforeHeight;
      const moved = scroller.scrollTop > beforeTop;
      if (added === 0 && !grew && !moved) stable++;
      else stable = 0;
      if (step % 20 === 0 || added > 0) {
        console.log(
          `  step ${step}  scrollTop=${scroller.scrollTop}/${scroller.scrollHeight}` +
            `  known=${known.size}  placeholders=${placeholderY.size}` +
            (added > 0 ? `  (+${added})` : ""),
        );
      }
      step++;
    }

    // Drive to the very bottom in case the bottom slot still has content
    // to load.
    let prevHeight = -1;
    for (let i = 0; i < 5 && prevHeight !== scroller.scrollHeight; i++) {
      prevHeight = scroller.scrollHeight;
      scroller.scrollTop = scroller.scrollHeight;
      await wait(800);
      collect();
    }
    console.log(`  pass "${label}" done: known=${known.size}  placeholders=${placeholderY.size}`);
  };

  await scrollPass({
    stepPx: Math.max(scroller.clientHeight - 100, 600),
    waitMs: 350,
    label: "fast",
  });
  await scrollPass({
    stepPx: Math.max(Math.round(scroller.clientHeight * 0.5), 400),
    waitMs: 700,
    label: "medium",
  });
  await scrollPass({
    stepPx: Math.max(Math.round(scroller.clientHeight * 0.3), 250),
    waitMs: 1000,
    label: "slow",
  });

  // Remediation passes: target each placeholder y at three offsets so we
  // catch the card whether its actual render position is slightly above
  // or below the recorded transform y.
  const REMEDIATION_ATTEMPTS = 5;
  for (let attempt = 1; attempt <= REMEDIATION_ATTEMPTS; attempt++) {
    if (placeholderY.size === 0) break;
    const ys = [...new Set([...placeholderY.values()])].sort((a, b) => a - b);
    console.log(
      `-- Remediation ${attempt}/${REMEDIATION_ATTEMPTS}: ${placeholderY.size} placeholder(s) at ${ys.length} unique y --`,
    );
    for (const y of ys) {
      for (const offset of [-400, 0, 400]) {
        const target = Math.max(0, y - scroller.clientHeight / 2 + offset);
        scroller.scrollTop = target;
        await wait(900);
        collect();
      }
    }
    console.log(`  attempt ${attempt} done: known=${known.size}  placeholders=${placeholderY.size}`);
  }

  // Build output. Stragglers go in too — with their photos.google.com URL
  // as the name so they're visible and inspectable, plus a flag and the
  // mediaKey for traceability. Better to surface them than silently drop.
  const knownEntries = [...known.entries()].map(([key, a]) => ({
    ...a,
    mediaKey: key,
  }));
  const stragglers = [...placeholderY.keys()]
    .filter((key) => !known.has(key))
    .map((key) => ({
      name: `https://photos.google.com${(hrefByKey.get(key) || `/album/${key}`).replace(/^\.\//, "/")}`,
      count: null,
      shared: false,
      mediaKey: key,
      unknown: true,
    }));

  const arr = [...knownEntries, ...stragglers].sort((a, b) =>
    a.name.localeCompare(b.name),
  );
  const json = JSON.stringify(arr, null, 2);

  let copied = false;
  if (typeof copy === "function") {
    try {
      copy(json);
      copied = true;
    } catch {}
  }
  if (!copied && navigator.clipboard && navigator.clipboard.writeText) {
    try {
      await navigator.clipboard.writeText(json);
      copied = true;
    } catch {}
  }

  console.log("");
  console.log(`Total album anchors seen: ${allKeys.size}`);
  console.log(`Named: ${known.size}    Unknown stragglers: ${stragglers.length}`);
  if (copied) {
    console.log(`Copied ${arr.length} entries to clipboard.`);
  } else {
    console.log(`${arr.length} entries collected. Copy the JSON below:`);
  }
  if (stragglers.length > 0) {
    const lines = stragglers.map(
      (s) => `  ${s.name}  (mediaKey=${s.mediaKey})`,
    );
    console.log(
      `\n${stragglers.length} album(s) had anchors that never rendered a title.\n` +
        `They are included in the JSON output with name = the album URL,\n` +
        `flagged with "unknown": true. Open each link to verify the actual album:\n` +
        lines.join("\n"),
    );
  }
  if (!copied) console.log(json);
  return arr;
})();
