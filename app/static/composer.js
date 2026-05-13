/* SDC Composer — single-page form with live preview.
 *
 * The form is shaped to fill a NewsletterIssue (see app/models.py). On every
 * change we debounce for ~400ms then POST the assembled JSON to
 * /api/preview/html and pipe the rendered HTML into the right-hand iframe.
 * "Download .eml" submits a hidden form so the browser handles the file save.
 */
(() => {
  // Session-expiry fetch wrapper. Easy Auth fronts the whole app; once
  // the session cookie expires, the auth proxy responds to JSON-shaped
  // API calls with a 401 and to HTML navigations with a 302 chain that
  // ends at login.microsoftonline.com. Either way, leaving the page
  // partly-alive after expiry surfaces as a confusing string of
  // background errors — including the "Unsafe attempt to load URL …
  // from frame …" complaint when a cross-origin redirect gets caught
  // by the browser's iframe security policy. Catch the 401 once at
  // the fetch boundary and trigger a top-level reload: Easy Auth then
  // owns the OAuth dance at the parent-navigation level where it's
  // allowed to redirect cross-origin.
  const _nativeFetch = window.fetch.bind(window);
  let _sessionReloadInFlight = false;
  window.fetch = (input, init) =>
    _nativeFetch(input, init).then((res) => {
      if (res.status === 401 && !_sessionReloadInFlight) {
        _sessionReloadInFlight = true;
        window.location.reload();
      }
      return res;
    });

  const form = document.getElementById('composer');
  const preview = document.getElementById('preview');
  const previewMeta = document.getElementById('preview-meta');
  const status = document.getElementById('status');

  // Form is now organized as a fixed Issue Details + a sortable region
  // (#sortable-sections) holding one or more Articles / Podcast / Static
  // blocks <details>, then fixed Bottom highlights and Footer. Multiple
  // instances of any sortable section type are allowed; their order is
  // user-controlled via drag-and-drop on the section summary bars and
  // round-trips through draft save/load via the `form_sections` payload
  // metadata array.
  const sortableSections = document.getElementById('sortable-sections');
  const highlightsContainer = document.getElementById('highlights');
  const highlightsCount = document.getElementById('highlights-count');

  const articleTpl = document.getElementById('article-template');
  const podcastTpl = document.getElementById('podcast-template');
  const timecodeTpl = document.getElementById('timecode-template');
  const staticTpl = document.getElementById('static-template');
  const highlightTpl = document.getElementById('highlight-template');
  const articlesSectionTpl = document.getElementById('articles-section-template');
  const podcastSectionTpl = document.getElementById('podcast-section-template');
  const staticBlocksSectionTpl = document.getElementById('static-blocks-section-template');

  // Stable per-section ids so refs (e.g. saved draft form_sections) survive
  // page reloads even though the underlying DOM is rebuilt from templates.
  let nextSectionId = 0;
  const newSectionId = () => `s-${++nextSectionId}`;

  // Helpers for walking sortable sections of a given type in DOM order.
  const sortableSectionsAll = () => [...sortableSections.querySelectorAll(':scope > .sortable-section')];
  const sortableSectionsOfType = (type) =>
    sortableSectionsAll().filter((s) => s.dataset.sectionType === type);
  const allArticleCards = () =>
    [...sortableSections.querySelectorAll(':scope > .sortable-section[data-section-type="articles"] .articles-container .article-card')];
  const allStaticCards = () =>
    [...sortableSections.querySelectorAll(':scope > .sortable-section[data-section-type="static_blocks"] .statics-container fieldset.card')];

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
  // ───────── Small shared utilities ──────────────────────────────────
  // statusSetter(el) returns a (msg, kind) → void function that writes
  // textContent + data-kind on the supplied element.
  const statusSetter = (el) => (msg, kind = 'info') => {
    if (!el) return;
    el.textContent = msg || '';
    el.dataset.kind = kind;
  };

  const openDialog = (d) => {
    if (!d) return;
    if (typeof d.showModal === 'function') d.showModal();
    else d.setAttribute('open', '');
  };
  const closeDialog = (d) => {
    if (!d) return;
    if (typeof d.close === 'function') d.close();
    else d.removeAttribute('open');
  };

  // readJsonDetail(res) → string. Convenience over the repeated
  //   let detail = ''; try { detail = (await res.json()).detail || ''; } catch {}
  // pattern at non-OK fetch sites. Swallows JSON parse errors (the
  // server might respond with a plain-text body) and returns '' so the
  // caller can use `detail || res.statusText` for the user-facing
  // message.
  const readJsonDetail = async (res) => {
    try {
      const body = await res.json();
      return (body && body.detail) || '';
    } catch {
      return '';
    }
  };

  /**
   * Surface a fetch error from /api/media/upload into a status setter.
   * Handles 429 Retry-After and generic detail extraction.
   */
  const surfaceUploadError = async (res, setter) => {
    const detail = await readJsonDetail(res);
    if (res.status === 429) {
      const retry = res.headers.get('Retry-After') || '?';
      setter(`Rate limited — retry in ${retry}s.`, 'error');
    } else {
      setter(`${res.status}: ${detail || res.statusText}`, 'error');
    }
  };

  /**
   * POST a file to /api/media/upload. Returns the MediaItem or throws.
   */
  const uploadMedia = async ({ file, kind }) => {
    const fd = new FormData();
    fd.append('kind', kind || 'logo');
    fd.append('file', file);
    const res = await fetch('/api/media/upload', { method: 'POST', body: fd });
    if (!res.ok) throw res;
    return res.json();
  };

  const setStatus = statusSetter(status);

  const quillInstances = new Map();

  /**
   * Initialize a Quill editor on a [data-quill-field] scope.
   * Syncs content back to the hidden sibling textarea on every change.
   */
  const wireQuillEditor = (scope) => {
    if (!scope) return;
    const container = scope.querySelector('.quill-container');
    const textarea = scope.querySelector('textarea');
    if (!container || !textarea) return;

    // Reuse existing Quill instance if we already wired this textarea.
    if (quillInstances.has(textarea)) return quillInstances.get(textarea);

    const isStatic = scope.dataset.quillStatic === 'true';
    const toolbarOptions = isStatic
      ? [['bold', 'italic'], [{ header: 2 }], ['link', 'image']]
      : [['bold', 'italic'], ['link']];

    const quill = new Quill(container, {
      theme: 'snow',
      modules: {
        toolbar: toolbarOptions,
      },
      placeholder: textarea.placeholder || '',
    });

    // Prevent double-click from potentially triggering system-level or
    // browser-level formatting (like Bold on macOS in some contexts).
    // Selection itself is handled by the browser before this fires or
    // via standard event bubbling; we just want to stop the event from
    // reaching anything that might interpret it as a format toggle.
    // Use capture phase (true) to intercept before Quill or other
    // listeners see it.
    quill.root.addEventListener('dblclick', (e) => {
      e.stopPropagation();
    }, true);

    // Sync Quill -> Textarea
    quill.on('text-change', () => {
      let html = quill.root.innerHTML;
      // Quill's "empty" is <p><br></p>; treat as blank.
      if (html === '<p><br></p>') html = '';

      if (html) {
        // Clean up repetitive <br> tags that Quill sometimes inserts or
        // that accumulate during paste/edit.
        html = html.replace(/(<br\s*\/?>){3,}/gi, '<br><br>');
      }

      if (scope.dataset.quillUnwrap === 'true' && html) {
        // Unwrap paragraphs for inline fields (Pull quote / Comment).
        // Convert internal <p> tags to <br> to preserve the line break
        // without the vertical margin of a paragraph block.
        html = html
          .replace(/^<p>/i, '')
          .replace(/<\/p>$/i, '')
          .replace(/<\/p><p>/gi, '<br>');
        // Final pass: ensure no triple-br even after unwrap.
        html = html.replace(/(<br\s*\/?>){3,}/gi, '<br><br>');
      }

      if (textarea.value !== html) {
        textarea.value = html;
        textarea.dispatchEvent(new Event('input', { bubbles: true }));
      }
    });

    if (isStatic) {
      const toolbar = quill.getModule('toolbar');
      toolbar.addHandler('image', () => {
        openLibraryPicker({
          kind: 'image',
          title: 'Insert an image',
          onPick: (item) => {
            const range = quill.getSelection(true);
            const url = item.url || `/api/media/${item.id}/content`;
            quill.insertEmbed(range.index, 'image', url, Quill.sources.USER);
            quill.setSelection(range.index + 1, Quill.sources.SILENT);
          },
        });
      });
    }

    quillInstances.set(textarea, quill);
    // Force normal weight on the root element to override inheritance.
    quill.root.style.fontWeight = '400';

    // Seed initial content from textarea (e.g. on page load or template use).
    if (textarea.value) {
      quill.root.innerHTML = textarea.value;
    }

    // Always ensure the editor doesn't think it's bold if it's empty.
    if (!textarea.value || textarea.value === '<p><br></p>') {
      quill.format('bold', false, Quill.sources.SILENT);
    }

    return quill;
  };

  const syncQuillFromTextarea = (textarea) => {
    const quill = quillInstances.get(textarea);
    if (quill) {
      const val = textarea.value || '';
      const html = val || '<p><br></p>';
      if (quill.root.innerHTML !== html) {
        quill.root.innerHTML = html;
        if (!val || val === '<p><br></p>') {
          quill.format('bold', false, Quill.sources.SILENT);
        }
      }
    }
  };

  // Bind an inline Image-description editor. Caller supplies the
  // input element, the status hint element, and (optionally) the
  // visibility row + an onSaved hook. The helper wires blur + Enter
  // to PUT /api/media/{id} (the id lives on `input.dataset.mediaId`)
  // and surfaces a Saved / Required / error status on every change.
  // Returns `{ show(item), persist() }` so the caller can paint the
  // editor from a freshly-fetched MediaItem or trigger a save
  // programmatically.
  const bindAltTextEditor = ({ input, statusEl, rowEl = null, onSaved = null }) => {
    if (!input) return { show: () => {}, persist: async () => {} };
    const setStatusOnly = statusSetter(statusEl);
    const show = (item) => {
      if (rowEl) rowEl.hidden = item == null;
      if (item == null) {
        input.value = '';
        delete input.dataset.mediaId;
        setStatusOnly('');
        return;
      }
      input.value = item.alt_text || '';
      input.dataset.mediaId = String(item.id);
      setStatusOnly(
        item.alt_text ? 'Saved.' : 'Required before this logo can be used.',
        item.alt_text ? 'ok' : 'error',
      );
    };
    const persist = async () => {
      const id = input?.dataset?.mediaId;
      if (!id) return;
      const value = input.value.trim();
      try {
        const res = await fetch(`/api/media/${encodeURIComponent(id)}`, {
          method: 'PUT',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ alt_text: value }),
        });
        if (!res.ok) {
          const detail = await readJsonDetail(res);
          setStatusOnly(`${res.status}: ${detail || res.statusText}`, 'error');
          return;
        }
        setStatusOnly(
          value ? 'Saved.' : 'Required before this logo can be used.',
          value ? 'ok' : 'error',
        );
        if (onSaved) onSaved({ value, id });
      } catch (err) {
        setStatusOnly('Network error: ' + err.message, 'error');
      }
    };
    input.addEventListener('blur', persist);
    input.addEventListener('keydown', (e) => {
      if (e.key === 'Enter') {
        e.preventDefault();
        input.blur();
      }
    });
    return { show, persist };
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
    // Article numbering is global across every Articles section so that
    // a highlight reference like "#3" still maps to a unique card even
    // when the form has multiple Articles sections.
    allArticleCards().forEach((card, i) => {
      const idx = card.querySelector('.idx');
      if (idx) idx.textContent = `#${i + 1}`;
      updateArticleHeaderPreview(card);
    });
    allStaticCards().forEach((card, i) => {
      const idx = card.querySelector('.idx');
      if (idx) idx.textContent = `#${i + 1}`;
    });
    // Per-section counts in the section summary badges.
    sortableSectionsOfType('articles').forEach((sec) => {
      const badge = sec.querySelector('[data-role="articles-count"]');
      if (badge) badge.textContent = sec.querySelectorAll('.article-card').length;
    });
    sortableSectionsOfType('static_blocks').forEach((sec) => {
      const badge = sec.querySelector('[data-role="static-count"]');
      if (badge) badge.textContent = sec.querySelectorAll('fieldset.card').length;
    });
    highlightsCount.textContent = highlightsContainer.querySelectorAll('.row').length;
    refreshHighlightOptions();
  };

  // Pick a target Articles section's container — defaults to the LAST one
  // so "+ Add article" from the Articles-section template appends to its
  // own list, but the FAB-less seed path uses the first section.
  const articlesContainerOf = (section) =>
    section.querySelector('[data-role="articles-container"]');
  const staticsContainerOf = (section) =>
    section.querySelector('[data-role="statics-container"]');

  const addArticle = (data = {}, container = null) => {
    const targetContainer = container
      || (sortableSectionsOfType('articles')[0]?.querySelector('[data-role="articles-container"]'));
    if (!targetContainer) return null;
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
    wireArticleLogoActions(node);
    wireArticleDragHandle(node);
    // Initialize Quill editors for Pull quote and Comment.
    node.querySelectorAll('[data-quill-field]').forEach((scope) => {
      wireQuillEditor(scope);
    });
    targetContainer.appendChild(node);
    renumber();

    // Rehydrate the inline alt-text editor for drafts that already
    // carry a publication_logo_media_id: fetch the media item, surface
    // the editor, and bind it. Single fetch per article on load — the
    // editor is otherwise hidden until Fetch / Upload / Library lights
    // it up.
    const seededMediaId = data?.publication_logo_media_id;
    if (seededMediaId) {
      const altRow = node.querySelector('[data-role="logo-alt-row"]');
      const altInput = node.querySelector('[data-role="logo-alt-text"]');
      const altStatusEl = node.querySelector('[data-role="logo-alt-status"]');
      fetch(`/api/media/${encodeURIComponent(seededMediaId)}`)
        .then((r) => (r.ok ? r.json() : null))
        .then((item) => {
          if (!item || !altRow || !altInput) return;
          altRow.hidden = false;
          altInput.value = item.alt_text || '';
          altInput.dataset.mediaId = String(item.id);
          if (altStatusEl) {
            altStatusEl.textContent = item.alt_text
              ? 'Saved.'
              : 'Required before this logo can be used.';
            altStatusEl.dataset.kind = item.alt_text ? 'ok' : 'error';
          }
        })
        .catch(() => { /* silent — stale id; the editor stays hidden */ });
    }
    return node;
  };

  // ───────── Article logo affordances ───────────────────────────────
  // The Logo URL field has three siblings: Fetch (register the URL in
  // the Media Library), Upload (POST a file via /api/media/upload and
  // remember the resulting Media Library id), and Library (open the
  // picker dialog and pick from kind=logo). Upload + uploaded-pick set
  // the hidden data-field="publication_logo_media_id" — Pydantic's URL
  // validator rejected the ``/api/media/{id}/content`` relative URL the
  // older flow used to stuff into the visible URL input, breaking the
  // issue payload on every preview/eml round-trip. URL-only entries
  // continue to live in data-field="publication_logo_url".
  const wireArticleLogoActions = (card) => {
    const logoInput = card.querySelector('[data-field="publication_logo_url"]');
    const mediaIdInput = card.querySelector('[data-field="publication_logo_media_id"]');
    const statusEl = card.querySelector('[data-role="logo-status"]');
    const fileInput = card.querySelector('[data-role="logo-upload"]');
    const altRow = card.querySelector('[data-role="logo-alt-row"]');
    const altInput = card.querySelector('[data-role="logo-alt-text"]');
    const altStatusEl = card.querySelector('[data-role="logo-alt-status"]');
    const setLogoMedia = (id) => {
      if (mediaIdInput) mediaIdInput.value = id == null ? '' : String(id);
    };
    const setLogoStatus = statusSetter(statusEl);
    // Shared inline alt-text editor — Article logos and Settings →
    // Masthead use the same bindAltTextEditor surface so the
    // "Saved." / "Required before this logo can be used." / PUT
    // contract stays in one place. The onSaved hook fires a preview
    // re-render so other surfaces (e.g. the masthead-Save validator)
    // pick up the new alt_text on the next round-trip.
    const altEditor = bindAltTextEditor({
      input: altInput,
      statusEl: altStatusEl,
      rowEl: altRow,
      onSaved: () => schedulePreview(),
    });
    const showAltEditor = altEditor.show;

    const registerBtn = card.querySelector('[data-action="register-logo"]');
    if (registerBtn) {
      registerBtn.addEventListener('click', async (e) => {
        e.preventDefault();
        const url = sanitizeUrlInput(logoInput);
        if (!url) {
          setLogoStatus('Enter a Logo URL first.', 'error');
          logoInput?.focus();
          return;
        }
        registerBtn.disabled = true;
        setLogoStatus('Registering…');
        try {
          const res = await fetch('/api/media', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ kind: 'logo', url }),
          });
          if (!res.ok) {
            let detail = '';
            try { detail = (await res.json()).detail || ''; } catch { /* ignore */ }
            setLogoStatus(`${res.status}: ${detail || res.statusText}`, 'error');
            return;
          }
          const item = await res.json();
          // Capture the registered library item id so the inline alt-
          // text editor can edit *this* row directly — the favicon URL
          // keeps rendering through `publication_logo_url`, the
          // media_id reference is just so the editor has something to
          // attach the description to. The eml controller will continue
          // to skip cid embedding because URL-sourced items have no
          // cached bytes yet (see _resolve_masthead_logo's twin pattern).
          setLogoMedia(item.id);
          showAltEditor(item);
          setLogoStatus(
            item.alt_text
              ? 'Registered in Logos & Media.'
              : 'Registered — fill the Image description below.',
            'ok',
          );
          schedulePreview();
        } catch (err) {
          setLogoStatus('Network error: ' + err.message, 'error');
        } finally {
          registerBtn.disabled = false;
        }
      });
    }

    const uploadBtn = card.querySelector('[data-action="upload-logo"]');
    if (uploadBtn && fileInput) {
      uploadBtn.addEventListener('click', (e) => {
        e.preventDefault();
        fileInput.click();
      });
      fileInput.addEventListener('change', async () => {
        const file = fileInput.files && fileInput.files[0];
        if (!file) return;
        uploadBtn.disabled = true;
        setLogoStatus('Uploading…');
        try {
          const item = await uploadMedia({ file, kind: 'logo' });
          // Stash the Media Library id in the hidden companion field —
          // the URL input would not accept a relative ``/api/media/…``
          // value through Pydantic on the next preview round-trip, so we
          // keep the URL field empty for upload-sourced logos and let
          // the template fall back to media_id.
          if (logoInput) logoInput.value = '';
          setLogoMedia(item.id);
          showAltEditor(item);
          setLogoStatus('Uploaded — fill the Image description below.', 'ok');
          schedulePreview();
        } catch (err) {
          if (err instanceof Response) await surfaceUploadError(err, setLogoStatus);
          else setLogoStatus('Network error: ' + err.message, 'error');
        } finally {
          uploadBtn.disabled = false;
          fileInput.value = '';
        }
      });
    }

    const pickBtn = card.querySelector('[data-action="pick-logo"]');
    if (pickBtn) {
      pickBtn.addEventListener('click', (e) => {
        e.preventDefault();
        openLibraryPicker({
          kind: 'logo',
          title: 'Pick an article logo',
          onPick: (item) => {
            // Two flavors of library item:
            //   - URL-sourced: paste the public URL into the URL field;
            //     keep the media_id so the inline alt-text editor can
            //     still bind to this row even though rendering goes
            //     through the URL.
            //   - Uploaded (item.url is null): keep the URL field empty
            //     and stash the Media Library id; the template prefers
            //     media_id and falls back to URL.
            if (item.url) {
              if (logoInput) logoInput.value = item.url;
              setLogoMedia(item.id);
            } else {
              if (logoInput) logoInput.value = '';
              setLogoMedia(item.id);
            }
            showAltEditor(item);
            setLogoStatus(`Using "${item.alt_text || 'library item'}".`, 'ok');
            schedulePreview();
          },
        });
      });
    }
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
    const els = [...container.querySelectorAll(':scope > .article-card:not(.dragging)')];
    let closest = { offset: Number.NEGATIVE_INFINITY, element: null };
    els.forEach((child) => {
      const box = child.getBoundingClientRect();
      const offset = y - box.top - box.height / 2;
      if (offset < 0 && offset > closest.offset) closest = { offset, element: child };
    });
    return closest.element;
  };

  // Find the .articles-container under the cursor — used for cross-section
  // article moves (drag from Articles A into Articles B). We resolve via
  // the parent section's bounding rect so an empty Articles section
  // (zero-height container) is still a valid drop target.
  const articlesContainerAtPoint = (x, y) => {
    const sections = [...sortableSections.querySelectorAll(':scope > .sortable-section[data-section-type="articles"]')];
    for (const sec of sections) {
      if (!sec.open) continue;
      const box = sec.getBoundingClientRect();
      if (x >= box.left && x <= box.right && y >= box.top && y <= box.bottom) {
        return sec.querySelector('.articles-container');
      }
    }
    return null;
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

  // Document-level dragover — chooses the .articles-container under the
  // cursor and reflows the dragged card live. Delegated rather than per-
  // container so newly-added Articles sections work without rewiring.
  document.addEventListener('dragover', (e) => {
    if (!draggingEl) return;
    const container = articlesContainerAtPoint(e.clientX, e.clientY);
    if (!container) return;
    e.preventDefault();
    const after = getDragAfterElement(container, e.clientY);
    if (after == null) container.appendChild(draggingEl);
    else if (after !== draggingEl) container.insertBefore(draggingEl, after);
  });

  // ───────── Article fetch (auto-fill from URL) ──────────────────────
  const fetchArticleInto = async (card) => {
    const urlInput = card.querySelector('[data-field="url"]');
    const statusEl = card.querySelector('[data-role="fetch-status"]');
    const button = card.querySelector('[data-action="fetch-article"]');
    const url = sanitizeUrlInput(urlInput);
    const setFetchStatus = statusSetter(statusEl);
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
      // Side-channel: when the scraper produced a publication_logo_url
      // (favicon), register it in Logos & Media as kind=logo so the
      // user gets a one-step path from "Fetch article" to a usable
      // library entry. The returned id is stashed onto the article's
      // hidden media_id field and surfaced through the inline alt-text
      // editor so the editor can satisfy the Image-description gate
      // without leaving the Article card. Failures are silent — the
      // visible URL field is set either way, so the article still
      // renders; only the inline editor is skipped.
      const logoEl = card.querySelector('[data-field="publication_logo_url"]');
      if (logoEl && logoEl.value) {
        try {
          const regRes = await fetch('/api/media', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ kind: 'logo', url: sanitizeUrl(logoEl.value) }),
          });
          if (regRes.ok) {
            const regItem = await regRes.json();
            const mediaEl = card.querySelector('[data-field="publication_logo_media_id"]');
            if (mediaEl) mediaEl.value = String(regItem.id);
            const altRow = card.querySelector('[data-role="logo-alt-row"]');
            const altInput = card.querySelector('[data-role="logo-alt-text"]');
            const altStatusEl = card.querySelector('[data-role="logo-alt-status"]');
            if (altRow && altInput) {
              altRow.hidden = false;
              altInput.value = regItem.alt_text || '';
              altInput.dataset.mediaId = String(regItem.id);
              if (altStatusEl) {
                altStatusEl.textContent = regItem.alt_text
                  ? 'Saved.'
                  : 'Required before this logo can be used.';
                altStatusEl.dataset.kind = regItem.alt_text ? 'ok' : 'error';
              }
            }
          }
        } catch { /* non-fatal */ }
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
  // Each Podcast section owns its own .podcast-fields container and
  // (when "Include podcast section" is checked) a cloned podcast template
  // node with its own timecodes list. The DOM is the source of truth;
  // helpers below look up the cloned node and timecodes container fresh
  // on each call so multiple Podcast sections coexist.
  const podcastFieldsOf = (section) =>
    section.querySelector('[data-role="podcast-fields"]');
  const podcastEnabledInputOf = (section) =>
    section.querySelector('[data-role="podcast-enabled"]');
  const podcastNodeOf = (section) =>
    section.querySelector('[data-role="podcast-fields"] > div');
  const timecodesContainerOf = (section) => {
    const node = podcastNodeOf(section);
    return node ? node.querySelector('#timecodes') : null;
  };

  const renderPodcastFields = (section) => {
    const fields = podcastFieldsOf(section);
    fields.innerHTML = '';
    const node = podcastTpl.content.firstElementChild.cloneNode(true);
    fields.appendChild(node);
    node.querySelector('#add-timecode').addEventListener('click', (e) => {
      e.preventDefault();
      addTimecode({}, node.querySelector('#timecodes'));
    });
    const ytFetchBtn = node.querySelector('[data-action="fetch-youtube"]');
    if (ytFetchBtn) {
      ytFetchBtn.addEventListener('click', (e) => {
        e.preventDefault();
        fetchYouTubeInto(node);
      });
    }
    return node;
  };

  // ───────── YouTube fetch (auto-fill title / summary / timecodes) ───
  const fetchYouTubeInto = async (root) => {
    const urlInput = root.querySelector('[data-field="youtube_video_url"]');
    const statusEl = root.querySelector('[data-role="youtube-fetch-status"]');
    const button = root.querySelector('[data-action="fetch-youtube"]');
    const url = sanitizeUrlInput(urlInput);
    const setFetchStatus = statusSetter(statusEl);
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
      // The fetch button is rendered inside the cloned podcast template
      // node, so its sibling timecodes container is reachable via root.
      let added = 0;
      const tcContainer = root.querySelector('#timecodes');
      if (Array.isArray(meta.timecodes) && tcContainer) {
        const existing = new Set(
          [...tcContainer.querySelectorAll('.row')].map((row) => {
            const tc = row.querySelector('[data-field="timecode"]')?.value || '';
            return trim(tc);
          }).filter(Boolean),
        );
        meta.timecodes.forEach((tc) => {
          if (!tc || !tc.timecode || existing.has(tc.timecode)) return;
          addTimecode(
            { timecode: tc.timecode, label: tc.label || '', url: tc.url || '' },
            tcContainer,
          );
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

  const addTimecode = (data = {}, container = null) => {
    if (!container) return null;
    const node = timecodeTpl.content.firstElementChild.cloneNode(true);
    Object.entries(data).forEach(([k, v]) => {
      const el = node.querySelector(`[data-field="${k}"]`);
      if (el && v !== null && v !== undefined) el.value = v;
    });
    node.querySelector('.btn-remove').addEventListener('click', () => {
      node.remove();
      schedulePreview();
    });
    container.appendChild(node);
    return node;
  };

  // Wires the "Include podcast section" checkbox of a Podcast section so
  // toggling reveals/hides the cloned podcast-template fields. Each
  // section stays independent of the others.
  const wirePodcastSection = (section) => {
    const cb = podcastEnabledInputOf(section);
    const fields = podcastFieldsOf(section);
    if (!cb || !fields) return;
    cb.addEventListener('change', () => {
      if (cb.checked) {
        fields.hidden = false;
        if (!podcastNodeOf(section)) renderPodcastFields(section);
      } else {
        fields.hidden = true;
      }
      schedulePreview();
    });
  };

  // ───────── Static blocks ───────────────────────────────────────────
  const addStatic = (data = {}, container = null) => {
    const targetContainer = container
      || (sortableSectionsOfType('static_blocks')[0]?.querySelector('[data-role="statics-container"]'));
    if (!targetContainer) return null;
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
    node.querySelectorAll('[data-quill-field]').forEach((scope) => {
      wireQuillEditor(scope);
    });
    targetContainer.appendChild(node);
    renumber();
    return node;
  };

  // ───────── Sortable section management ────────────────────────────
  // Sections (Articles / Podcast / Static blocks) live in #sortable-
  // sections. They are added via the floating "+" button or seeded as
  // defaults at init. When more than one section exists, drag handles on
  // each section's summary bar appear and the user can reorder them
  // freely (across types). The fixed Issue-Details / Bottom-highlights /
  // Footer <details> are NOT in the sortable region.
  let draggingSectionEl = null;

  const getSectionDragAfter = (y) => {
    const els = [...sortableSections.querySelectorAll(':scope > .sortable-section:not(.dragging)')];
    let closest = { offset: Number.NEGATIVE_INFINITY, element: null };
    els.forEach((child) => {
      const box = child.getBoundingClientRect();
      const offset = y - box.top - box.height / 2;
      if (offset < 0 && offset > closest.offset) closest = { offset, element: child };
    });
    return closest.element;
  };

  const wireSectionDragHandle = (section) => {
    const handle = section.querySelector('.section-drag-handle');
    if (!handle) return;
    handle.addEventListener('mousedown', () => { section.draggable = true; });
    handle.addEventListener('mouseup', () => { section.draggable = false; });
    // The handle sits inside <summary>; clicking it would toggle the
    // collapsed state, which is annoying when the user is reaching for
    // the drag affordance.
    handle.addEventListener('click', (e) => { e.preventDefault(); e.stopPropagation(); });
    section.addEventListener('dragstart', (e) => {
      if (!section.draggable) return;
      // If an article-card drag is in flight, don't let the section
      // start its own drag (e.dataTransfer is shared per gesture).
      if (draggingEl) return;
      draggingSectionEl = section;
      section.classList.add('dragging');
      e.dataTransfer.effectAllowed = 'move';
      e.dataTransfer.setData('text/plain', section.dataset.sectionId || '');
    });
    section.addEventListener('dragend', () => {
      section.classList.remove('dragging');
      section.draggable = false;
      if (draggingSectionEl === section) draggingSectionEl = null;
      renumber();
      schedulePreview();
    });
  };

  sortableSections.addEventListener('dragover', (e) => {
    if (!draggingSectionEl) return;
    e.preventDefault();
    const after = getSectionDragAfter(e.clientY);
    if (after == null) sortableSections.appendChild(draggingSectionEl);
    else if (after !== draggingSectionEl) sortableSections.insertBefore(draggingSectionEl, after);
  });

  // Toggle the .has-many class — its presence is what makes the per-
  // section drag handles visible (CSS rule). One sortable section means
  // there's nothing to reorder, so the handles stay hidden.
  const updateSectionDragVisibility = () => {
    const count = sortableSectionsAll().length;
    sortableSections.classList.toggle('has-many', count > 1);
  };

  const wireSection = (section) => {
    if (!section.dataset.sectionId) section.dataset.sectionId = newSectionId();
    wireSectionDragHandle(section);

    const removeBtn = section.querySelector('.section-remove');
    if (removeBtn) {
      removeBtn.addEventListener('click', async (e) => {
        e.preventDefault();
        e.stopPropagation();
        const type = section.dataset.sectionType;
        const label = type === 'articles' ? 'Articles' :
                      type === 'podcast' ? 'Podcast' : 'Static blocks';
        // Only prompt if the section actually has content the user
        // would lose; an empty newly-spawned section deletes silently.
        const hasContent =
          section.querySelectorAll('.article-card, fieldset.card').length > 0 ||
          (type === 'podcast' && podcastEnabledInputOf(section)?.checked);
        if (hasContent) {
          const ok = await confirmDelete(`Remove this ${label} section? Its contents will be discarded.`);
          if (!ok) return;
        }
        section.remove();
        updateSectionDragVisibility();
        renumber();
        schedulePreview();
      });
    }

    if (section.dataset.sectionType === 'articles') {
      const addBtn = section.querySelector('[data-action="add-article"]');
      if (addBtn) {
        addBtn.addEventListener('click', (e) => {
          e.preventDefault();
          addArticle({}, articlesContainerOf(section));
          schedulePreview();
        });
      }
    } else if (section.dataset.sectionType === 'static_blocks') {
      const addBtn = section.querySelector('[data-action="add-static"]');
      if (addBtn) {
        addBtn.addEventListener('click', (e) => {
          e.preventDefault();
          addStatic({}, staticsContainerOf(section));
          schedulePreview();
        });
      }
    } else if (section.dataset.sectionType === 'podcast') {
      wirePodcastSection(section);
    }
  };

  const cloneSection = (type) => {
    const tpl = (
      type === 'articles' ? articlesSectionTpl :
      type === 'podcast' ? podcastSectionTpl :
      type === 'static_blocks' ? staticBlocksSectionTpl : null
    );
    if (!tpl) return null;
    return tpl.content.firstElementChild.cloneNode(true);
  };

  // Add a new sortable section (articles / podcast / static_blocks).
  // ``id`` allows callers (loadIssueIntoForm) to restore a section's
  // stable id from a saved form_sections entry.
  const addSection = (type, { id = null, append = true } = {}) => {
    const node = cloneSection(type);
    if (!node) return null;
    if (id) node.dataset.sectionId = id;
    else node.dataset.sectionId = newSectionId();
    if (append) sortableSections.appendChild(node);
    wireSection(node);
    updateSectionDragVisibility();
    return node;
  };

  // ───────── Highlights ──────────────────────────────────────────────
  // A highlight row is either a pointer to one of THIS issue's articles
  // (article_index carries a stable DOM id; we remap to numeric position
  // in buildIssue so reorders don't drift) OR a pointer to an article
  // from a previously-archived issue (source_archive_id +
  // source_article_index + cached_* fields). The hidden field set is
  // exhaustive — the row carries whichever subset applies — and the
  // pydantic HighlightRef validator on the server enforces the
  // exactly-one-flavor rule. The search input below combines both:
  // current-issue articles surface as the first group of suggestions,
  // archive candidates come from /api/archive/highlight-candidates.
  const _escAttr = (s) =>
    String(s == null ? '' : s).replace(/[<>&"']/g, (c) =>
      ({ '<': '&lt;', '>': '&gt;', '&': '&amp;', '"': '&quot;', "'": '&#39;' }[c]));
  const _escHtml = _escAttr;

  // In-flight archive-candidate request per row, so a fast typist
  // doesn't have stale dropdowns racing the current one.
  const _highlightSearchSeq = new WeakMap();

  // Get every current-issue article as {id, headline, url, index_within_form}.
  const _currentArticlesAsCandidates = () =>
    allArticleCards().map((card, i) => {
      const headline = card.querySelector('[data-field="headline"]')?.value || '';
      const url = card.querySelector('[data-field="url"]')?.value || '';
      return {
        kind: 'in_issue',
        article_id: card.dataset.articleId,
        index_within_form: i,
        headline: headline.trim(),
        url: url.trim(),
      };
    });

  // Fetch archive candidates with simple debounce + abort-on-restart.
  const _highlightFetchDebounce = new Map();
  const fetchArchiveCandidates = (q) => {
    return new Promise((resolve) => {
      const key = '__candidate_fetch__';
      const prev = _highlightFetchDebounce.get(key);
      if (prev) clearTimeout(prev);
      _highlightFetchDebounce.set(
        key,
        setTimeout(async () => {
          try {
            const params = new URLSearchParams({ days: '365', limit: '50' });
            if (q) params.set('q', q);
            const res = await fetch(`/api/archive/highlight-candidates?${params}`);
            if (!res.ok) {
              resolve([]);
              return;
            }
            const items = await res.json();
            // Tag the source kind so the row can decide how to populate
            // hidden fields when the user picks one.
            resolve(items.map((c) => ({ kind: 'archive', ...c })));
          } catch {
            resolve([]);
          }
        }, 180),
      );
    });
  };

  const wireHighlightRow = (row) => {
    const searchInput = row.querySelector('[data-role="highlight-search"]');
    const dropdown = row.querySelector('[data-role="highlight-suggestions"]');
    const meta = row.querySelector('[data-role="highlight-meta"]');
    const articleIdInput = row.querySelector('[data-field="article_index"]');
    const archiveIdInput = row.querySelector('[data-field="source_archive_id"]');
    const archiveIndexInput = row.querySelector('[data-field="source_article_index"]');
    const cachedHeadline = row.querySelector('[data-field="cached_headline"]');
    const cachedUrl = row.querySelector('[data-field="cached_url"]');
    const cachedDate = row.querySelector('[data-field="cached_issue_date"]');
    const cachedYear = row.querySelector('[data-field="cached_issue_year"]');
    const cachedNumber = row.querySelector('[data-field="cached_issue_number"]');
    const cachedTitle = row.querySelector('[data-field="cached_issue_title"]');

    const clearRefFields = () => {
      [articleIdInput, archiveIdInput, archiveIndexInput,
       cachedHeadline, cachedUrl, cachedDate, cachedYear,
       cachedNumber, cachedTitle].forEach((el) => { if (el) el.value = ''; });
    };

    const setMeta = (label) => {
      if (meta) meta.textContent = label || '';
    };

    const pickCandidate = (candidate) => {
      clearRefFields();
      if (candidate.kind === 'in_issue') {
        articleIdInput.value = candidate.article_id;
        searchInput.value = candidate.headline || `(article #${candidate.index_within_form + 1})`;
        setMeta('This issue');
      } else {
        archiveIdInput.value = String(candidate.archive_id);
        archiveIndexInput.value = String(candidate.article_index);
        cachedHeadline.value = candidate.headline || '';
        cachedUrl.value = candidate.url || '';
        cachedDate.value = candidate.issue_date || '';
        cachedYear.value = candidate.issue_year != null ? String(candidate.issue_year) : '';
        cachedNumber.value = candidate.issue_number != null ? String(candidate.issue_number) : '';
        cachedTitle.value = candidate.issue_title || '';
        searchInput.value = candidate.headline || '';
        const parts = [];
        if (candidate.issue_date) parts.push(candidate.issue_date);
        if (candidate.issue_title) parts.push(candidate.issue_title);
        setMeta(parts.length ? `From ${parts.join(' · ')}` : 'From archive');
      }
      hideDropdown();
      schedulePreview();
    };

    const renderSuggestions = (candidates, ctx = {}) => {
      if (!dropdown) return;
      if (!candidates.length) {
        // Three distinct empty states — each gets actionable wording so
        // the editor knows why the list is bare and what to do next.
        const q = (ctx.q || '').trim();
        const noCurrent = (ctx.inIssueTotal || 0) === 0;
        const noArchive = (ctx.archiveTotal || 0) === 0;
        let msg;
        if (q && noArchive && noCurrent) {
          msg = "No archived issues yet — download or send an issue to populate the archive, then highlights from past editions will appear here.";
        } else if (q) {
          // We DID find archive or in-issue articles, just nothing
          // matching the user's substring.
          msg = "No matches — try fewer letters.";
        } else if (noArchive && noCurrent) {
          msg = "No articles yet — add Articles to this issue or download a past issue to surface highlights from prior editions.";
        } else if (noArchive) {
          msg = "No archived issues yet — current-issue articles will show up here as you add them; past-edition highlights will appear once you send your first issue.";
        } else {
          // noCurrent && archive has entries — user just hasn't typed.
          // The archive entries should have shown up; if they didn't,
          // the fetch failed or returned empty for some other reason.
          msg = "Start typing to search highlights from prior editions.";
        }
        dropdown.innerHTML = `<li class="hint">${_escHtml(msg)}</li>`;
        dropdown.hidden = false;
        searchInput.setAttribute('aria-expanded', 'true');
        dropdown._candidates = null;
        return;
      }
      dropdown.innerHTML = candidates.map((c, i) => {
        const tag = c.kind === 'in_issue' ? 'this issue'
          : (c.issue_date ? c.issue_date : 'archive');
        return `<li role="option" data-i="${i}"><span class="hl-sugg-title">${_escHtml(c.headline)}</span><span class="hl-sugg-tag">${_escHtml(tag)}</span></li>`;
      }).join('');
      dropdown.hidden = false;
      searchInput.setAttribute('aria-expanded', 'true');
      // Capture the array on the dropdown so click handler can pick it
      // out by index — avoids a per-li listener.
      dropdown._candidates = candidates;
    };

    const hideDropdown = () => {
      if (!dropdown) return;
      dropdown.hidden = true;
      dropdown._candidates = null;
      searchInput.setAttribute('aria-expanded', 'false');
    };

    const refreshSuggestions = async () => {
      const q = searchInput.value.trim();
      const seq = (_highlightSearchSeq.get(searchInput) || 0) + 1;
      _highlightSearchSeq.set(searchInput, seq);
      // inIssueTotal counts every visible article BEFORE we filter by
      // the user's query — empty-state messages use the unfiltered
      // totals so "no articles yet" vs "no matches" is distinguishable.
      const inIssueAll = _currentArticlesAsCandidates().filter((c) => c.headline);
      const inIssue = q
        ? inIssueAll.filter((c) => c.headline.toLowerCase().includes(q.toLowerCase()))
        : inIssueAll;
      const archive = await fetchArchiveCandidates(q);
      if (seq !== _highlightSearchSeq.get(searchInput)) return; // stale
      const archiveTotal = archive.length;
      // Dedup by URL across both sources — the current issue wins when
      // an article happens to be both this week AND in a recent archive.
      const seen = new Set();
      const merged = [];
      for (const c of [...inIssue, ...archive]) {
        const key = (c.url || `art:${c.article_id || ''}`).toLowerCase();
        if (seen.has(key)) continue;
        seen.add(key);
        merged.push(c);
      }
      // When the user hasn't typed yet, also probe the archive
      // unfiltered so the empty-state message knows whether the archive
      // has ANY entries at all. Skip the extra request if we already
      // got results back to keep the input snappy.
      let archiveAvailability = archiveTotal;
      if (!q && archiveTotal === 0) {
        try {
          const probe = await fetch('/api/archive/highlight-candidates?limit=1');
          if (probe.ok) {
            const items = await probe.json();
            archiveAvailability = items.length;
          }
        } catch { /* leave as 0 */ }
      }
      renderSuggestions(merged.slice(0, 50), {
        q,
        inIssueTotal: inIssueAll.length,
        archiveTotal: archiveAvailability,
      });
    };

    if (searchInput) {
      searchInput.addEventListener('input', refreshSuggestions);
      searchInput.addEventListener('focus', refreshSuggestions);
      searchInput.addEventListener('blur', () => {
        // Delay so the click on a suggestion lands before the blur hides it.
        setTimeout(hideDropdown, 120);
      });
      searchInput.addEventListener('keydown', (e) => {
        if (e.key === 'Escape') {
          hideDropdown();
        }
      });
    }
    if (dropdown) {
      dropdown.addEventListener('mousedown', (e) => {
        const li = e.target.closest('li[role="option"]');
        if (!li || !dropdown._candidates) return;
        e.preventDefault();
        const idx = Number(li.dataset.i);
        const cand = dropdown._candidates[idx];
        if (cand) pickCandidate(cand);
      });
    }

    // Expose a tiny seed helper the rehydrate path uses to populate a
    // row from saved-draft JSON without re-fetching.
    row._seedHighlight = (data) => {
      // Old-shape (in-issue) seeding: data.article_index is a NUMERIC
      // index into the loaded articles list. Resolve to the matching
      // card's DOM id, set article_index input to the id, display the
      // headline in the search field. Same idToOutIdx pattern as the
      // legacy select.
      if (data.article_index !== undefined && data.article_index !== null
          && data.source_archive_id == null) {
        const cards = allArticleCards();
        const card = cards[Number(data.article_index)];
        if (card && card.dataset.articleId) {
          articleIdInput.value = card.dataset.articleId;
          const headline = card.querySelector('[data-field="headline"]')?.value || '';
          searchInput.value = headline;
          setMeta('This issue');
        }
        return;
      }
      // New-shape (archive) seeding: every cached_* field is populated
      // by the server; just paint them into the row.
      if (data.source_archive_id != null && data.source_article_index != null) {
        archiveIdInput.value = String(data.source_archive_id);
        archiveIndexInput.value = String(data.source_article_index);
        cachedHeadline.value = data.cached_headline || '';
        cachedUrl.value = data.cached_url || '';
        cachedDate.value = data.cached_issue_date || '';
        cachedYear.value = data.cached_issue_year != null ? String(data.cached_issue_year) : '';
        cachedNumber.value = data.cached_issue_number != null ? String(data.cached_issue_number) : '';
        cachedTitle.value = data.cached_issue_title || '';
        searchInput.value = data.cached_headline || '';
        const parts = [];
        if (data.cached_issue_date) parts.push(data.cached_issue_date);
        if (data.cached_issue_title) parts.push(data.cached_issue_title);
        setMeta(parts.length ? `From ${parts.join(' · ')}` : 'From archive');
      }
    };
    return { pickCandidate };
  };

  // Wire row drag-and-drop reorder. Highlight rows live in #highlights
  // (a flat container, no nested sections) so the dnd is simpler than
  // the article-card flow — just reorder within one parent.
  const wireHighlightDrag = (row) => {
    const handle = row.querySelector('.highlight-drag-handle');
    if (!handle) return;
    handle.addEventListener('mousedown', () => { row.draggable = true; });
    handle.addEventListener('mouseup', () => { row.draggable = false; });
    handle.addEventListener('click', (e) => { e.preventDefault(); e.stopPropagation(); });
    row.addEventListener('dragstart', (e) => {
      if (!row.draggable) return;
      row.classList.add('dragging');
      try { e.dataTransfer.setData('text/plain', ''); } catch { /* ignore */ }
      e.dataTransfer.effectAllowed = 'move';
    });
    row.addEventListener('dragend', () => {
      row.classList.remove('dragging');
      row.draggable = false;
      schedulePreview();
    });
  };
  if (highlightsContainer) {
    highlightsContainer.addEventListener('dragover', (e) => {
      const dragging = highlightsContainer.querySelector('.dragging');
      if (!dragging) return;
      e.preventDefault();
      const rows = [...highlightsContainer.querySelectorAll('.row:not(.dragging)')];
      const after = rows.find((r) => {
        const box = r.getBoundingClientRect();
        return e.clientY < box.top + box.height / 2;
      });
      if (after) {
        highlightsContainer.insertBefore(dragging, after);
      } else {
        highlightsContainer.appendChild(dragging);
      }
    });
  }

  // refreshHighlightOptions used to keep <select> options in sync with
  // the current articles list. With the new search-driven UI there is
  // nothing to repopulate proactively — the search input does its own
  // lookup at focus / keystroke. We keep the name so existing callers
  // (article add/remove/reorder, headline edits) still hit something.
  const refreshHighlightOptions = () => { /* no-op — search-driven */ };

  const addHighlight = (data = {}) => {
    const node = highlightTpl.content.firstElementChild.cloneNode(true);
    highlightsContainer.appendChild(node);
    wireHighlightRow(node);
    wireHighlightDrag(node);
    if (Object.keys(data).length) node._seedHighlight(data);
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
        'newsletter_name',
        'from_name', 'from_email', 'to_email', 'reply_to',
        'masthead_logo_url',
      ].forEach((k) => {
        const v = currentSettings[k];
        if (v) root[k] = v;
      });
      // masthead_logo_media_id is numeric — `if (v)` would drop a legit
      // 0 in theory, but Media Library ids start at 1, so a truthy check
      // is safe here and avoids leaking `null` into the issue payload.
      if (currentSettings.masthead_logo_media_id) {
        root.masthead_logo_media_id = currentSettings.masthead_logo_media_id;
      }
      // Boolean toggle: don't gate on truthiness — `false` is meaningful.
      if (typeof currentSettings.include_read_more_link === 'boolean') {
        root.include_read_more_link = currentSettings.include_read_more_link;
      }
    }
    const issue = stripEmpty(root);

    // Walk every sortable section in DOM order, flattening articles and
    // static_blocks across multiple sections of the same type while
    // preserving the visual ordering. Each section contributes a
    // `form_sections` entry so the structure round-trips through draft
    // save / load. Pydantic ignores unknown extras, so the form_sections
    // field is dropped harmlessly by /api/preview/html and /api/eml.
    const idToOutIdx = new Map();
    issue.articles = [];
    issue.static_blocks = [];
    const formSections = [];
    let collectedPodcast = null;
    sortableSectionsAll().forEach((section) => {
      const type = section.dataset.sectionType;
      const sectionId = section.dataset.sectionId || '';
      if (type === 'articles') {
        const cards = [...section.querySelectorAll('.article-card')];
        let count = 0;
        cards.forEach((card) => {
          const fields = stripEmpty(collectFields(card));
          if (fields.headline && fields.url) {
            idToOutIdx.set(card.dataset.articleId, issue.articles.length);
            issue.articles.push(fields);
            count += 1;
          }
        });
        formSections.push({ type, id: sectionId, count });
      } else if (type === 'static_blocks') {
        const cards = [...section.querySelectorAll('fieldset.card')];
        let count = 0;
        cards.forEach((card) => {
          const fields = collectFields(card);
          const block = {
            title: fields.title || '',
            html: fields.html || '',
            enabled: !!fields.enabled,
          };
          if (block.title || block.html) {
            issue.static_blocks.push(block);
            count += 1;
          }
        });
        formSections.push({ type, id: sectionId, count });
      } else if (type === 'podcast') {
        const cb = podcastEnabledInputOf(section);
        const enabled = !!(cb && cb.checked);
        const node = podcastNodeOf(section);
        let collected = null;
        if (enabled && node) {
          const podFields = {};
          node.querySelectorAll('[data-field]').forEach((el) => {
            if (el.closest('#timecodes')) return;
            const key = el.dataset.field;
            let v;
            if (el.type === 'number') v = el.value === '' ? null : Number(el.value);
            else v = trim(el.value);
            podFields[key] = v;
          });
          const tcContainer = node.querySelector('#timecodes');
          const timecodes = tcContainer
            ? [...tcContainer.querySelectorAll('.row')]
                .map((row) => stripEmpty(collectFields(row)))
                .filter((tc) => tc.timecode && tc.label && tc.url)
            : [];
          const cleaned = stripEmpty(podFields);
          cleaned.timecodes = timecodes;
          if (cleaned.title || timecodes.length || cleaned.zoomcast_url || cleaned.podcast_url) {
            collected = cleaned;
          }
        }
        // Only the first podcast section with usable data flows into the
        // model's single `podcast` slot — extras still appear in
        // form_sections so the layout restores on load even if their
        // data is empty.
        if (collected && !collectedPodcast) collectedPodcast = collected;
        formSections.push({ type, id: sectionId, enabled });
      }
    });
    if (collectedPodcast) issue.podcast = collectedPodcast;
    issue.form_sections = formSections;

    // Highlights — two flavors:
    //   In-issue: article_index input carries the article's stable DOM id;
    //     resolve to the post-filter article position (numbered globally
    //     across all Articles sections) and drop refs whose article was
    //     removed or is still incomplete (no headline+url).
    //   Archive: source_archive_id + source_article_index identify the
    //     source article, cached_* fields carry the snapshot the
    //     renderer + grouping helper need. We round-trip every cached
    //     field so the bottom-highlights block renders consistently
    //     across draft save/load + preview cycles.
    issue.highlights = [...highlightsContainer.querySelectorAll('.row')]
      .map((row) => {
        const fields = collectFields(row);
        const archiveId = fields.source_archive_id;
        if (archiveId !== '' && archiveId != null) {
          const out = {
            source_archive_id: Number(archiveId),
            source_article_index: Number(fields.source_article_index),
          };
          if (fields.cached_headline) out.cached_headline = fields.cached_headline;
          if (fields.cached_url) out.cached_url = fields.cached_url;
          if (fields.cached_issue_date) out.cached_issue_date = fields.cached_issue_date;
          if (fields.cached_issue_year) out.cached_issue_year = Number(fields.cached_issue_year);
          if (fields.cached_issue_number) out.cached_issue_number = Number(fields.cached_issue_number);
          if (fields.cached_issue_title) out.cached_issue_title = fields.cached_issue_title;
          if (fields.override_label) out.override_label = fields.override_label;
          return out;
        }
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
  // Per-section "+ Add article" / "+ Add block" buttons live inside each
  // sortable-section clone and are wired by wireSection(); the only
  // top-level adder still living in the fixed Highlights section is
  // "+ Add highlight" below.
  document.getElementById('add-highlight').addEventListener('click', () => {
    addHighlight();
    schedulePreview();
  });

  // ───────── Floating "+" Add-section button ───────────────────────
  const addSectionFab = document.getElementById('add-section-fab');
  const addSectionMenu = document.getElementById('add-section-menu');
  const closeAddSectionMenu = () => {
    if (!addSectionMenu) return;
    addSectionMenu.hidden = true;
    addSectionFab?.setAttribute('aria-expanded', 'false');
  };
  const openAddSectionMenu = () => {
    if (!addSectionMenu) return;
    addSectionMenu.hidden = false;
    addSectionFab?.setAttribute('aria-expanded', 'true');
  };
  if (addSectionFab && addSectionMenu) {
    addSectionFab.addEventListener('click', (e) => {
      e.preventDefault();
      e.stopPropagation();
      if (addSectionMenu.hidden) openAddSectionMenu();
      else closeAddSectionMenu();
    });
    document.addEventListener('click', (e) => {
      if (addSectionMenu.hidden) return;
      if (addSectionMenu.contains(e.target) || addSectionFab.contains(e.target)) return;
      closeAddSectionMenu();
    });
    document.addEventListener('keydown', (e) => {
      if (e.key === 'Escape') closeAddSectionMenu();
    });
    addSectionMenu.querySelectorAll('button[data-add-type]').forEach((btn) => {
      btn.addEventListener('click', (e) => {
        e.preventDefault();
        const type = btn.dataset.addType;
        const sec = addSection(type);
        // Seed a starter card inside fresh Articles / Static-blocks
        // sections so the user has an editable target immediately.
        if (sec && type === 'articles') addArticle({}, articlesContainerOf(sec));
        if (sec && type === 'static_blocks') addStatic({}, staticsContainerOf(sec));
        closeAddSectionMenu();
        schedulePreview();
      });
    });
  }
  document.getElementById('refresh-preview').addEventListener('click', (e) => {
    e.preventDefault();
    doPreview();
  });
  document.getElementById('download-eml').addEventListener('click', (e) => {
    e.preventDefault();
    downloadEml();
  });
  // ───────── Compose new (replaces Reset) ───────────────────────────
  // Reset used to be a confirm() that nuked the form. Now the user is
  // offered a Save-as-Draft path so a half-done issue isn't lost when
  // they want to start fresh on a new edition.
  const composeNewDialog = document.getElementById('compose-new-dialog');
  const composeNewForm = document.getElementById('compose-new-form');
  const composeNewStatusEl = document.getElementById('compose-new-status');
  const setComposeNewStatus = statusSetter(composeNewStatusEl);
  const openComposeNewDialog = () => {
    if (composeNewForm) composeNewForm.reset();
    setComposeNewStatus('');
    openDialog(composeNewDialog);
  };
  const closeComposeNewDialog = () => closeDialog(composeNewDialog);
  const startFresh = () => {
    closeComposeNewDialog();
    location.reload();
  };

  document.getElementById('compose-new').addEventListener('click', (e) => {
    e.preventDefault();
    openComposeNewDialog();
  });
  document.getElementById('compose-new-cancel').addEventListener('click', (e) => {
    e.preventDefault();
    closeComposeNewDialog();
  });
  document.getElementById('compose-new-close').addEventListener('click', (e) => {
    e.preventDefault();
    closeComposeNewDialog();
  });
  document.getElementById('compose-new-discard').addEventListener('click', (e) => {
    e.preventDefault();
    startFresh();
  });
  document.getElementById('compose-new-save').addEventListener('click', async (e) => {
    e.preventDefault();
    const name = trim(composeNewForm.elements['draft_name']?.value || '');
    if (!name) {
      setComposeNewStatus('Enter a draft name to save.', 'error');
      composeNewForm.elements['draft_name']?.focus();
      return;
    }
    let payload;
    try { payload = buildIssue(); }
    catch (err) { setComposeNewStatus('Build error: ' + err.message, 'error'); return; }
    setComposeNewStatus('Saving…');
    try {
      const res = await fetch('/api/drafts', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ name, payload }),
      });
      if (!res.ok) {
        let detail = '';
        try { detail = (await res.json()).detail || ''; } catch { /* ignore */ }
        setComposeNewStatus(`Save ${res.status}: ${detail || res.statusText}`, 'error');
        return;
      }
      setComposeNewStatus('Saved.', 'ok');
      setTimeout(startFresh, 350);
    } catch (err) {
      setComposeNewStatus('Network error: ' + err.message, 'error');
    }
  });

  // ───────── Hamburger menu ─────────────────────────────────────────
  const menuTrigger = document.getElementById('open-menu');
  const mainMenu = document.getElementById('main-menu');
  const closeMainMenu = () => {
    if (!mainMenu || mainMenu.hidden) return;
    mainMenu.hidden = true;
    menuTrigger?.setAttribute('aria-expanded', 'false');
  };
  const openMainMenu = () => {
    if (!mainMenu) return;
    mainMenu.hidden = false;
    menuTrigger?.setAttribute('aria-expanded', 'true');
  };
  if (menuTrigger && mainMenu) {
    menuTrigger.addEventListener('click', (e) => {
      e.preventDefault();
      e.stopPropagation();
      if (mainMenu.hidden) openMainMenu();
      else closeMainMenu();
    });
    // Click outside collapses the menu; clicks on menu items also close
    // (after their dialog-open handler runs — bindings stack).
    document.addEventListener('click', (e) => {
      if (mainMenu.hidden) return;
      if (mainMenu.contains(e.target) || menuTrigger.contains(e.target)) return;
      closeMainMenu();
    });
    mainMenu.querySelectorAll('button[role="menuitem"]').forEach((b) => {
      b.addEventListener('click', () => closeMainMenu());
    });
    document.addEventListener('keydown', (e) => {
      if (e.key === 'Escape') closeMainMenu();
    });
  }

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
    'newsletter_name',
    'from_name', 'from_email', 'to_email', 'reply_to',
    'unsubscribe_url', 'masthead_logo_url',
    // Hidden field — value is a Media Library id (int) for an uploaded
    // masthead logo. Driven by the Upload button below the URL row;
    // snapshot/collect logic treats empty string as "unset" and any
    // non-empty value as an int.
    'masthead_logo_media_id',
    'initial_publication_date',
    'default_intro_html', 'default_footer_html',
    'preview_debounce_ms', 'auto_archive_on_download',
    'include_read_more_link', 'confirm_before_delete',
    'embed_all_media',
    // Autosave loop interval (0 = disabled). Wired up by the
    // autosave block further down — applySettingsToRuntime kicks
    // restartAutosave whenever this value changes.
    'autosave_interval_seconds',
    'load_last_draft_on_login',
  ];

  const setSettingsStatus = statusSetter(settingsStatus);

  // Dirty-state tracking — Save is enabled (and Discard surfaced) only
  // when the form values differ from the currentSettings snapshot.
  // Snapshot is rebuilt on every fillSettingsForm so pressing Save
  // resets dirty without needing a separate "saved" event hop.
  const settingsSaveBtn = document.getElementById('settings-save');
  const settingsDiscardBtn = document.getElementById('settings-discard');
  const settingsDirtyWarning = document.getElementById('settings-dirty-warning');
  let _settingsBaseline = '{}';
  const _snapshotSettingsForm = () => {
    const out = {};
    SETTINGS_FIELDS.forEach((k) => {
      const el = settingsForm.elements[k];
      if (!el) return;
      out[k] = el.type === 'checkbox' ? !!el.checked : (el.value || '');
    });
    return JSON.stringify(out);
  };
  const refreshSettingsDirty = () => {
    const dirty = _snapshotSettingsForm() !== _settingsBaseline;
    if (settingsSaveBtn) settingsSaveBtn.disabled = !dirty;
    if (settingsDiscardBtn) settingsDiscardBtn.hidden = !dirty;
    if (settingsDirtyWarning) settingsDirtyWarning.hidden = !dirty;
    return dirty;
  };

  const fillSettingsForm = (s) => {
    SETTINGS_FIELDS.forEach((k) => {
      const el = settingsForm.elements[k];
      if (!el) return;
      const v = s[k];
      if (el.type === 'checkbox') el.checked = !!v;
      else {
        el.value = v == null ? '' : v;
        syncQuillFromTextarea(el);
      }
      });

    // Hidden masthead-logo media id was just (re-)assigned by the loop;
    // re-render the thumbnail/clear-button affordance from the stored value.
    syncMastheadThumb();
    setMastheadStatus('');
    // Fetch the referenced media item (if any) and (re)populate the
    // inline alt-text editor so reopening Settings on an existing
    // upload picks up wherever the description was last left.
    syncMastheadAltEditor();
    // Re-baseline + reset Save state once the form mirrors settings.
    _settingsBaseline = _snapshotSettingsForm();
    refreshSettingsDirty();
  };

  // Any form change re-evaluates dirty state — bound at module load
  // since the form lives in the static markup (not cloned per-open).
  settingsForm.addEventListener('input', refreshSettingsDirty);
  settingsForm.addEventListener('change', refreshSettingsDirty);

  // ─── Masthead-logo upload affordance ─────────────────────────────────
  // The Settings form pairs a URL input (manual paste) with an Upload
  // button that pushes the file through /api/media/upload (kind=logo).
  // On success the returned Media Library id lands in a hidden
  // <input name="masthead_logo_media_id"> — that hidden value flows
  // through collectSettings just like any other field, persists into
  // AppSettings, and is later mirrored onto the NewsletterIssue payload
  // in buildIssue(). The eml controller resolves the id to inline bytes
  // (cid:) at .eml-build time so the recipient's mail client never
  // needs to fetch the logo from the deployed app.
  const mastheadHidden = settingsForm.elements['masthead_logo_media_id'];
  const mastheadThumb = document.getElementById('masthead-thumb');
  const mastheadUploadBtn = document.getElementById('masthead-upload-btn');
  const mastheadClearBtn = document.getElementById('masthead-clear-btn');
  const mastheadFileInput = document.getElementById('masthead-upload-file');
  const mastheadStatusEl = document.getElementById('masthead-upload-status');
  const mastheadAltRow = document.querySelector('[data-role="masthead-alt-row"]');
  const mastheadAltInput = document.getElementById('masthead-alt-input');
  const mastheadAltStatusEl = document.getElementById('masthead-alt-status');

  const setMastheadStatus = statusSetter(mastheadStatusEl);

  // Shared inline alt-text editor — same helper as the Article-card
  // path uses. onSaved swaps the upload-row status from the
  // "Save-will-be-rejected" warning to "Image described — press Save
  // to apply." once the description lands.
  const mastheadAltEditor = bindAltTextEditor({
    input: mastheadAltInput,
    statusEl: mastheadAltStatusEl,
    rowEl: mastheadAltRow,
    onSaved: ({ value }) => {
      if (value) setMastheadStatus('Image described — press Save to apply.', 'ok');
    },
  });
  const showMastheadAltEditor = mastheadAltEditor.show;

  // Reflect the hidden input's current value into the thumbnail + clear
  // button visibility. Called whenever the value changes (programmatic
  // set after upload, clear button, fillSettingsForm on dialog open).
  const syncMastheadThumb = () => {
    if (!mastheadHidden || !mastheadThumb || !mastheadClearBtn) return;
    const id = mastheadHidden.value;
    if (id) {
      // Append the id as a cache-busting hint so a fresh upload that
      // happens to reuse the same id (unlikely, but possible across
      // teardown + reupload cycles) doesn't show the stale image.
      mastheadThumb.src = `/api/media/${encodeURIComponent(id)}/content?_=${id}`;
      mastheadThumb.hidden = false;
      mastheadClearBtn.hidden = false;
    } else {
      mastheadThumb.removeAttribute('src');
      mastheadThumb.hidden = true;
      mastheadClearBtn.hidden = true;
    }
  };

  // Refresh the alt-text editor whenever the masthead media id changes
  // (open dialog rehydrate, fresh upload, clear). Fetches the media
  // item to populate alt_text in the input. Hidden when no id.
  const syncMastheadAltEditor = async () => {
    const id = mastheadHidden?.value;
    if (!id) {
      showMastheadAltEditor(null);
      return;
    }
    try {
      const res = await fetch(`/api/media/${encodeURIComponent(id)}`);
      if (!res.ok) {
        showMastheadAltEditor(null);
        return;
      }
      showMastheadAltEditor(await res.json());
    } catch {
      showMastheadAltEditor(null);
    }
  };

  if (mastheadUploadBtn && mastheadFileInput) {
    mastheadUploadBtn.addEventListener('click', (e) => {
      e.preventDefault();
      mastheadFileInput.click();
    });
    mastheadFileInput.addEventListener('change', async () => {
      const file = mastheadFileInput.files && mastheadFileInput.files[0];
      if (!file) return;
      mastheadUploadBtn.disabled = true;
      setMastheadStatus('Uploading…');
      try {
        const item = await uploadMedia({ file, kind: 'logo' });
        mastheadHidden.value = String(item.id);
        syncMastheadThumb();
        refreshSettingsDirty();
        // Surface the inline alt-text editor directly so the user can
        // satisfy the Save-time alt-text gate without leaving Settings.
        // The status message points at the (now-visible) input rather
        // than asking the user to detour through Logos & Media.
        showMastheadAltEditor(item);
        if (item.alt_text) {
          setMastheadStatus('Uploaded — press Save to apply.', 'ok');
        } else {
          setMastheadStatus('Uploaded. Fill the Image description below before Save.', 'error');
          // Pull focus into the alt-text input so the editor can just
          // start typing — no extra click required.
          mastheadAltInput?.focus();
        }
      } catch (err) {
        if (err instanceof Response) await surfaceUploadError(err, setMastheadStatus);
        else setMastheadStatus('Network error: ' + err.message, 'error');
      } finally {
        mastheadUploadBtn.disabled = false;
        mastheadFileInput.value = '';
      }
    });
  }

  if (mastheadClearBtn) {
    mastheadClearBtn.addEventListener('click', (e) => {
      e.preventDefault();
      mastheadHidden.value = '';
      syncMastheadThumb();
      showMastheadAltEditor(null);
      refreshSettingsDirty();
      setMastheadStatus('Cleared. The Logos & Media item remains; only the reference is removed.');
    });
  }

  const collectSettings = () => {
    const out = {};
    SETTINGS_FIELDS.forEach((k) => {
      const el = settingsForm.elements[k];
      if (!el) return;
      if (el.type === 'checkbox') {
        out[k] = el.checked;
      } else if (el.type === 'number' || k === 'masthead_logo_media_id') {
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
      if (el && val !== null && val !== undefined) {
        el.value = String(val);
        syncQuillFromTextarea(el);
      }
    };
    if (s.unsubscribe_url) set('unsubscribe_url', s.unsubscribe_url);
    if (s.default_intro_html) set('intro_html', s.default_intro_html);
    if (s.default_footer_html) set('footer_html', s.default_footer_html);
    if (typeof s.preview_debounce_ms === 'number' && s.preview_debounce_ms >= 0) {
      previewDebounceMs = s.preview_debounce_ms;
    }
    // Autosave interval lives in settings so a power user can dial it
    // down on a slow link. restartAutosave handles 0 (disabled) and
    // re-arms the timer when the interval changes.
    if (typeof restartAutosave === 'function') {
      restartAutosave(typeof s.autosave_interval_seconds === 'number'
        ? s.autosave_interval_seconds
        : 45);
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
    openDialog(settingsDialog);
  };

  const closeSettingsDialog = () => closeDialog(settingsDialog);

  // Close-with-unsaved guard: when the form is dirty, ask the user
  // whether to discard. Confirmed → drop their edits and close.
  // Cancelled → leave the dialog open so they can Save or keep
  // editing. The earlier behavior refused to close at all, which
  // trapped the user in the dialog whenever the inline Discard
  // button was overlooked.
  const tryCloseSettingsDialog = () => {
    if (refreshSettingsDirty()) {
      const ok = window.confirm(
        'You have unsaved Settings changes.\n\nDiscard them and close?',
      );
      if (!ok) {
        setSettingsStatus('Save or Discard your changes first.', 'error');
        settingsForm.querySelector('input, textarea, select')?.focus();
        return false;
      }
      // User chose Discard — wipe edits, then close.
      discardSettingsChanges();
    }
    closeSettingsDialog();
    return true;
  };

  const discardSettingsChanges = () => {
    if (currentSettings) fillSettingsForm(currentSettings);
    setSettingsStatus('Discarded.', 'ok');
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
      // Re-baseline the form so the Save/Discard pair go quiet again.
      fillSettingsForm(currentSettings);
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
      syncQuillFromTextarea(el);
    });

    // Wipe any existing sortable sections so the loaded issue is the
    // single source of truth — drafts/templates fully replace, never
    // merge, the form's sortable region.
    sortableSections.innerHTML = '';

    // Reconstruct sections from the saved `form_sections` metadata when
    // present. Each entry slices articles[]/static_blocks[] in order,
    // so multi-section drafts round-trip exactly. Without form_sections
    // we fall back to one section of each type — backward compatible
    // with drafts saved by older builds of the composer.
    const articleList = Array.isArray(issue.articles) ? issue.articles : [];
    const staticList = Array.isArray(issue.static_blocks) ? issue.static_blocks : [];
    const podcast = (issue.podcast && typeof issue.podcast === 'object') ? issue.podcast : null;
    const formSections = Array.isArray(issue.form_sections) ? issue.form_sections : null;

    let articleCursor = 0;
    let staticCursor = 0;
    let podcastConsumed = false;

    if (formSections && formSections.length) {
      formSections.forEach((entry) => {
        if (!entry || typeof entry !== 'object') return;
        const type = entry.type;
        const sec = addSection(type, { id: entry.id || null });
        if (!sec) return;
        if (type === 'articles') {
          const count = Number.isInteger(entry.count) ? Math.max(0, entry.count) : articleList.length;
          const slice = articleList.slice(articleCursor, articleCursor + count);
          articleCursor += slice.length;
          slice.forEach((a) => addArticle(a || {}, articlesContainerOf(sec)));
        } else if (type === 'static_blocks') {
          const count = Number.isInteger(entry.count) ? Math.max(0, entry.count) : staticList.length;
          const slice = staticList.slice(staticCursor, staticCursor + count);
          staticCursor += slice.length;
          slice.forEach((b) => addStatic(b || {}, staticsContainerOf(sec)));
        } else if (type === 'podcast') {
          const cb = podcastEnabledInputOf(sec);
          const fields = podcastFieldsOf(sec);
          // Only the first podcast section's data exists in the model;
          // additional podcast sections restore as empty (the user can
          // tick "Include podcast section" to populate).
          if (entry.enabled && podcast && !podcastConsumed) {
            podcastConsumed = true;
            cb.checked = true;
            fields.hidden = false;
            const node = renderPodcastFields(sec);
            ['episode_number', 'guest', 'title', 'zoomcast_url', 'podcast_url', 'youtube_video_url', 'summary'].forEach((k) => {
              const el = node.querySelector(`[data-field="${k}"]`);
              if (el && podcast[k] != null) el.value = String(podcast[k]);
            });
            if (Array.isArray(podcast.timecodes)) {
              podcast.timecodes.forEach((tc) =>
                addTimecode(tc || {}, node.querySelector('#timecodes')));
            }
          } else {
            cb.checked = false;
            fields.hidden = true;
          }
        }
      });
    }

    // Tail: any leftover articles / static blocks / podcast not covered
    // by form_sections fall through into freshly-spawned sections so
    // nothing is silently dropped during a partial-payload load.
    const tailArticles = articleList.slice(articleCursor);
    if (tailArticles.length || sortableSectionsOfType('articles').length === 0) {
      const sec = sortableSectionsOfType('articles').slice(-1)[0]
        || addSection('articles');
      tailArticles.forEach((a) => addArticle(a || {}, articlesContainerOf(sec)));
      if (sec.querySelectorAll('.article-card').length === 0) {
        addArticle({}, articlesContainerOf(sec));
      }
    }
    const tailStatic = staticList.slice(staticCursor);
    if (tailStatic.length || sortableSectionsOfType('static_blocks').length === 0) {
      const sec = sortableSectionsOfType('static_blocks').slice(-1)[0]
        || addSection('static_blocks');
      tailStatic.forEach((b) => addStatic(b || {}, staticsContainerOf(sec)));
    }
    if (!podcastConsumed) {
      const podSec = sortableSectionsOfType('podcast')[0] || addSection('podcast');
      const cb = podcastEnabledInputOf(podSec);
      const fields = podcastFieldsOf(podSec);
      if (podcast) {
        cb.checked = true;
        fields.hidden = false;
        const node = renderPodcastFields(podSec);
        ['episode_number', 'guest', 'title', 'zoomcast_url', 'podcast_url', 'youtube_video_url', 'summary'].forEach((k) => {
          const el = node.querySelector(`[data-field="${k}"]`);
          if (el && podcast[k] != null) el.value = String(podcast[k]);
        });
        if (Array.isArray(podcast.timecodes)) {
          podcast.timecodes.forEach((tc) => addTimecode(tc || {}, node.querySelector('#timecodes')));
        }
      } else {
        cb.checked = false;
        fields.hidden = true;
      }
    }
    updateSectionDragVisibility();

    // Highlights — clear, then re-add. Must run after articles so the
    // refreshHighlightOptions select gets the right option list.
    highlightsContainer.innerHTML = '';
    (Array.isArray(issue.highlights) ? issue.highlights : []).forEach((h) => {
      addHighlight(h || {});
    });

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

  const setTemplatesStatus = statusSetter(templatesStatus);

  const escapeHtml = (s) =>
    String(s == null ? '' : s).replace(/[<>&"']/g, (c) =>
      ({ '<': '&lt;', '>': '&gt;', '&': '&amp;', '"': '&quot;', "'": '&#39;' }[c]));

  // Trash-icon SVG markup reused by every delete affordance (templates,
  // drafts, archive). Bundled here so tweaks happen in one place.
  const TRASH_ICON_SVG = (
    '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" '
    + 'stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">'
    + '<polyline points="3 6 5 6 21 6"/>'
    + '<path d="M19 6l-1 14a2 2 0 0 1-2 2H8a2 2 0 0 1-2-2L5 6"/>'
    + '<path d="M10 11v6"/><path d="M14 11v6"/>'
    + '<path d="M9 6V4a2 2 0 0 1 2-2h2a2 2 0 0 1 2 2v2"/>'
    + '</svg>'
  );

  // ───────── Confirm-delete dialog ───────────────────────────────────
  // Replaces the old `confirm()` browser prompt for delete buttons. Returns
  // a Promise that resolves true on Delete and false on Cancel/close. If
  // the user has switched the setting off, we skip the dialog entirely
  // and resolve true so the call site doesn't need to branch.
  const confirmDeleteDialog = document.getElementById('confirm-delete-dialog');
  const confirmDeleteMessage = document.getElementById('confirm-delete-message');
  const confirmDeleteSkip = document.getElementById('confirm-delete-skip');
  let confirmDeleteResolve = null;
  const closeConfirmDeleteDialog = (result) => {
    closeDialog(confirmDeleteDialog);
    if (confirmDeleteResolve) {
      const r = confirmDeleteResolve;
      confirmDeleteResolve = null;
      r(!!result);
    }
  };
  const confirmDelete = (message) => {
    if (currentSettings && currentSettings.confirm_before_delete === false) {
      return Promise.resolve(true);
    }
    if (!confirmDeleteDialog || typeof confirmDeleteDialog.showModal !== 'function') {
      // Fall back to the old confirm() if <dialog> is unsupported.
      return Promise.resolve(window.confirm(message || 'Delete?'));
    }
    return new Promise((resolve) => {
      confirmDeleteResolve = resolve;
      if (confirmDeleteMessage) confirmDeleteMessage.textContent = message || 'This cannot be undone.';
      if (confirmDeleteSkip) confirmDeleteSkip.checked = false;
      confirmDeleteDialog.showModal();
    });
  };
  if (confirmDeleteDialog) {
    document.getElementById('confirm-delete-cancel').addEventListener('click', (e) => {
      e.preventDefault();
      closeConfirmDeleteDialog(false);
    });
    document.getElementById('confirm-delete-close').addEventListener('click', (e) => {
      e.preventDefault();
      closeConfirmDeleteDialog(false);
    });
    document.getElementById('confirm-delete-confirm').addEventListener('click', async (e) => {
      e.preventDefault();
      // Persist the "don't ask again" preference before resolving so the
      // next delete in this session honors the new setting.
      if (confirmDeleteSkip && confirmDeleteSkip.checked) {
        if (currentSettings) currentSettings.confirm_before_delete = false;
        try {
          await fetch('/api/settings', {
            method: 'PUT',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ ...(currentSettings || {}), confirm_before_delete: false }),
          });
        } catch { /* non-fatal */ }
      }
      closeConfirmDeleteDialog(true);
    });
    // Native dismissal (Escape key) should resolve as cancel, not orphan
    // the pending Promise.
    confirmDeleteDialog.addEventListener('close', () => {
      if (confirmDeleteResolve) {
        const r = confirmDeleteResolve;
        confirmDeleteResolve = null;
        r(false);
      }
    });
  }

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
          ${t.builtin ? '' : `<button type="button" class="btn-icon-danger" data-action="delete" aria-label="Delete template" title="Delete template">${TRASH_ICON_SVG}</button>`}
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
    if (!(await confirmDelete(`Delete template "${name}"? This cannot be undone.`))) return;
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
    openDialog(templatesDialog);
  };

  const closeTemplatesDialog = () => closeDialog(templatesDialog);

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

  const setDraftsStatus = statusSetter(draftsStatus);

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
          <button type="button" class="btn-icon-danger" data-action="delete" aria-label="Delete draft" title="Delete draft">${TRASH_ICON_SVG}</button>
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

  const _applyDraftToForm = (draft) => {
    loadIssueIntoForm(draft.payload || {});
    const dateEl = form.elements['issue_date'];
    if (dateEl && !dateEl.value) {
      dateEl.value = new Date().toISOString().slice(0, 10);
    }
    // Hand the autosave loop the freshly-loaded draft so subsequent
    // ticks PUT to *this* row instead of forking a separate Autosave
    // — copy.
    if (typeof stampAutosaveSaved === 'function') {
      try { stampAutosaveSaved(draft.id, JSON.stringify(buildIssue())); } catch { /* ignore */ }
    }
    validateRequired();
    doPreview();
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
      _applyDraftToForm(draft);
      setDraftsStatus(`Loaded "${draft.name}"`, 'ok');
      setTimeout(closeDraftsDialog, 350);
    } catch (e) {
      setDraftsStatus('Network error: ' + e.message, 'error');
    }
  };

  const loadLatestDraft = async () => {
    try {
      const res = await fetch('/api/drafts/latest');
      if (!res.ok) return;
      const draft = await res.json();
      _applyDraftToForm(draft);
    } catch { /* ignore */ }
  };

  const deleteDraft = async (id, name) => {
    if (!(await confirmDelete(`Delete draft "${name}"? This cannot be undone.`))) return;
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
    openDialog(draftsDialog);
  };

  const closeDraftsDialog = () => closeDialog(draftsDialog);

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
      // Re-target autosave at the freshly-named draft so the loop
      // stops PUTing the previous "Autosave — Untitled" row. The
      // success branch hasn't read the body yet, so this is the first
      // .json() call on `res`.
      try {
        const saved = await res.json();
        if (typeof stampAutosaveSaved === 'function' && saved?.id != null) {
          stampAutosaveSaved(saved.id, JSON.stringify(payload));
        }
      } catch { /* swallow — autosave will recover on the next tick */ }
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

  const setArchiveStatus = statusSetter(archiveStatus);

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
          <button type="button" class="btn-icon-danger" data-action="delete" aria-label="Delete archived issue" title="Delete archived issue">${TRASH_ICON_SVG}</button>
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
    if (!(await confirmDelete(`Delete archived issue "${label}"? This cannot be undone.`))) return;
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
    openDialog(archiveDialog);
  };

  const closeArchiveDialog = () => closeDialog(archiveDialog);

  document.getElementById('open-archive').addEventListener('click', openArchiveDialog);
  document.getElementById('archive-close').addEventListener('click', (e) => {
    e.preventDefault();
    closeArchiveDialog();
  });

  // ───────── Media Library dialog ───────────────────────────────────
  // Cached PNG/JPG library — Article logos + Static-block images. The
  // dialog lists every entry with a thumbnail (or placeholder for
  // pending/failed items), the source URL or filename, the kind +
  // status badge, and an inline editor for the required Image
  // description. Save persists via PUT /api/media/{id}; Delete removes
  // the entry. The Add-by-URL form below the list does an idempotent
  // POST /api/media — re-providing the same URL returns the existing
  // row rather than duplicating it.
  const mediaDialog = document.getElementById('media-dialog');
  const mediaList = document.getElementById('media-list');
  const mediaCreateForm = document.getElementById('media-create-form');
  const mediaUploadForm = document.getElementById('media-upload-form');
  const mediaStatus = document.getElementById('media-status');
  const mediaUploadStatus = document.getElementById('media-upload-status');
  const mediaTabs = document.querySelectorAll('#media-dialog .media-tab');

  const setMediaStatus = statusSetter(mediaStatus);
  const setMediaUploadStatus = statusSetter(mediaUploadStatus);

  // Tab strip — exactly one Add form is visible at a time. Hidden
  // forms keep their state so a half-typed URL survives a tab switch.
  const selectMediaTab = (which) => {
    mediaTabs.forEach((t) => {
      t.setAttribute('aria-selected', t.dataset.mediaTab === which ? 'true' : 'false');
    });
    if (mediaCreateForm) mediaCreateForm.hidden = which !== 'url';
    if (mediaUploadForm) mediaUploadForm.hidden = which !== 'upload';
  };
  mediaTabs.forEach((t) => {
    t.addEventListener('click', (e) => {
      e.preventDefault();
      selectMediaTab(t.dataset.mediaTab);
    });
  });

  const MEDIA_KIND_LABEL = { logo: 'Logo', image: 'Image' };
  const MEDIA_STATUS_LABEL = { pending: 'Pending', ready: 'Ready', failed: 'Failed' };

  const mediaThumbHtml = (item) => {
    if (item.has_bytes) {
      return `<img class="media-thumb" src="/api/media/${item.id}/content" alt="${escapeHtml(item.alt_text || 'Media thumbnail')}">`;
    }
    const label = item.status === 'failed' ? '!' : '…';
    const cls = item.status === 'failed' ? 'media-thumb media-thumb-failed' : 'media-thumb media-thumb-pending';
    return `<span class="${cls}" aria-hidden="true">${label}</span>`;
  };

  const mediaSourceHtml = (item) => {
    if (item.url) {
      return `<a href="${escapeHtml(item.url)}" target="_blank" rel="noopener">${escapeHtml(item.url)}</a>`;
    }
    return `<span class="media-source-upload">Uploaded${item.filename ? ` · ${escapeHtml(item.filename)}` : ''}</span>`;
  };

  const renderMediaList = (items) => {
    if (!Array.isArray(items) || items.length === 0) {
      mediaList.innerHTML = '<li class="hint">No media yet. Add by URL below or upload from an Article logo / Static block.</li>';
      return;
    }
    mediaList.innerHTML = items.map((m) => {
      const usable = !!(m.alt_text && m.alt_text.trim());
      return `
      <li data-media-id="${m.id}" class="template-row media-row${usable ? '' : ' media-row-needs-alt'}">
        <div class="media-thumb-wrap">${mediaThumbHtml(m)}</div>
        <div class="template-meta media-meta">
          <strong>${escapeHtml(MEDIA_KIND_LABEL[m.kind] || m.kind)}<span class="badge media-badge media-badge-${m.status}">${escapeHtml(MEDIA_STATUS_LABEL[m.status] || m.status)}</span></strong>
          <small class="media-source">${mediaSourceHtml(m)}</small>
          ${m.error ? `<small class="media-error">${escapeHtml(m.error)}</small>` : ''}
          <label class="media-alt-label">
            <span>Image description</span>
            <input type="text" class="media-alt-input" data-field="alt_text" value="${escapeHtml(m.alt_text || '')}" placeholder="Required before this item can be used" maxlength="500">
          </label>
        </div>
        <div class="template-actions media-actions">
          <button type="button" class="btn" data-action="save-alt">Save</button>
          <button type="button" class="btn-icon-danger" data-action="delete" aria-label="Delete media item" title="Delete media item">${TRASH_ICON_SVG}</button>
        </div>
      </li>
    `;
    }).join('');
  };

  const refreshMediaList = async () => {
    setMediaStatus('');
    try {
      const res = await fetch('/api/media');
      if (!res.ok) {
        setMediaStatus(`List failed: ${res.status}`, 'error');
        return;
      }
      renderMediaList(await res.json());
    } catch (e) {
      setMediaStatus('Network error: ' + e.message, 'error');
    }
  };

  const saveMediaAlt = async (id, altText) => {
    setMediaStatus('Saving…');
    try {
      const res = await fetch(`/api/media/${id}`, {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ alt_text: altText }),
      });
      if (!res.ok) {
        let detail = '';
        try { detail = (await res.json()).detail || ''; } catch { /* ignore */ }
        setMediaStatus(`Save ${res.status}: ${detail || res.statusText}`, 'error');
        return;
      }
      await refreshMediaList();
      setMediaStatus('Saved.', 'ok');
    } catch (e) {
      setMediaStatus('Network error: ' + e.message, 'error');
    }
  };

  const deleteMediaItem = async (id, label) => {
    if (!(await confirmDelete(`Delete media item "${label}"? This cannot be undone.`))) return;
    setMediaStatus('Deleting…');
    try {
      const res = await fetch(`/api/media/${id}`, { method: 'DELETE' });
      if (!res.ok) {
        let detail = '';
        try { detail = (await res.json()).detail || ''; } catch { /* ignore */ }
        setMediaStatus(`Delete ${res.status}: ${detail || res.statusText}`, 'error');
        return;
      }
      await refreshMediaList();
      setMediaStatus('Deleted.', 'ok');
    } catch (e) {
      setMediaStatus('Network error: ' + e.message, 'error');
    }
  };

  mediaList.addEventListener('click', (e) => {
    const btn = e.target.closest('button[data-action]');
    if (!btn) return;
    const row = btn.closest('[data-media-id]');
    if (!row) return;
    const id = Number(row.dataset.mediaId);
    if (btn.dataset.action === 'save-alt') {
      const input = row.querySelector('input[data-field="alt_text"]');
      saveMediaAlt(id, input ? input.value : '');
    } else if (btn.dataset.action === 'delete') {
      const label = row.querySelector('.media-source')?.textContent
        || row.querySelector('strong')?.textContent
        || `#${id}`;
      deleteMediaItem(id, label);
    }
  });

  const openMediaDialog = async () => {
    setMediaStatus('');
    setMediaUploadStatus('');
    mediaCreateForm.reset();
    if (mediaUploadForm) mediaUploadForm.reset();
    selectMediaTab('url');
    await refreshMediaList();
    openDialog(mediaDialog);
  };

  const closeMediaDialog = () => closeDialog(mediaDialog);

  mediaCreateForm.addEventListener('submit', async (e) => {
    e.preventDefault();
    const fd = new FormData(mediaCreateForm);
    const kind = trim(fd.get('kind') || 'logo');
    const url = trim(fd.get('url') || '');
    if (!url) {
      setMediaStatus('URL required.', 'error');
      return;
    }
    setMediaStatus('Adding…');
    try {
      const res = await fetch('/api/media', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ kind, url: sanitizeUrl(url) }),
      });
      if (!res.ok) {
        let detail = '';
        try { detail = (await res.json()).detail || ''; } catch { /* ignore */ }
        setMediaStatus(`Add ${res.status}: ${detail || res.statusText}`, 'error');
        return;
      }
      mediaCreateForm.reset();
      await refreshMediaList();
      setMediaStatus('Added — fill in the Image description before use.', 'ok');
    } catch (err) {
      setMediaStatus('Network error: ' + err.message, 'error');
    }
  });

  // Logos & Media upload flow is now button-driven (matches Settings
  // masthead): the visible "Upload…" button opens the OS file picker
  // via the hidden file input's .click(); the picker's selection
  // event drives the actual POST. No separate submit step — the user
  // picks `kind`, clicks Upload, picks a file, and the row appears in
  // the list above. Keeping the form submit handler as a fallback
  // (in case someone presses Enter while focused inside the form)
  // keeps the original UX path alive.
  const mediaUploadTrigger = document.getElementById('media-upload-trigger');
  const mediaUploadFileInput = document.getElementById('media-upload-file');
  const _runMediaUpload = async () => {
    const file = mediaUploadFileInput?.files?.[0];
    if (!file || file.size === 0) {
      setMediaUploadStatus('Choose a PNG or JPG file first.', 'error');
      return;
    }
    const kindEl = mediaUploadForm?.elements?.kind;
    const kind = kindEl ? kindEl.value : 'logo';
    setMediaUploadStatus('Uploading…');
    if (mediaUploadTrigger) mediaUploadTrigger.disabled = true;
    try {
      await uploadMedia({ file, kind });
      // Reset the file input so the same file can be re-picked if the
      // user actually wants to re-upload (without `.value = ''` the
      // change event won't fire on a same-file pick).
      if (mediaUploadFileInput) mediaUploadFileInput.value = '';
      await refreshMediaList();
      setMediaUploadStatus('Uploaded — fill in the Image description before use.', 'ok');
    } catch (err) {
      if (err instanceof Response) await surfaceUploadError(err, setMediaUploadStatus);
      else setMediaUploadStatus('Network error: ' + err.message, 'error');
    } finally {
      if (mediaUploadTrigger) mediaUploadTrigger.disabled = false;
    }
  };
  if (mediaUploadTrigger && mediaUploadFileInput) {
    mediaUploadTrigger.addEventListener('click', (e) => {
      e.preventDefault();
      // Clear so the change event fires even when the user re-picks
      // the same file after a failed upload.
      mediaUploadFileInput.value = '';
      mediaUploadFileInput.click();
    });
    mediaUploadFileInput.addEventListener('change', () => {
      if (!mediaUploadFileInput.files?.[0]) return;
      _runMediaUpload();
    });
  }
  if (mediaUploadForm) {
    mediaUploadForm.addEventListener('submit', (e) => {
      // Keep submit as a fallback (Enter key inside the form, etc.) —
      // route through the same upload runner.
      e.preventDefault();
      _runMediaUpload();
    });
  }

  document.getElementById('open-media').addEventListener('click', openMediaDialog);
  document.getElementById('media-close').addEventListener('click', (e) => {
    e.preventDefault();
    closeMediaDialog();
  });

  // ───────── Library picker ─────────────────────────────────────────
  // Reusable mini-picker used by Article Logo (kind=logo) and the
  // forthcoming Static-block image-embed toolbar (kind=image). The
  // dialog filters /api/media to one kind, dims items missing an
  // alt_text (still clickable but the calling action surfaces the
  // missing description so the user can fix it in Media Library).
  const mediaPickerDialog = document.getElementById('media-picker-dialog');
  const mediaPickerList = document.getElementById('media-picker-list');
  const mediaPickerStatus = document.getElementById('media-picker-status');
  const mediaPickerHint = document.getElementById('media-picker-hint');
  const mediaPickerTitle = document.getElementById('media-picker-title');
  const mediaPickerUrlForm = document.getElementById('media-picker-url-form');
  const mediaPickerUrlStatus = document.getElementById('media-picker-url-status');
  const setMediaPickerUrlStatus = statusSetter(mediaPickerUrlStatus);

  let _pickerCallback = null;

  const setMediaPickerStatus = statusSetter(mediaPickerStatus);

  const renderPickerList = (items, kindFilter) => {
    const filtered = items.filter((m) => !kindFilter || m.kind === kindFilter);
    if (filtered.length === 0) {
      mediaPickerList.innerHTML = `<li class="hint">No ${kindFilter || 'media'} entries yet — add one in Logos & Media first.</li>`;
      return;
    }
    mediaPickerList.innerHTML = filtered.map((m) => {
      const usable = !!(m.alt_text && m.alt_text.trim());
      const source = m.url
        ? escapeHtml(m.url)
        : `Uploaded${m.filename ? ' · ' + escapeHtml(m.filename) : ''}`;
      const thumb = m.has_bytes
        ? `<img class="media-thumb" src="/api/media/${m.id}/content" alt="${escapeHtml(m.alt_text || 'Media thumbnail')}">`
        : `<span class="media-thumb media-thumb-${m.status === 'failed' ? 'failed' : 'pending'}" aria-hidden="true">${m.status === 'failed' ? '!' : '…'}</span>`;
      return `
        <li data-media-id="${m.id}" class="template-row media-row${usable ? '' : ' media-row-needs-alt'}">
          <div class="media-thumb-wrap">${thumb}</div>
          <div class="template-meta media-meta">
            <strong>${escapeHtml(m.alt_text || '(no description)')}</strong>
            <small class="media-source">${source}</small>
          </div>
        </li>`;
    }).join('');
  };

  // Track the picker's current `kind` so the URL-fetch form below
  // knows what to POST. Set on every openLibraryPicker, cleared on
  // close so a stale value doesn't slip into a later URL fetch.
  let _pickerKind = null;
  const openLibraryPicker = async ({ kind, title, onPick }) => {
    _pickerCallback = onPick;
    _pickerKind = kind || null;
    if (mediaPickerTitle) mediaPickerTitle.textContent = title || 'Pick from Logos & Media';
    if (mediaPickerHint) {
      mediaPickerHint.textContent =
        'Click an item to use it. Items missing an Image description are dimmed — set one in Logos & Media before referencing.';
    }
    // Reset the URL-fetch form so values from the last open don't
    // bleed across.
    if (mediaPickerUrlForm) {
      mediaPickerUrlForm.reset();
      setMediaPickerUrlStatus('');
    }
    setMediaPickerStatus('Loading…');
    try {
      const res = await fetch('/api/media');
      if (!res.ok) {
        setMediaPickerStatus(`List failed: ${res.status}`, 'error');
        renderPickerList([], kind);
      } else {
        renderPickerList(await res.json(), kind);
        setMediaPickerStatus('');
      }
    } catch (err) {
      setMediaPickerStatus('Network error: ' + err.message, 'error');
      renderPickerList([], kind);
    }
    openDialog(mediaPickerDialog);
  };

  const closeLibraryPicker = () => {
    _pickerCallback = null;
    _pickerKind = null;
    closeDialog(mediaPickerDialog);
  };

  mediaPickerList.addEventListener('click', async (e) => {
    const row = e.target.closest('.media-row');
    if (!row) return;
    const id = Number(row.dataset.mediaId);
    if (!id) return;
    setMediaPickerStatus('Fetching…');
    try {
      const res = await fetch(`/api/media/${id}`);
      if (!res.ok) {
        setMediaPickerStatus(`Fetch ${res.status}`, 'error');
        return;
      }
      const item = await res.json();
      const cb = _pickerCallback;
      closeLibraryPicker();
      if (cb) cb(item);
    } catch (err) {
      setMediaPickerStatus('Network error: ' + err.message, 'error');
    }
  });

  if (mediaPickerUrlForm) {
    mediaPickerUrlForm.addEventListener('submit', async (e) => {
      e.preventDefault();
      const fd = new FormData(mediaPickerUrlForm);
      const url = trim(fd.get('url') || '');
      const altText = trim(fd.get('alt_text') || '');
      if (!url) {
        setMediaPickerUrlStatus('URL is required.', 'error');
        return;
      }
      if (!altText) {
        setMediaPickerUrlStatus('Image description is required.', 'error');
        return;
      }
      if (!_pickerKind) {
        // Defensive: the picker should always carry a kind, but bail
        // cleanly if it doesn't rather than POSTing nonsense.
        setMediaPickerUrlStatus('Picker is missing a kind context.', 'error');
        return;
      }
      setMediaPickerUrlStatus('Fetching…');
      try {
        // Step 1: register / upsert the URL in Logos & Media. Idempotent
        // on URL, so re-submitting the same URL returns the existing
        // row (kind preserved). The endpoint 403s when
        // AppSettings.media_url_imports_enabled is off — surface that
        // verbatim.
        const regRes = await fetch('/api/media', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ kind: _pickerKind, url: sanitizeUrl(url) }),
        });
        if (!regRes.ok) {
          let detail = '';
          try { detail = (await regRes.json()).detail || ''; } catch { /* ignore */ }
          setMediaPickerUrlStatus(`${regRes.status}: ${detail || regRes.statusText}`, 'error');
          return;
        }
        let item = await regRes.json();
        // Step 2: stamp the Image description so the picker's
        // onPick consumer doesn't get blocked on empty alt_text.
        // Skip when the existing row already had a non-empty
        // description (preserve operator intent).
        if (!(item.alt_text || '').trim()) {
          const altRes = await fetch(`/api/media/${item.id}`, {
            method: 'PUT',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ alt_text: altText }),
          });
          if (altRes.ok) {
            item = await altRes.json();
          }
        }
        const cb = _pickerCallback;
        closeLibraryPicker();
        if (cb) cb(item);
      } catch (err) {
        setMediaPickerUrlStatus('Network error: ' + err.message, 'error');
      }
    });
  }

  document.getElementById('media-picker-close').addEventListener('click', (e) => {
    e.preventDefault();
    closeLibraryPicker();
  });
  document.getElementById('media-picker-cancel').addEventListener('click', (e) => {
    e.preventDefault();
    closeLibraryPicker();
  });

  document.getElementById('open-settings').addEventListener('click', openSettingsDialog);
  document.getElementById('settings-close').addEventListener('click', (e) => {
    e.preventDefault();
    tryCloseSettingsDialog();
  });
  if (settingsDiscardBtn) {
    settingsDiscardBtn.addEventListener('click', (e) => {
      e.preventDefault();
      discardSettingsChanges();
    });
  }
  // The native <dialog> Esc key fires a 'cancel' event — route it
  // through the same guard so a dirty form can't be discarded by Esc.
  settingsDialog.addEventListener('cancel', (e) => {
    e.preventDefault();
    tryCloseSettingsDialog();
  });
  settingsForm.addEventListener('submit', saveSettings);

  // ───────── Initial seed: today's date + default sections ──────────
  // The composer ships with one of each sortable section type so a
  // brand-new draft has the same starting layout the user has always
  // seen. Extra sections are added on demand via the FAB.
  const today = new Date().toISOString().slice(0, 10);
  form.elements['issue_date'].value = today;
  const seedArticlesSection = addSection('articles');
  addSection('podcast');
  const seedStaticsSection = addSection('static_blocks');
  addArticle(
    { headline: '', url: '', read_more_label: 'Read more' },
    articlesContainerOf(seedArticlesSection),
  );
  addStatic(
    {
      title: 'Sponsor / book / conference',
      html: '<p>Carry-forward block — duplicate from last issue.</p>',
      enabled: true,
    },
    staticsContainerOf(seedStaticsSection),
  );
  updateSectionDragVisibility();

  // ───────── Autosave loop ───────────────────────────────────────────
  // Periodically captures the in-progress issue as a draft so a tab
  // close / browser crash / network blip never burns an hour of edits.
  // Lifecycle:
  //   - boot: seed _lastSavedSerialized from the current form so the
  //     first tick is a no-op against a brand-new blank form.
  //   - tick: every ``autosave_interval_seconds`` (driven from
  //     AppSettings; 0 disables). buildIssue → JSON.stringify, compare
  //     to last-saved snapshot. If equal, just stamp the status with
  //     "Up to date". If different, POST or PUT depending on whether
  //     we already have an _activeAutosaveDraftId.
  //   - manual save: when the user uses the Save-draft dialog, the
  //     manual save path (further down in this file) also stamps
  //     _activeAutosaveDraftId so subsequent autosaves PUT against
  //     that draft instead of forking a separate "Autosave —" row.
  //   - draft load / Compose New: reset _activeAutosaveDraftId so the
  //     next autosave starts fresh against the loaded draft.
  //   - beforeunload: sendBeacon a final save if dirty so the keystrokes
  //     between the last tick and the close don't get lost.
  const autosaveStatusEl = document.getElementById('autosave-status');
  let _activeAutosaveDraftId = null;
  let _lastSavedSerialized = '';
  let _autosaveTimer = null;
  let _autosaveIntervalMs = 45_000;
  let _autosaveInFlight = false;
  let _autosaveLastWrittenAt = null;

  const setAutosaveStatus = statusSetter(autosaveStatusEl);

  // Format "Saved 2 min ago" for the status pill. Tries to match the
  // cadence the user actually cares about — "just now" vs minutes vs
  // hours — and rounds down to avoid the slightly-off "0 sec ago".
  const _humanizeSince = (when) => {
    if (!when) return '';
    const sec = Math.max(0, Math.floor((Date.now() - when) / 1000));
    if (sec < 5) return 'just now';
    if (sec < 60) return `${sec} sec ago`;
    const min = Math.floor(sec / 60);
    if (min < 60) return `${min} min ago`;
    const hr = Math.floor(min / 60);
    return `${hr} h ago`;
  };

  const _serializeForAutosave = () => {
    try {
      return JSON.stringify(buildIssue());
    } catch {
      return null;
    }
  };

  // Public-ish: callers can stamp the "saved" state when they know a
  // full save just landed (manual draft save, draft load, etc.) so
  // the next autosave tick sees a clean baseline.
  const stampAutosaveSaved = (id, serialized) => {
    if (id != null) _activeAutosaveDraftId = id;
    if (serialized != null) _lastSavedSerialized = serialized;
    _autosaveLastWrittenAt = Date.now();
    setAutosaveStatus(`Saved ${_humanizeSince(_autosaveLastWrittenAt)}`, 'saved');
  };

  // Called from compose-new and other "blank slate" entry points so
  // the autosave loop opens a fresh draft instead of overwriting the
  // previous edit's autosave row.
  const resetAutosaveTarget = () => {
    _activeAutosaveDraftId = null;
    _lastSavedSerialized = _serializeForAutosave() || '';
    _autosaveLastWrittenAt = null;
    setAutosaveStatus('', 'idle');
  };

  // Snapshot refresh: refresh the saved baseline without writing to
  // the server — used when we want autosave to "consider this state
  // already saved" (e.g. after loadIssueIntoForm replaces the form).
  const refreshAutosaveBaseline = () => {
    _lastSavedSerialized = _serializeForAutosave() || '';
  };

  // Any open <dialog> means the user is mid-modal (Settings, Drafts,
  // Logos & Media, Save-draft, Compose-new, confirm-delete, …). The
  // autosave loop should NOT fire while a dialog is open — the form
  // beneath may be in a transient state the user wouldn't choose to
  // freeze into a draft, and triggering a network save while a modal
  // is on screen can interleave error toasts in confusing ways.
  const _isAnyDialogOpen = () => !!document.querySelector('dialog[open]');

  const _autosaveTick = async () => {
    if (_autosaveInFlight) return;
    if (_isAnyDialogOpen()) {
      // Surface the pause so the operator isn't surprised when the
      // "Saved N sec ago" label stops ticking.
      setAutosaveStatus('Paused (dialog open)', 'disabled');
      return;
    }
    // Bail when the user hasn't actually typed anything yet — a blank
    // form is the default state and not worth a draft row.
    const titleEl = form.elements['issue_title'];
    if (titleEl && !titleEl.value.trim()) return;
    const serialized = _serializeForAutosave();
    if (!serialized) {
      // buildIssue threw (probably a half-typed URL); skip this tick
      // and try again on the next.
      return;
    }
    if (serialized === _lastSavedSerialized) {
      // No changes — surface "Up to date" so the operator can see the
      // loop is alive even when there's nothing to do.
      if (_autosaveLastWrittenAt) {
        setAutosaveStatus(`Saved ${_humanizeSince(_autosaveLastWrittenAt)}`, 'saved');
      }
      return;
    }
    _autosaveInFlight = true;
    setAutosaveStatus('Saving…', 'saving');
    try {
      const payload = JSON.parse(serialized);
      let res, item;
      if (_activeAutosaveDraftId != null) {
        res = await fetch(`/api/drafts/${_activeAutosaveDraftId}`, {
          method: 'PUT',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ payload }),
        });
      } else {
        const title = (titleEl && titleEl.value.trim()) || 'Untitled';
        const stamp = new Date().toISOString().slice(0, 16).replace('T', ' ');
        res = await fetch('/api/drafts', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({
            name: `Autosave — ${title} (${stamp} UTC)`,
            payload,
          }),
        });
      }
      if (!res.ok) {
        // Surface the failure but keep the loop alive — next tick
        // will retry on the same dirty state, and a manual draft save
        // is still available.
        let detail = '';
        try { detail = (await res.json()).detail || ''; } catch { /* ignore */ }
        setAutosaveStatus(`Autosave ${res.status}: ${detail || res.statusText}`, 'error');
        return;
      }
      item = await res.json();
      if (item && item.id != null) _activeAutosaveDraftId = item.id;
      _lastSavedSerialized = serialized;
      _autosaveLastWrittenAt = Date.now();
      setAutosaveStatus(`Saved ${_humanizeSince(_autosaveLastWrittenAt)}`, 'saved');
    } catch (err) {
      setAutosaveStatus(`Autosave network error: ${err.message}`, 'error');
    } finally {
      _autosaveInFlight = false;
    }
  };

  // Refresh the "Saved N sec/min ago" text without doing any actual
  // work — keeps the pill honest between full ticks.
  const _autosaveStatusRefresh = () => {
    if (_autosaveLastWrittenAt) {
      setAutosaveStatus(`Saved ${_humanizeSince(_autosaveLastWrittenAt)}`, 'saved');
    }
  };

  // Public-ish: AppSettings round-trip handler restarts the timer
  // when the configured interval changes (including 0 → disabled).
  const restartAutosave = (intervalSeconds) => {
    if (_autosaveTimer) {
      clearInterval(_autosaveTimer);
      _autosaveTimer = null;
    }
    if (!intervalSeconds || intervalSeconds <= 0) {
      setAutosaveStatus('Autosave off', 'disabled');
      return;
    }
    _autosaveIntervalMs = intervalSeconds * 1000;
    // Two timers: the main tick at the configured interval, and a
    // lighter status-refresh tick every 30s so the "Saved N min ago"
    // pill ages correctly without us doing real work each second.
    _autosaveTimer = setInterval(_autosaveTick, _autosaveIntervalMs);
    setInterval(_autosaveStatusRefresh, 30_000);
    setAutosaveStatus('Autosave on', 'idle');
  };

  // Final-flush before tab close. sendBeacon is the only fire-and-
  // forget transport that survives unload reliably across browsers.
  window.addEventListener('beforeunload', () => {
    if (_autosaveInFlight) return;
    const serialized = _serializeForAutosave();
    if (!serialized || serialized === _lastSavedSerialized) return;
    try {
      const payload = JSON.parse(serialized);
      let url, body;
      if (_activeAutosaveDraftId != null) {
        // Drafts PUT supports JSON only; sendBeacon supports a Blob
        // with the right Content-Type to keep the server-side parser
        // happy. The draft PUT endpoint accepts {payload} on its own
        // so this is a single-write final flush.
        url = `/api/drafts/${_activeAutosaveDraftId}`;
        body = JSON.stringify({ payload });
      } else {
        const title = (form.elements['issue_title']?.value || 'Untitled').trim();
        const stamp = new Date().toISOString().slice(0, 16).replace('T', ' ');
        url = '/api/drafts';
        body = JSON.stringify({
          name: `Autosave — ${title} (${stamp} UTC)`,
          payload,
        });
      }
      // PUT semantics: sendBeacon sends as POST regardless of the
      // method intent. We accept this trade-off only for the create
      // path; for the update path we fall back to fetch with keepalive
      // which DOES allow PUT.
      if (_activeAutosaveDraftId != null) {
        fetch(url, {
          method: 'PUT',
          headers: { 'Content-Type': 'application/json' },
          body,
          keepalive: true,
        });
      } else {
        navigator.sendBeacon(url, new Blob([body], { type: 'application/json' }));
      }
    } catch { /* swallow — best-effort */ }
  });

  // Wire compose-new to clear the autosave target so the next autosave
  // opens a fresh draft row instead of overwriting the previous edit.
  const _composeNewBtn = document.getElementById('compose-new');
  if (_composeNewBtn) {
    _composeNewBtn.addEventListener('click', () => {
      // The actual blank-form behavior is wired elsewhere; we just
      // piggyback to reset the autosave state at the same moment.
      resetAutosaveTarget();
    }, { capture: true });
  }

  // Wire the static-form rich-text toolbars (Intro Message lives in
  // Issue Details — not cloned from a template, so addArticle's
  // querySelectorAll loop won't pick it up). Anything inside the
  // composer <form> that carries data-quill-field and a sibling textarea
  // gets the same toolbar wiring.
  form.querySelectorAll('[data-quill-field]').forEach((scope) => {
    wireQuillEditor(scope);
  });
  settingsForm.querySelectorAll('[data-quill-field]').forEach((scope) => {
    wireQuillEditor(scope);
  });

  // Pull settings before the first preview so any saved defaults
  // (from_name/from_email/etc.) end up in the rendered .eml.
  loadSettings().then(async (s) => {
    if (s) {
      applySettingsToForm(s);
      if (s.load_last_draft_on_login) {
        await loadLatestDraft();
      }
    }
    validateRequired();
    doPreview();
    // applySettingsToForm called restartAutosave for us; seed the
    // baseline from the freshly-loaded form so the first tick doesn't
    // false-positive on the seed-default sections.
    refreshAutosaveBaseline();
  });
})();
