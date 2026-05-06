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
  const renumber = () => {
    [...articlesContainer.querySelectorAll('fieldset.card')].forEach((card, i) => {
      const idx = card.querySelector('.idx');
      if (idx) idx.textContent = `#${i + 1}`;
    });
    [...staticContainer.querySelectorAll('fieldset.card')].forEach((card, i) => {
      const idx = card.querySelector('.idx');
      if (idx) idx.textContent = `#${i + 1}`;
    });
    articlesCount.textContent = articlesContainer.querySelectorAll('fieldset.card').length;
    staticCount.textContent = staticContainer.querySelectorAll('fieldset.card').length;
    highlightsCount.textContent = highlightsContainer.querySelectorAll('.row').length;
    refreshHighlightOptions();
  };

  const addArticle = (data = {}) => {
    const node = articleTpl.content.firstElementChild.cloneNode(true);
    Object.entries(data).forEach(([k, v]) => {
      const el = node.querySelector(`[data-field="${k}"]`);
      if (el && v !== null && v !== undefined) el.value = v;
    });
    node.querySelector('.btn-remove').addEventListener('click', () => {
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
    articlesContainer.appendChild(node);
    renumber();
    return node;
  };

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
  const refreshHighlightOptions = () => {
    const articles = [...articlesContainer.querySelectorAll('fieldset.card')];
    const optionsHtml = articles.map((a, i) => {
      const headline = a.querySelector('[data-field="headline"]').value || `(article #${i + 1})`;
      const truncated = headline.length > 60 ? headline.slice(0, 57) + '…' : headline;
      return `<option value="${i}">#${i + 1} — ${truncated.replace(/[<>&"']/g, (c) =>
        ({ '<': '&lt;', '>': '&gt;', '&': '&amp;', '"': '&quot;', "'": '&#39;' }[c]))}</option>`;
    }).join('');
    highlightsContainer.querySelectorAll('select[data-field="article_index"]').forEach((sel) => {
      const prev = sel.value;
      sel.innerHTML = optionsHtml;
      if (prev !== '' && articles[Number(prev)]) sel.value = prev;
    });
  };

  const addHighlight = (data = {}) => {
    const node = highlightTpl.content.firstElementChild.cloneNode(true);
    highlightsContainer.appendChild(node);
    refreshHighlightOptions();
    if (data.article_index !== undefined && data.article_index !== null) {
      node.querySelector('[data-field="article_index"]').value = String(data.article_index);
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
      if (k === 'issue_year' || k === 'issue_number') {
        root[k] = trimmed === '' ? null : Number(trimmed);
      } else {
        root[k] = trimmed;
      }
    }
    const issue = stripEmpty(root);

    // Articles — drop incomplete rows so the preview keeps rendering while
    // Alain is mid-edit (headline + url are required by the Pydantic model).
    // Keep a card-index → output-index map so highlight refs (which carry
    // DOM card indices) can be remapped after filtering.
    const cardArticles = [...articlesContainer.querySelectorAll('fieldset.card')]
      .map((card) => stripEmpty(collectFields(card)));
    const cardIdxToOutIdx = new Map();
    issue.articles = [];
    cardArticles.forEach((a, i) => {
      if (a.headline && a.url) {
        cardIdxToOutIdx.set(i, issue.articles.length);
        issue.articles.push(a);
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

    // Highlights — refs carry DOM card indices; remap to the post-filter
    // article indices and drop refs whose article was incomplete.
    issue.highlights = [...highlightsContainer.querySelectorAll('.row')]
      .map((row) => {
        const fields = collectFields(row);
        const idx = fields.article_index;
        if (idx === '' || idx === null || idx === undefined) return null;
        const remapped = cardIdxToOutIdx.get(Number(idx));
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
  const schedulePreview = () => {
    setStatus('Editing…');
    if (pending) clearTimeout(pending);
    pending = setTimeout(() => {
      pending = null;
      doPreview();
    }, 400);
  };

  const doPreview = async () => {
    setStatus('Rendering…');
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

  // ───────── Download .eml ───────────────────────────────────────────
  const downloadEml = async () => {
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
  togglePreview.addEventListener('click', () => {
    const hidden = layout.classList.toggle('preview-hidden');
    togglePreview.textContent = hidden ? 'Show preview' : 'Hide preview';
    togglePreview.setAttribute('aria-pressed', hidden ? 'true' : 'false');
  });

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
  doPreview();
})();
