/* SDC Composer — single-page form with live preview.
 *
 * The form is shaped to fill a NewsletterIssue (see app/models.py). On every
 * change we debounce for ~400ms then POST the assembled JSON to
 * /api/preview/html and pipe the rendered HTML into the right-hand iframe.
 * "Download .eml" submits a hidden form so the browser handles the file save.
 */
(() => {
  const form = document.getElementById('composer');
  const preview = document.getElementById('preview');
  const previewMeta = document.getElementById('preview-meta');
  const status = document.getElementById('status');

  const articlesContainer = document.getElementById('articles');
  const staticContainer = document.getElementById('static-blocks');
  const highlightsContainer = document.getElementById('highlights');
  const podcastEnabled = document.getElementById('podcast-enabled');
  const podcastFields = document.getElementById('podcast-fields');

  const articlesCount = document.getElementById('articles-count');
  const staticCount = document.getElementById('static-count');
  const highlightsCount = document.getElementById('highlights-count');

  const articleTpl = document.getElementById('article-template');
  const podcastTpl = document.getElementById('podcast-template');
  const timecodeTpl = document.getElementById('timecode-template');
  const staticTpl = document.getElementById('static-template');
  const highlightTpl = document.getElementById('highlight-template');

  // ───────── Helpers ─────────────────────────────────────────────────
  const trim = (v) => (typeof v === 'string' ? v.trim() : v);
  const nonEmpty = (v) => v !== '' && v !== null && v !== undefined;

  // Mirror of app/links.py: unwrap Microsoft Safelinks before they enter
  // the issue. Server-side validation runs the same logic, but doing it
  // on the client too means the user sees the clean URL immediately —
  // including before they click Fetch — and avoids a round-trip.
  const SAFELINK_HOST_RX = /(^|\.)safelinks\.protection\.outlook\.com$/i;
  const sanitizeUrl = (raw) => {
    if (typeof raw !== 'string') return raw;
    let current = raw.trim();
    for (let i = 0; i < 5; i += 1) {
      let parsed;
      try { parsed = new URL(current); } catch { return current; }
      if (!SAFELINK_HOST_RX.test(parsed.hostname)) return current;
      const inner = parsed.searchParams.get('url') || parsed.searchParams.get('URL');
      if (!inner) return current;
      let innerParsed;
      try { innerParsed = new URL(inner); } catch { return current; }
      if (!/^https?:$/.test(innerParsed.protocol)) return current;
      current = inner;
    }
    return current;
  };
  const sanitizeUrlInput = (input) => {
    if (!input) return '';
    const cleaned = sanitizeUrl(input.value || '');
    if (cleaned !== input.value) input.value = cleaned;
    return cleaned;
  };
  const setStatus = (msg, kind = 'info') => {
    status.textContent = msg || '';
    status.dataset.kind = kind;
  };

  // Hoisted up so updateYearDisplay (defined just below) can read the
  // initial_publication_date without hitting the temporal dead zone.
  let currentSettings = null;

  // Mirror of app/models.compute_issue_year — same anniversary-based math
  // so the printed "Nth year" matches whether the issue is rendered via
  // the live preview (client-only) or assembled server-side. Returns null
  // when either input is missing/invalid; the form shows '—' in that
  // case and buildIssue omits issue_year (server validation will surface
  // the missing setting to the user via a 422).
  const computeIssueYear = (issueDateStr, initialDateStr) => {
    if (!issueDateStr || !initialDateStr) return null;
    const id = new Date(`${issueDateStr}T00:00:00`);
    const ip = new Date(`${initialDateStr}T00:00:00`);
    if (Number.isNaN(id.getTime()) || Number.isNaN(ip.getTime())) return null;
    let years = id.getFullYear() - ip.getFullYear();
    const idMD = id.getMonth() * 100 + id.getDate();
    const ipMD = ip.getMonth() * 100 + ip.getDate();
    if (idMD < ipMD) years -= 1;
    return Math.max(1, years + 1);
  };

  const _ordinalSuffix = (n) => {
    if (n >= 10 && n <= 20) return 'th';
    return ({ 1: 'st', 2: 'nd', 3: 'rd' }[n % 10]) || 'th';
  };

  const yearDisplay = document.getElementById('issue-year-display');
  const updateYearDisplay = () => {
    if (!yearDisplay) return;
    const issueDate = form.elements['issue_date']?.value || '';
    const initial = currentSettings?.initial_publication_date || '';
    const y = computeIssueYear(issueDate, initial);
    if (y === null) {
      yearDisplay.textContent = '—';
      yearDisplay.title = (
        initial ? 'Set the issue date.' :
        'Set Inception Date in Settings.'
      );
    } else {
      yearDisplay.textContent = `${y}${_ordinalSuffix(y)} year`;
      yearDisplay.title = '';
    }
  };

  const collectFields = (root) => {
    const out = {};
    root.querySelectorAll('[data-field]').forEach((el) => {
      const key = el.dataset.field;
      let v;
      if (el.type === 'checkbox') v = el.checked;
      else if (el.type === 'number') v = el.value === '' ? null : Number(el.value);
      else v = trim(el.value);
      out[key] = v;
    });
    return out;
  };

  const stripEmpty = (obj) => {
    const out = {};
    Object.entries(obj).forEach(([k, v]) => {
      if (v === null || v === undefined) return;
      if (typeof v === 'string' && v === '') return;
      out[k] = v;
    });
    return out;
  };

  // ───────── Articles ────────────────────────────────────────────────
  // Each article card carries a stable `data-article-id` so reordering
  // by drag-and-drop doesn't break highlight references — highlights
  // store the article id in the DOM and resolve to a numeric position
  // index only at buildIssue() time. Without this, dragging articles
  // would leave highlight selects pointing at the wrong row.
  let nextArticleId = 0;
  const newArticleId = () => `a-${++nextArticleId}`;

  const updateArticleHeaderPreview = (card) => {
    const preview = card.querySelector('.card-headline-preview');
    const input = card.querySelector('[data-field="headline"]');
    if (!preview || !input) return;
    const h = (input.value || '').trim();
    preview.textContent = h ? ` — ${h.length > 60 ? h.slice(0, 57) + '…' : h}` : '';
  };

  const renumber = () => {
    [...articlesContainer.querySelectorAll('.article-card')].forEach((card, i) => {
      const idx = card.querySelector('.idx');
      if (idx) idx.textContent = `#${i + 1}`;
      updateArticleHeaderPreview(card);
    });
    [...staticContainer.querySelectorAll('fieldset.card')].forEach((card, i) => {
      const idx = card.querySelector('.idx');
      if (idx) idx.textContent = `#${i + 1}`;
    });
    articlesCount.textContent = articlesContainer.querySelectorAll('.article-card').length;
    staticCount.textContent = staticContainer.querySelectorAll('fieldset.card').length;
    highlightsCount.textContent = highlightsContainer.querySelectorAll('.row').length;
    refreshHighlightOptions();
  };

  const addArticle = (data = {}) => {
    const node = articleTpl.content.firstElementChild.cloneNode(true);
    node.dataset.articleId = newArticleId();
    Object.entries(data).forEach(([k, v]) => {
      const el = node.querySelector(`[data-field="${k}"]`);
      if (el && v !== null && v !== undefined) el.value = v;
    });
    const removeBtn = node.querySelector('.btn-remove');
    removeBtn.addEventListener('click', (e) => {
      // Inside a <summary>, a button click would also toggle the <details>.
      e.preventDefault();
      e.stopPropagation();
      node.remove();
      renumber();
      schedulePreview();
    });
    const fetchBtn = node.querySelector('[data-action="fetch-article"]');
    if (fetchBtn) {
      fetchBtn.addEventListener('click', (e) => {
        e.preventDefault();
        fetchArticleInto(node);
      });
    }
    wireArticleDragHandle(node);
    articlesContainer.appendChild(node);
    renumber();
    return node;
  };

  // ───────── Article reorder (drag & drop) ───────────────────────────
  // Native HTML5 DnD scoped to the drag handle: draggable is toggled on
  // mousedown of the handle (and reset on dragend / mouseup) so clicks
  // inside form inputs don't accidentally start a drag of the whole card.
  // Reorder is done live during dragover (insertBefore the nearest sibling
  // whose vertical midpoint is below the cursor); on dragend we renumber
  // and re-render. Highlight selects keep their values because they store
  // the stable `article-id`, not a positional index.
  let draggingEl = null;

  const getDragAfterElement = (container, y) => {
    const els = [...container.querySelectorAll('.article-card:not(.dragging)')];
    let closest = { offset: Number.NEGATIVE_INFINITY, element: null };
    els.forEach((child) => {
      const box = child.getBoundingClientRect();
      const offset = y - box.top - box.height / 2;
      if (offset < 0 && offset > closest.offset) closest = { offset, element: child };
    });
    return closest.element;
  };

  const wireArticleDragHandle = (card) => {
    const handle = card.querySelector('.drag-handle');
    if (!handle) return;
    handle.addEventListener('mousedown', () => { card.draggable = true; });
    handle.addEventListener('mouseup', () => { card.draggable = false; });
    // Click on the handle (no drag) would otherwise toggle <details>.
    handle.addEventListener('click', (e) => { e.preventDefault(); e.stopPropagation(); });
    card.addEventListener('dragstart', (e) => {
      if (!card.draggable) return;
      draggingEl = card;
      card.classList.add('dragging');
      e.dataTransfer.effectAllowed = 'move';
      e.dataTransfer.setData('text/plain', card.dataset.articleId || '');
    });
    card.addEventListener('dragend', () => {
      card.classList.remove('dragging');
      card.draggable = false;
      if (draggingEl === card) draggingEl = null;
      renumber();
      schedulePreview();
    });
  };

  // Container-level dragover — moves the dragged card to its new slot
  // continuously so the user sees the target position update live.
  articlesContainer.addEventListener('dragover', (e) => {
    if (!draggingEl) return;
    e.preventDefault();
    const after = getDragAfterElement(articlesContainer, e.clientY);
    if (after == null) articlesContainer.appendChild(draggingEl);
    else if (after !== draggingEl) articlesContainer.insertBefore(draggingEl, after);
  });

  // ───────── Article fetch (auto-fill from URL) ──────────────────────
  const fetchArticleInto = async (card) => {
    const urlInput = card.querySelector('[data-field="url"]');
    const statusEl = card.querySelector('[data-role="fetch-status"]');
    const button = card.querySelector('[data-action="fetch-article"]');
    const url = sanitizeUrlInput(urlInput);
    const setFetchStatus = (msg, kind = 'info') => {
      if (!statusEl) return;
      statusEl.textContent = msg || '';
      statusEl.dataset.kind = kind;
    };
    if (!url) {
      setFetchStatus('Enter a URL first.', 'error');
      urlInput?.focus();
      return;
    }
    if (button) {
      button.disabled = true;
    }
    setFetchStatus('Fetching…');
    try {
      const res = await fetch(
        `/api/fetch/article?url=${encodeURIComponent(url)}`,
      );
      if (!res.ok) {
        let detail = '';
        try { detail = (await res.json()).detail || ''; } catch { /* ignore */ }
        setFetchStatus(`Fetch ${res.status}: ${detail || res.statusText}`, 'error');
        return;
      }
      const meta = await res.json();
      const fields = ['headline', 'byline', 'date_text', 'pull_quote', 'publication_logo_url', 'read_more_url'];
      let filled = 0;
      fields.forEach((f) => {
        const el = card.querySelector(`[data-field="${f}"]`);
        if (!el) return;
        if (!nonEmpty(trim(el.value)) && nonEmpty(meta[f])) {
          el.value = meta[f];
          filled += 1;
        }
      });
      if (filled === 0) {
        setFetchStatus('Fetched, but all fields already had values.', 'ok');
      } else {
        setFetchStatus(`Filled ${filled} field${filled === 1 ? '' : 's'}.`, 'ok');
      }
      schedulePreview();
    } catch (e) {
      setFetchStatus('Network error: ' + e.message, 'error');
    } finally {
      if (button) {
        button.disabled = false;
      }
    }
  };

  // ───────── Podcast ─────────────────────────────────────────────────
  let podcastNode = null;
  let timecodesContainer = null;
  const renderPodcastFields = () => {
    podcastFields.innerHTML = '';
    podcastNode = podcastTpl.content.firstElementChild.cloneNode(true);
    podcastFields.appendChild(podcastNode);
    timecodesContainer = podcastNode.querySelector('#timecodes');
    podcastNode.querySelector('#add-timecode').addEventListener('click', (e) => {
      e.preventDefault();
      addTimecode();
    });
    const ytFetchBtn = podcastNode.querySelector('[data-action="fetch-youtube"]');
    if (ytFetchBtn) {
      ytFetchBtn.addEventListener('click', (e) => {
        e.preventDefault();
        fetchYouTubeInto(podcastNode);
      });
    }
  };

  // ───────── YouTube fetch (auto-fill title / summary / timecodes) ───
  const fetchYouTubeInto = async (root) => {
    const urlInput = root.querySelector('[data-field="youtube_video_url"]');
    const statusEl = root.querySelector('[data-role="youtube-fetch-status"]');
    const button = root.querySelector('[data-action="fetch-youtube"]');
    const url = sanitizeUrlInput(urlInput);
    const setFetchStatus = (msg, kind = 'info') => {
      if (!statusEl) return;
      statusEl.textContent = msg || '';
      statusEl.dataset.kind = kind;
    };
    if (!url) {
      setFetchStatus('Enter a YouTube URL first.', 'error');
      urlInput?.focus();
      return;
    }
    if (button) button.disabled = true;
    setFetchStatus('Fetching…');
    try {
      const res = await fetch(
        `/api/fetch/youtube?url=${encodeURIComponent(url)}`,
      );
      if (!res.ok) {
        let detail = '';
        try { detail = (await res.json()).detail || ''; } catch { /* ignore */ }
        setFetchStatus(`Fetch ${res.status}: ${detail || res.statusText}`, 'error');
        return;
      }
      const meta = await res.json();
      let filled = 0;
      // Only fill empty fields (don't clobber Alain's edits).
      ['title', 'summary', 'youtube_video_url'].forEach((f) => {
        const el = root.querySelector(`[data-field="${f}"]`);
        if (!el) return;
        if (!nonEmpty(trim(el.value)) && nonEmpty(meta[f])) {
          el.value = meta[f];
          filled += 1;
        }
      });
      // Append timecodes — but don't duplicate ones already in the list.
      let added = 0;
      if (Array.isArray(meta.timecodes) && timecodesContainer) {
        const existing = new Set(
          [...timecodesContainer.querySelectorAll('.row')].map((row) => {
            const tc = row.querySelector('[data-field="timecode"]')?.value || '';
            return trim(tc);
          }).filter(Boolean),
        );
        meta.timecodes.forEach((tc) => {
          if (!tc || !tc.timecode || existing.has(tc.timecode)) return;
          addTimecode({ timecode: tc.timecode, label: tc.label || '', url: tc.url || '' });
          existing.add(tc.timecode);
          added += 1;
        });
      }
      if (filled === 0 && added === 0) {
        setFetchStatus('Fetched, but nothing new to fill.', 'ok');
      } else {
        const parts = [];
        if (filled) parts.push(`${filled} field${filled === 1 ? '' : 's'}`);
        if (added) parts.push(`${added} timecode${added === 1 ? '' : 's'}`);
        setFetchStatus(`Filled ${parts.join(' + ')}.`, 'ok');
      }
      schedulePreview();
    } catch (e) {
      setFetchStatus('Network error: ' + e.message, 'error');
    } finally {
      if (button) button.disabled = false;
    }
  };

  const addTimecode = (data = {}) => {
    if (!timecodesContainer) return;
    const node = timecodeTpl.content.firstElementChild.cloneNode(true);
    Object.entries(data).forEach(([k, v]) => {
      const el = node.querySelector(`[data-field="${k}"]`);
      if (el && v !== null && v !== undefined) el.value = v;
    });
    node.querySelector('.btn-remove').addEventListener('click', () => {
      node.remove();
      schedulePreview();
    });
    timecodesContainer.appendChild(node);
    return node;
  };

  podcastEnabled.addEventListener('change', () => {
    if (podcastEnabled.checked) {
      podcastFields.hidden = false;
      if (!podcastNode) {
        renderPodcastFields();
      }
    } else {
      podcastFields.hidden = true;
    }
    schedulePreview();
  });

  // ───────── Static blocks ───────────────────────────────────────────
  const addStatic = (data = {}) => {
    const node = staticTpl.content.firstElementChild.cloneNode(true);
    Object.entries(data).forEach(([k, v]) => {
      const el = node.querySelector(`[data-field="${k}"]`);
      if (!el) return;
      if (el.type === 'checkbox') el.checked = !!v;
      else if (v !== null && v !== undefined) el.value = v;
    });
    node.querySelector('.btn-remove').addEventListener('click', () => {
      node.remove();
      renumber();
      schedulePreview();
    });
    staticContainer.appendChild(node);
    renumber();
    return node;
  };

  // ───────── Highlights ──────────────────────────────────────────────
  // Highlight <select> options carry the article's stable `data-article-id`
  // as the option value; the printed label still shows "#N — headline" with
  // N being the article's *current* DOM position. This keeps a highlight
  // bound to its referenced article through reorder/insert/remove without
  // numeric-index drift. The saved-issue JSON still uses numeric
  // `article_index` (model contract); we map IDs <-> indices at the
  // load/build boundaries (loadIssueIntoForm + buildIssue).
  const _escAttr = (s) =>
    String(s == null ? '' : s).replace(/[<>&"']/g, (c) =>
      ({ '<': '&lt;', '>': '&gt;', '&': '&amp;', '"': '&quot;', "'": '&#39;' }[c]));

  const refreshHighlightOptions = () => {
    const articles = [...articlesContainer.querySelectorAll('.article-card')];
    const optionsHtml = articles.map((a, i) => {
      const headline = a.querySelector('[data-field="headline"]').value || `(article #${i + 1})`;
      const truncated = headline.length > 60 ? headline.slice(0, 57) + '…' : headline;
      const id = a.dataset.articleId || '';
      return `<option value="${_escAttr(id)}">#${i + 1} — ${_escAttr(truncated)}</option>`;
    }).join('');
    highlightsContainer.querySelectorAll('select[data-field="article_index"]').forEach((sel) => {
      const prev = sel.value;
      sel.innerHTML = optionsHtml;
      // Preserve selection by article id; if the referenced article was
      // removed, the select falls through to the first option.
      if (prev && [...sel.options].some((o) => o.value === prev)) {
        sel.value = prev;
      }
    });
  };

  const addHighlight = (data = {}) => {
    const node = highlightTpl.content.firstElementChild.cloneNode(true);
    highlightsContainer.appendChild(node);
    refreshHighlightOptions();
    // `data.article_index` is the saved numeric DOM position from
    // template/draft JSON. Resolve it to the corresponding card's id.
    if (data.article_index !== undefined && data.article_index !== null) {
      const cards = [...articlesContainer.querySelectorAll('.article-card')];
      const card = cards[Number(data.article_index)];
      if (card && card.dataset.articleId) {
        node.querySelector('[data-field="article_index"]').value = card.dataset.articleId;
      }
    }
    if (data.override_label) {
      node.querySelector('[data-field="override_label"]').value = data.override_label;
    }
    node.querySelector('.btn-remove').addEventListener('click', () => {
      node.remove();
      renumber();
      schedulePreview();
    });
    renumber();
    return node;
  };

  // ───────── Issue assembly ──────────────────────────────────────────
  const buildIssue = () => {
    const fd = new FormData(form);
    const root = {};
    for (const [k, v] of fd.entries()) {
      const trimmed = trim(v);
      if (k === 'issue_number') {
        root[k] = trimmed === '' ? null : Number(trimmed);
      } else {
        root[k] = trimmed;
      }
    }
    // issue_year is no longer a form field — derive it from the issue
    // date and the configured initial publication date so the masthead
    // line ("Nth year of SmartDrivingCars eLetter") stays accurate as
    // the publication ages. If the setting is unset (fresh install) we
    // fall back to "1st year" so the preview keeps rendering; the
    // user-facing display flags this with a tooltip pointing at Settings.
    const computedYear = computeIssueYear(
      form.elements['issue_date']?.value || '',
      currentSettings?.initial_publication_date || '',
    );
    root.issue_year = computedYear ?? 1;

    // Email envelope + masthead logo are app-wide only (no per-issue
    // form fields). Inject them straight from the loaded settings so
    // every preview and downloaded .eml carries the configured values.
    if (currentSettings) {
      [
        'from_name', 'from_email', 'to_email', 'reply_to',
        'masthead_logo_url',
      ].forEach((k) => {
        const v = currentSettings[k];
        if (v) root[k] = v;
      });
      // Boolean toggle: don't gate on truthiness — `false` is meaningful.
      if (typeof currentSettings.include_read_more_link === 'boolean') {
        root.include_read_more_link = currentSettings.include_read_more_link;
      }
    }
    const issue = stripEmpty(root);

    // Articles — drop incomplete rows so the preview keeps rendering while
    // Alain is mid-edit (headline + url are required by the Pydantic model).
    // Build an article-id → output-index map so highlight refs (which carry
    // stable DOM ids) can be resolved after filtering AND after reorder.
    const cards = [...articlesContainer.querySelectorAll('.article-card')];
    const idToOutIdx = new Map();
    issue.articles = [];
    cards.forEach((card) => {
      const fields = stripEmpty(collectFields(card));
      if (fields.headline && fields.url) {
        idToOutIdx.set(card.dataset.articleId, issue.articles.length);
        issue.articles.push(fields);
      }
    });

    // Podcast
    if (podcastEnabled.checked && podcastNode) {
      const podFields = {};
      podcastNode.querySelectorAll(':scope > .grid-2 > [data-field], :scope > .grid-2 > label > [data-field]').forEach(() => {});
      podcastNode.querySelectorAll('[data-field]').forEach((el) => {
        // Skip timecode inputs (they live inside #timecodes which we read below)
        if (el.closest('#timecodes')) return;
        const key = el.dataset.field;
        let v;
        if (el.type === 'number') v = el.value === '' ? null : Number(el.value);
        else v = trim(el.value);
        podFields[key] = v;
      });
      const timecodes = [...timecodesContainer.querySelectorAll('.row')]
        .map((row) => stripEmpty(collectFields(row)))
        .filter((tc) => tc.timecode && tc.label && tc.url);
      const cleaned = stripEmpty(podFields);
      cleaned.timecodes = timecodes;
      if (cleaned.title || timecodes.length || cleaned.zoomcast_url || cleaned.podcast_url) {
        issue.podcast = cleaned;
      }
    }

    // Static blocks
    issue.static_blocks = [...staticContainer.querySelectorAll('fieldset.card')]
      .map((card) => {
        const fields = collectFields(card);
        return { title: fields.title || '', html: fields.html || '', enabled: !!fields.enabled };
      })
      .filter((b) => b.title || b.html);

    // Highlights — selects carry the article's stable id; resolve to the
    // post-filter article indices and drop refs whose article was removed
    // or is still incomplete (no headline+url).
    issue.highlights = [...highlightsContainer.querySelectorAll('.row')]
      .map((row) => {
        const fields = collectFields(row);
        const id = fields.article_index;
        if (id === '' || id === null || id === undefined) return null;
        const remapped = idToOutIdx.get(String(id));
        if (remapped === undefined) return null;
        const out = { article_index: remapped };
        if (fields.override_label) out.override_label = fields.override_label;
        return out;
      })
      .filter((h) => h !== null);

    return issue;
  };

  // ───────── Preview ─────────────────────────────────────────────────
  let pending = null;
  let previewDebounceMs = 400;
  const schedulePreview = () => {
    setStatus('Editing…');
    if (pending) clearTimeout(pending);
    pending = setTimeout(() => {
      pending = null;
      doPreview();
    }, previewDebounceMs);
  };

  // Friendly placeholder shown in the iframe when required fields are
  // missing. Saves the user from seeing the raw 422 / pydantic dump
  // mid-edit; the inline field errors (under each input) tell them
  // exactly which value to provide.
  const renderRequiredFieldsPlaceholder = () => {
    preview.srcdoc = `<!doctype html><html><head><meta charset="utf-8"></head><body style="margin:0;padding:48px 32px;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Helvetica,Arial,sans-serif;color:#1f2430;background:#fafbfc;line-height:1.55;">
      <div style="max-width:480px;">
        <div style="font-size:11px;letter-spacing:0.08em;text-transform:uppercase;color:#6b7280;margin-bottom:6px;">Preview paused</div>
        <h2 style="color:#1a4480;margin:0 0 12px;font-size:18px;font-weight:600;">A couple of details are needed first</h2>
        <p style="margin:0 0 8px;font-size:13px;color:#1f2430;">Fill in <strong>Issue Number</strong> and <strong>Issue Title</strong> in the <em>Issue Details</em> section to render the newsletter preview.</p>
        <p style="margin:8px 0 0;font-size:12px;color:#6b7280;">The Download button is disabled until both are set.</p>
      </div>
    </body></html>`;
    previewMeta.textContent = '';
  };

  const doPreview = async () => {
    setStatus('Rendering…');
    // Intercept missing required fields before POSTing — without this,
    // pydantic returns a 422 and the iframe would surface the raw
    // validation dump, which is hostile to a user mid-edit.
    if (!validateRequired()) {
      renderRequiredFieldsPlaceholder();
      setStatus('Issue Number and Issue Title are required.', 'error');
      return;
    }
    let issue;
    try {
      issue = buildIssue();
    } catch (e) {
      setStatus('Build error: ' + e.message, 'error');
      return;
    }
    try {
      const res = await fetch('/api/preview/html', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(issue),
      });
      if (!res.ok) {
        const detail = await res.text();
        const trimmed = detail.length > 240 ? detail.slice(0, 240) + '…' : detail;
        preview.srcdoc = `<!doctype html><html><body style="font-family:ui-monospace,monospace;padding:16px;color:#b91c1c;"><strong>${res.status} ${res.statusText}</strong><pre style="white-space:pre-wrap;">${trimmed.replace(/[<>&]/g, (c) => ({ '<': '&lt;', '>': '&gt;', '&': '&amp;' }[c]))}</pre></body></html>`;
        setStatus(`Preview ${res.status}`, 'error');
        return;
      }
      const html = await res.text();
      preview.srcdoc = html;
      previewMeta.textContent = `${html.length.toLocaleString()} bytes`;
      setStatus('Updated', 'ok');
    } catch (e) {
      setStatus('Network error: ' + e.message, 'error');
    }
  };

  // ───────── Required-field validation ───────────────────────────────
  // Issue Number and Issue Title gate the Download button: an issue
  // missing either of those is a half-built draft and would render with
  // a placeholder header in the .eml. Inline error messages appear under
  // each empty field; the button stays disabled until both are filled.
  const REQUIRED_FIELDS = [
    ['issue_number', 'Issue Number is required.'],
    ['issue_title', 'Issue Title is required.'],
  ];
  const validateRequired = () => {
    const errors = [];
    REQUIRED_FIELDS.forEach(([name, msg]) => {
      const el = form.elements[name];
      const v = (el?.value ?? '').toString().trim();
      const errEl = document.querySelector(`[data-error-for="${name}"]`);
      if (!v) {
        errors.push(name);
        if (errEl) {
          errEl.textContent = msg;
          errEl.hidden = false;
        }
      } else if (errEl) {
        errEl.hidden = true;
      }
    });
    const dlBtn = document.getElementById('download-eml');
    if (dlBtn) {
      const blocked = errors.length > 0;
      dlBtn.disabled = blocked;
      dlBtn.title = blocked
        ? 'Fill Issue Number and Issue Title before downloading.'
        : '';
    }
    return errors.length === 0;
  };

  // ───────── Download .eml ───────────────────────────────────────────
  const downloadEml = async () => {
    if (!validateRequired()) {
      setStatus('Issue Number and Issue Title are required.', 'error');
      return;
    }
    setStatus('Building .eml…');
    let issue;
    try { issue = buildIssue(); } catch (e) { setStatus('Build error: ' + e.message, 'error'); return; }
    try {
      const res = await fetch('/api/eml', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(issue),
      });
      if (!res.ok) {
        const detail = await res.text();
        setStatus(`Download ${res.status}: ${detail.slice(0, 120)}`, 'error');
        return;
      }
      const disp = res.headers.get('Content-Disposition') || '';
      const m = disp.match(/filename="([^"]+)"/);
      const filename = m ? m[1] : 'newsletter.eml';
      const blob = await res.blob();
      const url = URL.createObjectURL(blob);
      const a = document.createElement('a');
      a.href = url;
      a.download = filename;
      document.body.appendChild(a);
      a.click();
      a.remove();
      setTimeout(() => URL.revokeObjectURL(url), 1000);
      setStatus(`Saved ${filename}`, 'ok');
    } catch (e) {
      setStatus('Network error: ' + e.message, 'error');
    }
  };

  // ───────── Wiring ──────────────────────────────────────────────────
  document.getElementById('add-article').addEventListener('click', () => {
    addArticle();
    schedulePreview();
  });
  document.getElementById('add-static').addEventListener('click', () => {
    addStatic();
    schedulePreview();
  });
  document.getElementById('add-highlight').addEventListener('click', () => {
    addHighlight();
    schedulePreview();
  });
  document.getElementById('refresh-preview').addEventListener('click', (e) => {
    e.preventDefault();
    doPreview();
  });
  document.getElementById('download-eml').addEventListener('click', (e) => {
    e.preventDefault();
    downloadEml();
  });
  document.getElementById('reset-form').addEventListener('click', (e) => {
    e.preventDefault();
    if (!confirm('Reset the form? Unsaved entries will be lost.')) return;
    location.reload();
  });

  // ───────── Preview pane toggle + viewport switch ───────────────────
  const layout = document.querySelector('.layout');
  const previewPane = document.querySelector('.preview-pane');
  const togglePreview = document.getElementById('toggle-preview');

  const syncToggleState = () => {
    if (!togglePreview) return;
    const hidden = layout.classList.contains('preview-hidden');
    const label = hidden ? 'Show preview' : 'Hide preview';
    togglePreview.setAttribute('aria-pressed', hidden ? 'true' : 'false');
    togglePreview.setAttribute('aria-label', label);
    togglePreview.setAttribute('title', label);
  };

  togglePreview.addEventListener('click', () => {
    layout.classList.toggle('preview-hidden');
    syncToggleState();
  });

  // ───────── Form / preview pane resizer ─────────────────────────────
  // The user can drag the vertical splitter to retune how much room the
  // composer form gets vs. the preview iframe. Dragging close to the
  // right edge collapses the preview the same way clicking the
  // hide-preview icon does — so a "yank all the way over" produces the
  // familiar collapsed layout with the toggle still reachable.
  const resizer = document.querySelector('.layout-resizer');
  if (resizer) {
    // Below ~312px, the preview iframe is too narrow to render a faithful
    // newsletter view (the email content's narrowest @media breakpoint),
    // so we snap the pane closed once the user drags into that zone.
    const COLLAPSE_THRESHOLD_PX = 312; // distance from right edge
    const MIN_FORM_WIDTH = 280;
    const MIN_PREVIEW_WIDTH = 312;
    let dragPointerId = null;

    const setFormPaneWidth = (px) => {
      layout.style.setProperty('--form-pane-width', `${Math.round(px)}px`);
    };

    const onPointerMove = (e) => {
      if (dragPointerId !== e.pointerId) return;
      const rect = layout.getBoundingClientRect();
      const fromLeft = e.clientX - rect.left;
      const collapseAt = rect.width - COLLAPSE_THRESHOLD_PX;
      if (fromLeft >= collapseAt) {
        // Past the snap zone → behave like the hide-preview toggle.
        layout.classList.add('preview-hidden');
      } else {
        layout.classList.remove('preview-hidden');
        const clamped = Math.max(
          MIN_FORM_WIDTH,
          Math.min(rect.width - MIN_PREVIEW_WIDTH, fromLeft),
        );
        setFormPaneWidth(clamped);
      }
    };

    const endDrag = (e) => {
      if (dragPointerId === null) return;
      try { resizer.releasePointerCapture(dragPointerId); } catch (_) { /* ignore */ }
      dragPointerId = null;
      resizer.classList.remove('dragging');
      document.body.classList.remove('layout-resizing');
      syncToggleState();
      // Force a re-render so the iframe's @media queries re-evaluate
      // against the new pane width (preview width may have changed).
      doPreview();
    };

    resizer.addEventListener('pointerdown', (e) => {
      // Only the primary button (or any touch/pen contact) starts a drag.
      if (e.button !== undefined && e.button !== 0) return;
      e.preventDefault();
      dragPointerId = e.pointerId;
      try { resizer.setPointerCapture(e.pointerId); } catch (_) { /* ignore */ }
      resizer.classList.add('dragging');
      document.body.classList.add('layout-resizing');
    });
    resizer.addEventListener('pointermove', onPointerMove);
    resizer.addEventListener('pointerup', endDrag);
    resizer.addEventListener('pointercancel', endDrag);
  }

  const viewportButtons = document.querySelectorAll(
    '.viewport-switch button[data-viewport]',
  );
  viewportButtons.forEach((btn) => {
    btn.addEventListener('click', () => {
      const vp = btn.dataset.viewport;
      previewPane.classList.remove('vp-phone', 'vp-tablet', 'vp-desktop');
      previewPane.classList.add(`vp-${vp}`);
      viewportButtons.forEach((b) =>
        b.setAttribute('aria-pressed', b === btn ? 'true' : 'false'),
      );
      // Force a fresh render so the iframe re-evaluates its embedded
      // @media queries against the new width and the user sees the layout
      // recomputed end-to-end (not just a CSS resize of stale content).
      doPreview();
    });
  });

  // Strip Microsoft Safelinks the moment a URL field is touched so the
  // user sees the cleaned URL and the preview/eml never carry the wrapper.
  // Run before schedulePreview so the debounced render reads cleaned values.
  const maybeSanitizeUrlEvent = (e) => {
    const t = e.target;
    if (t && t.tagName === 'INPUT' && t.type === 'url') sanitizeUrlInput(t);
  };
  form.addEventListener('paste', (e) => {
    // Wait a tick so the pasted value lands in input.value.
    setTimeout(() => maybeSanitizeUrlEvent(e), 0);
  });
  form.addEventListener('change', maybeSanitizeUrlEvent);
  form.addEventListener('input', schedulePreview);
  form.addEventListener('change', schedulePreview);
  // Live-update the article header bar's headline preview while typing.
  form.addEventListener('input', (e) => {
    const t = e.target;
    if (t && t.matches && t.matches('[data-field="headline"]')) {
      const card = t.closest('.article-card');
      if (card) updateArticleHeaderPreview(card);
    }
  });
  // Recompute the displayed enumerated year whenever the issue date
  // changes; the dependency on initial_publication_date is refreshed
  // on settings load/save (see loadSettings / saveSettings below).
  form.addEventListener('input', (e) => {
    if (e.target && e.target.name === 'issue_date') updateYearDisplay();
  });
  form.addEventListener('change', (e) => {
    if (e.target && e.target.name === 'issue_date') updateYearDisplay();
  });
  // Re-validate required fields on every keystroke so the inline error
  // hides the moment the user types something and the Download button
  // re-enables in lockstep.
  form.addEventListener('input', (e) => {
    if (e.target && (e.target.name === 'issue_number' || e.target.name === 'issue_title')) {
      validateRequired();
    }
  });

  // ───────── Settings dialog ─────────────────────────────────────────
  // App-wide defaults loaded from /api/settings. Used to seed the form on
  // first load (so the user can edit defaults once and have every new
  // draft start there) and exposed via a modal dialog.
  const settingsDialog = document.getElementById('settings-dialog');
  const settingsForm = document.getElementById('settings-form');
  const settingsStatus = document.getElementById('settings-status');
  const SETTINGS_FIELDS = [
    'from_name', 'from_email', 'to_email', 'reply_to',
    'unsubscribe_url', 'masthead_logo_url',
    'initial_publication_date',
    'default_intro_html', 'default_footer_html',
    'preview_debounce_ms', 'auto_archive_on_download',
    'include_read_more_link',
  ];

  const setSettingsStatus = (msg, kind = 'info') => {
    if (!settingsStatus) return;
    settingsStatus.textContent = msg || '';
    settingsStatus.dataset.kind = kind;
  };

  const fillSettingsForm = (s) => {
    SETTINGS_FIELDS.forEach((k) => {
      const el = settingsForm.elements[k];
      if (!el) return;
      const v = s[k];
      if (el.type === 'checkbox') el.checked = !!v;
      else el.value = v == null ? '' : v;
    });
  };

  const collectSettings = () => {
    const out = {};
    SETTINGS_FIELDS.forEach((k) => {
      const el = settingsForm.elements[k];
      if (!el) return;
      if (el.type === 'checkbox') {
        out[k] = el.checked;
      } else if (el.type === 'number') {
        const n = el.value === '' ? null : Number(el.value);
        if (n !== null && !Number.isNaN(n)) out[k] = n;
      } else {
        const v = trim(el.value);
        if (v !== '') out[k] = v;
      }
    });
    return out;
  };

  // Seed the main composer form from saved settings. The email envelope
  // (from_*/to_email/reply_to) and masthead logo are app-wide only —
  // they are NOT set into form fields here because the form no longer
  // collects them; buildIssue() pulls them straight from currentSettings
  // when assembling the payload. We still seed the per-issue fields that
  // do exist on the form (intro_html, footer_html, unsubscribe_url) so
  // saved defaults appear as starting values for a new draft.
  const applySettingsToForm = (s) => {
    if (!s) return;
    const set = (name, val) => {
      const el = form.elements[name];
      if (el && val !== null && val !== undefined) el.value = String(val);
    };
    if (s.unsubscribe_url) set('unsubscribe_url', s.unsubscribe_url);
    if (s.default_intro_html) set('intro_html', s.default_intro_html);
    if (s.default_footer_html) set('footer_html', s.default_footer_html);
    if (typeof s.preview_debounce_ms === 'number' && s.preview_debounce_ms >= 0) {
      previewDebounceMs = s.preview_debounce_ms;
    }
  };

  const loadSettings = async () => {
    try {
      const res = await fetch('/api/settings');
      if (!res.ok) return null;
      const data = await res.json();
      currentSettings = data;
      updateYearDisplay();
      return data;
    } catch (e) {
      return null;
    }
  };

  const openSettingsDialog = async () => {
    setSettingsStatus('');
    if (!currentSettings) await loadSettings();
    if (currentSettings) fillSettingsForm(currentSettings);
    if (typeof settingsDialog.showModal === 'function') {
      settingsDialog.showModal();
    } else {
      settingsDialog.setAttribute('open', '');
    }
  };

  const closeSettingsDialog = () => {
    if (typeof settingsDialog.close === 'function') {
      settingsDialog.close();
    } else {
      settingsDialog.removeAttribute('open');
    }
  };

  const saveSettings = async (e) => {
    if (e) e.preventDefault();
    const payload = collectSettings();
    setSettingsStatus('Saving…');
    try {
      const res = await fetch('/api/settings', {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(payload),
      });
      if (!res.ok) {
        let detail = '';
        try { detail = (await res.json()).detail || ''; } catch { /* ignore */ }
        setSettingsStatus(`Save ${res.status}: ${detail || res.statusText}`, 'error');
        return;
      }
      currentSettings = await res.json();
      // Reflect any preview-debounce change immediately.
      if (typeof currentSettings.preview_debounce_ms === 'number') {
        previewDebounceMs = currentSettings.preview_debounce_ms;
      }
      // The enumerated year may now have changed (initial publication date).
      updateYearDisplay();
      schedulePreview();
      setSettingsStatus('Saved', 'ok');
      setTimeout(closeSettingsDialog, 350);
    } catch (err) {
      setSettingsStatus('Network error: ' + err.message, 'error');
    }
  };

  // ───────── Issue load (for templates / drafts) ────────────────────
  // Replace the form contents with a NewsletterIssue payload. Used by
  // the templates dialog ("Use") and (future) drafts dialog ("Load").
  // Missing fields fall back to the issue's current values, except for
  // repeating sections — they are fully replaced (no merge).
  const loadIssueIntoForm = (issue) => {
    if (!issue || typeof issue !== 'object') return;
    // Note: `issue_year` is intentionally absent — it is now derived from
    // the issue date + AppSettings.initial_publication_date and shown via
    // the read-only #issue-year-display output, not edited in the form.
    // Email envelope (from_*/to_email/reply_to) and masthead_logo_url are
    // app-wide settings only, so they are not loaded into form fields
    // either; buildIssue() picks them up from currentSettings each time.
    const SCALAR_FIELDS = [
      'issue_number', 'issue_title', 'issue_date',
      'display_date', 'intro_html', 'subject',
      'footer_html', 'unsubscribe_url',
    ];
    SCALAR_FIELDS.forEach((name) => {
      const el = form.elements[name];
      if (!el) return;
      const v = issue[name];
      if (v == null) {
        if (el.type !== 'checkbox') el.value = '';
        return;
      }
      el.value = String(v);
    });

    // Articles — clear, then re-add.
    articlesContainer.innerHTML = '';
    (Array.isArray(issue.articles) ? issue.articles : []).forEach((a) => {
      addArticle(a || {});
    });
    if (articlesContainer.children.length === 0) {
      addArticle({}); // keep at least one empty card so the user can start typing
    }

    // Static blocks — clear, then re-add.
    staticContainer.innerHTML = '';
    (Array.isArray(issue.static_blocks) ? issue.static_blocks : []).forEach((b) => {
      addStatic(b || {});
    });

    // Highlights — clear, then re-add. Must run after articles so the
    // refreshHighlightOptions select gets the right option list.
    highlightsContainer.innerHTML = '';
    (Array.isArray(issue.highlights) ? issue.highlights : []).forEach((h) => {
      addHighlight(h || {});
    });

    // Podcast — toggle the section and fill if present.
    if (issue.podcast && typeof issue.podcast === 'object') {
      podcastEnabled.checked = true;
      podcastFields.hidden = false;
      renderPodcastFields();
      const pod = issue.podcast;
      ['episode_number', 'guest', 'title', 'zoomcast_url', 'podcast_url', 'youtube_video_url', 'summary'].forEach((k) => {
        const el = podcastNode.querySelector(`[data-field="${k}"]`);
        if (el && pod[k] != null) el.value = String(pod[k]);
      });
      if (Array.isArray(pod.timecodes)) {
        pod.timecodes.forEach((tc) => addTimecode(tc || {}));
      }
    } else {
      podcastEnabled.checked = false;
      podcastFields.hidden = true;
      podcastFields.innerHTML = '';
      podcastNode = null;
      timecodesContainer = null;
    }

    renumber();
    updateYearDisplay();
    schedulePreview();
  };

  // ───────── Templates dialog ────────────────────────────────────────
  // Persisted presets in the DB. The dialog lists them, lets the user
  // pick one to seed the form ("Use"), delete user-owned ones, and save
  // the current form as a new template.
  const templatesDialog = document.getElementById('templates-dialog');
  const templatesList = document.getElementById('templates-list');
  const templatesCreateForm = document.getElementById('templates-create-form');
  const templatesStatus = document.getElementById('templates-status');

  const setTemplatesStatus = (msg, kind = 'info') => {
    if (!templatesStatus) return;
    templatesStatus.textContent = msg || '';
    templatesStatus.dataset.kind = kind;
  };

  const escapeHtml = (s) =>
    String(s == null ? '' : s).replace(/[<>&"']/g, (c) =>
      ({ '<': '&lt;', '>': '&gt;', '&': '&amp;', '"': '&quot;', "'": '&#39;' }[c]));

  const renderTemplatesList = (items) => {
    if (!Array.isArray(items) || items.length === 0) {
      templatesList.innerHTML = '<li class="hint">No templates yet.</li>';
      return;
    }
    templatesList.innerHTML = items.map((t) => `
      <li data-template-id="${t.id}" class="template-row${t.builtin ? ' builtin' : ''}">
        <div class="template-meta">
          <strong>${escapeHtml(t.name)}</strong>
          ${t.builtin ? '<span class="badge">built-in</span>' : ''}
          ${t.description ? `<small>${escapeHtml(t.description)}</small>` : ''}
        </div>
        <div class="template-actions">
          <button type="button" class="btn" data-action="use">Use</button>
          ${t.builtin ? '' : '<button type="button" class="btn ghost" data-action="delete">Delete</button>'}
        </div>
      </li>
    `).join('');
  };

  const refreshTemplatesList = async () => {
    setTemplatesStatus('');
    try {
      const res = await fetch('/api/templates');
      if (!res.ok) {
        setTemplatesStatus(`List failed: ${res.status}`, 'error');
        return;
      }
      renderTemplatesList(await res.json());
    } catch (e) {
      setTemplatesStatus('Network error: ' + e.message, 'error');
    }
  };

  const useTemplate = async (id) => {
    setTemplatesStatus('Loading…');
    try {
      const res = await fetch(`/api/templates/${id}`);
      if (!res.ok) {
        setTemplatesStatus(`Load ${res.status}`, 'error');
        return;
      }
      const tpl = await res.json();
      loadIssueIntoForm(tpl.payload || {});
      // If the template has no issue_date, seed today so the form is valid.
      const dateEl = form.elements['issue_date'];
      if (dateEl && !dateEl.value) {
        dateEl.value = new Date().toISOString().slice(0, 10);
      }
      setTemplatesStatus(`Loaded "${tpl.name}"`, 'ok');
      setTimeout(closeTemplatesDialog, 350);
    } catch (e) {
      setTemplatesStatus('Network error: ' + e.message, 'error');
    }
  };

  const deleteTemplate = async (id, name) => {
    if (!confirm(`Delete template "${name}"? This cannot be undone.`)) return;
    setTemplatesStatus('Deleting…');
    try {
      const res = await fetch(`/api/templates/${id}`, { method: 'DELETE' });
      if (!res.ok) {
        let detail = '';
        try { detail = (await res.json()).detail || ''; } catch { /* ignore */ }
        setTemplatesStatus(`Delete ${res.status}: ${detail || res.statusText}`, 'error');
        return;
      }
      await refreshTemplatesList();
      setTemplatesStatus('Deleted.', 'ok');
    } catch (e) {
      setTemplatesStatus('Network error: ' + e.message, 'error');
    }
  };

  templatesList.addEventListener('click', (e) => {
    const btn = e.target.closest('button[data-action]');
    if (!btn) return;
    const row = btn.closest('[data-template-id]');
    if (!row) return;
    const id = Number(row.dataset.templateId);
    if (btn.dataset.action === 'use') useTemplate(id);
    else if (btn.dataset.action === 'delete') {
      const name = row.querySelector('strong')?.textContent || '';
      deleteTemplate(id, name);
    }
  });

  const openTemplatesDialog = async () => {
    setTemplatesStatus('');
    templatesCreateForm.reset();
    await refreshTemplatesList();
    if (typeof templatesDialog.showModal === 'function') {
      templatesDialog.showModal();
    } else {
      templatesDialog.setAttribute('open', '');
    }
  };

  const closeTemplatesDialog = () => {
    if (typeof templatesDialog.close === 'function') {
      templatesDialog.close();
    } else {
      templatesDialog.removeAttribute('open');
    }
  };

  templatesCreateForm.addEventListener('submit', async (e) => {
    e.preventDefault();
    const fd = new FormData(templatesCreateForm);
    const name = trim(fd.get('name') || '');
    const description = trim(fd.get('description') || '') || null;
    if (!name) {
      setTemplatesStatus('Name required.', 'error');
      return;
    }
    let payload;
    try { payload = buildIssue(); }
    catch (err) { setTemplatesStatus('Build error: ' + err.message, 'error'); return; }
    setTemplatesStatus('Saving…');
    try {
      const res = await fetch('/api/templates', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ name, description, payload }),
      });
      if (!res.ok) {
        let detail = '';
        try { detail = (await res.json()).detail || ''; } catch { /* ignore */ }
        setTemplatesStatus(`Save ${res.status}: ${detail || res.statusText}`, 'error');
        return;
      }
      templatesCreateForm.reset();
      await refreshTemplatesList();
      setTemplatesStatus('Saved.', 'ok');
    } catch (err) {
      setTemplatesStatus('Network error: ' + err.message, 'error');
    }
  });

  document.getElementById('open-templates').addEventListener('click', openTemplatesDialog);
  document.getElementById('templates-close').addEventListener('click', (e) => {
    e.preventDefault();
    closeTemplatesDialog();
  });

  // ───────── Drafts dialog ──────────────────────────────────────────
  // Saved-but-incomplete issues. Differs from templates: no builtin flag,
  // no name uniqueness, list ordered by recency.
  const draftsDialog = document.getElementById('drafts-dialog');
  const draftsList = document.getElementById('drafts-list');
  const draftsCreateForm = document.getElementById('drafts-create-form');
  const draftsStatus = document.getElementById('drafts-status');

  const setDraftsStatus = (msg, kind = 'info') => {
    if (!draftsStatus) return;
    draftsStatus.textContent = msg || '';
    draftsStatus.dataset.kind = kind;
  };

  const formatDraftTimestamp = (iso) => {
    if (!iso) return '';
    const d = new Date(iso);
    if (Number.isNaN(d.getTime())) return iso;
    return d.toLocaleString();
  };

  const renderDraftsList = (items) => {
    if (!Array.isArray(items) || items.length === 0) {
      draftsList.innerHTML = '<li class="hint">No drafts yet.</li>';
      return;
    }
    draftsList.innerHTML = items.map((d) => `
      <li data-draft-id="${d.id}" class="template-row">
        <div class="template-meta">
          <strong>${escapeHtml(d.name)}</strong>
          <small>updated ${escapeHtml(formatDraftTimestamp(d.updated_at))}</small>
        </div>
        <div class="template-actions">
          <button type="button" class="btn" data-action="load">Load</button>
          <button type="button" class="btn ghost" data-action="delete">Delete</button>
        </div>
      </li>
    `).join('');
  };

  const refreshDraftsList = async () => {
    setDraftsStatus('');
    try {
      const res = await fetch('/api/drafts');
      if (!res.ok) {
        setDraftsStatus(`List failed: ${res.status}`, 'error');
        return;
      }
      renderDraftsList(await res.json());
    } catch (e) {
      setDraftsStatus('Network error: ' + e.message, 'error');
    }
  };

  const loadDraft = async (id) => {
    setDraftsStatus('Loading…');
    try {
      const res = await fetch(`/api/drafts/${id}`);
      if (!res.ok) {
        setDraftsStatus(`Load ${res.status}`, 'error');
        return;
      }
      const draft = await res.json();
      loadIssueIntoForm(draft.payload || {});
      const dateEl = form.elements['issue_date'];
      if (dateEl && !dateEl.value) {
        dateEl.value = new Date().toISOString().slice(0, 10);
      }
      setDraftsStatus(`Loaded "${draft.name}"`, 'ok');
      setTimeout(closeDraftsDialog, 350);
    } catch (e) {
      setDraftsStatus('Network error: ' + e.message, 'error');
    }
  };

  const deleteDraft = async (id, name) => {
    if (!confirm(`Delete draft "${name}"? This cannot be undone.`)) return;
    setDraftsStatus('Deleting…');
    try {
      const res = await fetch(`/api/drafts/${id}`, { method: 'DELETE' });
      if (!res.ok) {
        let detail = '';
        try { detail = (await res.json()).detail || ''; } catch { /* ignore */ }
        setDraftsStatus(`Delete ${res.status}: ${detail || res.statusText}`, 'error');
        return;
      }
      await refreshDraftsList();
      setDraftsStatus('Deleted.', 'ok');
    } catch (e) {
      setDraftsStatus('Network error: ' + e.message, 'error');
    }
  };

  draftsList.addEventListener('click', (e) => {
    const btn = e.target.closest('button[data-action]');
    if (!btn) return;
    const row = btn.closest('[data-draft-id]');
    if (!row) return;
    const id = Number(row.dataset.draftId);
    if (btn.dataset.action === 'load') loadDraft(id);
    else if (btn.dataset.action === 'delete') {
      const name = row.querySelector('strong')?.textContent || '';
      deleteDraft(id, name);
    }
  });

  const openDraftsDialog = async () => {
    setDraftsStatus('');
    draftsCreateForm.reset();
    await refreshDraftsList();
    if (typeof draftsDialog.showModal === 'function') {
      draftsDialog.showModal();
    } else {
      draftsDialog.setAttribute('open', '');
    }
  };

  const closeDraftsDialog = () => {
    if (typeof draftsDialog.close === 'function') {
      draftsDialog.close();
    } else {
      draftsDialog.removeAttribute('open');
    }
  };

  draftsCreateForm.addEventListener('submit', async (e) => {
    e.preventDefault();
    const fd = new FormData(draftsCreateForm);
    const name = trim(fd.get('name') || '');
    if (!name) {
      setDraftsStatus('Name required.', 'error');
      return;
    }
    let payload;
    try { payload = buildIssue(); }
    catch (err) { setDraftsStatus('Build error: ' + err.message, 'error'); return; }
    setDraftsStatus('Saving…');
    try {
      const res = await fetch('/api/drafts', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ name, payload }),
      });
      if (!res.ok) {
        let detail = '';
        try { detail = (await res.json()).detail || ''; } catch { /* ignore */ }
        setDraftsStatus(`Save ${res.status}: ${detail || res.statusText}`, 'error');
        return;
      }
      draftsCreateForm.reset();
      await refreshDraftsList();
      setDraftsStatus('Saved.', 'ok');
    } catch (err) {
      setDraftsStatus('Network error: ' + err.message, 'error');
    }
  });

  document.getElementById('open-drafts').addEventListener('click', openDraftsDialog);
  document.getElementById('drafts-close').addEventListener('click', (e) => {
    e.preventDefault();
    closeDraftsDialog();
  });

  // ───────── Archive dialog ─────────────────────────────────────────
  // Read-only list of finalized issues. Auto-archive happens server-side
  // on POST /api/eml when settings.auto_archive_on_download is on.
  // Each row offers Download (re-fetch the stored .eml) + Delete.
  const archiveDialog = document.getElementById('archive-dialog');
  const archiveList = document.getElementById('archive-list');
  const archiveStatus = document.getElementById('archive-status');

  const setArchiveStatus = (msg, kind = 'info') => {
    if (!archiveStatus) return;
    archiveStatus.textContent = msg || '';
    archiveStatus.dataset.kind = kind;
  };

  const formatBytes = (n) => {
    if (typeof n !== 'number' || !Number.isFinite(n)) return '';
    if (n < 1024) return `${n} B`;
    if (n < 1024 * 1024) return `${(n / 1024).toFixed(1)} KB`;
    return `${(n / (1024 * 1024)).toFixed(2)} MB`;
  };

  const renderArchiveList = (items) => {
    if (!Array.isArray(items) || items.length === 0) {
      archiveList.innerHTML = '<li class="hint">No archived issues yet. Download a .eml with auto-archive on to populate.</li>';
      return;
    }
    archiveList.innerHTML = items.map((a) => `
      <li data-archive-id="${a.id}" class="template-row">
        <div class="template-meta">
          <strong>${escapeHtml(a.subject || a.slug)}</strong>
          <small>${escapeHtml(a.filename)} · ${formatBytes(a.size_bytes)} · ${escapeHtml(formatDraftTimestamp(a.created_at))}</small>
        </div>
        <div class="template-actions">
          <button type="button" class="btn" data-action="download">Download</button>
          <button type="button" class="btn ghost" data-action="delete">Delete</button>
        </div>
      </li>
    `).join('');
  };

  const refreshArchiveList = async () => {
    setArchiveStatus('');
    try {
      const res = await fetch('/api/archive');
      if (!res.ok) {
        setArchiveStatus(`List failed: ${res.status}`, 'error');
        return;
      }
      renderArchiveList(await res.json());
    } catch (e) {
      setArchiveStatus('Network error: ' + e.message, 'error');
    }
  };

  const downloadArchive = async (id, filename) => {
    setArchiveStatus('Downloading…');
    try {
      const res = await fetch(`/api/archive/${id}/download`);
      if (!res.ok) {
        setArchiveStatus(`Download ${res.status}`, 'error');
        return;
      }
      const disp = res.headers.get('Content-Disposition') || '';
      const m = disp.match(/filename="([^"]+)"/);
      const name = m ? m[1] : (filename || 'newsletter.eml');
      const blob = await res.blob();
      const url = URL.createObjectURL(blob);
      const a = document.createElement('a');
      a.href = url;
      a.download = name;
      document.body.appendChild(a);
      a.click();
      a.remove();
      setTimeout(() => URL.revokeObjectURL(url), 1000);
      setArchiveStatus(`Saved ${name}`, 'ok');
    } catch (e) {
      setArchiveStatus('Network error: ' + e.message, 'error');
    }
  };

  const deleteArchive = async (id, label) => {
    if (!confirm(`Delete archived issue "${label}"? This cannot be undone.`)) return;
    setArchiveStatus('Deleting…');
    try {
      const res = await fetch(`/api/archive/${id}`, { method: 'DELETE' });
      if (!res.ok) {
        let detail = '';
        try { detail = (await res.json()).detail || ''; } catch { /* ignore */ }
        setArchiveStatus(`Delete ${res.status}: ${detail || res.statusText}`, 'error');
        return;
      }
      await refreshArchiveList();
      setArchiveStatus('Deleted.', 'ok');
    } catch (e) {
      setArchiveStatus('Network error: ' + e.message, 'error');
    }
  };

  archiveList.addEventListener('click', (e) => {
    const btn = e.target.closest('button[data-action]');
    if (!btn) return;
    const row = btn.closest('[data-archive-id]');
    if (!row) return;
    const id = Number(row.dataset.archiveId);
    const label = row.querySelector('strong')?.textContent || '';
    const filename = row.querySelector('small')?.textContent?.split(' · ')[0] || '';
    if (btn.dataset.action === 'download') downloadArchive(id, filename);
    else if (btn.dataset.action === 'delete') deleteArchive(id, label);
  });

  const openArchiveDialog = async () => {
    setArchiveStatus('');
    await refreshArchiveList();
    if (typeof archiveDialog.showModal === 'function') {
      archiveDialog.showModal();
    } else {
      archiveDialog.setAttribute('open', '');
    }
  };

  const closeArchiveDialog = () => {
    if (typeof archiveDialog.close === 'function') {
      archiveDialog.close();
    } else {
      archiveDialog.removeAttribute('open');
    }
  };

  document.getElementById('open-archive').addEventListener('click', openArchiveDialog);
  document.getElementById('archive-close').addEventListener('click', (e) => {
    e.preventDefault();
    closeArchiveDialog();
  });

  document.getElementById('open-settings').addEventListener('click', openSettingsDialog);
  document.getElementById('settings-cancel').addEventListener('click', (e) => {
    e.preventDefault();
    closeSettingsDialog();
  });
  document.getElementById('settings-close').addEventListener('click', (e) => {
    e.preventDefault();
    closeSettingsDialog();
  });
  settingsForm.addEventListener('submit', saveSettings);

  // ───────── Initial seed: today's date + a starter article + sponsor block ──
  const today = new Date().toISOString().slice(0, 10);
  form.elements['issue_date'].value = today;
  addArticle({
    headline: '',
    url: '',
    read_more_label: 'Read more',
  });
  addStatic({
    title: 'Sponsor / book / conference',
    html: '<p>Carry-forward block — duplicate from last issue.</p>',
    enabled: true,
  });
  // Pull settings before the first preview so any saved defaults
  // (from_name/from_email/etc.) end up in the rendered .eml.
  loadSettings().then((s) => {
    if (s) applySettingsToForm(s);
    validateRequired();
    doPreview();
  });
})();
