'use strict';

const $ = (id) => document.getElementById(id);
const pageSize = 20;
const selection = new Set();
let selectionKey = '';           // search parameters (minus paging) the selection belongs to
let activeRequest = null;
let nextOffset = null;
let currentOffset = 0;
let currentParams = new URLSearchParams();
let lastSearchURL = '/';
const canCopy = Boolean(navigator.clipboard && window.isSecureContext);

function element(tag, text, className) {
  const node = document.createElement(tag);
  if (text !== undefined) node.textContent = text;
  if (className) node.className = className;
  return node;
}
function link(text, href, className) {
  const node = element('a', text, className);
  node.href = href;
  return node;
}
const paperURL = (id) => '/papers/' + encodeURIComponent(id);
const apiURL = (id) => '/v1/papers/' + encodeURIComponent(id);
const dateText = (date) => date ? String(date).slice(0, 10) : 'Date unknown';
const sizeText = (bytes) => Number.isFinite(bytes) ? (bytes / 1e6).toLocaleString(undefined, {maximumFractionDigits: 2}) + ' MB' : '—';

async function request(path, signal, accept = 'application/json') {
  const response = await fetch(path, {signal, credentials: 'same-origin', headers: {'Accept': accept}});
  if (!response.ok) {
    let error = 'The archive is unavailable. Please try again.';
    if (response.status === 404) error = 'This paper was not found in the archive.';
    if (response.status === 400 || response.status === 503) {
      const body = await response.json().catch(() => null);
      if (response.status === 400) error = 'Check your search terms and filters.';
      if (body && typeof body.error === 'string' && body.error.trim()) error = body.error;
    }
    throw new Error(error);
  }
  return accept === 'application/json' ? response.json() : response.text();
}

// Copies the paper's BibTeX entry; the button itself reports the outcome.
function copyBibtexButton(id, className) {
  const button = element('button', 'Copy BibTeX', className);
  button.type = 'button';
  let timer = null;
  button.addEventListener('click', async () => {
    const label = button.textContent;
    button.disabled = true;
    try {
      await navigator.clipboard.writeText(await request(apiURL(id) + '/bibtex', undefined, 'text/plain'));
      button.textContent = 'Copied';
      button.classList.add('done');
    } catch {
      button.textContent = 'Copy failed';
    }
    button.disabled = false;
    clearTimeout(timer);
    timer = setTimeout(() => { button.textContent = label; button.classList.remove('done'); }, 1800);
  });
  return button;
}

function updateSelection() {
  const button = $('export-selected');
  button.disabled = selection.size === 0;
  button.textContent = selection.size ? `Export ${selection.size} selected as BibTeX` : 'Export selected as BibTeX';
}

// Same search with one filter changed and paging reset; clicking a tag narrows the current query.
function searchWith(changes) {
  const params = new URLSearchParams(currentParams);
  params.delete('limit');
  params.delete('offset');
  for (const [key, value] of Object.entries(changes)) {
    if (value) params.set(key, value); else params.delete(key);
  }
  return '/?' + params;
}

function metaLine(paper, id) {
  const meta = element('p', undefined, 'paper-meta');
  meta.append(element('span', dateText(paper.published)), element('span', 'arXiv:' + id, 'id'));
  for (const category of paper.categories || []) {
    const tag = link(category, searchWith({category}), 'tag');
    if (currentParams.get('category') === category) tag.classList.add('active');
    tag.title = 'Show ' + category + ' papers for this search';
    meta.append(tag);
  }
  return meta;
}

function paperRow(paper) {
  const article = element('article', undefined, 'paper-row');
  const check = element('input', undefined, 'selection');
  check.type = 'checkbox';
  check.setAttribute('aria-label', 'Select ' + (paper.title || paper.id));
  check.checked = selection.has(paper.id);
  check.addEventListener('change', () => {
    if (check.checked) selection.add(paper.id); else selection.delete(paper.id);
    updateSelection();
  });
  const content = element('div');
  const heading = element('h2');
  heading.append(link(paper.title || paper.id, paperURL(paper.id)));
  content.append(heading,
    element('p', paper.authors || 'Authors not recorded', 'authors'),
    metaLine(paper, paper.id),
    element('p', paper.abstract || 'No abstract recorded.', 'abstract-excerpt'));
  const actions = element('div', undefined, 'artifact-links');
  actions.append(paper.has_pdf ? link('PDF', apiURL(paper.id) + '/pdf') : element('span', 'No local PDF', 'unavailable'));
  // The detail page distinguishes packaged sources from the historical TeX subset.
  actions.append(paper.has_source ? link('LaTeX source', paperURL(paper.id) + '#sources') : element('span', 'No local LaTeX', 'unavailable'));
  actions.append(link('BibTeX', apiURL(paper.id) + '/bibtex'));
  if (canCopy) actions.append(copyBibtexButton(paper.id, 'text-button'));
  content.append(actions);
  article.append(check, content);
  return article;
}

function setSearchMessage(text, error = false) {
  $('search-message').textContent = text;
  $('search-message').className = error ? 'message error' : 'message';
  $('search-message').hidden = !text;
}

async function loadStatus(signal) {
  try {
    const status = await request('/v1/search/status', signal);
    const count = Number(status.indexed_papers || 0).toLocaleString();
    $('index-status').textContent = count + ' papers indexed' + (status.baseline_complete ? '' : ', initial indexing still running');
  } catch (error) {
    if (error.name !== 'AbortError') $('index-status').textContent = 'Index status unavailable';
  }
}

function syncKeyHint() {
  $('query').parentElement.classList.toggle('filled', $('query').value.length > 0);
}

async function showSearch() {
  activeRequest?.abort();
  const controller = new AbortController();
  activeRequest = controller;
  $('search-view').hidden = false;
  $('detail-view').hidden = true;
  document.title = 'Paper archive';
  const params = new URLSearchParams(location.search);
  for (const [name, id] of [['q', 'query'], ['category', 'category'], ['date_from', 'date-from'], ['date_to', 'date-to'], ['availability', 'availability'], ['sort', 'sort']]) {
    $(id).value = params.get(name) || (name === 'availability' ? 'all' : name === 'sort' ? 'relevance' : '');
  }
  syncKeyHint();
  if (params.get('q')) document.title = params.get('q') + ' · Paper archive';
  currentOffset = Math.max(0, Number.parseInt(params.get('offset') || '0', 10) || 0);
  currentParams = new URLSearchParams(params);
  currentParams.set('limit', pageSize);
  currentParams.set('offset', currentOffset);
  lastSearchURL = location.pathname + location.search;
  // Selection survives paging within one search so several pages can be exported together.
  const key = searchWith({});
  if (key !== selectionKey) { selection.clear(); selectionKey = key; }
  updateSelection();
  nextOffset = null;
  $('results').setAttribute('aria-busy', 'true');
  $('results-label').textContent = 'Searching…';
  $('pagination').hidden = true;
  setSearchMessage('');
  if (!$('index-status').textContent) void loadStatus(controller.signal);
  try {
    const data = await request('/v1/search?' + currentParams, controller.signal);
    if (controller.signal.aborted) return;
    const items = data.items || [];
    $('results').replaceChildren(...items.map(paperRow));
    const label = element('span', items.length ? `Results ${currentOffset + 1}–${currentOffset + items.length}` : 'No results');
    $('results-label').replaceChildren(label);
    if (Number.isFinite(data.elapsed_ms)) $('results-label').append(element('small', (data.elapsed_ms / 1000).toFixed(2) + ' s'));
    if (!items.length) setSearchMessage('No papers match this search. Try fewer words or broader filters.');
    nextOffset = data.has_more && Number.isInteger(data.next_offset) && data.next_offset > currentOffset
      ? data.next_offset : null;
    if (data.has_more && nextOffset === null) {
      setSearchMessage('You have reached the search result limit. Narrow your search or add filters to find more specific papers.');
    }
    $('previous').disabled = currentOffset === 0;
    $('next').disabled = nextOffset === null;
    $('page-label').textContent = `Page ${Math.floor(currentOffset / pageSize) + 1}`;
    $('pagination').hidden = !items.length && currentOffset === 0;
  } catch (error) {
    if (error.name !== 'AbortError') {
      $('results').replaceChildren();
      $('results-label').textContent = 'Search unavailable';
      setSearchMessage(error.message, true);
    }
  } finally {
    if (!controller.signal.aborted) $('results').setAttribute('aria-busy', 'false');
  }
}

function sourceBrowser(id, version, label) {
  const wrapper = element('div', undefined, 'source-browser');
  const button = element('button', label);
  button.type = 'button';
  const list = element('ul', undefined, 'source-list');
  const message = element('p', '', 'muted');
  message.setAttribute('role', 'status');
  button.addEventListener('click', async () => {
    button.disabled = true;
    message.textContent = 'Loading source files…';
    try {
      const suffix = version === undefined ? '' : '?version=' + version;
      const data = await request(apiURL(id) + '/sources' + suffix);
      list.replaceChildren(...data.files.map((file) => {
        const params = new URLSearchParams({path: file.path});
        if (version !== undefined) params.set('version', version);
        const row = element('li');
        row.append(link(file.path, apiURL(id) + '/source-file?' + params), element('span', sizeText(file.size), 'muted'));
        return row;
      }));
      message.textContent = `${data.files.length} source files.`;
      button.hidden = true;
    } catch (error) {
      message.textContent = error.message;
      button.disabled = false;
    }
  });
  wrapper.append(button, message, list);
  return wrapper;
}

function renderDetail(data) {
  const paper = data.display || data;
  const id = paper.id || data.id;
  const fragment = document.createDocumentFragment();
  fragment.append(element('h1', paper.title || id, 'paper-heading'),
    element('p', paper.authors || 'Authors not recorded', 'detail-authors'),
    metaLine(paper, id));
  const actions = element('div', undefined, 'detail-actions');
  const arxivId = encodeURIComponent(id);
  if (paper.has_pdf) actions.append(link('Open PDF', apiURL(id) + '/pdf', 'primary'));
  else actions.append(link('PDF on arXiv', 'https://arxiv.org/pdf/' + arxivId, 'external'));
  if (canCopy) actions.append(copyBibtexButton(id));
  actions.append(link('Download BibTeX', apiURL(id) + '/bibtex'));
  actions.append(link('arXiv page', 'https://arxiv.org/abs/' + arxivId, 'external'));
  for (const anchor of actions.querySelectorAll('.external')) anchor.rel = 'noopener noreferrer';
  fragment.append(actions);
  const abstract = element('section', undefined, 'detail-section');
  abstract.append(element('h2', 'Abstract'), element('p', paper.abstract || 'No abstract recorded.', 'full-abstract'));
  fragment.append(abstract);
  const sources = element('section', undefined, 'detail-section');
  sources.id = 'sources';
  sources.append(element('h2', 'Local files and revisions'));
  sources.append(element('p', paper.latest_version ? `Latest known revision: v${paper.latest_version}. Local files may be from an earlier revision.` : 'Latest revision is not recorded.', 'muted'));
  const artifacts = data.artifacts || [];
  if (artifacts.length) {
    const table = element('table', undefined, 'artifact-table');
    const head = element('thead');
    const heading = element('tr');
    for (const name of ['File', 'Revision', 'Status', 'Size', '']) {
      const cell = element('th', name); cell.scope = 'col'; heading.append(cell);
    }
    head.append(heading);
    const body = element('tbody');
    for (const artifact of artifacts) {
      const row = element('tr');
      row.append(element('td', artifact.kind === 'pdf' ? 'PDF' : 'LaTeX source'),
        element('td', artifact.version ? 'v' + artifact.version : 'Unknown'),
        element('td', artifact.status), element('td', sizeText(artifact.original_size)));
      const action = element('td');
      if (artifact.status === 'available' && ['pdf', 'source'].includes(artifact.kind)) {
        action.append(link(artifact.kind === 'pdf' ? 'Open PDF' : 'Download', apiURL(id) + '/' + artifact.kind + '?version=' + artifact.version));
      }
      row.append(action); body.append(row);
    }
    table.append(head, body); sources.append(table);
  } else sources.append(element('p', 'No PDF or source package is stored locally yet.', 'muted'));
  if (data.has_historical_source) {
    sources.append(element('p', 'Historical LaTeX is available. This is the collected TeX subset; its revision is unknown.', 'muted'));
    sources.append(sourceBrowser(id, 0, 'Browse historical LaTeX files'));
  }
  const packageSource = artifacts.find((artifact) => artifact.kind === 'source' && artifact.status === 'available');
  if (packageSource) sources.append(sourceBrowser(id, packageSource.version, 'Browse source package files'));
  const jobs = data.jobs || [];
  if (jobs.length) {
    const jobList = element('ul', undefined, 'source-list muted');
    for (const job of jobs) jobList.append(element('li', `${job.kind === 'pdf' ? 'PDF' : 'Source'} v${job.version}: ${job.status}${job.error ? ' — ' + job.error : ''}`));
    sources.append(element('h3', 'Acquisition status'), jobList);
  }
  fragment.append(sources);
  $('paper-detail').replaceChildren(fragment);
  document.title = (paper.title || id) + ' · Paper archive';
  if (location.hash === '#sources') sources.scrollIntoView();
}

async function showDetail(id) {
  activeRequest?.abort();
  const controller = new AbortController();
  activeRequest = controller;
  $('search-view').hidden = true;
  $('detail-view').hidden = false;
  $('back-to-search').href = lastSearchURL;
  $('paper-detail').replaceChildren(element('p', 'Loading paper…', 'message'));
  try {
    const data = await request(apiURL(id), controller.signal);
    if (!controller.signal.aborted) renderDetail(data);
  } catch (error) {
    if (error.name !== 'AbortError') $('paper-detail').replaceChildren(element('p', error.message, 'message error'));
  }
}

function route() {
  if (location.pathname.startsWith('/papers/')) {
    try { void showDetail(decodeURIComponent(location.pathname.slice('/papers/'.length))); }
    catch { $('paper-detail').replaceChildren(element('p', 'Invalid paper address.', 'message error')); }
  } else void showSearch();
}
function navigate(url) {
  history.pushState(null, '', url);
  route();
  window.scrollTo(0, 0);
}
$('search-form').addEventListener('submit', (event) => {
  event.preventDefault();
  const params = new URLSearchParams();
  for (const [key, raw] of new FormData(event.currentTarget)) {
    const value = String(raw).trim();
    if (value && !(key === 'availability' && value === 'all') && !(key === 'sort' && value === 'relevance')) params.set(key, value);
  }
  navigate('/?' + params);
});
$('sort').addEventListener('change', () => $('search-form').requestSubmit());
$('query').addEventListener('input', syncKeyHint);
// Press "/" anywhere outside a field to jump to the search box.
document.addEventListener('keydown', (event) => {
  if (event.key !== '/' || event.ctrlKey || event.metaKey || event.altKey) return;
  const target = event.target;
  if (target instanceof HTMLElement && (target.isContentEditable || ['INPUT', 'TEXTAREA', 'SELECT'].includes(target.tagName))) return;
  event.preventDefault();
  if ($('search-view').hidden) navigate(lastSearchURL);
  $('query').focus();
  $('query').select();
});
function movePage(offset) {
  const params = new URLSearchParams(currentParams);
  params.delete('limit');
  params.set('offset', offset);
  navigate('/?' + params);
}
$('previous').addEventListener('click', () => movePage(Math.max(0, currentOffset - pageSize)));
$('next').addEventListener('click', () => { if (nextOffset !== null) movePage(nextOffset); });
$('export-selected').addEventListener('click', () => {
  if (selection.size) window.location.assign('/v1/bibtex?' + new URLSearchParams({ids: [...selection].join(',')}));
});
document.addEventListener('click', (event) => {
  const anchor = event.target.closest('a');
  if (!anchor || event.button !== 0 || event.metaKey || event.ctrlKey || event.shiftKey || event.altKey || anchor.target || anchor.download) return;
  const url = new URL(anchor.href);
  if (url.origin === location.origin && (url.pathname === '/' || url.pathname.startsWith('/papers/'))) {
    event.preventDefault(); navigate(url.pathname + url.search + url.hash);
  }
});
window.addEventListener('popstate', route);
route();
if (!location.search && !location.pathname.startsWith('/papers/')) $('query').focus();
