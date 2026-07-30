'use strict';

// Registration-number prefix convention (e.g. "DOC-12345"). Keep in sync
// with the server's REG_NUMBER_PREFIX env var.
const REG_NUMBER_PREFIX = 'DOC';

// ── REALTIME (Socket.IO) ─────────────────────────────────────────────────
// Replaces the old 4s/15s/30s HTTP polling for messages/notifications with
// server push. Server hooks live in app.py (sio_emit calls). Each socket
// event below just triggers the *existing* fetch/render functions
// immediately instead of waiting for the next poll tick — this keeps all
// the tested rendering logic (receipts, reactions, pins, i18n) exactly as
// it was, so the only thing that changes is *when* it runs.
// The old setInterval polling is kept too, at a much longer interval, as a
// safety net in case a socket connection drops silently.
let _rtSocket = null;
let _rtTypingClearTimers = {};

function rtInit() {
    if (typeof io === 'undefined') {
        console.warn('[realtime] socket.io client not loaded — falling back to polling only');
        return;
    }
    if (_rtSocket) return;
    _rtSocket = io({ transports: ['polling'] });

    _rtSocket.on('connect', () => {
        // Re-join whichever message group is currently open (e.g. after a
        // reconnect), so events keep flowing without the user re-clicking.
        if (typeof _msgActiveGroupId !== 'undefined' && _msgActiveGroupId) {
            _rtSocket.emit('join_group', { group_id: _msgActiveGroupId });
        }
    });

    _rtSocket.on('new_message', (data) => {
        if (typeof msgLoadGroups === 'function') msgLoadGroups();
        if (typeof _msgActiveGroupId !== 'undefined' && data && data.group_id === _msgActiveGroupId
            && typeof msgFetchMessages === 'function') {
            msgFetchMessages(false);
        }
    });

    ['message_edited', 'message_deleted', 'reaction_updated', 'pin_updated'].forEach(evt => {
        _rtSocket.on(evt, (data) => {
            if (typeof _msgActiveGroupId !== 'undefined' && data && data.group_id === _msgActiveGroupId
                && typeof msgFetchMessages === 'function') {
                msgFetchMessages(false);
            }
        });
    });

    _rtSocket.on('typing', (data) => {
        if (!data || typeof _msgActiveGroupId === 'undefined' || data.group_id !== _msgActiveGroupId) return;
        if (typeof msgRenderTyping !== 'function') return;
        const uid = data.user_id;
        msgRenderTyping([uid]);
        clearTimeout(_rtTypingClearTimers[uid]);
        _rtTypingClearTimers[uid] = setTimeout(() => msgRenderTyping([]), 3000);
    });

    _rtSocket.on('notification', () => {
        if (typeof loadNotifications === 'function') loadNotifications();
    });

    _rtSocket.on('disconnect', () => {
        console.warn('[realtime] socket disconnected — relying on polling fallback until reconnect');
    });
}

document.addEventListener('DOMContentLoaded', rtInit);

// ── NAV GROUP TOGGLE ──────────────────────────────────────────────────────
function toggleNavGroup(btn) {
    const group = btn.closest('.nav-group');
    if (!group) return;
    const isOpen = group.classList.contains('open');
    document.querySelectorAll('.nav-group.open').forEach(g => g.classList.remove('open'));
    if (!isOpen) group.classList.add('open');
}

let allEntities = [];
let allFoldersByDept = {};   // keyed by Sys_Department.ID
let folderModalDeptId = null;
const canDeleteMainFolders = (document.body.getAttribute('data-role') || '').toLowerCase() === 'admin';

// ── Permission helpers ─────────────────────────────────────────────────────
const _currentRole = (document.body.getAttribute('data-role') || '').toLowerCase();
const _isAdmin     = _currentRole === 'admin';

/**
 * In-memory cache of allowed dept IDs, refreshed from the server on each
 * loadEntities() call. Starts from the value embedded in <body> at page load
 * so the first render is instant; subsequent calls always use the latest value.
 * null = admin (unrestricted). Set = regular user's allowed IDs.
 */
let _allowedDeptIdsCache = _isAdmin ? null : (() => {
    const raw = (document.body.getAttribute('data-allowed-deps') || '').trim();
    if (!raw) return new Set();
    return new Set(raw.split(',').map(s => parseInt(s.trim(), 10)).filter(n => !isNaN(n)));
})();

/**
 * Returns the current in-memory Set of allowed Sys_Department IDs.
 * Admins get null (= unrestricted).
 */
function _getAllowedDeptIds() {
    return _allowedDeptIdsCache;
}

/**
 * Fetch the latest allowed dept IDs from the server (always reads DB).
 * Call this before rendering the folder tree so that admin permission
 * changes take effect without requiring the user to log out and back in.
 */
async function _refreshAllowedDeptIds() {
    if (_isAdmin) { _allowedDeptIdsCache = null; return; }
    try {
        const res = await fetch('/api/my/deps');
        if (!res.ok) return; // network issue — keep existing cache
        const data = await res.json();
        if (data.admin) {
            _allowedDeptIdsCache = null;
        } else {
            _allowedDeptIdsCache = new Set((data.dep_ids || []).map(Number));
        }
    } catch (e) {
        // Silently keep existing cache on failure
    }
}

/** Returns true if the current user can access a Sys_Department row by its ID. */
function _canAccessDept(deptId) {
    if (_isAdmin) return true;
    const allowed = _getAllowedDeptIds();
    if (allowed === null) return true;
    return allowed.has(Number(deptId));
}

// ── Permission Modal ───────────────────────────────────────────────────────
function showPermissionDenied(title, msg) {
    const modal = document.getElementById('permissionModal');
    if (!modal) return;
    const titleEl = document.getElementById('permModalTitle');
    const msgEl   = document.getElementById('permModalMsg');
    if (titleEl) titleEl.textContent = title || (currentLang === 'ar' ? 'تم رفض الوصول' : 'Access Denied');
    if (msgEl)   msgEl.textContent   = msg   || (currentLang === 'ar' ? 'ليس لديك صلاحية لهذا المجلد.' : 'You do not have permission to access this folder.');
    modal.style.display = 'flex';
}

function closePermissionModal() {
    const modal = document.getElementById('permissionModal');
    if (modal) modal.style.display = 'none';
}

document.addEventListener('keydown', e => {
    if (e.key === 'Escape') closePermissionModal();
});

/** API list endpoints must return a JSON array; on DB/HTTP errors the server returns { error: "..." }. */
function apiErrorMessage(data, res, fallback) {
    if (data && typeof data.error === 'string' && data.error.trim()) return data.error.trim();
    if (!res.ok) return fallback || `Request failed (${res.status})`;
    return fallback || 'Unexpected response from server';
}

// ── LOAD ENTITIES (Sys_Department) ─────────────────────────────────────────
async function loadEntities() {
    // Always refresh allowed dept IDs from the DB before rendering the tree.
    // This ensures admin permission changes take effect immediately for the
    // affected user without requiring a logout/login.
    await _refreshAllowedDeptIds();

    const sel = document.getElementById('entitySelect');
    try {
        const res = await fetch('/api/entities');
        const data = await res.json();
        if (!res.ok || !Array.isArray(data)) {
            allEntities = [];
            if (sel) sel.innerHTML = '';
            const msg = apiErrorMessage(data, res, 'Could not load folders from the database');
            showToast(
                currentLang === 'ar' ? 'خطأ في قاعدة البيانات: ' + msg : 'Database error: ' + msg,
                'error'
            );
            renderFullFolderTree();
            return;
        }
        allEntities = data;
        if (sel) {
            // Only populate the archive dropdown with depts the user can access
            const accessibleEntities = allEntities.filter(e => _canAccessDept(e.id));
            sel.innerHTML = accessibleEntities.map(e =>
                `<option value="${e.id}">${escapeHtml(e.name)}</option>`
            ).join('');
        }
        await loadAllFolders();
        _populateInqDeptSelect();
        rptPopulateDeptFilter();
        wfPopulateEntitySelect();
    } catch (e) {
        console.error('Failed to load entities:', e);
        allEntities = [];
        if (sel) sel.innerHTML = '';
        showToast(
            currentLang === 'ar' ? 'تعذر الاتصال بالخادم' : 'Could not reach the server',
            'error'
        );
        renderFullFolderTree();
    }
}

// ── Inquiry folder/dept filter helpers ────────────────────────────────────
function _populateInqDeptSelect() {
    const sel = document.getElementById('adv-dept');
    if (!sel) return;
    const lang = currentLang;
    const allLabel = lang === 'ar' ? 'كل المجلدات' : 'All Folders';
    // Only show departments the current user is allowed to access
    const visibleEntities = allEntities.filter(e => _canAccessDept(e.id));
    sel.innerHTML = `<option value="">${allLabel}</option>` +
        visibleEntities.map(e => `<option value="${e.id}">${escapeHtml(e.name)}</option>`).join('');
    _populateInqFolderSelect('');
}

function _populateInqFolderSelect(entityId) {
    const sel = document.getElementById('adv-folder');
    if (!sel) return;
    const lang = currentLang;
    const allLabel = lang === 'ar' ? 'كل المجلدات الفرعية' : 'All Subfolders';
    if (!entityId) {
        // All accessible folders — flatten all subfolders from accessible depts only
        const all = [];
        allEntities.filter(e => _canAccessDept(e.id)).forEach(e => {
            (allFoldersByDept[e.id] || []).forEach(f => {
                if (!all.some(x => x.id === f.id)) all.push(f);
            });
        });
        sel.innerHTML = `<option value="">${allLabel}</option>` +
            all.map(f => `<option value="${f.id}">${escapeHtml(f.name)}</option>`).join('');
    } else {
        // Only show if the selected dept is accessible
        if (!_canAccessDept(Number(entityId))) {
            sel.innerHTML = `<option value="">${allLabel}</option>`;
            return;
        }
        const folders = allFoldersByDept[entityId] || [];
        sel.innerHTML = `<option value="">${allLabel}</option>` +
            folders.map(f => `<option value="${f.id}">${escapeHtml(f.name)}</option>`).join('');
    }
}

function onInqDeptChange() {
    const deptSel = document.getElementById('adv-dept');
    const deptId  = deptSel?.value || '';
    _populateInqFolderSelect(deptId);
    _buildCustomFeFilters(deptId);
    onInquiryFilterInput();
}

// Cache the last loaded Fe config so we don't refetch on every keystroke
let _lastFeConfigDeptId = null;
let _lastFeConfig = null; // { Fe1:{label,options}, Fe2:{...}, ..., Fe4:{label}, ... }

async function _buildCustomFeFilters(deptId) {
    const container = document.getElementById('inq-custom-filters');
    if (!container) return;

    // Clear existing custom filters
    container.innerHTML = '';
    _lastFeConfigDeptId = null;
    _lastFeConfig = null;

    if (!deptId) return;

    // Fetch field labels + dropdown options in parallel
    let fields = {}, options = {};
    try {
        [fields, options] = await Promise.all([
            fetch(`/api/entities/${deptId}/fields`).then(r => r.ok ? r.json() : {}),
            fetch(`/api/entities/${deptId}/dropdown-options`).then(r => r.ok ? r.json() : {})
        ]);
    } catch(e) { return; }

    // Build a unified config: Fe1-Fe3 with options, Fe4-Fe7 text
    const config = {};
    for (let i = 1; i <= 7; i++) {
        const label = (fields[`Fe${i}Name`] || '').trim();
        if (!label) continue;
        if (i <= 3) {
            const opts = (options[`Fe${i}`] || {}).options || [];
            if (opts.length > 0) config[`Fe${i}`] = { label, type: 'select', options: opts };
        } else {
            config[`Fe${i}`] = { label, type: 'text' };
        }
    }

    if (!Object.keys(config).length) return;

    _lastFeConfigDeptId = deptId;
    _lastFeConfig = config;

    // Render a filter input for each configured field
    const isAr = currentLang === 'ar';
    for (const [feKey, cfg] of Object.entries(config)) {
        const feIdx = parseInt(feKey.replace('Fe', ''), 10);
        const wrap = document.createElement('div');
        wrap.className = 'inq-custom-filter-wrap';
        wrap.setAttribute('data-fe', feKey);

        if (cfg.type === 'select') {
            const allLabel = isAr ? `— ${cfg.label} —` : `— ${cfg.label} —`;
            const optionsHtml = cfg.options
                .map(o => `<option value="${o.replace(/"/g,'&quot;')}">${o}</option>`)
                .join('');
            wrap.innerHTML = `
                <select id="adv-fe${feIdx}" class="inq-input inq-input--custom-fe inq-input--fe-select"
                    title="${cfg.label}" onchange="onInquiryFilterInput()">
                    <option value="">${allLabel}</option>
                    ${optionsHtml}
                </select>`;
        } else {
            wrap.innerHTML = `
                <input id="adv-fe${feIdx}" type="text"
                    class="inq-input inq-input--custom-fe inq-input--fe-text"
                    placeholder="${cfg.label}"
                    title="${cfg.label}"
                    oninput="onInquiryFilterInput()"
                    onkeydown="onInquiryFilterKeydown(event)">`;
        }
        container.appendChild(wrap);
    }
}

// ── LOAD SUBFOLDERS for all entities ──────────────────────────────────────
async function loadAllFolders() {
    allFoldersByDept = {};
    await Promise.all(allEntities.map(async e => {
        try {
            // Use dept_id (e.g. 46, 53) — the Dept_ID column that Adco_Folder uses
            const res  = await fetch(`/api/folders/${e.dept_id}`);
            const data = await res.json();
            // Key by entity.id so the tree render (which uses e.id) still works
            if (res.ok && Array.isArray(data)) {
                allFoldersByDept[e.id] = data;
            } else {
                allFoldersByDept[e.id] = [];
                if (!res.ok || !Array.isArray(data)) {
                    const err = apiErrorMessage(data, res);
                    console.warn(`Folders for dept_id ${e.dept_id}:`, err);
                }
            }
        } catch (_) { allFoldersByDept[e.id] = []; }
    }));
    renderFullFolderTree();
    if (allEntities.length) updateVolumeOptions();
}

// ── FAVOURITES ────────────────────────────────────────────────────────────
const _FAV_KEY = 'docportal_fav_folders';

function _favGet() {
    try { return JSON.parse(localStorage.getItem(_FAV_KEY) || '[]'); } catch(e) { return []; }
}
function _favSave(list) {
    try { localStorage.setItem(_FAV_KEY, JSON.stringify(list)); } catch(e) {}
}
function _favIsPinned(folderId) {
    return _favGet().some(f => f.id === folderId);
}
function _favToggle(folderId, deptId, event) {
    event.stopPropagation();
    const folders = allFoldersByDept[deptId] || [];
    const folder  = folders.find(f => f.id === folderId);
    if (!folder) return;
    let list = _favGet();
    if (_favIsPinned(folderId)) {
        list = list.filter(f => f.id !== folderId);
    } else {
        const entity = allEntities.find(e => e.id === deptId);
        list.push({
            id:        folderId,
            deptId:    deptId,
            name:      folder.name,
            deptName:  entity ? entity.name : '',
        });
    }
    _favSave(list);
    // Re-render both the pinned section and the star buttons in the tree
    _favRenderPinned();
    // Update just the star button(s) for this folder without full re-render
    document.querySelectorAll(`.fav-star-btn[data-folder-id="${folderId}"]`).forEach(btn => {
        const pinned = _favIsPinned(folderId);
        btn.classList.toggle('fav-star-btn--active', pinned);
        btn.title = pinned
            ? (currentLang === 'ar' ? 'إلغاء التثبيت' : 'Unpin favourite')
            : (currentLang === 'ar' ? 'تثبيت كمفضل' : 'Pin as favourite');
        btn.querySelector('i').className = pinned ? 'ph-fill ph-star' : 'ph ph-star';
    });
}
function _favRenderPinned() {
    const wrap = document.getElementById('favPinnedSection');
    if (!wrap) return;
    const list = _favGet();
    if (!list.length) { wrap.style.display = 'none'; return; }
    const isAr = currentLang === 'ar';
    wrap.style.display = '';
    wrap.innerHTML = `
        <div class="fav-pinned-header">
            <i class="ph-fill ph-star" style="color:var(--warning,#d97706);font-size:13px"></i>
            <span>${isAr ? 'المفضلة' : 'Favourites'}</span>
        </div>
        ${list.map(f => `
        <div class="fav-pinned-item" onclick="selectFolderFromTree(${f.deptId}, ${f.id}, event)"
             title="${escapeHtml(f.deptName)} › ${escapeHtml(f.name)}">
            <i class="ph ph-folder" style="font-size:13px;flex-shrink:0"></i>
            <span class="fav-pinned-name">${escapeHtml(f.name)}</span>
            <span class="fav-pinned-dept">${escapeHtml(f.deptName)}</span>
            <button type="button" class="fav-star-btn fav-star-btn--active fav-pinned-unpin"
                data-folder-id="${f.id}"
                title="${isAr ? 'إلغاء التثبيت' : 'Unpin'}"
                onclick="event.stopPropagation(); _favToggle(${f.id}, ${f.deptId}, event)">
                <i class="ph-fill ph-star"></i>
            </button>
        </div>`).join('')}`;
}

// ── RENDER FULL FOLDER TREE ────────────────────────────────────────────────
function renderFullFolderTree() {
    const tree = document.getElementById('folderTree');
    if (!tree) return;
    tree.innerHTML = allEntities.map(e => {
        const accessible  = _canAccessDept(e.id);
        const folders     = (allFoldersByDept[e.id] || []).filter(f => (f.parent_id || 0) === 0);
        const hasChildren = folders.length > 0;

        if (!accessible) {
            const deptNameEsc = e.name.replace(/\\/g,'\\\\').replace(/'/g,"\\'");
            return `
        <div class="tree-dept-wrap">
            <div class="tree-dept tree-dept--locked"
                 onclick="event.stopPropagation(); showPermissionDenied(
                     currentLang === 'ar' ? '\u062a\u0645 \u0631\u0641\u0636 \u0627\u0644\u0648\u0635\u0648\u0644' : 'Access Denied',
                     currentLang === 'ar' ? '\u0644\u064a\u0633 \u0644\u062f\u064a\u0643 \u0635\u0644\u0627\u062d\u064a\u0629 \u0627\u0644\u0648\u0635\u0648\u0644 \u0625\u0644\u0649: ${escapeHtml(e.name)}' : 'You do not have permission to access: ${escapeHtml(e.name)}'
                 )">
                <span class="tree-arrow">　</span>
                <span><i class="ph ph-archive-box"></i></span>
                <span>${escapeHtml(e.name)}</span>
                <span class="lock-badge"><i class="ph ph-lock"></i></span>
            </div>
        </div>`;
        }

        const childrenHtml = hasChildren
            ? renderTreeChildren(e.id, 0, 0)
            : '<div style="color:var(--muted);font-size:12px;padding:6px 14px">No subfolders</div>';
        return `
        <div class="tree-dept-wrap">
            <div class="tree-dept" onclick="toggleDept(${e.id}, event)">
                <span class="tree-arrow" id="arrow-${e.id}">${hasChildren ? '\u25b6' : '\u3000'}</span>
                <span><i class="ph ph-archive-box"></i></span>
                <span>${escapeHtml(e.name)}</span>
                <span class="tree-badge">${folders.length}</span>
                <button
                    type="button"
                    class="folder-add-btn"
                    title="Edit main folder"
                    onclick="event.stopPropagation(); openFolderBuilder(${e.id})"
                    style="margin-inline-start:4px"
                ><i class="ph ph-pencil-simple"></i></button>
                <button
                    type="button"
                    class="folder-add-btn"
                    title="Add subfolder"
                    onclick="event.stopPropagation(); promptCreateFolder(0, ${e.id})"
                    style="margin-inline-start:4px"
                >+</button>
            </div>
            <div class="tree-dept-children" id="dept-children-${e.id}" style="display:none">
                ${childrenHtml}
            </div>
        </div>`;
    }).join('');
    _favRenderPinned();
}

function renderTreeChildren(deptId, parentId, depth) {
    const folders = (allFoldersByDept[deptId] || [])
        .filter(f => (f.parent_id || 0) === parentId);
    if (!folders.length) return '';
    return folders.map(f => {
        const hasChildren = (allFoldersByDept[deptId] || []).some(c => (c.parent_id || 0) === f.id);
        const childHtml   = renderTreeChildren(deptId, f.id, depth + 1);
        const icon        = depth === 0 ? '<i class="ph ph-folder"></i>' : '<i class="ph ph-folder-open"></i>';
        const indent      = 10 + depth * 14;
        const childrenId  = `folder-children-${f.id}`;
        const depthDots   = depth > 0 ? `<span class="folder-depth-dots">${'\u00b7'.repeat(depth)}</span>` : '';
        return `
            <div class="tree-folder-wrap" id="wrap-${f.id}" data-depth="${depth}">
                <div class="tree-child" style="padding-inline-start:${indent}px">
                    <span class="tree-folder-arrow" id="farrow-${f.id}"
                        onclick="event.stopPropagation(); toggleFolderChildren(${f.id})"
                        style="visibility:${hasChildren ? 'visible' : 'hidden'}; cursor:pointer; padding:2px 4px">\u25b6</span>
                    <span onclick="selectFolderFromTree(${deptId}, ${f.id}, event)" style="flex:1;display:flex;align-items:center;gap:5px;cursor:pointer;overflow:hidden">
                        <span>${icon}</span>
                        ${depthDots}
                        <span class="folder-tree-name">${escapeHtml(f.name)}</span>
                    </span>
                    <button type="button" class="folder-scan-btn" title="Scan / upload files into this folder"
                        onclick="event.stopPropagation(); openScannerModal(${f.id}, '${f.name.replace(/'/g,"\\'")}', 'folder')"><i class="ph ph-camera"></i></button>
                    <button type="button" class="fav-star-btn ${_favIsPinned(f.id) ? 'fav-star-btn--active' : ''}"
                        data-folder-id="${f.id}"
                        title="${_favIsPinned(f.id) ? (currentLang === 'ar' ? 'إلغاء التثبيت' : 'Unpin favourite') : (currentLang === 'ar' ? 'تثبيت كمفضل' : 'Pin as favourite')}"
                        onclick="event.stopPropagation(); _favToggle(${f.id}, ${deptId}, event)">
                        <i class="${_favIsPinned(f.id) ? 'ph-fill ph-star' : 'ph ph-star'}"></i>
                    </button>
                    <button type="button" class="folder-add-btn folder-add-btn--inline" title="Add subfolder"
                        onclick="event.stopPropagation(); promptCreateFolder(${f.id}, ${deptId})"
                        style="margin-inline-start:4px">+</button>
                </div>
                ${hasChildren ? `<div id="${childrenId}" style="display:none">${childHtml}</div>` : ''}
            </div>`;
    }).join('');
}

function toggleDept(deptId, event) {
    event.stopPropagation();
    const children = document.getElementById(`dept-children-${deptId}`);
    const arrow    = document.getElementById(`arrow-${deptId}`);
    if (!children) return;
    const isOpen = children.style.display !== 'none';
    children.style.display = isOpen ? 'none' : 'block';
    if (arrow) arrow.textContent = isOpen ? '▶' : '▼';
}

function toggleFolderChildren(folderId) {
    const children = document.getElementById(`folder-children-${folderId}`);
    const arrow    = document.getElementById(`farrow-${folderId}`);
    if (!children) return;
    const isOpen = children.style.display !== 'none';
    children.style.display = isOpen ? 'none' : 'block';
    if (arrow) arrow.textContent = isOpen ? '▶' : '▼';
}

function selectFolderFromTree(deptId, id, event) {
    event.stopPropagation();

    // ── Permission check: verify user can access this department ─────────────
    if (!_canAccessDept(deptId)) {
        const entity = allEntities.find(e => e.id === deptId);
        const deptName = entity ? entity.name : String(deptId);
        showPermissionDenied(
            currentLang === 'ar' ? 'تم رفض الوصول' : 'Access Denied',
            currentLang === 'ar'
                ? 'ليس لديك صلاحية الوصول إلى: ' + deptName
                : 'You do not have permission to access: ' + deptName
        );
        return;
    }

    const allFolders = allFoldersByDept[deptId] || [];
    const folder = allFolders.find(f => f.id === id);
    if (!folder) return;

    // Build full breadcrumb path
    const parts = [folder.name];
    let current = folder;
    const _visited = new Set([current.id]);
    while (current.parent_id) {
        if (_visited.has(current.parent_id)) break; // cyclic parent chain — stop instead of hanging
        const parent = allFolders.find(f => f.id === current.parent_id);
        if (!parent) break;
        parts.unshift(parent.name);
        current = parent;
        _visited.add(current.id);
    }
    const fullPath = parts.join(' › ');

    const entity     = allEntities.find(e => e.id === deptId);
    const realDeptId = entity ? entity.dept_id : deptId;

    // ── Step 1: Set entitySelect to match this folder's department ────────────
    const entitySel = document.getElementById('entitySelect');
    if (entitySel) {
        entitySel.value = String(deptId);
        // Rebuild volume options for this entity then set the folder
        updateVolumeOptions().then(() => {
            selectVolume(fullPath, id, realDeptId);
        });
    } else {
        selectVolume(fullPath, id, realDeptId);
    }

    // ── Step 2: Highlight selected folder in tree ─────────────────────────────
    document.querySelectorAll('.tree-child').forEach(t => t.classList.remove('active'));
    const selectedEl = document.querySelector(`#wrap-${id} > .tree-child`);
    if (selectedEl) selectedEl.classList.add('active');

    // ── Step 3: Expand all ancestor folders ───────────────────────────────────
    let ancestor = folder;
    while (ancestor.parent_id) {
        const parentFolder = allFolders.find(f => f.id === ancestor.parent_id);
        if (!parentFolder) break;
        const childrenDiv = document.getElementById(`folder-children-${parentFolder.id}`);
        const arrow = document.getElementById(`farrow-${parentFolder.id}`);
        if (childrenDiv && childrenDiv.style.display === 'none') {
            childrenDiv.style.display = 'block';
            if (arrow) arrow.textContent = '▼';
        }
        ancestor = parentFolder;
    }

    // ── Step 4: Expand this folder's own children inline ─────────────────────
    const childrenDiv = document.getElementById(`folder-children-${id}`);
    const arrow = document.getElementById(`farrow-${id}`);
    if (childrenDiv) {
        childrenDiv.style.display = 'block';
        if (arrow) arrow.textContent = '▼';
    }

    // ── Step 5: Update the selected folder banner ─────────────────────────────
    const label = document.getElementById('selectedVolumeLabel');
    if (label) label.innerHTML = '<i class="ph ph-folder"></i> ' + fullPath;
}

async function deleteFolderFromTree(deptId, id, event) {
    event.stopPropagation();
    const folder = (allFoldersByDept[deptId] || []).find(f => f.id === id);
    const folderName = folder ? folder.name : `#${id}`;
    const ok = confirm(`Delete folder "${folderName}" and all its subfolders?`);
    if (!ok) return;

    try {
        const res = await fetch(`/api/folders/${id}`, { method: 'DELETE' });
        const data = await res.json();
        if (!res.ok || !data.success) {
            showToast('Delete failed: ' + (data.error || 'Unknown error'), 'error');
            return;
        }

        const input = document.getElementById('volumeInput');
        if (input && parseInt(input.dataset.folderId || '0', 10) === id) {
            input.value = '';
            delete input.dataset.folderId;
        }
        const label = document.getElementById('selectedVolumeLabel');
        if (label) label.textContent = currentLang === 'ar' ? 'لم يتم اختيار مجلد بعد' : 'No folder selected yet';

        await loadAllFolders();
        await updateVolumeOptions();
        showToast('Folder deleted successfully', 'success');
    } catch (e) {
        showToast('Error deleting folder', 'error');
    }
}

async function deleteMainFolder(entityId, event) {
    event.stopPropagation();
    const entity = allEntities.find(e => e.id === entityId);
    const name = entity ? entity.name : `#${entityId}`;
    const ok = confirm(`Delete main folder "${name}" and all subfolders?`);
    if (!ok) return;
    try {
        const res = await fetch(`/api/entities/${entityId}`, { method: 'DELETE' });
        const data = await res.json();
        if (!res.ok || !data.success) {
            showToast('Delete failed: ' + (data.error || 'Unknown error'), 'error');
            return;
        }
        await loadEntities();
        showToast('Main folder deleted successfully', 'success');
    } catch (e) {
        showToast('Error deleting main folder', 'error');
    }
}

// ── VOLUME TYPEAHEAD ───────────────────────────────────────────────────────
async function updateVolumeOptions() {
    const sel = document.getElementById('entitySelect');
    if (!sel) return;
    const entityId = sel.value;
    const entity   = allEntities.find(e => String(e.id) === String(entityId));
    const realDeptId = entity ? entity.dept_id : entityId;
    const allFolders = allFoldersByDept[entityId] || [];

    // Build full breadcrumb path for every folder at every depth
    function buildPath(folder) {
        const parts = [folder.name];
        let current = folder;
        const _visited = new Set([current.id]);
        while (current.parent_id) {
            if (_visited.has(current.parent_id)) break; // cyclic parent chain — stop instead of hanging
            const parent = allFolders.find(f => f.id === current.parent_id);
            if (!parent) break;
            parts.unshift(parent.name);
            current = parent;
            _visited.add(current.id);
        }
        return parts;
    }

    currentVolumes = allFolders.map(f => {
        const pathParts = buildPath(f);
        return {
            id: f.id,
            name: f.name,
            path: pathParts.slice(0, -1).join(' › '),
            fullPath: pathParts.join(' › '),
            dept_id: realDeptId,
        };
    });

    const input = document.getElementById('volumeInput');
    if (input) {
        input.value = '';
        delete input.dataset.folderId;
        delete input.dataset.folderDeptId;
        input.placeholder = currentVolumes.length ? 'Type to search folder...' : 'No subfolders available';
        input.disabled = currentVolumes.length === 0;
    }

    // Always reset the subfolder label whenever the folder selection is cleared
    const selectedVolumeLabel = document.getElementById('selectedVolumeLabel');
    if (selectedVolumeLabel) {
        selectedVolumeLabel.textContent = currentLang === 'ar' ? 'لم يتم اختيار مجلد بعد' : 'No folder selected yet';
    }

    renderVolumeDropdown('');
}

let currentVolumes = [];

function filterVolume() {
    const q = document.getElementById('volumeInput')?.value || '';
    renderVolumeDropdown(q);
    showVolumeDropdown();
}

function renderVolumeDropdown(query) {
    const dd = document.getElementById('volumeDropdown');
    if (!dd) return;
    const q = query.trim().toLowerCase();
    const matches = q
        ? currentVolumes.filter(v => v.fullPath.toLowerCase().includes(q))
        : currentVolumes;
    if (!matches.length) { dd.innerHTML = ''; dd.classList.remove('open'); return; }

    // Sort by fullPath so parents always appear above their children
    const sorted = [...matches].sort((a, b) => a.fullPath.localeCompare(b.fullPath));

    dd.innerHTML = sorted.map(v => {
        const depth = (v.fullPath.match(/›/g) || []).length;
        const indent = 12 + depth * 16;
        const hlName = q
            ? v.name.replace(new RegExp(`(${q})`, 'gi'), '<mark>$1</mark>')
            : v.name;
        const icon = depth === 0
            ? '<i class="ph ph-folder" style="margin-inline-end:6px;color:var(--accent)"></i>'
            : '<i class="ph ph-folder-open" style="margin-inline-end:6px;opacity:0.6"></i>';
        return `<div class="volume-option" style="padding-inline-start:${indent}px"
            onmousedown="selectVolume('${v.fullPath.replace(/'/g,"\\'")}', ${v.id}, ${v.dept_id || 0})">
            ${icon}<span class="vol-opt-name">${hlName}</span>
        </div>`;
    }).join('');
}

// ── STEP INDICATOR HELPER ──────────────────────────────────────────────────
function goToStep(stepNumber) {
    const totalSteps = 4;
    for (let i = 1; i <= totalSteps; i++) {
        const el = document.getElementById(`step${i}-ind`);
        if (!el) continue;
        el.classList.remove('done', 'active');
        if (i < stepNumber) {
            el.classList.add('done');
        } else if (i === stepNumber) {
            el.classList.add('active');
        }
    }
    // Update connectors
    const connectors = document.querySelectorAll('.step-connector');
    connectors.forEach((c, idx) => {
        c.classList.toggle('connector-done', idx < stepNumber - 1);
    });
}

function selectVolume(name, id, deptId) {
    const input = document.getElementById('volumeInput');
    if (input) {
        input.value = name || '';
        if (id) input.dataset.folderId = id;
        if (deptId) input.dataset.folderDeptId = deptId;
    }
    hideVolumeDropdown();
    const label = document.getElementById('selectedVolumeLabel');
    if (label) label.innerHTML = '<i class="ph ph-folder"></i> ' + name;

    // Render custom fields for the selected folder's entity
    renderCustomFieldsZone(deptId);

    // Advance wizard to step 2 (Fill Details)
    goToStep(2);

    // Smooth-scroll the right column into view so the form is visible
    const formRight = document.querySelector('.form-col-right');
    if (formRight) {
        setTimeout(() => {
            formRight.scrollIntoView({ behavior: 'smooth', block: 'start' });
        }, 80);
    }
}

function showVolumeDropdown() {
    const dd = document.getElementById('volumeDropdown');
    if (dd && currentVolumes.length) dd.classList.add('open');
}

function hideVolumeDropdown() {
    document.getElementById('volumeDropdown')?.classList.remove('open');
}

// ── SAVE DOCUMENT TO DB ────────────────────────────────────────────────────
async function saveDocumentToDb() {
    const folderId = document.getElementById('volumeInput')?.dataset.folderId;
    if (!folderId) { showToast('Please select a folder first', 'error'); return; }

    // Task 3: auto-fill today's date if blank
    const regDateEl = document.getElementById('registrationDate');
    if (regDateEl && !regDateEl.value) {
        const t = new Date();
        regDateEl.value = `${t.getFullYear()}/${String(t.getMonth()+1).padStart(2,'0')}/${String(t.getDate()).padStart(2,'0')}`;
    }

    const formData = new FormData();
    formData.append('type_id', document.getElementById('documentType')?.value || 1);
    formData.append('cat_id', 1);
    formData.append('date', regDateEl?.value || '');
    formData.append('importance_id', document.getElementById('importanceSelect')?.value || 1);
    formData.append('secret_id', document.getElementById('confidentialitySelect')?.value || 1);
    formData.append('subject', document.getElementById('topicInput')?.value || '');
    formData.append('keywords', document.getElementById('keywordsInput')?.value || '');
    formData.append('doc_number', document.getElementById('documentNumber')?.value || '');
    formData.append('form_date', document.getElementById('documentDate')?.value || '');
    formData.append('folder_id', folderId);
    formData.append('file_description', document.getElementById('statementInput')?.value || '');

    // Task 6A: send the folder's dept_id so backend can set To_Dept correctly
    const folderDeptId = document.getElementById('volumeInput')?.dataset.folderDeptId || '';
    if (folderDeptId) formData.append('folder_dept_id', folderDeptId);

    // Append custom field values (Fe1–Fe7)
    const cfValues = getCustomFieldValues();
    for (let i = 1; i <= 7; i++) {
        const key = `Fe${i}`;
        if (cfValues[key] !== undefined) formData.append(key, cfValues[key]);
    }

    _archivePendingFiles.forEach((f, i) => formData.append('files', f, getArchiveFileName(i)));

    // OCR is opt-in: only tell the backend to run/store OCR for files the
    // user actually clicked "Extract Text" on AND that are part of this
    // save's file list. _ocrUsedFileNames/_ocrTextByFileName are keyed by each
    // file's ORIGINAL name (set at extraction time, before any rename), but
    // the backend matches ocr_requested_files/ocr_extracted_text against the
    // filename it actually receives on the upload — which is the rename, if
    // one was made. So we look up membership by original name but key the
    // outgoing data by the (possibly renamed) upload name.
    const ocrRequestedNames = [];
    const ocrExtractedText = {};
    _archivePendingFiles.forEach((f, i) => {
        if (!_ocrUsedFileNames.has(f.name)) return;
        const uploadName = getArchiveFileName(i);
        ocrRequestedNames.push(uploadName);
        if (_ocrTextByFileName[f.name]) ocrExtractedText[uploadName] = _ocrTextByFileName[f.name];
    });
    if (ocrRequestedNames.length) {
        formData.append('ocr_requested_files', JSON.stringify(ocrRequestedNames));

        // Send along the text already extracted during preview so the
        // backend can store it directly instead of re-running OCR a
        // second time on the saved file.
        if (Object.keys(ocrExtractedText).length) {
            formData.append('ocr_extracted_text', JSON.stringify(ocrExtractedText));
        }
    }

    if (window._removedAttachmentIds && window._removedAttachmentIds.length) {
        formData.append('remove_attachment_ids', JSON.stringify(window._removedAttachmentIds));
    }

    try {
        // Check if we're in edit mode
        const form   = document.getElementById('section-archive');
        const editId = form?.dataset.editId || null;
        let res;
        if (editId) {
            // Update existing document
            formData.append('_method', 'PATCH');
            res = await fetch(`/api/documents/${editId}`, { method: 'POST', body: formData });
        } else {
            res = await fetch('/api/documents', { method: 'POST', body: formData });
        }
        const data = await res.json();
        if (data.success) {
            // Task 7: registration number is now numeric only
            const registrationNumber = String(data.registration_number || data.id);
            const regNum = document.getElementById('registrationNumber');
            if (regNum) regNum.value = registrationNumber;

            // Clear edit mode if active
            if (form?.dataset.editId) {
                delete form.dataset.editId;
                const saveBtn = document.querySelector('[onclick="saveDocument()"]');
                if (saveBtn) { saveBtn.innerHTML = '<i class="ph ph-floppy-disk"></i> <span>Save Document</span>'; delete saveBtn.dataset.isUpdate; }
                const existingWrap = document.getElementById('existingAttachmentsWrap');
                if (existingWrap) existingWrap.style.display = 'none';
                window._removedAttachmentIds = [];
            }

            // Task 4: show hijri date
            if (data.hijri_date) {
                const hijriEl = document.getElementById('hijriDateDisplay');
                if (hijriEl) hijriEl.value = data.hijri_date;
            }
            showToast('Document saved successfully!', 'success');
            return data;
        }
        showToast('Error: ' + data.error, 'error');
        return null;
    } catch(e) {
        showToast('Failed to save document', 'error');
        return null;
    }
}

// ── HIJRI CONVERSION (client-side) ────────────────────────────────────────
function gregorianToHijri(year, month, day) {
    if (!year || !month || !day) return '';
    const a = Math.floor((14 - month) / 12);
    const y = year + 4800 - a;
    const m = month + 12 * a - 3;
    const jdn = day + Math.floor((153 * m + 2) / 5) + 365 * y +
                Math.floor(y / 4) - Math.floor(y / 100) + Math.floor(y / 400) - 32045;
    let l = jdn - 1948440 + 10632;
    const n = Math.floor((l - 1) / 10631);
    l = l - 10631 * n + 354;
    const j = Math.floor((10985 - l) / 5316) * Math.floor((50 * l) / 17719) +
              Math.floor(l / 5670) * Math.floor((43 * l) / 15238);
    l = l - Math.floor((10985 - l) / 5316) * Math.floor((179 * j) / 2 - 2) -
            Math.floor(l / 5670) * Math.floor((199 * j) / 2 - 99);
    const hd = (l % 30) + 1;
    const hm = Math.floor(l / 30) + 1;
    const hy = 30 * n + j - 30;
    return `${hy}/${String(hm).padStart(2,'0')}/${String(hd).padStart(2,'0')}`;
}

function updateHijriDisplay() {
    const regDate = document.getElementById('registrationDate')?.value || '';
    const hijriEl = document.getElementById('hijriDateDisplay');
    if (!hijriEl) return;
    try {
        const parts = regDate.replace(/-/g,'/').split('/');
        if (parts.length === 3) {
            const h = gregorianToHijri(+parts[0], +parts[1], +parts[2]);
            hijriEl.value = h || '';
        }
    } catch(_) {}
}


let currentLang = localStorage.getItem('lang') || 'en';

// ── LANGUAGE ───────────────────────────────────────────────────────────────
function setLang(lang) {
    currentLang = lang;
    const root = document.getElementById('html-root');
    root.setAttribute('lang', lang);
    root.setAttribute('dir', lang === 'ar' ? 'rtl' : 'ltr');

    document.querySelectorAll('[data-en]').forEach(el => {
        const val = el.getAttribute('data-' + lang);
        if (val) el.textContent = val;
    });

    document.querySelectorAll('[data-en-placeholder]').forEach(el => {
        const val = el.getAttribute('data-' + lang + '-placeholder');
        if (val) el.placeholder = val;
    });

    const searchInput = document.getElementById('searchInput');
    if (searchInput) {
        searchInput.placeholder = lang === 'ar'
            ? 'ابحث برقم المستند أو الموضوع أو الكلمات المفتاحية'
            : 'Search by document number, subject, or keywords';
    }

    const regNum = document.getElementById('registrationNumber');
    if (regNum && !regNum.value.trim()) setRegistrationPlaceholder();

    const btnEn = document.getElementById('btn-en');
    const btnAr = document.getElementById('btn-ar');
    if (btnEn) btnEn.classList.toggle('active', lang === 'en');
    if (btnAr) btnAr.classList.toggle('active', lang === 'ar');

    localStorage.setItem('lang', lang);

    // Re-render any already-loaded notifications so open panel updates
    // immediately instead of waiting for the next poll cycle.
    if (typeof renderNotifications === 'function') renderNotifications(_lastNotifItems);

    // Re-populate the inquiry folder/subfolder selects with translated placeholder
    if (allEntities.length) _populateInqDeptSelect();

    renderSearch();
}

// ── SECTIONS ───────────────────────────────────────────────────────────────
function showSection(name) {
    // ── Admin-only guard for Control Panel ───────────────────────────────────
    if (name === 'control' && !_isAdmin) {
        showPermissionDenied(
            currentLang === 'ar' ? 'غير مصرح' : 'Unauthorized',
            currentLang === 'ar'
                ? 'لوحة التحكم متاحة للمسؤول فقط. يرجى التواصل مع المسؤول.'
                : 'The Control Panel is for administrators only. Please contact your administrator.'
        );
        return;
    }

    // ── Page access guard: all three main pages ──────────────────────────────
    const _pageAccessMap = { inquiries: 1, archive: 2, folders: 3, workflow: 4, messages: 5 };
    if (_pageAccessMap[name] !== undefined && !_allowed(_pageAccessMap[name], 'can_open')) {
        showPermissionDenied(
            currentLang === 'ar' ? 'غير مصرح' : 'Unauthorized',
            currentLang === 'ar'
                ? 'ليس لديك صلاحية الوصول إلى هذه الصفحة. يرجى التواصل مع المسؤول.'
                : 'You are not authorized to access this page. Please contact your administrator.'
        );
        return;
    }

    // ── Block removed pages (operations, scanner) entirely ───────────────────
    const removedPages = ['operations', 'scanner'];
    if (removedPages.includes(name)) return;

    document.querySelectorAll('.page-section').forEach(s => s.classList.remove('active'));
    const sec = document.getElementById('section-' + name);
    if (sec) sec.classList.add('active');

    // The top-right "Send for Approval" button only belongs on the
    // Workflow > New Request pane; switchWfTab() handles showing it once
    // we're inside Workflow, but leaving the section entirely must hide it.
    if (name !== 'workflow') {
        const topBtn = document.getElementById('wfTopSendBtn');
        const topDraftBtn = document.getElementById('wfTopSaveDraftBtn');
        if (topBtn) topBtn.style.display = 'none';
        if (topDraftBtn) topDraftBtn.style.display = 'none';
    }

    document.querySelectorAll('.side-link').forEach(l => {
        l.classList.toggle('active', l.getAttribute('data-section') === name);
    });

    if (name === 'inquiries') {
        syncInquiryDateFields();
        renderSearch();
        loadStats();
        _recentRender();
    }

    if (name === 'archive') {
        wfLoadExpiryAlerts();
    }

    if (name === 'reports') {
        rptLoadReports();
    }

    if (name === 'workflow') {
        wfLoadInbox();
        wfLoadSent();
        _wfResetNewRequestForm();
        switchWfTab('new');
    }

    if (name === 'messages') {
        msgLoadGroups();
    }
    if (name !== 'messages') {
        msgStopPolling();
    }

    closeNotif();
}

// ── GROUP MESSAGING ──────────────────────────────────────────────────────
let _msgActiveGroupId = null;
let _msgActiveGroupIsCreator = false;
let _msgActiveGroupMemberIds = [];
let _msgLastSeenId = 0;
let _msgPollTimer = null;
let _msgActiveGroupCreatorId = null;
let _msgBubbleEls = {};   // message id -> bubble DOM element (for placing "seen by" badges)
let _msgMineIds = [];     // ids of messages sent by me, in ascending order
let _msgSinceTs = null;   // ISO timestamp of the last successful poll, for edit/delete/reaction sync
let _msgReplyToId = null; // message id currently being replied to, or null
let _msgEditingId = null; // message id currently being edited, or null
let _msgTypingLastPing = 0;
const MSG_QUICK_REACTIONS = ['👍', '❤️', '😂', '😮', '😢', '🙏'];
let _msgAllUsers = null;
let _msgGroupsCache = [];

async function msgFetchUsers() {
    if (_msgAllUsers) return _msgAllUsers;
    try {
        const res = await fetch('/api/users/list-all');
        const data = await res.json();
        _msgAllUsers = data.users || [];
    } catch (e) {
        _msgAllUsers = [];
    }
    return _msgAllUsers;
}

async function msgLoadGroups() {
    const listEl = document.getElementById('msgGroupList');
    if (!listEl) return;
    try {
        const res = await fetch('/api/messages/groups');
        const data = await res.json();
        const groups = data.groups || [];
        _msgGroupsCache = groups;
        if (!groups.length) {
            listEl.innerHTML = `<div class="msg-empty" data-en="No group chats yet" data-ar="لا توجد محادثات جماعية حتى الآن">${currentLang === 'ar' ? 'لا توجد محادثات جماعية حتى الآن' : 'No group chats yet'}</div>`;
        } else {
            listEl.innerHTML = groups.map(g => {
                const preview = g.last_message ? escapeHtml((g.last_message.sender ? g.last_message.sender + ': ' : '') + g.last_message.body) : (currentLang === 'ar' ? 'لا توجد رسائل بعد' : 'No messages yet');
                const activeCls = (g.id === _msgActiveGroupId) ? ' active' : '';
                const badge = g.unread_count > 0 ? `<span class="msg-unread-dot">${g.unread_count}</span>` : '';
                return `<div class="msg-group-item${activeCls}" onclick="msgOpenGroup(${g.id})">
                    <div class="msg-group-item-top">
                        <strong>${escapeHtml(g.name)}</strong>
                        ${badge}
                    </div>
                    <div class="msg-group-item-preview">${preview}</div>
                </div>`;
            }).join('');
        }
        const totalUnread = groups.reduce((s, g) => s + (g.unread_count || 0), 0);
        msgUpdateBadge(totalUnread);
    } catch (e) {
        listEl.innerHTML = `<div class="msg-empty">${currentLang === 'ar' ? 'تعذر تحميل المجموعات' : 'Could not load groups'}</div>`;
    }
}

function msgUpdateBadge(count) {
    const badge = document.getElementById('msgBadge');
    if (!badge) return;
    if (count > 0) {
        badge.textContent = count > 99 ? '99+' : count;
        badge.style.display = '';
    } else {
        badge.style.display = 'none';
    }
}

async function msgRefreshUnreadBadge() {
    try {
        const res = await fetch('/api/messages/unread-count');
        const data = await res.json();
        msgUpdateBadge(data.unread_count || 0);
    } catch (e) { /* ignore */ }
}

async function msgOpenGroup(groupId) {
    const g = _msgGroupsCache.find(x => x.id === groupId);
    const groupName = g ? g.name : '';
    _msgActiveGroupId = groupId;
    _msgActiveGroupIsCreator = !!(g && g.is_creator);
    _msgActiveGroupCreatorId = g ? g.created_by : null;
    _msgActiveGroupMemberIds = (g && g.member_ids) || [];
    _msgLastSeenId = 0;
    _msgBubbleEls = {};
    _msgMineIds = [];
    _msgSinceTs = null;
    _msgReplyToId = null;
    _msgEditingId = null;
    const pane = document.getElementById('msgChatPane');
    pane.innerHTML = `
        <div class="msg-chat-header">
            <strong>${escapeHtml(groupName)}</strong>
            <div class="msg-chat-header-actions">
                <button type="button" class="btn-ghost btn-sm" onclick="msgToggleSearchBox()" title="${currentLang === 'ar' ? 'بحث' : 'Search'}"><i class="ph ph-magnifying-glass"></i></button>
                ${_msgActiveGroupIsCreator ? `<button type="button" class="btn-ghost btn-sm msg-add-member-btn" onclick="msgOpenAddMemberModal()" title="${currentLang === 'ar' ? 'إضافة أعضاء' : 'Add members'}"><i class="ph ph-user-plus"></i></button>` : ''}
                ${_msgActiveGroupIsCreator ? `<button type="button" class="btn-ghost btn-sm msg-delete-group-btn" onclick="msgDeleteGroup()" title="${currentLang === 'ar' ? 'حذف المجموعة' : 'Delete group'}"><i class="ph ph-trash"></i></button>` : ''}
            </div>
        </div>
        <div class="msg-search-box" id="msgSearchBox" style="display:none">
            <input type="text" id="msgSearchInput" class="form-input" placeholder="${currentLang === 'ar' ? 'ابحث في المحادثة...' : 'Search this conversation...'}" oninput="msgRunSearch(this.value)">
            <div class="msg-search-results" id="msgSearchResults"></div>
        </div>
        <div class="msg-pinned-bar" id="msgPinnedBar" style="display:none"></div>
        <div class="msg-chat-body" id="msgChatBody"></div>
        <div class="msg-typing-indicator" id="msgTypingIndicator"></div>
        <div class="msg-reply-banner" id="msgReplyBanner" style="display:none"></div>
        <div class="msg-chat-input-row">
            <div class="msg-attach-wrap">
                <button type="button" class="btn-ghost btn-sm" title="Attach" onclick="msgToggleAttachMenu(event)"><i class="ph ph-paperclip"></i></button>
                <div class="msg-attach-menu" id="msgAttachMenu" style="display:none">
                    <button type="button" onclick="msgChooseLaptopUpload()"><i class="ph ph-laptop"></i> <span data-en="From laptop" data-ar="من الكمبيوتر">From laptop</span></button>
                    <button type="button" onclick="msgChooseArchiveAttach()"><i class="ph ph-archive-box"></i> <span data-en="From archived documents" data-ar="من المستندات المؤرشفة">From archived documents</span></button>
                </div>
            </div>
            <input type="text" id="msgChatInput" class="form-input" placeholder="${currentLang === 'ar' ? 'اكتب رسالة...' : 'Type a message...'}" onkeydown="if(event.key==='Enter')msgSendMessage()" oninput="msgPingTyping()">
            <button type="button" class="btn-primary btn-sm" onclick="msgSendMessage()"><i class="ph ph-paper-plane-tilt"></i></button>
        </div>`;
    document.querySelectorAll('.msg-group-item').forEach(el => el.classList.remove('active'));
    if (_rtSocket && _rtSocket.connected) {
        _rtSocket.emit('join_group', { group_id: groupId });
    }
    await msgFetchMessages(true);
    msgStartPolling();
}

async function msgDeleteGroup() {
    if (!_msgActiveGroupId || !_msgActiveGroupIsCreator) return;
    const confirmMsg = currentLang === 'ar' ? 'هل تريد حذف هذه المجموعة؟ لا يمكن التراجع عن ذلك.' : 'Delete this group chat? This cannot be undone.';
    if (!confirm(confirmMsg)) return;
    try {
        const res = await fetch(`/api/messages/groups/${_msgActiveGroupId}`, { method: 'DELETE' });
        const data = await res.json();
        if (data.error) { alert(data.error); return; }
        msgStopPolling();
        _msgActiveGroupId = null;
        document.getElementById('msgChatPane').innerHTML = `<div class="msg-chat-placeholder" data-en="Select a group or start a new one" data-ar="اختر مجموعة أو ابدأ مجموعة جديدة">${currentLang === 'ar' ? 'اختر مجموعة أو ابدأ مجموعة جديدة' : 'Select a group or start a new one'}</div>`;
        await msgLoadGroups();
    } catch (e) {
        alert(currentLang === 'ar' ? 'تعذر حذف المجموعة' : 'Could not delete group');
    }
}

async function msgOpenAddMemberModal() {
    if (!_msgActiveGroupId || !_msgActiveGroupIsCreator) return;
    const status = document.getElementById('msgAddMemberStatus');
    if (status) status.textContent = '';
    document.getElementById('msgAddMemberModal').style.display = 'flex';
    const users = await msgFetchUsers();
    const selfId = document.body.dataset.userId || '';
    const usersById = {};
    users.forEach(u => { usersById[u.user_id] = u; });

    const currentList = document.getElementById('msgCurrentMembersList');
    if (currentList) {
        currentList.innerHTML = _msgActiveGroupMemberIds.map(id => {
            const u = usersById[id];
            const name = u ? (u.full_name || u.username) : `User #${id}`;
            const isCreator = String(id) === String(_msgActiveGroupCreatorId);
            return `
                <div class="msg-member-row" style="justify-content:space-between;align-items:center">
                    <span class="msg-member-name">${escapeHtml(name)}${isCreator ? ` <small style="opacity:.6">(${currentLang === 'ar' ? 'المنشئ' : 'creator'})</small>` : ''}</span>
                    ${isCreator ? '' : `<button type="button" class="btn-ghost btn-sm" title="${currentLang === 'ar' ? 'إزالة' : 'Remove'}" onclick="msgRemoveMember(${id})"><i class="ph ph-x"></i></button>`}
                </div>`;
        }).join('') || `<div class="msg-empty">${currentLang === 'ar' ? 'لا يوجد أعضاء' : 'No members'}</div>`;
    }

    const candidates = users.filter(u => !_msgActiveGroupMemberIds.includes(u.user_id) && String(u.user_id) !== String(selfId));
    const picker = document.getElementById('msgAddMemberPicker');
    picker.innerHTML = candidates.map(u => `
        <label class="msg-member-row">
            <input type="checkbox" class="msg-add-member-checkbox" value="${u.user_id}">
            <span class="msg-member-name">${escapeHtml(u.full_name || u.username)}</span>
        </label>`).join('') || `<div class="msg-empty">${currentLang === 'ar' ? 'الجميع أعضاء بالفعل' : 'Everyone is already in this group'}</div>`;
}

async function msgRemoveMember(userId) {
    if (!_msgActiveGroupId || !_msgActiveGroupIsCreator) return;
    const status = document.getElementById('msgAddMemberStatus');
    const confirmMsg = currentLang === 'ar' ? 'إزالة هذا العضو من المجموعة؟' : 'Remove this member from the group?';
    if (!confirm(confirmMsg)) return;
    try {
        const res = await fetch(`/api/messages/groups/${_msgActiveGroupId}/members/${userId}`, { method: 'DELETE' });
        const data = await res.json();
        if (data.error) { if (status) status.textContent = data.error; return; }
        _msgActiveGroupMemberIds = _msgActiveGroupMemberIds.filter(id => id !== userId);
        await msgLoadGroups();
        await msgOpenAddMemberModal();
    } catch (e) {
        if (status) status.textContent = currentLang === 'ar' ? 'تعذرت الإزالة' : 'Could not remove member';
    }
}

function msgCloseAddMemberModal() {
    document.getElementById('msgAddMemberModal').style.display = 'none';
}

async function msgSubmitAddMembers() {
    const status = document.getElementById('msgAddMemberStatus');
    const ids = Array.from(document.querySelectorAll('.msg-add-member-checkbox:checked')).map(el => parseInt(el.value, 10));
    if (!ids.length) {
        status.textContent = currentLang === 'ar' ? 'اختر عضوًا واحدًا على الأقل' : 'Select at least one member';
        return;
    }
    try {
        const res = await fetch(`/api/messages/groups/${_msgActiveGroupId}/members`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ member_ids: ids }),
        });
        const data = await res.json();
        if (data.error) { status.textContent = data.error; return; }
        msgCloseAddMemberModal();
        await msgLoadGroups();
        _msgActiveGroupMemberIds = _msgActiveGroupMemberIds.concat(ids);
    } catch (e) {
        status.textContent = currentLang === 'ar' ? 'تعذرت الإضافة' : 'Could not add members';
    }
}

function msgBuildBubbleHtml(m) {
    const time = m.created_at ? new Date(m.created_at).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' }) : '';
    if (m.is_deleted) {
        return `${!m.is_mine ? `<div class="msg-bubble-sender">${escapeHtml(m.sender)}</div>` : ''}<div class="msg-bubble-text msg-deleted-text">${currentLang === 'ar' ? 'تم حذف هذه الرسالة' : 'This message was deleted'}</div><div class="msg-bubble-time">${time}</div>`;
    }
    const replyHtml = m.reply_to ? `
        <div class="msg-reply-quote" onclick="msgJumpToMessage(${m.reply_to.id})">
            <strong>${escapeHtml(m.reply_to.sender)}</strong>
            <span>${escapeHtml(m.reply_to.snippet)}</span>
        </div>` : '';
    const attHtml = (m.attachments || []).map(a => {
        if (a.type === 'archive') {
            return `<div class="msg-att-chip" onclick="viewTransaction(${a.doc_id})" title="${currentLang === 'ar' ? 'فتح المستند' : 'Open document'}">
                <i class="ph ph-archive-box"></i>
                <span>${escapeHtml(a.doc_subject || a.file_name)} <small>#${a.doc_reg_number || a.doc_id}</small></span>
            </div>`;
        }
        return `<div class="msg-att-chip" onclick="downloadAttachmentFile('/api/messages/attachments/${a.id}/download', '${escapeHtml(a.file_name).replace(/'/g, "\\'")}')" title="${currentLang === 'ar' ? 'تنزيل' : 'Download'}">
            <i class="ph ph-paperclip"></i>
            <span>${escapeHtml(a.file_name)}</span>
        </div>`;
    }).join('');
    const editedTag = m.is_edited ? ` <span class="msg-edited-tag">(${currentLang === 'ar' ? 'معدّلة' : 'edited'})</span>` : '';
    const canDelete = m.is_mine || _msgActiveGroupIsCreator;
    const toolbar = `
        <div class="msg-bubble-toolbar">
            <button type="button" title="${currentLang === 'ar' ? 'رد' : 'Reply'}" onclick="msgStartReply(${m.id})"><i class="ph ph-arrow-bend-up-left"></i></button>
            <button type="button" title="${currentLang === 'ar' ? 'تفاعل' : 'React'}" onclick="msgOpenReactionPicker(event, ${m.id})"><i class="ph ph-smiley"></i></button>
            <button type="button" title="${currentLang === 'ar' ? 'تثبيت' : 'Pin'}" onclick="msgTogglePin(${m.id})"><i class="ph ph-push-pin"></i></button>
            ${m.is_mine ? `<button type="button" title="${currentLang === 'ar' ? 'تعديل' : 'Edit'}" onclick="msgStartEdit(${m.id})"><i class="ph ph-pencil-simple"></i></button>` : ''}
            ${canDelete ? `<button type="button" title="${currentLang === 'ar' ? 'حذف' : 'Delete'}" onclick="msgDeleteMessage(${m.id})"><i class="ph ph-trash"></i></button>` : ''}
        </div>`;
    return `${replyHtml}${!m.is_mine ? `<div class="msg-bubble-sender">${escapeHtml(m.sender)}</div>` : ''}${m.body ? `<div class="msg-bubble-text">${escapeHtml(m.body)}</div>` : ''}${attHtml ? `<div class="msg-att-list">${attHtml}</div>` : ''}<div class="msg-reactions-row"></div>${toolbar}<div class="msg-bubble-time">${time}${editedTag}</div>`;
}
let _msgFetchInFlight = false;

async function msgFetchMessages(scrollToBottom) {
    if (!_msgActiveGroupId) return;
    const body = document.getElementById('msgChatBody');
    if (!body) return;
    if (_msgFetchInFlight) return; // a poll or another call is already in flight — avoid re-fetching the same since_id
    _msgFetchInFlight = true;
    try {
        const sinceTsParam = _msgSinceTs ? `&since_ts=${encodeURIComponent(_msgSinceTs)}` : '';
        const res = await fetch(`/api/messages/groups/${_msgActiveGroupId}/messages?since_id=${_msgLastSeenId}${sinceTsParam}`);
        const data = await res.json();
        const items = data.messages || [];
        if (items.length) {
            items.forEach(m => {
                if (_msgBubbleEls[m.id]) return; // already rendered — never append the same message twice
                const div = document.createElement('div');
                div.className = 'msg-bubble' + (m.is_mine ? ' mine' : '') + (m.is_deleted ? ' deleted' : '');
                div.dataset.msgId = m.id;
                div.innerHTML = msgBuildBubbleHtml(m);
                body.appendChild(div);
                _msgBubbleEls[m.id] = div;
                if (m.is_mine) _msgMineIds.push(m.id);
            });
            _msgLastSeenId = items[items.length - 1].id;
            if (scrollToBottom || true) body.scrollTop = body.scrollHeight;
            msgLoadGroups();
        }
        (data.updated_messages || []).forEach(msgPatchUpdatedMessage);
        msgRenderReceipts(data.receipts || []);
        Object.keys(data.reactions || {}).forEach(id => msgApplyReactionsToBubble(id, data.reactions[id]));
        msgRenderPins(data.pins || []);
        msgRenderTyping(data.typing_user_ids || []);
        if (data.server_time) _msgSinceTs = data.server_time;
    } catch (e) { /* ignore transient poll errors */
    } finally {
        _msgFetchInFlight = false;
    }
}

// Patches an already-rendered bubble in place after an edit or soft-delete,
// so history further back than the current poll window stays in sync.
function msgPatchUpdatedMessage(u) {
    const el = _msgBubbleEls[u.id];
    if (!el) return;
    const wasMine = el.classList.contains('mine');
    const fakeMsg = { id: u.id, is_mine: wasMine, body: u.body, is_edited: u.is_edited, is_deleted: u.is_deleted, created_at: null, attachments: [] };
    // Preserve the original sender label / time text since patches don't resend them.
    const senderEl = el.querySelector('.msg-bubble-sender');
    const timeEl = el.querySelector('.msg-bubble-time');
    const oldTime = timeEl ? timeEl.textContent.replace(/\s*\(.*?\)\s*$/, '').trim() : '';
    if (u.is_deleted) {
        el.classList.add('deleted');
        el.innerHTML = `${senderEl ? senderEl.outerHTML : ''}<div class="msg-bubble-text msg-deleted-text">${currentLang === 'ar' ? 'تم حذف هذه الرسالة' : 'This message was deleted'}</div><div class="msg-bubble-time">${oldTime}</div>`;
    } else {
        const textEl = el.querySelector('.msg-bubble-text');
        if (textEl) textEl.textContent = u.body;
        if (u.is_edited && timeEl && !timeEl.querySelector('.msg-edited-tag')) {
            const tag = document.createElement('span');
            tag.className = 'msg-edited-tag';
            tag.textContent = ` (${currentLang === 'ar' ? 'معدّلة' : 'edited'})`;
            timeEl.appendChild(tag);
        }
    }
}

// Renders "Seen by ..." under the last message-of-mine that each other member
// has read up to. Multiple members reading up to the same message are merged
// into a single badge.
function msgRenderReceipts(receipts) {
    const body = document.getElementById('msgChatBody');
    if (!body) return;
    body.querySelectorAll('.msg-seen-badge').forEach(el => el.remove());
    if (!_msgMineIds.length) return;

    const byAnchor = {};
    receipts.forEach(r => {
        if (!r.last_read_msg_id) return;
        let anchor = null;
        for (let i = _msgMineIds.length - 1; i >= 0; i--) {
            if (_msgMineIds[i] <= r.last_read_msg_id) { anchor = _msgMineIds[i]; break; }
        }
        if (anchor == null) return;
        (byAnchor[anchor] = byAnchor[anchor] || []).push(r.name || '');
    });

    Object.keys(byAnchor).forEach(anchorId => {
        const el = _msgBubbleEls[anchorId];
        if (!el) return;
        const names = byAnchor[anchorId].filter(Boolean);
        const label = names.length > 2
            ? `${currentLang === 'ar' ? 'شوهدت من قبل' : 'Seen by'} ${names.length}`
            : `${currentLang === 'ar' ? 'شوهدت من قبل' : 'Seen by'} ${names.join(', ')}`;
        const badge = document.createElement('div');
        badge.className = 'msg-seen-badge';
        badge.title = names.join(', ');
        badge.textContent = names.length ? label : '';
        if (names.length) el.appendChild(badge);
    });
}

// ── Reactions ────────────────────────────────────────────────────────────
function msgApplyReactionsToBubble(msgId, reactions) {
    const el = _msgBubbleEls[msgId];
    if (!el) return;
    const row = el.querySelector('.msg-reactions-row');
    if (!row) return;
    if (!reactions || !reactions.length) { row.innerHTML = ''; return; }
    row.innerHTML = reactions.map(r => `
        <button type="button" class="msg-reaction-chip${r.mine ? ' mine' : ''}" onclick="msgToggleReaction(${msgId}, '${r.emoji}')">
            <span>${r.emoji}</span><small>${r.count}</small>
        </button>`).join('');
}

async function msgToggleReaction(msgId, emoji) {
    if (!_msgActiveGroupId) return;
    try {
        const res = await fetch(`/api/messages/groups/${_msgActiveGroupId}/messages/${msgId}/react`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ emoji }),
        });
        const data = await res.json();
        if (data.error) return;
        msgApplyReactionsToBubble(msgId, data.reactions || []);
    } catch (e) { /* ignore */ }
}

function msgOpenReactionPicker(ev, msgId) {
    ev.stopPropagation();
    document.querySelectorAll('.msg-emoji-picker').forEach(el => el.remove());
    const picker = document.createElement('div');
    picker.className = 'msg-emoji-picker';
    picker.innerHTML = MSG_QUICK_REACTIONS.map(e => `<button type="button" onclick="msgToggleReaction(${msgId}, '${e}');this.closest('.msg-emoji-picker').remove()">${e}</button>`).join('');
    document.body.appendChild(picker);
    const rect = ev.currentTarget.getBoundingClientRect();
    picker.style.top = `${rect.bottom + window.scrollY + 4}px`;
    picker.style.left = `${rect.left + window.scrollX}px`;
    const closeOnce = (e2) => { if (!picker.contains(e2.target)) { picker.remove(); document.removeEventListener('click', closeOnce); } };
    setTimeout(() => document.addEventListener('click', closeOnce), 0);
}

// ── Reply ────────────────────────────────────────────────────────────────
function msgStartReply(msgId) {
    const el = _msgBubbleEls[msgId];
    if (!el) return;
    const textEl = el.querySelector('.msg-bubble-text');
    const senderEl = el.querySelector('.msg-bubble-sender');
    const sender = senderEl ? senderEl.textContent : (currentLang === 'ar' ? 'أنت' : 'You');
    const snippet = textEl ? textEl.textContent.slice(0, 80) : '';
    _msgReplyToId = msgId;
    const banner = document.getElementById('msgReplyBanner');
    if (banner) {
        banner.style.display = 'flex';
        banner.innerHTML = `
            <div class="msg-reply-banner-text"><strong>${escapeHtml(currentLang === 'ar' ? 'الرد على' : 'Replying to')} ${escapeHtml(sender)}</strong><span>${escapeHtml(snippet)}</span></div>
            <button type="button" onclick="msgCancelReply()"><i class="ph ph-x"></i></button>`;
    }
    document.getElementById('msgChatInput')?.focus();
}

function msgCancelReply() {
    _msgReplyToId = null;
    const banner = document.getElementById('msgReplyBanner');
    if (banner) { banner.style.display = 'none'; banner.innerHTML = ''; }
}

function msgJumpToMessage(msgId) {
    const el = _msgBubbleEls[msgId];
    if (!el) return;
    el.scrollIntoView({ behavior: 'smooth', block: 'center' });
    el.classList.add('msg-flash');
    setTimeout(() => el.classList.remove('msg-flash'), 1200);
}

// ── Edit ─────────────────────────────────────────────────────────────────
function msgStartEdit(msgId) {
    const el = _msgBubbleEls[msgId];
    const textEl = el && el.querySelector('.msg-bubble-text');
    if (!textEl) return;
    _msgEditingId = msgId;
    msgCancelReply();
    const input = document.getElementById('msgChatInput');
    if (input) {
        input.value = textEl.textContent;
        input.focus();
    }
    const banner = document.getElementById('msgReplyBanner');
    if (banner) {
        banner.style.display = 'flex';
        banner.innerHTML = `
            <div class="msg-reply-banner-text"><strong>${escapeHtml(currentLang === 'ar' ? 'تعديل الرسالة' : 'Editing message')}</strong></div>
            <button type="button" onclick="msgCancelEdit()"><i class="ph ph-x"></i></button>`;
    }
}

function msgCancelEdit() {
    _msgEditingId = null;
    const input = document.getElementById('msgChatInput');
    if (input) input.value = '';
    const banner = document.getElementById('msgReplyBanner');
    if (banner) { banner.style.display = 'none'; banner.innerHTML = ''; }
}

async function msgDeleteMessage(msgId) {
    if (!_msgActiveGroupId) return;
    const confirmMsg = currentLang === 'ar' ? 'حذف هذه الرسالة؟' : 'Delete this message?';
    if (!confirm(confirmMsg)) return;
    try {
        const res = await fetch(`/api/messages/groups/${_msgActiveGroupId}/messages/${msgId}`, { method: 'DELETE' });
        const data = await res.json();
        if (data.error) { alert(data.error); return; }
        msgPatchUpdatedMessage({ id: msgId, body: '', is_edited: false, is_deleted: true });
    } catch (e) {
        alert(currentLang === 'ar' ? 'تعذر حذف الرسالة' : 'Could not delete message');
    }
}

// ── Pins ─────────────────────────────────────────────────────────────────
function msgRenderPins(pins) {
    const bar = document.getElementById('msgPinnedBar');
    if (!bar) return;
    if (!pins || !pins.length) { bar.style.display = 'none'; bar.innerHTML = ''; return; }
    bar.style.display = 'flex';
    bar.innerHTML = pins.map(p => `
        <div class="msg-pin-item">
            <i class="ph ph-push-pin"></i>
            <span class="msg-pin-snippet" onclick="msgJumpToMessage(${p.msg_id})"><strong>${escapeHtml(p.sender)}:</strong> ${escapeHtml(p.snippet)}</span>
            <button type="button" onclick="msgUnpinMessage(${p.msg_id})" title="${currentLang === 'ar' ? 'إلغاء التثبيت' : 'Unpin'}"><i class="ph ph-x"></i></button>
        </div>`).join('');
}

async function msgTogglePin(msgId) {
    const el = _msgBubbleEls[msgId];
    const alreadyPinned = document.querySelector(`#msgPinnedBar .msg-pin-item span[onclick="msgJumpToMessage(${msgId})"]`);
    if (alreadyPinned) { msgUnpinMessage(msgId); return; }
    if (!_msgActiveGroupId) return;
    try {
        const res = await fetch(`/api/messages/groups/${_msgActiveGroupId}/pin`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ msg_id: msgId }),
        });
        const data = await res.json();
        if (data.error) { alert(data.error); return; }
        await msgFetchMessages(false);
    } catch (e) { /* ignore */ }
}

async function msgUnpinMessage(msgId) {
    if (!_msgActiveGroupId) return;
    try {
        await fetch(`/api/messages/groups/${_msgActiveGroupId}/pin/${msgId}`, { method: 'DELETE' });
        await msgFetchMessages(false);
    } catch (e) { /* ignore */ }
}

// ── Typing indicator ────────────────────────────────────────────────────
function msgRenderTyping(typingUserIds) {
    const el = document.getElementById('msgTypingIndicator');
    if (!el) return;
    if (!typingUserIds || !typingUserIds.length) { el.style.display = 'none'; el.textContent = ''; return; }
    const names = typingUserIds.map(id => {
        const u = (_msgAllUsers || []).find(x => x.user_id === id);
        return u ? (u.full_name || u.username) : (currentLang === 'ar' ? 'شخص ما' : 'Someone');
    });
    const label = names.length > 1
        ? (currentLang === 'ar' ? `${names.length} أشخاص يكتبون...` : `${names.length} people are typing...`)
        : (currentLang === 'ar' ? `${names[0]} يكتب...` : `${names[0]} is typing...`);
    el.textContent = label;
    el.style.display = 'block';
}

function msgPingTyping() {
    if (!_msgActiveGroupId) return;
    const now = Date.now();
    if (now - _msgTypingLastPing < 2500) return;
    _msgTypingLastPing = now;
    if (_rtSocket && _rtSocket.connected) {
        _rtSocket.emit('typing', { group_id: _msgActiveGroupId });
    } else {
        // Socket not connected (e.g. still loading, or reconnecting) — fall
        // back to the HTTP endpoint, which still works and also emits.
        fetch(`/api/messages/groups/${_msgActiveGroupId}/typing`, { method: 'POST' }).catch(() => {});
    }
}

// ── Search ───────────────────────────────────────────────────────────────
let _msgSearchDebounce = null;
function msgToggleSearchBox() {
    const box = document.getElementById('msgSearchBox');
    if (!box) return;
    const willShow = box.style.display === 'none';
    box.style.display = willShow ? 'block' : 'none';
    if (willShow) document.getElementById('msgSearchInput')?.focus();
    else document.getElementById('msgSearchResults').innerHTML = '';
}

function msgRunSearch(query) {
    clearTimeout(_msgSearchDebounce);
    const resultsEl = document.getElementById('msgSearchResults');
    if (!query || !query.trim()) { if (resultsEl) resultsEl.innerHTML = ''; return; }
    _msgSearchDebounce = setTimeout(async () => {
        if (!_msgActiveGroupId) return;
        try {
            const res = await fetch(`/api/messages/groups/${_msgActiveGroupId}/search?q=${encodeURIComponent(query)}`);
            const data = await res.json();
            const results = data.results || [];
            if (!resultsEl) return;
            resultsEl.innerHTML = results.map(r => `
                <div class="msg-search-result" onclick="msgSearchJump(${r.id})">
                    <strong>${escapeHtml(r.sender)}</strong>
                    <span>${escapeHtml(r.body.slice(0, 100))}</span>
                </div>`).join('') || `<div class="msg-empty">${currentLang === 'ar' ? 'لا نتائج' : 'No results'}</div>`;
        } catch (e) { /* ignore */ }
    }, 300);
}

function msgSearchJump(msgId) {
    if (_msgBubbleEls[msgId]) {
        msgJumpToMessage(msgId);
    } else {
        showToast(currentLang === 'ar' ? 'الرسالة ليست ضمن السجل المعروض حاليًا' : 'Message is outside the currently loaded history', 'info');
    }
}

function msgStartPolling() {
    msgStopPolling();
    // Sockets are now the primary path (see rtInit / 'new_message' etc.
    // above) — this interval is only a safety net for a dropped/slow
    // connection, so it's much longer than the old 4s.
    _msgPollTimer = setInterval(() => msgFetchMessages(false), 45000);
}

function msgStopPolling() {
    if (_rtSocket && _rtSocket.connected && _msgActiveGroupId) {
        _rtSocket.emit('leave_group', { group_id: _msgActiveGroupId });
    }
    if (_msgPollTimer) { clearInterval(_msgPollTimer); _msgPollTimer = null; }
}

let _msgSendInFlight = false;

async function msgSendMessage() {
    const input = document.getElementById('msgChatInput');
    if (!input || !_msgActiveGroupId) return;
    const body = input.value.trim();
    if (!body) return;
    if (_msgSendInFlight) return; // already sending — ignore a fast double Enter/click
    _msgSendInFlight = true;

    if (_msgEditingId) {
        const editId = _msgEditingId;
        input.value = '';
        msgCancelEdit();
        try {
            const res = await fetch(`/api/messages/groups/${_msgActiveGroupId}/messages/${editId}`, {
                method: 'PUT',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ body }),
            });
            const data = await res.json();
            if (data.error) { alert(data.error); return; }
            msgPatchUpdatedMessage({ id: editId, body, is_edited: true, is_deleted: false });
        } catch (e) {
            alert(currentLang === 'ar' ? 'تعذر تعديل الرسالة' : 'Could not edit message');
        } finally {
            _msgSendInFlight = false;
        }
        return;
    }

    const replyTo = _msgReplyToId;
    input.value = '';
    msgCancelReply();
    try {
        const res = await fetch(`/api/messages/groups/${_msgActiveGroupId}/messages`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ body, reply_to_msg_id: replyTo }),
        });
        const data = await res.json();
        if (data.error) { alert(data.error); return; }
        await msgFetchMessages(true);
    } catch (e) {
        alert(currentLang === 'ar' ? 'تعذر إرسال الرسالة' : 'Could not send message');
    } finally {
        _msgSendInFlight = false;
    }
}

async function msgOpenNewGroupModal() {
    const modal = document.getElementById('msgNewGroupModal');
    const status = document.getElementById('msgNewGroupStatus');
    const nameInput = document.getElementById('msgGroupNameInput');
    if (status) status.textContent = '';
    if (nameInput) nameInput.value = '';
    modal.style.display = 'flex';
    const users = await msgFetchUsers();
    const selfId = document.body.dataset.userId || '';
    const picker = document.getElementById('msgMemberPicker');
    picker.innerHTML = users.filter(u => String(u.user_id) !== String(selfId)).map(u => `
        <label class="msg-member-row">
            <input type="checkbox" class="msg-member-checkbox" value="${u.user_id}">
            <span class="msg-member-name">${escapeHtml(u.full_name || u.username)}</span>
        </label>`).join('') || `<div class="msg-empty">${currentLang === 'ar' ? 'لا يوجد مستخدمون' : 'No users found'}</div>`;
}

function msgCloseNewGroupModal() {
    document.getElementById('msgNewGroupModal').style.display = 'none';
}

async function msgSubmitNewGroup() {
    const name = document.getElementById('msgGroupNameInput').value.trim();
    const status = document.getElementById('msgNewGroupStatus');
    const memberIds = Array.from(document.querySelectorAll('.msg-member-checkbox:checked')).map(el => parseInt(el.value, 10));
    if (!memberIds.length) {
        status.textContent = currentLang === 'ar' ? 'اختر عضوًا واحدًا على الأقل' : 'Select at least one member';
        return;
    }
    try {
        const res = await fetch('/api/messages/groups', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ name, member_ids: memberIds }),
        });
        const data = await res.json();
        if (data.error) { status.textContent = data.error; return; }
        msgCloseNewGroupModal();
        await msgLoadGroups();
        msgOpenGroup(data.group_id, name || 'Group Chat');
    } catch (e) {
        status.textContent = currentLang === 'ar' ? 'تعذر إنشاء المجموعة' : 'Could not create group';
    }
}

function msgToggleAttachMenu(ev) {
    ev.stopPropagation();
    const menu = document.getElementById('msgAttachMenu');
    if (!menu) return;
    const willShow = menu.style.display === 'none';
    menu.style.display = willShow ? 'flex' : 'none';
    if (willShow) {
        const closeOnce = () => { menu.style.display = 'none'; document.removeEventListener('click', closeOnce); };
        setTimeout(() => document.addEventListener('click', closeOnce), 0);
    }
}

function msgChooseLaptopUpload() {
    document.getElementById('msgAttachMenu').style.display = 'none';
    document.getElementById('msgFileInput').click();
}

let _msgUploadInFlight = false;

async function msgHandleFileSelect(ev) {
    const files = ev.target.files;
    if (!files || !files.length || !_msgActiveGroupId) return;
    if (_msgUploadInFlight) { ev.target.value = ''; return; } // already uploading — ignore a duplicate trigger
    _msgUploadInFlight = true;
    const caption = (document.getElementById('msgChatInput') || {}).value || '';
    const fd = new FormData();
    for (const f of files) fd.append('files', f);
    fd.append('body', caption.trim());
    ev.target.value = '';
    try {
        const res = await fetch(`/api/messages/groups/${_msgActiveGroupId}/messages/upload`, { method: 'POST', body: fd });
        const data = await res.json();
        if (data.error) { alert(data.error); return; }
        const input = document.getElementById('msgChatInput');
        if (input) input.value = '';
        await msgFetchMessages(true);
    } catch (e) {
        alert(currentLang === 'ar' ? 'تعذر رفع الملف' : 'Could not upload file');
    } finally {
        _msgUploadInFlight = false;
    }
}

function msgChooseArchiveAttach() {
    document.getElementById('msgAttachMenu').style.display = 'none';
    if (!_msgActiveGroupId) return;
    document.getElementById('msgArchiveModal').style.display = 'flex';
    document.getElementById('msgArchiveSearchInput').value = '';
    document.getElementById('msgArchiveStatus').textContent = '';
    document.getElementById('msgArchiveResults').innerHTML = `<div class="msg-empty">${currentLang === 'ar' ? 'ابحث عن مستند لإرفاقه' : 'Search for a document to attach'}</div>`;
    _msgArchiveSelected.clear();
}

function msgCloseArchiveModal() {
    document.getElementById('msgArchiveModal').style.display = 'none';
}

const _msgArchiveSelected = new Set();
let _msgArchiveSearchTimer = null;

function msgDebouncedArchiveSearch() {
    clearTimeout(_msgArchiveSearchTimer);
    _msgArchiveSearchTimer = setTimeout(msgRunArchiveSearch, 350);
}

async function msgRunArchiveSearch() {
    const q = document.getElementById('msgArchiveSearchInput').value.trim();
    const resultsEl = document.getElementById('msgArchiveResults');
    if (!q) {
        resultsEl.innerHTML = `<div class="msg-empty">${currentLang === 'ar' ? 'ابحث عن مستند لإرفاقه' : 'Search for a document to attach'}</div>`;
        return;
    }
    resultsEl.innerHTML = `<div class="msg-empty">${currentLang === 'ar' ? 'جاري البحث...' : 'Searching...'}</div>`;
    try {
        const res = await fetch(`/api/documents/search?q=${encodeURIComponent(q)}&page_size=20`);
        const data = await res.json();
        const docs = data.results || [];
        if (!docs.length) {
            resultsEl.innerHTML = `<div class="msg-empty">${currentLang === 'ar' ? 'لا توجد نتائج' : 'No results'}</div>`;
            return;
        }
        resultsEl.innerHTML = docs.map(d => `
            <label class="msg-member-row">
                <input type="checkbox" class="msg-archive-checkbox" value="${d.id}" ${_msgArchiveSelected.has(d.id) ? 'checked' : ''} onchange="msgToggleArchiveSelect(${d.id}, this.checked)">
                <span class="msg-member-name">${escapeHtml(d.subject || '—')} <small style="color:var(--muted)">#${d.registration_number || d.id}</small></span>
            </label>`).join('');
    } catch (e) {
        resultsEl.innerHTML = `<div class="msg-empty">${currentLang === 'ar' ? 'تعذر البحث' : 'Search failed'}</div>`;
    }
}

function msgToggleArchiveSelect(id, checked) {
    if (checked) _msgArchiveSelected.add(id); else _msgArchiveSelected.delete(id);
}

async function msgSubmitArchiveAttach() {
    const status = document.getElementById('msgArchiveStatus');
    if (!_msgArchiveSelected.size) {
        status.textContent = currentLang === 'ar' ? 'اختر مستندًا واحدًا على الأقل' : 'Select at least one document';
        return;
    }
    const caption = (document.getElementById('msgChatInput') || {}).value || '';
    try {
        const res = await fetch(`/api/messages/groups/${_msgActiveGroupId}/messages/attach-archive`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ doc_ids: Array.from(_msgArchiveSelected), body: caption.trim() }),
        });
        const data = await res.json();
        if (data.error) { status.textContent = data.error; return; }
        const input = document.getElementById('msgChatInput');
        if (input) input.value = '';
        msgCloseArchiveModal();
        await msgFetchMessages(true);
    } catch (e) {
        status.textContent = currentLang === 'ar' ? 'تعذر الإرفاق' : 'Could not attach';
    }
}

// Periodically refresh the sidebar unread badge regardless of which section is active
// Safety-net interval — 'new_message' socket events already trigger
// msgLoadGroups(), which recomputes this badge instantly on real activity.
setInterval(msgRefreshUnreadBadge, 60000);
document.addEventListener('DOMContentLoaded', msgRefreshUnreadBadge);

// ── Workflow expiry-alert banner (Archive page) ─────────────────────────────
// Fetches documents (submitted by or assigned to the current user) expiring
// within the user's configured alert window and shows/hides the amber
// banner near the top of the Archive page accordingly.
async function wfLoadExpiryAlerts() {
    const banner = document.getElementById('wfExpiryAlertBanner');
    const msgEl = document.getElementById('wfExpiryAlertMsg');
    if (!banner || !msgEl) return;
    try {
        const res = await fetch('/api/workflow/expiry-alerts');
        const data = await res.json();
        const alerts = (data && data.alerts) || [];
        if (!alerts.length) {
            banner.style.display = 'none';
            sessionStorage.removeItem('wfExpiryAlertDismissedKey');
            return;
        }
        const alertKey = alerts.map(a => `${a.instance_id}:${a.days_remaining}`).join(',');
        banner.dataset.alertKey = alertKey;
        if (sessionStorage.getItem('wfExpiryAlertDismissedKey') === alertKey) {
            banner.style.display = 'none';
            return;
        }
        const first = alerts[0];
        const dayWord = (n) => currentLang === 'ar'
            ? (n <= 0 ? 'اليوم' : `خلال ${n} يوم`)
            : (n <= 0 ? 'today' : (n === 1 ? 'in 1 day' : `in ${n} days`));
        let msg;
        if (alerts.length === 1) {
            msg = currentLang === 'ar'
                ? `مستند واحد على وشك الانتهاء — "${first.subject}" ينتهي ${dayWord(first.days_remaining)}.`
                : `1 document is expiring soon — "${first.subject}" expires ${dayWord(first.days_remaining)}.`;
        } else {
            msg = currentLang === 'ar'
                ? `${alerts.length} مستندات على وشك الانتهاء — أقربها "${first.subject}" ينتهي ${dayWord(first.days_remaining)}.`
                : `${alerts.length} documents are expiring soon — the soonest, "${first.subject}", expires ${dayWord(first.days_remaining)}.`;
        }
        msgEl.textContent = msg;
        banner.style.display = 'flex';
    } catch (e) {
        console.error('[Workflow] failed to load expiry alerts', e);
    }
}

function wfDismissExpiryAlertBanner(btn) {
    const banner = btn.closest('#wfExpiryAlertBanner');
    if (!banner) return;
    if (banner.dataset.alertKey) sessionStorage.setItem('wfExpiryAlertDismissedKey', banner.dataset.alertKey);
    banner.style.display = 'none';
}

// ── WORKFLOW MODULE (Inbox / Sent / Reject / Resubmit) ──────────────────────
let _wfCurrentInstanceId = null;

function switchWfTab(tab) {
    document.querySelectorAll('.wf-tab').forEach(b => b.classList.toggle('active', b.dataset.wftab === tab));
    document.getElementById('wfInboxPane').classList.toggle('active', tab === 'inbox');
    document.getElementById('wfSentPane').classList.toggle('active', tab === 'sent');
    document.getElementById('wfHistoryPane').classList.toggle('active', tab === 'history');
    document.getElementById('wfNewPane').classList.toggle('active', tab === 'new');
    if (tab === 'history') wfInitHistoryTab();
    // Persistent top-right Save Draft / Send for Approval only make sense
    // on the New Request pane — hide them everywhere else.
    const topBtn = document.getElementById('wfTopSendBtn');
    const topDraftBtn = document.getElementById('wfTopSaveDraftBtn');
    if (topBtn) topBtn.style.display = (tab === 'new') ? '' : 'none';
    if (topDraftBtn) topDraftBtn.style.display = (tab === 'new') ? '' : 'none';
}

// ═══════════════════════════════════════════════════════════════════════════
// WORKFLOW — NEW SUBMISSION (UI only — no backend wiring yet)
// Steps: Upload/Scan → Fill Metadata → Preview → Select Approver → Send
// ═══════════════════════════════════════════════════════════════════════════

let wfNewStep = 1;

function wfSetStep(n) {
    wfNewStep = n;
    document.querySelectorAll('#wfNewPane .step').forEach(el => {
        const stepNum = parseInt(el.id.replace('wfStep', '').replace('-ind', ''), 10);
        el.classList.remove('active', 'done');
        if (stepNum < n) el.classList.add('done');
        else if (stepNum === n) el.classList.add('active');
    });
}

function wfOpenPreview() {
    const modal = document.getElementById('wfPreviewModal');
    if (modal) modal.style.display = 'flex';
    wfSetStep(3);
}

function wfClosePreview() {
    const modal = document.getElementById('wfPreviewModal');
    if (modal) modal.style.display = 'none';
}

// Cache of selectable users for the "Send To" typeahead, loaded once per
// New Request visit (see _wfLoadUsersForApprover()).
let _wfApproverUsers = [];
let _wfApproverUsersLoaded = false;

async function _wfLoadUsersForApprover() {
    if (_wfApproverUsersLoaded) return;
    try {
        const res = await fetch('/api/users/list-all');
        const data = await res.json();
        if (!res.ok) throw new Error(data.error || 'Failed to load users');
        const selfId = document.body.dataset.userId || '';
        _wfApproverUsers = (data.users || [])
            .filter(u => String(u.user_id) !== String(selfId))
            .map(u => ({
                id: u.user_id,
                label: u.full_name || u.username || u.email || `User #${u.user_id}`,
            }));
        _wfApproverUsersLoaded = true;
    } catch (e) {
        _wfApproverUsers = [];
    }
    wfRenderUserDropdown('');
}

function wfRenderUserDropdown(query) {
    const dd = document.getElementById('wfUserDropdown');
    if (!dd) return;
    const q = query.trim().toLowerCase();
    const matches = q
        ? _wfApproverUsers.filter(u => u.label.toLowerCase().includes(q))
        : _wfApproverUsers;
    if (!matches.length) {
        dd.innerHTML = `<div class="volume-option" style="cursor:default;opacity:.6">${
            _wfApproverUsersLoaded
                ? (currentLang === 'ar' ? 'لا يوجد مستخدم مطابق' : 'No matching user')
                : (currentLang === 'ar' ? 'جارٍ تحميل المستخدمين…' : 'Loading users…')
        }</div>`;
        return;
    }
    dd.innerHTML = matches.map(u => {
        const hl = q
            ? escapeHtml(u.label).replace(new RegExp(`(${q})`, 'gi'), '<mark>$1</mark>')
            : escapeHtml(u.label);
        return `<div class="volume-option" onmousedown="wfSelectUser('${escapeHtml(u.label).replace(/'/g, "\\'")}', ${u.id})">${hl}</div>`;
    }).join('');
}

function wfFilterUsers() {
    const q = document.getElementById('wfUserInput')?.value || '';
    wfRenderUserDropdown(q);
    wfShowUserDropdown();
}

function wfShowUserDropdown() {
    _wfLoadUsersForApprover();
    document.getElementById('wfUserDropdown')?.classList.add('open');
}

function wfHideUserDropdown() {
    document.getElementById('wfUserDropdown')?.classList.remove('open');
}

// Multi-select "Send To" state — an array of {id, name} the user has
// picked, rendered as removable chips below the typeahead input.
let _wfSelectedUsers = [];

function wfSelectUser(name, id) {
    id = Number(id);
    if (!_wfSelectedUsers.some(u => u.id === id)) {
        _wfSelectedUsers.push({ id, name });
    }
    const input = document.getElementById('wfUserInput');
    if (input) input.value = '';
    wfRenderUserChips();
    wfHideUserDropdown();
    wfSetStep(4);
}

function wfRemoveUser(id) {
    id = Number(id);
    _wfSelectedUsers = _wfSelectedUsers.filter(u => u.id !== id);
    wfRenderUserChips();
}

function wfRenderUserChips() {
    const box = document.getElementById('wfUserChips');
    if (!box) return;
    box.innerHTML = _wfSelectedUsers.map(u => `
        <span class="profile-badge" style="display:inline-flex;align-items:center;gap:6px;background:var(--surface3,#e5e7eb);color:var(--text)">
            ${escapeHtml(u.name)}
            <i class="ph ph-x" style="cursor:pointer" onclick="wfRemoveUser(${u.id})"></i>
        </span>
    `).join('');
}

function _wfClearSelectedUsers() {
    _wfSelectedUsers = [];
    wfRenderUserChips();
}

// ── WORKFLOW — Entity / Department + Volume (Folder) picker ────────────────
// Mirrors the Archive page's entitySelect/volume-typeahead pattern, reusing
// the already-loaded allEntities / allFoldersByDept globals (no extra API
// calls needed).
let wfCurrentVolumes = [];

function wfPopulateEntitySelect() {
    const sel = document.getElementById('wfEntitySelect');
    if (!sel) return;
    const accessibleEntities = (allEntities || []).filter(e => _canAccessDept(e.id));
    sel.innerHTML = accessibleEntities.map(e =>
        `<option value="${e.id}">${escapeHtml(e.name)}</option>`
    ).join('');
    wfUpdateVolumeOptions();
}

function wfUpdateVolumeOptions() {
    const sel = document.getElementById('wfEntitySelect');
    if (!sel) return;
    const entityId = sel.value;
    const entity = (allEntities || []).find(e => String(e.id) === String(entityId));
    const realDeptId = entity ? entity.dept_id : entityId;
    const allFolders = allFoldersByDept[entityId] || [];

    function buildPath(folder) {
        const parts = [folder.name];
        let current = folder;
        const _visited = new Set([current.id]);
        while (current.parent_id) {
            if (_visited.has(current.parent_id)) break; // cyclic parent chain — stop instead of hanging
            const parent = allFolders.find(f => f.id === current.parent_id);
            if (!parent) break;
            parts.unshift(parent.name);
            current = parent;
            _visited.add(current.id);
        }
        return parts;
    }

    wfCurrentVolumes = allFolders.map(f => {
        const pathParts = buildPath(f);
        return {
            id: f.id,
            name: f.name,
            path: pathParts.slice(0, -1).join(' › '),
            fullPath: pathParts.join(' › '),
            dept_id: realDeptId,
        };
    });

    const input = document.getElementById('wfVolumeInput');
    if (input) {
        input.value = '';
        delete input.dataset.folderId;
        delete input.dataset.folderDeptId;
        input.placeholder = wfCurrentVolumes.length ? 'Type to search folder...' : 'No subfolders available';
        input.disabled = wfCurrentVolumes.length === 0;
    }
    wfRenderVolumeDropdown('');
}

function wfFilterVolume() {
    const q = document.getElementById('wfVolumeInput')?.value || '';
    wfRenderVolumeDropdown(q);
    wfShowVolumeDropdown();
}

function wfRenderVolumeDropdown(query) {
    const dd = document.getElementById('wfVolumeDropdown');
    if (!dd) return;
    const q = query.trim().toLowerCase();
    const matches = q
        ? wfCurrentVolumes.filter(v => v.fullPath.toLowerCase().includes(q))
        : wfCurrentVolumes;
    if (!matches.length) { dd.innerHTML = ''; dd.classList.remove('open'); return; }

    const sorted = [...matches].sort((a, b) => a.fullPath.localeCompare(b.fullPath));
    dd.innerHTML = sorted.map(v => {
        const depth = (v.fullPath.match(/›/g) || []).length;
        const indent = 12 + depth * 16;
        const hlName = q
            ? v.name.replace(new RegExp(`(${q})`, 'gi'), '<mark>$1</mark>')
            : v.name;
        const icon = depth === 0
            ? '<i class="ph ph-folder" style="margin-inline-end:6px;color:var(--accent)"></i>'
            : '<i class="ph ph-folder-open" style="margin-inline-end:6px;opacity:0.6"></i>';
        return `<div class="volume-option" style="padding-inline-start:${indent}px"
            onmousedown="wfSelectVolume('${v.fullPath.replace(/'/g, "\\'")}', ${v.id}, ${v.dept_id || 0})">
            ${icon}<span class="vol-opt-name">${hlName}</span>
        </div>`;
    }).join('');
}

function wfSelectVolume(name, id, deptId) {
    const input = document.getElementById('wfVolumeInput');
    if (input) {
        input.value = name || '';
        if (id) input.dataset.folderId = id;
        if (deptId) input.dataset.folderDeptId = deptId;
    }
    wfHideVolumeDropdown();
}

function wfShowVolumeDropdown() {
    const dd = document.getElementById('wfVolumeDropdown');
    if (dd && wfCurrentVolumes.length) dd.classList.add('open');
}

function wfHideVolumeDropdown() {
    document.getElementById('wfVolumeDropdown')?.classList.remove('open');
}

// InstanceID of the draft currently loaded into the New Request form, if
// any — set by wfContinueDraft(), cleared once sent or on a fresh form.
let _wfEditingDraftId = null;

// Files picked/scanned in the wizard but not yet uploaded — sent as
// multipart form data alongside the metadata on Send/Save Draft. Cleared
// on reset. See _wfSubmitFormData() for how these get posted.
let _wfPendingFiles = [];

function wfHandleFilePick(event) {
    const files = Array.from(event.target.files || []);
    _wfPendingFiles.push(...files);
    event.target.value = ''; // allow picking the same file again
    _wfRenderPendingFiles();
    if (_wfPendingFiles.length) {
        document.getElementById('wfAttachMainBox')?.classList.add('has-file');
        wfSetStep(2);
    }
}

function wfRemovePendingFile(idx) {
    _wfPendingFiles.splice(idx, 1);
    _wfRenderPendingFiles();
    if (!_wfPendingFiles.length) {
        document.getElementById('wfAttachMainBox')?.classList.remove('has-file');
    }
}

function _wfFormatFileSize(bytes) {
    if (!bytes && bytes !== 0) return '';
    if (bytes < 1024) return `${bytes} B`;
    if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
    return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
}

function _wfRenderPendingFiles() {
    const list = document.getElementById('wfPendingFilesList');
    if (!list) return;
    if (!_wfPendingFiles.length) {
        list.innerHTML = '';
        return;
    }
    list.innerHTML = _wfPendingFiles.map((f, idx) => `
        <div class="file-item" id="wf-file-item-${idx}">
            <span class="file-item-icon">${getFileIcon(f.name)}</span>
            <span class="file-item-name file-item-name--clickable" onclick="previewPendingFile(${idx}, _wfPendingFiles)" title="${currentLang === 'ar' ? 'معاينة' : 'Preview'}">${escapeHtml(f.name)}</span>
            <span class="file-item-size">${_wfFormatFileSize(f.size)}</span>
            <button type="button" class="file-item-preview" onclick="previewPendingFile(${idx}, _wfPendingFiles)" title="${currentLang === 'ar' ? 'معاينة' : 'Preview'}"><i class="ph ph-eye"></i></button>
            <button type="button" class="file-item-remove" onclick="wfRemovePendingFile(${idx})" title="${currentLang === 'ar' ? 'إزالة' : 'Remove'}">✕</button>
        </div>
    `).join('');
}

function _wfGatherNewRequestFields() {
    const topicInput = document.getElementById('wfTopicInput');
    const volumeInput = document.getElementById('wfVolumeInput');
    const entitySelect = document.getElementById('wfEntitySelect');
    return {
        topic: topicInput?.value || '',
        assignedUserIds: _wfSelectedUsers.map(u => u.id),
        doc_date: document.getElementById('wfDocDate')?.value || '',
        keywords: document.getElementById('wfKeywordsInput')?.value || '',
        importance: document.getElementById('wfImportanceSelect')?.value || '',
        statement: document.getElementById('wfStatementInput')?.value || '',
        deptId: entitySelect?.value || '',
        folderId: volumeInput?.dataset.folderId || '',
        expiryDate: document.getElementById('wfExpiryDate')?.value || '',
        linkedAttachmentIds: _wfLinkedAttachments.map(a => a.id),
    };
}

function _wfResetNewRequestForm() {
    _wfEditingDraftId = null;
    _wfPendingFiles = [];
    _wfRenderPendingFiles();
    document.getElementById('wfAttachMainBox')?.classList.remove('has-file');
    const topicInput = document.getElementById('wfTopicInput');
    const userInput = document.getElementById('wfUserInput');
    if (topicInput) topicInput.value = '';
    if (userInput) { userInput.value = ''; delete userInput.dataset.userId; }
    _wfClearSelectedUsers();
    ['wfDocDate', 'wfKeywordsInput', 'wfStatementInput', 'wfExpiryDate'].forEach(id => {
        const el = document.getElementById(id);
        if (el) el.value = '';
    });
    const exportImportSelect = document.getElementById('wfExportImportSelect');
    if (exportImportSelect) exportImportSelect.value = '';
    _wfLinkedAttachments = [];
    _wfRenderLinkedAttachChips();
    wfPopulateEntitySelect();
    wfSetStep(1);
}

// ── Export / Import -> Link: lets a user link an already-approved
//    attachment onto this new request instead of (re-)uploading it. ───────
let _wfLinkedAttachments = []; // {id, file_name, transaction_id, subject}

function _wfRenderLinkedAttachChips() {
    const box = document.getElementById('wfLinkedAttachChips');
    if (!box) return;
    if (!_wfLinkedAttachments.length) { box.innerHTML = ''; return; }
    box.innerHTML = _wfLinkedAttachments.map((a, idx) => `
        <span class="chip" style="display:inline-flex;align-items:center;gap:6px;background:var(--bg-subtle,#f1f5f9);border:1px solid var(--border);border-radius:20px;padding:4px 10px;font-size:.8rem">
            <i class="ph ph-link-simple"></i>
            ${escapeHtml(a.file_name)}
            <span style="color:var(--text-muted)">(#${escapeHtml(String(a.transaction_id))})</span>
            <button type="button" onclick="wfRemoveLinkedAttachment(${idx})" style="border:none;background:none;cursor:pointer;color:var(--text-muted)" title="${currentLang === 'ar' ? 'إزالة' : 'Remove'}">✕</button>
        </span>
    `).join('');
}

function wfRemoveLinkedAttachment(idx) {
    _wfLinkedAttachments.splice(idx, 1);
    _wfRenderLinkedAttachChips();
}

function wfAddLinkedAttachment(att, tx) {
    if (_wfLinkedAttachments.some(a => a.id === att.id)) {
        showToast(currentLang === 'ar' ? 'هذا المرفق مضاف بالفعل' : 'That attachment is already added', 'info');
        return;
    }
    _wfLinkedAttachments.push({
        id: att.id,
        file_name: att.file_name,
        transaction_id: tx.id,
        subject: tx.subject,
    });
    _wfRenderLinkedAttachChips();
    wfCloseLinkPicker();
    showToast(currentLang === 'ar' ? 'تم ربط المرفق' : 'Attachment linked', 'success');
}

// Looked up by wfAddLinkedAttachmentById() below — keyed by transaction id
// so the "Link" button never has to embed JSON (with the document's own
// subject/filename, which may contain quotes) directly into an onclick
// attribute, which was silently breaking the button.
let _wfLinkPickerResultsById = {};

function wfAddLinkedAttachmentById(txId, attId) {
    const tx = _wfLinkPickerResultsById[txId];
    const att = tx && (tx.attachments || []).find(a => a.id === attId);
    if (!tx || !att) {
        showToast(currentLang === 'ar' ? 'تعذر العثور على المرفق' : 'Could not find that attachment', 'error');
        return;
    }
    wfAddLinkedAttachment(att, tx);
}

function wfOpenLinkPicker() {
    const modal = document.getElementById('wfLinkPickerModal');
    if (!modal) return;
    modal.style.display = 'flex';
    const input = document.getElementById('wfLinkPickerSearch');
    if (input) { input.value = ''; setTimeout(() => input.focus(), 50); }
    // Load every approved/archived document with attachments right away —
    // the person can still narrow it down by typing.
    _wfRunLinkPickerSearch();
}

function wfCloseLinkPicker() {
    const modal = document.getElementById('wfLinkPickerModal');
    if (modal) modal.style.display = 'none';
}

let _wfLinkPickerDebounce = null;
function wfLinkPickerSearchDebounced() {
    clearTimeout(_wfLinkPickerDebounce);
    _wfLinkPickerDebounce = setTimeout(_wfRunLinkPickerSearch, 350);
}

async function _wfRunLinkPickerSearch() {
    const q = document.getElementById('wfLinkPickerSearch')?.value.trim() || '';
    const resultsBox = document.getElementById('wfLinkPickerResults');
    if (!resultsBox) return;
    resultsBox.innerHTML = `<p style="color:var(--text-muted);font-size:.85rem;text-align:center;padding:1.5rem 0">
        <i class="ph ph-spinner" style="animation:spin 0.8s linear infinite"></i> ${currentLang === 'ar' ? 'جارٍ البحث…' : 'Searching…'}</p>`;
    try {
        const res = await fetch(`/api/documents/search?q=${encodeURIComponent(q)}&page_size=25`);
        const data = await res.json();
        if (!res.ok) {
            resultsBox.innerHTML = `<p style="color:#dc2626;font-size:.85rem;text-align:center;padding:1.5rem 0">${escapeHtml(data.error || 'Search failed')}</p>`;
            return;
        }
        const results = (data.results || []).filter(r => (r.attachments || []).length);
        _wfLinkPickerResultsById = {};
        results.forEach(tx => { _wfLinkPickerResultsById[tx.id] = tx; });
        if (!results.length) {
            resultsBox.innerHTML = `<p style="color:var(--text-muted);font-size:.85rem;text-align:center;padding:1.5rem 0">
                ${currentLang === 'ar' ? 'لا توجد نتائج بمرفقات.' : 'No results with attachments found.'}</p>`;
            return;
        }
        resultsBox.innerHTML = results.map(tx => `
            <div style="border:1px solid var(--border);border-radius:10px;padding:.6rem .8rem;margin-bottom:.6rem">
                <div style="font-weight:600;font-size:.88rem;display:flex;justify-content:space-between;gap:8px">
                    <span>${escapeHtml(tx.subject || '(no subject)')}</span>
                    <span style="color:var(--text-muted);font-weight:400">#${escapeHtml(String(tx.id))}</span>
                </div>
                <div style="display:flex;flex-direction:column;gap:4px;margin-top:.5rem">
                    ${(tx.attachments || []).map(att => `
                        <div style="display:flex;align-items:center;justify-content:space-between;gap:8px;background:var(--bg-subtle,#f8fafc);border-radius:6px;padding:.35rem .6rem">
                            <span style="display:flex;align-items:center;gap:6px;font-size:.82rem;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">
                                <i class="ph ph-file"></i>${escapeHtml(att.file_name)}
                            </span>
                            <button type="button" class="btn-ghost btn-xs"
                                style="border-color:var(--blue-light,#3b82f6);color:var(--blue-dark,#1e6fc4);flex-shrink:0"
                                onclick="wfAddLinkedAttachmentById(${JSON.stringify(tx.id)}, ${JSON.stringify(att.id)})">
                                <i class="ph ph-link-simple"></i> ${currentLang === 'ar' ? 'ربط' : 'Link'}
                            </button>
                        </div>
                    `).join('')}
                </div>
            </div>
        `).join('');
    } catch (e) {
        resultsBox.innerHTML = `<p style="color:#dc2626;font-size:.85rem;text-align:center;padding:1.5rem 0">${currentLang === 'ar' ? 'خطأ في الاتصال' : 'Connection error'}</p>`;
    }
}

// Wraps fetch with a hard timeout so a hung/unresponsive server shows a
// clear error instead of the button silently doing nothing forever.
async function _wfFetchWithTimeout(url, options, timeoutMs = 20000) {
    const controller = new AbortController();
    const t = setTimeout(() => controller.abort(), timeoutMs);
    try {
        return await fetch(url, { ...options, signal: controller.signal });
    } catch (e) {
        if (e.name === 'AbortError') {
            throw new Error(currentLang === 'ar'
                ? 'انتهت مهلة الاتصال بالخادم — تحقق من اتصال الشبكة أو حالة الخادم'
                : 'Server took too long to respond — check your connection or server status');
        }
        throw e;
    } finally {
        clearTimeout(t);
    }
}

// Posts the wizard payload to `url`. If any files are pending, sends
// multipart/form-data (fields + files[]) so the backend can save them
// against the instance in the same request; otherwise falls back to the
// plain JSON body every existing caller already used.
async function _wfPostWithAttachments(url, payload) {
    if (_wfPendingFiles.length === 0) {
        return _wfFetchWithTimeout(url, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(payload),
        });
    }
    const fd = new FormData();
    Object.entries(payload).forEach(([k, v]) => fd.append(k, v == null ? '' : v));
    _wfPendingFiles.forEach(f => fd.append('files', f));
    return _wfFetchWithTimeout(url, { method: 'POST', body: fd }, 60000); // files need more time
}

async function wfSendForApproval() {
    const f = _wfGatherNewRequestFields();

    if (!f.topic) {
        showToast(currentLang === 'ar' ? 'أدخل موضوع المستند' : 'Enter a document topic', 'error');
        return;
    }
    if (!f.deptId) {
        showToast(currentLang === 'ar' ? 'اختر الجهة / الإدارة' : 'Select an Entity / Department', 'error');
        return;
    }
    if (!f.assignedUserIds || !f.assignedUserIds.length) {
        showToast(currentLang === 'ar' ? 'اختر مستخدماً واحداً على الأقل لإرسال الموافقة إليه' : 'Select at least one user to send for approval', 'error');
        return;
    }

    const payload = {
        topic: f.topic,
        assigned_user_ids: f.assignedUserIds.join(','),
        doc_date: f.doc_date,
        keywords: f.keywords,
        importance: f.importance,
        statement: f.statement,
        dept_id: f.deptId,
        folder_id: f.folderId,
        expiry_date: f.expiryDate,
        linked_attachment_ids: JSON.stringify(f.linkedAttachmentIds || []),
    };

    // Visible loading state — without this, a slow/hung server request
    // looks identical to "the button does nothing" from the user's side.
    // Two buttons trigger this (the bottom action-bar one and the
    // persistent top-right one) — keep both in sync.
    const btns = [document.getElementById('wfSendBtn'), document.getElementById('wfTopSendBtn')].filter(Boolean);
    const btnOriginalHtml = new Map(btns.map(b => [b, b.innerHTML]));
    btns.forEach(b => {
        b.disabled = true;
        b.innerHTML = `<i class="ph ph-spinner" style="animation:spin 0.8s linear infinite"></i> <span>${currentLang === 'ar' ? 'جارٍ الإرسال…' : 'Sending…'}</span>`;
    });

    try {
        // If this form was opened from a saved draft, promote that draft
        // instead of creating a brand-new submission. Any newly-picked
        // files need to land via the update endpoint first (send-draft
        // itself doesn't accept files, since drafts already stage theirs
        // via save-draft/update) — then send.
        if (_wfEditingDraftId) {
            if (_wfPendingFiles.length > 0) {
                const updateRes = await _wfPostWithAttachments(
                    `/api/workflow/drafts/${_wfEditingDraftId}/update`, payload);
                const updateData = await updateRes.json();
                if (!updateRes.ok || !updateData.success) {
                    showToast(updateData.error || (currentLang === 'ar' ? 'فشل حفظ المرفقات' : 'Failed to save attachments'), 'error');
                    return;
                }
                _wfPendingFiles = [];
            }
        }
        const url = _wfEditingDraftId
            ? `/api/workflow/drafts/${_wfEditingDraftId}/send`
            : '/api/workflow/submit';
        const res = _wfEditingDraftId
            ? await _wfFetchWithTimeout(url, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(payload) })
            : await _wfPostWithAttachments(url, payload);
        const data = await res.json();
        if (!res.ok || !data.success) {
            showToast(data.error || (currentLang === 'ar' ? 'فشل إرسال المستند للموافقة' : 'Failed to send for approval'), 'error');
            return;
        }
        showToast(currentLang === 'ar' ? 'تم إرسال المستند للموافقة' : 'Document sent for approval', 'success');
        if (data.warning) console.warn('[Workflow]', data.warning);
        _wfResetNewRequestForm();
        wfLoadSent();
    } catch (e) {
        console.error('[Workflow] send for approval failed', e);
        showToast(e && e.message ? e.message : (currentLang === 'ar' ? 'خطأ في الاتصال' : 'Connection error'), 'error');
    } finally {
        btns.forEach(b => {
            b.disabled = false;
            b.innerHTML = btnOriginalHtml.get(b);
        });
    }
}

// ── Save Draft — persists the form without notifying anyone; the
//    recipient is optional at this point. ────────────────────────────────
async function wfSaveDraft() {
    const f = _wfGatherNewRequestFields();
    if (!f.topic) {
        showToast(currentLang === 'ar' ? 'أدخل موضوع المستند' : 'Enter a document topic', 'error');
        return;
    }

    const payload = {
        topic: f.topic,
        assigned_user_ids: (f.assignedUserIds || []).join(','),
        doc_date: f.doc_date,
        keywords: f.keywords,
        importance: f.importance,
        statement: f.statement,
        dept_id: f.deptId,
        folder_id: f.folderId,
        expiry_date: f.expiryDate,
        linked_attachment_ids: JSON.stringify(f.linkedAttachmentIds || []),
    };

    try {
        const url = _wfEditingDraftId
            ? `/api/workflow/drafts/${_wfEditingDraftId}/update`
            : '/api/workflow/save-draft';
        const res = await _wfPostWithAttachments(url, payload);
        const data = await res.json();
        if (!res.ok || !data.success) {
            showToast(data.error || (currentLang === 'ar' ? 'فشل حفظ المسودة' : 'Failed to save draft'), 'error');
            return;
        }
        _wfEditingDraftId = data.instance_id;
        _wfPendingFiles = [];
        _wfRenderPendingFiles();
        // Already persisted server-side against this instance — clear so a
        // later save doesn't re-send (and re-insert) the same links.
        _wfLinkedAttachments = [];
        _wfRenderLinkedAttachChips();
        showToast(currentLang === 'ar' ? 'تم حفظ المسودة' : 'Draft saved', 'success');
        wfLoadSent();
    } catch (e) {
        console.error('[Workflow] save draft failed', e);
        showToast(currentLang === 'ar' ? 'خطأ في الاتصال' : 'Connection error', 'error');
    }
}

// ── Continue editing a saved draft from the Sent tab ─────────────────────
async function wfContinueDraft(instanceId) {
    try {
        const res = await fetch('/api/workflow/drafts');
        const data = await res.json();
        const draft = (data.items || []).find(d => d.instance_id === instanceId);
        if (!draft) {
            showToast(currentLang === 'ar' ? 'تعذر العثور على المسودة' : 'Draft not found', 'error');
            return;
        }
        _wfEditingDraftId = instanceId;
        switchWfTab('new');
        document.getElementById('wfTopicInput').value = draft.subject || '';
        document.getElementById('wfKeywordsInput').value = draft.keywords || '';
        document.getElementById('wfStatementInput').value = draft.statement || '';
        document.getElementById('wfDocDate').value = draft.doc_date || '';
        document.getElementById('wfExpiryDate').value = draft.expiry_date || '';
        if (draft.importance_id) document.getElementById('wfImportanceSelect').value = String(draft.importance_id);
        _wfClearSelectedUsers();
        if (draft.assigned_user_id) {
            _wfSelectedUsers.push({ id: Number(draft.assigned_user_id), name: draft.assigned_user_name || `User #${draft.assigned_user_id}` });
            wfRenderUserChips();
        }
        // Repopulate Entity/Department + Volume (Folder), same lookup path
        // used by wfUpdateVolumeOptions/wfSelectVolume.
        const entitySel = document.getElementById('wfEntitySelect');
        if (draft.dept_id && entitySel) {
            entitySel.value = String(draft.dept_id);
            wfUpdateVolumeOptions();
            if (draft.folder_id) {
                const folder = (allFoldersByDept[draft.dept_id] || []).find(f => f.id === draft.folder_id);
                if (folder) {
                    const match = wfCurrentVolumes.find(v => v.id === draft.folder_id);
                    if (match) wfSelectVolume(match.fullPath, match.id, match.dept_id);
                }
            }
        }
        wfSetStep(draft.assigned_user_id ? 4 : (draft.subject ? 2 : 1));
    } catch (e) {
        console.error('[Workflow] continue draft failed', e);
        showToast(currentLang === 'ar' ? 'خطأ في الاتصال' : 'Connection error', 'error');
    }
}

// ── Send a draft straight from the Sent tab without reopening the form ───
async function wfSendDraftNow(instanceId) {
    try {
        const res = await fetch(`/api/workflow/drafts/${instanceId}/send`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({}),
        });
        const data = await res.json();
        if (!data.success) {
            showToast(data.error || (currentLang === 'ar' ? 'فشل الإرسال' : 'Send failed'), 'error');
            return;
        }
        showToast(currentLang === 'ar' ? 'تم إرسال المستند للموافقة' : 'Document sent for approval', 'success');
        wfLoadSent();
    } catch (e) {
        showToast(currentLang === 'ar' ? 'خطأ في الاتصال' : 'Connection error', 'error');
    }
}

async function wfDeleteDraft(instanceId) {
    if (!confirm(currentLang === 'ar' ? 'هل تريد حذف هذه المسودة؟' : 'Delete this draft?')) return;
    try {
        const res = await fetch(`/api/workflow/drafts/${instanceId}`, { method: 'DELETE' });
        const data = await res.json();
        if (!data.success) {
            showToast(data.error || (currentLang === 'ar' ? 'فشل الحذف' : 'Delete failed'), 'error');
            return;
        }
        wfLoadSent();
    } catch (e) {
        showToast(currentLang === 'ar' ? 'خطأ في الاتصال' : 'Connection error', 'error');
    }
}

// ── Remove any item from the Sent list (Pending / Approved / Rejected /
//    etc.) — a soft delete that only hides it from this user's Sent view.
//    It does not touch the underlying archived document or registration. ──
async function wfDeleteSentItem(instanceId) {
    if (!confirm(currentLang === 'ar'
        ? 'إزالة هذا العنصر من قائمة المرسل؟ لن يؤثر هذا على المستند المؤرشف.'
        : 'Remove this item from your Sent list? This will not affect the archived document.')) return;
    try {
        const res = await fetch(`/api/workflow/sent/${instanceId}`, { method: 'DELETE' });
        const data = await res.json();
        if (!data.success) {
            showToast(data.error || (currentLang === 'ar' ? 'فشل الحذف' : 'Delete failed'), 'error');
            return;
        }
        showToast(currentLang === 'ar' ? 'تمت الإزالة' : 'Removed', 'success');
        wfLoadSent();
    } catch (e) {
        showToast(currentLang === 'ar' ? 'خطأ في الاتصال' : 'Connection error', 'error');
    }
}

async function wfLoadInbox() {
    const tbody = document.getElementById('wfInboxBody');
    tbody.innerHTML = '';
    try {
        const res = await fetch('/api/workflow/inbox');
        const data = await res.json();
        if (!data.success) return;
        (data.items || []).forEach(item => {
            const tr = document.createElement('tr');
            tr.style.cursor = 'pointer';
            tr.title = currentLang === 'ar' ? 'اضغط لعرض المرفقات واتخاذ إجراء' : 'Click to review attachments and take action';
            tr.onclick = () => wfOpenAttachments(item.instance_id, true);
            tr.innerHTML = `
                <td>${escapeHtml(item.subject || '')}</td>
                <td>${escapeHtml(item.submitted_by_name || '')}</td>
                <td>${escapeHtml(item.step_name || '')}</td>
                <td>#${item.submission_number}</td>
                <td>
                    <button class="btn-ghost" onclick="event.stopPropagation(); wfOpenAttachments(${item.instance_id}, true)" data-en="Attachments" data-ar="المرفقات">Attachments</button>
                </td>`;
            tbody.appendChild(tr);
        });
        if (typeof applyLangToNewNodes === 'function') applyLangToNewNodes(tbody);
    } catch (e) {
        console.error('[Workflow] inbox load failed', e);
    }
}

function wfFilterSentRows() {
    const q = (document.getElementById('wfSentSearch')?.value || '').trim().toLowerCase();
    document.querySelectorAll('#wfSentBody tr').forEach(tr => {
        const subject = (tr.children[0]?.textContent || '').toLowerCase();
        tr.style.display = (!q || subject.includes(q)) ? '' : 'none';
    });
}

function wfClearSentFilters() {
    const input = document.getElementById('wfSentSearch');
    if (input) input.value = '';
    wfFilterSentRows();
}

async function wfLoadSent() {
    const tbody = document.getElementById('wfSentBody');
    tbody.innerHTML = '';
    try {
        const res = await fetch('/api/workflow/sent');
        const data = await res.json();
        if (!data.success) return;
        (data.items || []).forEach(item => {
            const canResubmit = item.status === 'Rejected';
            const isDraft = item.status === 'Draft';
            const deleteBtn = `<button class="btn-ghost" style="color:#dc2626" onclick="wfDeleteSentItem(${item.instance_id})" data-en="Delete" data-ar="حذف">Delete</button>`;
            let actionsHtml;
            if (isDraft) {
                actionsHtml = `
                    <button class="btn-ghost" onclick="wfOpenAttachments(${item.instance_id})" data-en="Attachments" data-ar="المرفقات">Attachments</button>
                    <button class="btn-ghost" onclick="wfContinueDraft(${item.instance_id})" data-en="Continue Editing" data-ar="متابعة التحرير">Continue Editing</button>
                    <button class="btn-primary" onclick="wfSendDraftNow(${item.instance_id})" data-en="Send" data-ar="إرسال">Send</button>
                    ${deleteBtn}`;
            } else if (canResubmit) {
                actionsHtml = `
                    <button class="btn-ghost" onclick="wfOpenAttachments(${item.instance_id})" data-en="Attachments" data-ar="المرفقات">Attachments</button>
                    <button class="btn-primary" onclick="openWfResubmitModal(${item.instance_id})" data-en="Resubmit" data-ar="إعادة الإرسال">Resubmit</button>
                    ${deleteBtn}`;
            } else {
                actionsHtml = `
                    <button class="btn-ghost" onclick="wfOpenAttachments(${item.instance_id})" data-en="Attachments" data-ar="المرفقات">Attachments</button>
                    <button class="btn-ghost" onclick="wfViewTimeline(${item.instance_id})" data-en="View Timeline" data-ar="عرض السجل الزمني">View Timeline</button>
                    ${deleteBtn}`;
            }
            const tr = document.createElement('tr');
            tr.innerHTML = `
                <td>${escapeHtml(item.subject || '')}</td>
                <td>${_wfStatusBadgeWithViewedHint(item.status, item.viewed_on)}</td>
                <td>${isDraft ? '—' : '#' + item.submission_number}</td>
                <td>${actionsHtml}</td>`;
            tbody.appendChild(tr);
        });
        if (typeof applyLangToNewNodes === 'function') applyLangToNewNodes(tbody);
        wfFilterSentRows();
    } catch (e) {
        console.error('[Workflow] sent load failed', e);
    }
}

// ── Attachments viewer (Inbox/Sent/History/Review) — fetches the current
//    list for an instance (staged pre-approval, or archived post-approval;
//    the backend picks the right table transparently) and renders
//    preview/download links pointed at the same endpoint either way. ──────
async function wfOpenAttachments(instanceId, reviewActions) {
    const modal = document.getElementById('wfAttachmentsModal');
    const list = document.getElementById('wfAttachmentsList');
    const actionsBox = document.getElementById('wfAttachmentsReviewActions');
    if (!modal || !list) return;
    list.innerHTML = `<div style="color:var(--sidebar-muted,#94a3b8)">${currentLang === 'ar' ? 'جارٍ التحميل…' : 'Loading…'}</div>`;
    resetWfPreviewPane();
    modal.style.display = 'grid';

    // Inbox review — show Approve / Reject / Forward right next to the
    // attachments, so the reviewer doesn't have to close this and hunt
    // for a separate button to act on what they just looked at.
    if (actionsBox) {
        if (reviewActions) {
            actionsBox.style.display = 'flex';
            actionsBox.innerHTML = `
                <button type="button" class="btn-primary btn-sm" onclick="closeWfAttachmentsModal(); wfApprove(${instanceId})">
                    <i class="ph ph-check"></i> ${currentLang === 'ar' ? 'موافقة' : 'Approve'}
                </button>
                <button type="button" class="btn-ghost btn-sm" onclick="closeWfAttachmentsModal(); openWfApproveSendModal(${instanceId})">
                    <i class="ph ph-paper-plane-tilt"></i> ${currentLang === 'ar' ? 'موافقة وإرسال' : 'Approve and Send'}
                </button>
                <button type="button" class="btn-ghost btn-sm" style="color:#dc2626" onclick="closeWfAttachmentsModal(); openWfRejectModal(${instanceId})">
                    <i class="ph ph-x"></i> ${currentLang === 'ar' ? 'رفض' : 'Reject'}
                </button>
                <button type="button" class="btn-ghost btn-sm" onclick="closeWfAttachmentsModal(); openWfForwardModal(${instanceId})">
                    <i class="ph ph-arrow-bend-up-right"></i> ${currentLang === 'ar' ? 'إحالة' : 'Forward'}
                </button>`;
        } else {
            actionsBox.style.display = 'none';
            actionsBox.innerHTML = '';
        }
    }

    try {
        const res = await fetch(`/api/workflow/instances/${instanceId}/attachments`);
        const data = await res.json();
        const items = data.attachments || [];
        if (!items.length) {
            list.innerHTML = `<div style="color:var(--sidebar-muted,#94a3b8)">${currentLang === 'ar' ? 'لا توجد مرفقات' : 'No attachments'}</div>`;
            return;
        }
        list.innerHTML = items.map((a, i) => {
            const previewUrl = `/api/workflow/instances/${instanceId}/attachments/${a.id}/preview`;
            const downloadUrl = `/api/workflow/instances/${instanceId}/attachments/${a.id}/download`;
            const name = a.file_name || `File ${i + 1}`;
            const linkedBadge = a.is_linked
                ? `<span style="display:inline-flex;align-items:center;gap:3px;font-size:10.5px;font-weight:600;color:var(--blue-dark,#1e6fc4);background:var(--blue-glow,#eaf2fb);border-radius:10px;padding:1px 7px;margin-inline-start:4px"><i class="ph ph-link-simple"></i>${currentLang === 'ar' ? 'مرتبط' : 'Linked'}</span>`
                : '';
            return `<div class="view-attach-item view-attach-item--clickable"
                data-preview-url="${escAttr(previewUrl)}"
                data-download-url="${escAttr(downloadUrl)}"
                data-name="${escAttr(name)}"
                onclick="onWfAttachItemClick(this)">
                ${getFileIcon(name)}
                <span class="view-attach-name" title="${escAttr(name)}">${escapeHtml(name)}</span>${linkedBadge}
                <span style="color:var(--sidebar-muted,#94a3b8);font-size:12px;">${_wfFormatFileSize(a.file_size)}</span>
                <button type="button" class="sr-btn sr-btn-view"
                    onclick="event.stopPropagation(); downloadAttachmentFile('${escAttr(downloadUrl)}','${escAttr(name)}')"
                    title="${currentLang === 'ar' ? 'تنزيل' : 'Download'}"><i class="ph ph-download-simple"></i></button>
                ${a.can_sign === false ? '' : `<button type="button" class="sr-btn sr-btn-view"
                    onclick="event.stopPropagation(); openSignatureModal(${a.id}, { onSigned: () => wfOpenAttachments(${instanceId}, ${!!reviewActions}) })"
                    title="${currentLang === 'ar' ? 'توقيع' : 'Sign'}"><i class="ph ph-pen-nib"></i></button>`}
                ${a.is_signed ? `<button type="button" class="sr-btn sr-btn-view"
                    onclick="event.stopPropagation(); removeAttachmentSignature(${a.id}, () => wfOpenAttachments(${instanceId}, ${!!reviewActions}))"
                    title="${currentLang === 'ar' ? 'إزالة التوقيع' : 'Remove signature'}"><i class="ph ph-eraser"></i></button>` : ''}
            </div>`;
        }).join('');

        // Auto-preview the first attachment, same as the archive document viewer.
        const first = items[0];
        if (first) {
            wfPreviewAttachment(
                `/api/workflow/instances/${instanceId}/attachments/${first.id}/preview`,
                first.file_name,
                `/api/workflow/instances/${instanceId}/attachments/${first.id}/download`
            );
        }
    } catch (e) {
        list.innerHTML = `<div style="color:#dc2626">${currentLang === 'ar' ? 'تعذر تحميل المرفقات' : 'Failed to load attachments'}</div>`;
        console.error('[Workflow] attachments load failed', e);
    }
}

function closeWfAttachmentsModal() {
    const modal = document.getElementById('wfAttachmentsModal');
    if (modal) modal.style.display = 'none';
    const actionsBox = document.getElementById('wfAttachmentsReviewActions');
    if (actionsBox) { actionsBox.style.display = 'none'; actionsBox.innerHTML = ''; }
    resetWfPreviewPane();
}

async function wfApprove(instanceId) {
    try {
        const res = await fetch(`/api/workflow/instances/${instanceId}/approve`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({}),
        });
        const data = await res.json();
        if (!data.success) {
            if (data.needs_more_approvers) {
                // Minimum-approvals limit isn't met yet — open the picker so
                // the user can pick who to send it to next.
                openWfApproveSendModal(instanceId, data.error);
                return;
            }
            alert(data.error || 'Approve failed');
            return;
        }
        if (data.status === 'Approved' && data.registration_number) {
            alert(currentLang === 'ar'
                ? `تمت الموافقة والأرشفة. رقم التسجيل: ${data.registration_number}`
                : `Approved and archived. Registration number: ${data.registration_number}`);
        }
        wfLoadInbox();
        if (data.warning) console.warn('[Workflow]', data.warning);
    } catch (e) {
        alert('Approve failed: ' + e.message);
    }
}

// ── Approve and Send — used both as its own button, and as the follow-up
//    picker when a plain Approve fails because the admin's minimum-approvals
//    setting hasn't been met yet. ────────────────────────────────────────────
let _wfApproveSendInstanceId = null;
let _wfApproveSendSelected = [];

function openWfApproveSendModal(instanceId, notice) {
    _wfApproveSendInstanceId = instanceId;
    _wfApproveSendSelected = [];
    const input = document.getElementById('wfApproveSendUserInput');
    if (input) input.value = '';
    document.getElementById('wfApproveSendError').style.display = 'none';
    const noticeEl = document.getElementById('wfApproveSendNotice');
    if (noticeEl) {
        if (notice) { noticeEl.textContent = notice; noticeEl.style.display = 'block'; }
        else { noticeEl.style.display = 'none'; noticeEl.textContent = ''; }
    }
    wfApproveSendRenderChips();
    document.getElementById('wfApproveSendModal').style.display = 'flex';
    _wfLoadUsersForApprover();
}

function closeWfApproveSendModal() {
    document.getElementById('wfApproveSendModal').style.display = 'none';
    _wfApproveSendInstanceId = null;
}

function wfApproveSendFilterUsers() {
    const q = (document.getElementById('wfApproveSendUserInput')?.value || '').trim().toLowerCase();
    const dd = document.getElementById('wfApproveSendUserDropdown');
    if (!dd) return;
    const matches = q ? _wfApproverUsers.filter(u => u.label.toLowerCase().includes(q)) : _wfApproverUsers;
    if (!matches.length) {
        dd.innerHTML = `<div class="volume-option" style="cursor:default;opacity:.6">${currentLang === 'ar' ? 'لا يوجد مستخدم مطابق' : 'No matching user'}</div>`;
    } else {
        dd.innerHTML = matches.map(u => `<div class="volume-option" onmousedown="wfApproveSendSelectUser('${escapeHtml(u.label).replace(/'/g, "\\'")}', ${u.id})">${escapeHtml(u.label)}</div>`).join('');
    }
    dd.classList.add('open');
}

function wfApproveSendShowDropdown() {
    wfApproveSendFilterUsers();
}

function wfApproveSendHideDropdown() {
    document.getElementById('wfApproveSendUserDropdown')?.classList.remove('open');
}

function wfApproveSendSelectUser(name, id) {
    id = Number(id);
    if (!_wfApproveSendSelected.some(u => u.id === id)) {
        _wfApproveSendSelected.push({ id, name });
    }
    const input = document.getElementById('wfApproveSendUserInput');
    if (input) input.value = '';
    wfApproveSendRenderChips();
    wfApproveSendHideDropdown();
}

function wfApproveSendRemoveUser(id) {
    id = Number(id);
    _wfApproveSendSelected = _wfApproveSendSelected.filter(u => u.id !== id);
    wfApproveSendRenderChips();
}

function wfApproveSendRenderChips() {
    const box = document.getElementById('wfApproveSendChips');
    if (!box) return;
    box.innerHTML = _wfApproveSendSelected.map(u => `
        <span class="profile-badge" style="display:inline-flex;align-items:center;gap:6px;background:var(--surface3,#e5e7eb);color:var(--text)">
            ${escapeHtml(u.name)}
            <i class="ph ph-x" style="cursor:pointer" onclick="wfApproveSendRemoveUser(${u.id})"></i>
        </span>
    `).join('');
}

async function confirmWfApproveSend() {
    const errEl = document.getElementById('wfApproveSendError');
    if (!_wfApproveSendSelected.length) {
        errEl.textContent = currentLang === 'ar' ? 'اختر مستخدماً واحداً على الأقل.' : 'Select at least one user.';
        errEl.style.display = 'block';
        return;
    }
    errEl.style.display = 'none';
    try {
        const res = await fetch(`/api/workflow/instances/${_wfApproveSendInstanceId}/approve`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ assigned_user_ids: _wfApproveSendSelected.map(u => u.id) }),
        });
        const data = await res.json();
        if (!data.success) {
            errEl.textContent = data.error || (currentLang === 'ar' ? 'فشلت العملية' : 'Approve and Send failed');
            errEl.style.display = 'block';
            return;
        }
        closeWfApproveSendModal();
        showToast(currentLang === 'ar' ? 'تمت الموافقة وإرسال المستند' : 'Approved and forwarded for further approval', 'success');
        wfLoadInbox();
        if (data.warning) console.warn('[Workflow]', data.warning);
    } catch (e) {
        errEl.textContent = 'Approve and Send failed: ' + e.message;
        errEl.style.display = 'block';
    }
}

function openWfRejectModal(instanceId) {
    _wfCurrentInstanceId = instanceId;
    document.getElementById('wfRejectReason').value = '';
    document.getElementById('wfRejectError').style.display = 'none';
    document.getElementById('wfRejectModal').style.display = 'flex';
}

function closeWfRejectModal() {
    document.getElementById('wfRejectModal').style.display = 'none';
    _wfCurrentInstanceId = null;
}

async function confirmWfReject() {
    const reason = document.getElementById('wfRejectReason').value.trim();
    const errEl = document.getElementById('wfRejectError');
    // Mandatory reason — mirrors the server-side check, so the user gets
    // instant feedback instead of a round-trip for something this obvious.
    if (!reason) {
        errEl.textContent = currentLang === 'ar' ? 'سبب الرفض مطلوب.' : 'A rejection reason is required.';
        errEl.style.display = 'block';
        return;
    }
    try {
        const res = await fetch(`/api/workflow/instances/${_wfCurrentInstanceId}/reject`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ reason }),
        });
        const data = await res.json();
        if (!data.success) {
            errEl.textContent = data.error || 'Rejection failed.';
            errEl.style.display = 'block';
            return;
        }
        closeWfRejectModal();
        wfLoadInbox();
        if (data.warning) console.warn('[Workflow]', data.warning);
    } catch (e) {
        errEl.textContent = 'Rejection failed: ' + e.message;
        errEl.style.display = 'block';
    }
}

async function openWfForwardModal(instanceId) {
    _wfCurrentInstanceId = instanceId;
    document.getElementById('wfForwardNote').value = '';
    document.getElementById('wfForwardError').style.display = 'none';
    const sel = document.getElementById('wfForwardUserSelect');
    sel.innerHTML = `<option value="">${currentLang === 'ar' ? 'جارٍ التحميل…' : 'Loading…'}</option>`;
    document.getElementById('wfForwardModal').style.display = 'flex';
    try {
        const res = await fetch('/api/users/list-emails');
        const data = await res.json();
        if (!res.ok) throw new Error(data.error || 'Failed to load users');
        const selfId = document.body.dataset.userId || '';
        const users = (data.users || []).filter(u => String(u.user_id) !== String(selfId));
        sel.innerHTML = `<option value="">${currentLang === 'ar' ? 'اختر مستخدماً' : 'Select a user'}</option>` +
            users.map(u => `<option value="${u.user_id}">${escapeHtml(u.full_name || u.username || u.email)}</option>`).join('');
    } catch (e) {
        sel.innerHTML = `<option value="">${currentLang === 'ar' ? 'فشل تحميل المستخدمين' : 'Failed to load users'}</option>`;
    }
}

function closeWfForwardModal() {
    document.getElementById('wfForwardModal').style.display = 'none';
    _wfCurrentInstanceId = null;
}

async function confirmWfForward() {
    const targetUserId = document.getElementById('wfForwardUserSelect').value;
    const note = document.getElementById('wfForwardNote').value.trim();
    const errEl = document.getElementById('wfForwardError');
    if (!targetUserId) {
        errEl.textContent = currentLang === 'ar' ? 'الرجاء اختيار مستخدم للإحالة إليه.' : 'Please select a user to forward to.';
        errEl.style.display = 'block';
        return;
    }
    try {
        const res = await fetch(`/api/workflow/instances/${_wfCurrentInstanceId}/forward`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ target_user_id: targetUserId, note }),
        });
        const data = await res.json();
        if (!data.success) {
            errEl.textContent = data.error || 'Forward failed.';
            errEl.style.display = 'block';
            return;
        }
        closeWfForwardModal();
        wfLoadInbox();
        showToast(currentLang === 'ar' ? 'تم إحالة المستند' : 'Document forwarded', 'success');
        if (data.warning) console.warn('[Workflow]', data.warning);
    } catch (e) {
        errEl.textContent = 'Forward failed: ' + e.message;
        errEl.style.display = 'block';
    }
}

async function openWfResubmitModal(instanceId) {
    _wfCurrentInstanceId = instanceId;
    document.getElementById('wfResubmitComment').value = '';
    const box = document.getElementById('wfPriorComments');
    box.innerHTML = currentLang === 'ar' ? 'جارٍ التحميل…' : 'Loading…';
    document.getElementById('wfResubmitModal').style.display = 'flex';
    try {
        const res = await fetch(`/api/workflow/instances/${instanceId}/rejection-context`);
        const data = await res.json();
        if (!data.success || !(data.comments || []).length) {
            box.innerHTML = `<p class="section-sub">${currentLang === 'ar' ? 'لا توجد تعليقات سابقة' : 'No prior comments'}</p>`;
            return;
        }
        box.innerHTML = data.comments.map(c => `
            <div class="wf-comment">
                <strong>${escapeHtml(c.by)}</strong>
                <span class="wf-comment-meta">(Submission ${c.submission ?? '-'})</span>
                <p>${escapeHtml(c.text)}</p>
            </div>
        `).join('');
    } catch (e) {
        box.innerHTML = `<p class="section-sub">Failed to load prior comments</p>`;
    }
}

function closeWfResubmitModal() {
    document.getElementById('wfResubmitModal').style.display = 'none';
    _wfCurrentInstanceId = null;
}

async function confirmWfResubmit() {
    // Comment is intentionally optional — no client-side or server-side
    // requirement, per spec.
    const comment = document.getElementById('wfResubmitComment').value.trim();
    try {
        const res = await fetch(`/api/workflow/instances/${_wfCurrentInstanceId}/resubmit`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ comment }),
        });
        const data = await res.json();
        if (!data.success) { alert(data.error || 'Resubmission failed.'); return; }
        closeWfResubmitModal();
        wfLoadSent();
        if (data.warning) console.warn('[Workflow]', data.warning);
    } catch (e) {
        alert('Resubmission failed: ' + e.message);
    }
}

// ── WORKFLOW HISTORY TAB ─────────────────────────────────────────────────
let _wfHistFiltersLoaded = false;

async function wfInitHistoryTab() {
    if (!_wfHistFiltersLoaded) {
        await wfPopulateHistoryUserFilters();
        _wfHistFiltersLoaded = true;
    }
    wfLoadHistory();
}

async function wfPopulateHistoryUserFilters() {
    const senderSel = document.getElementById('wfHistSenderFilter');
    const assigneeSel = document.getElementById('wfHistAssigneeFilter');
    const approverSel = document.getElementById('wfHistApproverFilter');
    if (!senderSel || !assigneeSel) return;
    try {
        // Scoped to exactly the same "my history" rule as the history list
        // itself (submitted by me, assigned to me, or acted on by me) —
        // these dropdowns should only ever list people relevant to what
        // this user can actually see, not the entire user directory.
        const res = await fetch('/api/workflow/history/filter-options');
        const data = await res.json();
        const toOpts = (list) => (list || []).map(u =>
            `<option value="${u.user_id}">${escapeHtml(u.full_name || `User #${u.user_id}`)}</option>`
        ).join('');
        senderSel.insertAdjacentHTML('beforeend', toOpts(data.senders));
        assigneeSel.insertAdjacentHTML('beforeend', toOpts(data.assignees));
        if (approverSel) approverSel.insertAdjacentHTML('beforeend', toOpts(data.approvers));
    } catch (e) {
        console.error('[Workflow] failed to load users for history filters', e);
    }
    wfPopulateHistoryDeptFilter();
}

function wfPopulateHistoryDeptFilter() {
    const sel = document.getElementById('wfHistDeptFilter');
    if (!sel) return;
    const current = sel.value;
    const entities = (typeof allEntities !== 'undefined' && Array.isArray(allEntities)) ? allEntities : [];
    const allowed = (typeof _canAccessDept === 'function') ? entities.filter(e => _canAccessDept(e.id)) : entities;
    sel.innerHTML = '<option value="" data-en="All departments" data-ar="كل الإدارات">All departments</option>' + allowed
        .map(e => `<option value="${e.id}">${escapeHtml(e.name || e.display_name || e.dept_name || `Dept ${e.id}`)}</option>`)
        .join('');
    if (current) sel.value = current;
}

let _wfHistSearchTimer = null;
function wfHistorySearchDebounced() {
    if (_wfHistSearchTimer) clearTimeout(_wfHistSearchTimer);
    _wfHistSearchTimer = setTimeout(() => wfLoadHistory(), 350);
}

function wfHistoryResetFilters() {
    document.getElementById('wfHistStatusFilter').value = '';
    document.getElementById('wfHistSenderFilter').value = '';
    document.getElementById('wfHistAssigneeFilter').value = '';
    document.getElementById('wfHistDeptFilter').value = '';
    document.getElementById('wfHistApproverFilter').value = '';
    document.getElementById('wfHistDateFrom').value = '';
    document.getElementById('wfHistDateTo').value = '';
    document.getElementById('wfHistSearch').value = '';
    wfLoadHistory();
}

const WF_STATUS_BADGE_CLASS = {
    'Draft': 'wf-status-badge--draft',
    'Pending': 'wf-status-badge--pending',
    'Pending Approval': 'wf-status-badge--pending',
    'Viewed': 'wf-status-badge--viewed',
    'In Progress': 'wf-status-badge--inprogress',
    'Forwarded': 'wf-status-badge--forwarded',
    'Approved': 'wf-status-badge--approved',
    'Rejected': 'wf-status-badge--rejected',
};

// Human-friendly label + "Viewed on ..." hint shown under a status badge.
function _wfStatusBadgeWithViewedHint(status, viewedOnIso) {
    let html = _wfStatusBadge(status);
    if (viewedOnIso) {
        const d = new Date(viewedOnIso);
        const label = currentLang === 'ar' ? `شوهد ${d.toLocaleString()}` : `Viewed ${d.toLocaleString()}`;
        html += `<span class="wf-viewed-hint">${escapeHtml(label)}</span>`;
    }
    return html;
}

function _wfStatusBadge(status) {
    const cls = WF_STATUS_BADGE_CLASS[status] || 'wf-status-badge--default';
    return `<span class="wf-status-badge ${cls}">${escapeHtml(status || '—')}</span>`;
}

async function wfLoadHistory() {
    const tbody = document.getElementById('wfHistoryBody');
    if (!tbody) return;
    tbody.innerHTML = `<tr><td colspan="7" style="text-align:center;color:var(--sidebar-muted,#94a3b8)">${currentLang === 'ar' ? 'جارٍ التحميل…' : 'Loading…'}</td></tr>`;

    const params = new URLSearchParams();
    const status = document.getElementById('wfHistStatusFilter')?.value || '';
    const sender = document.getElementById('wfHistSenderFilter')?.value || '';
    const assignee = document.getElementById('wfHistAssigneeFilter')?.value || '';
    const dept = document.getElementById('wfHistDeptFilter')?.value || '';
    const approver = document.getElementById('wfHistApproverFilter')?.value || '';
    const dateFrom = document.getElementById('wfHistDateFrom')?.value || '';
    const dateTo = document.getElementById('wfHistDateTo')?.value || '';
    const search = document.getElementById('wfHistSearch')?.value.trim() || '';
    if (status) params.set('status', status);
    if (sender) params.set('sender_id', sender);
    if (assignee) params.set('assignee_id', assignee);
    if (dept) params.set('dept_id', dept);
    if (approver) params.set('approver_id', approver);
    if (dateFrom) params.set('date_from', dateFrom);
    if (dateTo) params.set('date_to', dateTo);
    if (search) params.set('search', search);

    try {
        const res = await fetch(`/api/workflow/history?${params.toString()}`);
        let data;
        try {
            data = await res.json();
        } catch (parseErr) {
            // Server didn't return JSON at all (e.g. a raw 500/404 HTML
            // error page) — show the real status instead of a vague
            // "Connection error" so this is diagnosable from the UI.
            tbody.innerHTML = `<tr><td colspan="7" style="text-align:center;color:#dc2626">
                ${currentLang === 'ar' ? 'خطأ في الخادم' : 'Server error'} (HTTP ${res.status})
                <button class="btn-ghost" style="margin-left:8px" onclick="wfLoadHistory()"><i class="ph ph-arrow-clockwise"></i> ${currentLang === 'ar' ? 'إعادة المحاولة' : 'Retry'}</button>
            </td></tr>`;
            console.error('[Workflow] history non-JSON response', res.status, await res.text().catch(() => ''));
            return;
        }
        if (!res.ok || !data.success) {
            tbody.innerHTML = `<tr><td colspan="7" style="text-align:center;color:#dc2626">
                ${escapeHtml(data.error || 'Failed to load history')}
                <button class="btn-ghost" style="margin-left:8px" onclick="wfLoadHistory()"><i class="ph ph-arrow-clockwise"></i> ${currentLang === 'ar' ? 'إعادة المحاولة' : 'Retry'}</button>
            </td></tr>`;
            return;
        }
        const items = data.items || [];
        if (!items.length) {
            const emptyText = currentLang === 'ar' ? 'لا توجد سجلات مطابقة' : 'No matching records';
            tbody.innerHTML = `<tr><td colspan="7" style="text-align:center;color:var(--sidebar-muted,#94a3b8)">${emptyText}</td></tr>`;
            return;
        }
        tbody.innerHTML = items.map(item => {
            const assignees = (item.current_assignees || []).map(escapeHtml).join(', ') || '—';
            const submittedOn = item.submitted_on ? new Date(item.submitted_on).toLocaleString() : '—';
            return `<tr>
                <td>${escapeHtml(item.subject || '')}</td>
                <td>${_wfStatusBadgeWithViewedHint(item.status, item.viewed_on)}</td>
                <td>${escapeHtml(item.sender_name || '')}</td>
                <td>${assignees}</td>
                <td>#${item.submission_number}</td>
                <td>${submittedOn}</td>
                <td>
                    <button class="btn-ghost" onclick="wfOpenAttachments(${item.instance_id})" data-en="Attachments" data-ar="المرفقات">Attachments</button>
                    <button class="btn-ghost" onclick="wfViewTimeline(${item.instance_id})" data-en="View Details" data-ar="عرض التفاصيل">View Details</button>
                </td>
            </tr>`;
        }).join('');
        if (typeof applyLangToNewNodes === 'function') applyLangToNewNodes(tbody);
    } catch (e) {
        console.error('[Workflow] history load failed', e);
        tbody.innerHTML = `<tr><td colspan="7" style="text-align:center;color:#dc2626">
            ${currentLang === 'ar' ? 'خطأ في الاتصال' : 'Connection error'}
            <button class="btn-ghost" style="margin-left:8px" onclick="wfLoadHistory()"><i class="ph ph-arrow-clockwise"></i> ${currentLang === 'ar' ? 'إعادة المحاولة' : 'Retry'}</button>
        </td></tr>`;
    }
}

// ── WORKFLOW HISTORY DETAIL MODAL (complete timeline, categorized) ──────────
const WF_ACTION_LABELS = {
    SUBMITTED:   { en: 'Submitted', ar: 'تم الإرسال' },
    APPROVED:    { en: 'Approved',  ar: 'تمت الموافقة' },
    ARCHIVED:    { en: 'Fully Approved & Archived', ar: 'تمت الموافقة النهائية والأرشفة' },
    FORWARDED:   { en: 'Forwarded', ar: 'تمت الإحالة' },
    REJECTED:    { en: 'Rejected',  ar: 'تم الرفض' },
    RESUBMITTED: { en: 'Resubmitted', ar: 'أعيد الإرسال' },
    COMMENT:     { en: 'Comment',   ar: 'تعليق' },
};

function _wfActionLabel(action) {
    const l = WF_ACTION_LABELS[action];
    if (!l) return action;
    return currentLang === 'ar' ? l.ar : l.en;
}

function _wfHistItemHtml(e) {
    const when = e.on ? new Date(e.on).toLocaleString() : '';
    return `<div class="wf-hist-item">
        <div class="wf-hist-item-top">
            <span>${escapeHtml(_wfActionLabel(e.action))} — ${escapeHtml(e.by || '')}</span>
            <span class="wf-hist-item-time">${when}</span>
        </div>
        ${e.notes ? `<div class="wf-hist-item-notes">${escapeHtml(e.notes)}</div>` : ''}
    </div>`;
}

function _wfRenderHistList(containerId, events, emptyEn, emptyAr) {
    const el = document.getElementById(containerId);
    if (!el) return;
    if (!events.length) {
        el.innerHTML = `<div class="wf-hist-empty">${currentLang === 'ar' ? emptyAr : emptyEn}</div>`;
        return;
    }
    el.innerHTML = events.map(_wfHistItemHtml).join('');
}

async function wfViewTimeline(instanceId) {
    const modal = document.getElementById('wfHistDetailModal');
    const sub = document.getElementById('wfHistDetailSub');
    const summary = document.getElementById('wfHistDetailSummary');
    if (modal) modal.style.display = 'flex';
    if (sub) sub.textContent = currentLang === 'ar' ? 'جارٍ التحميل…' : 'Loading…';
    if (summary) summary.innerHTML = '';

    try {
        const res = await fetch(`/api/workflow/instances/${instanceId}/timeline`);
        const data = await res.json();
        if (!data.success) {
            if (sub) sub.textContent = data.error || 'Could not load timeline';
            return;
        }
        const events = data.events || [];
        if (sub) sub.textContent = currentLang === 'ar'
            ? `رقم السير: ${instanceId} — ${events.length} حدث`
            : `Instance #${instanceId} — ${events.length} event(s)`;

        _wfRenderHistList('wfHistDetailTimeline', events,
            'No events recorded yet.', 'لا توجد أحداث مسجلة بعد.');
        _wfRenderHistList('wfHistDetailApprovals', events.filter(e => e.action === 'APPROVED' || e.action === 'ARCHIVED'),
            'No approvals yet.', 'لا توجد موافقات بعد.');
        _wfRenderHistList('wfHistDetailForwards', events.filter(e => e.action === 'FORWARDED'),
            'This document has not been forwarded.', 'لم تتم إحالة هذا المستند.');
        _wfRenderHistList('wfHistDetailRejections', events.filter(e => e.action === 'REJECTED'),
            'This document has not been rejected.', 'لم يتم رفض هذا المستند.');

        _wfCurrentCommentInstanceId = instanceId;
        wfLoadComments(instanceId);
    } catch (e) {
        if (sub) sub.textContent = 'Failed to load timeline: ' + e.message;
    }
}

function closeWfHistDetail() {
    const modal = document.getElementById('wfHistDetailModal');
    if (modal) modal.style.display = 'none';
    _wfCurrentCommentInstanceId = null;
    wfCloseMentionDropdown();
}

// ── WORKFLOW COMMENTS & @MENTIONS (Task 14) ──────────────────────────────
// Mentions are encoded inline in the raw comment text as @[Full Name](id)
// tokens (matches the server's parser) so no extra table/column is needed
// on top of the pre-existing WF_Comments table. The composer only ever
// inserts these tokens through the picker below — a person never has to
// type the bracket syntax themselves.
let _wfCurrentCommentInstanceId = null;
let _wfMentionUsersCache = null; // [{user_id, full_name, username, email}]
let _wfMentionActiveIndex = 0;
let _wfMentionMatchStart = -1; // index of the '@' that's currently being completed

async function _wfGetMentionUsers() {
    if (_wfMentionUsersCache) return _wfMentionUsersCache;
    try {
        const res = await fetch('/api/users/list-emails');
        const data = await res.json();
        _wfMentionUsersCache = data.users || [];
    } catch (e) {
        _wfMentionUsersCache = [];
    }
    return _wfMentionUsersCache;
}

// Renders @[Name](id) tokens as highlighted, non-editable-looking chips.
function _wfRenderCommentText(text) {
    const escaped = escapeHtml(text || '');
    return escaped.replace(/@\[([^\[\]]{1,100})\]\((\d+)\)/g,
        (_m, name) => `<span class="wf-mention-chip">@${escapeHtml(name)}</span>`);
}

function _wfCommentItemHtml(c) {
    const when = c.on ? new Date(c.on).toLocaleString() : '';
    return `<div class="wf-comment-item">
        <div class="wf-comment-item-top">
            <span class="wf-comment-author">${escapeHtml(c.by_name || '')}</span>
            <span class="wf-comment-time">${when}</span>
        </div>
        <div class="wf-comment-body">${_wfRenderCommentText(c.text)}</div>
    </div>`;
}

async function wfLoadComments(instanceId) {
    const thread = document.getElementById('wfCommentThread');
    if (!thread) return;
    thread.innerHTML = `<div class="wf-hist-empty">${currentLang === 'ar' ? 'جارٍ التحميل…' : 'Loading…'}</div>`;
    try {
        const res = await fetch(`/api/workflow/instances/${instanceId}/comments`);
        const data = await res.json();
        if (!data.success) {
            thread.innerHTML = `<div class="wf-hist-empty">${escapeHtml(data.error || 'Failed to load comments')}</div>`;
            return;
        }
        const comments = data.comments || [];
        if (!comments.length) {
            thread.innerHTML = `<div class="wf-hist-empty">${currentLang === 'ar' ? 'لا توجد تعليقات بعد.' : 'No comments yet.'}</div>`;
            return;
        }
        thread.innerHTML = comments.map(_wfCommentItemHtml).join('');
        thread.scrollTop = thread.scrollHeight;
    } catch (e) {
        thread.innerHTML = `<div class="wf-hist-empty">${currentLang === 'ar' ? 'خطأ في الاتصال' : 'Connection error'}</div>`;
    }
}

async function wfPostComment() {
    const input = document.getElementById('wfCommentInput');
    if (!input || !_wfCurrentCommentInstanceId) return;
    const text = input.value.trim();
    if (!text) return;
    try {
        const res = await fetch(`/api/workflow/instances/${_wfCurrentCommentInstanceId}/comments`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ text }),
        });
        const data = await res.json();
        if (!data.success) {
            showToast(data.error || 'Failed to post comment', 'error');
            return;
        }
        input.value = '';
        wfCloseMentionDropdown();
        wfLoadComments(_wfCurrentCommentInstanceId);
    } catch (e) {
        showToast('Failed to post comment: ' + e.message, 'error');
    }
}

// Typing "@" opens a filtered dropdown of users; picking one inserts the
// @[Name](id) token in place of the partial "@query" that was typed.
async function wfOnCommentInput(evt) {
    const input = evt.target;
    const caret = input.selectionStart;
    const upToCaret = input.value.slice(0, caret);
    const match = upToCaret.match(/(?:^|\s)@([^\s@]{0,40})$/);
    if (!match) {
        wfCloseMentionDropdown();
        return;
    }
    _wfMentionMatchStart = caret - match[1].length - 1; // index of '@'
    const query = match[1].toLowerCase();
    const users = await _wfGetMentionUsers();
    const filtered = users.filter(u => {
        const label = (u.full_name || u.username || u.email || '').toLowerCase();
        return label.includes(query);
    }).slice(0, 8);
    if (!filtered.length) {
        wfCloseMentionDropdown();
        return;
    }
    _wfMentionActiveIndex = 0;
    wfRenderMentionDropdown(filtered);
}

function wfRenderMentionDropdown(users) {
    const dd = document.getElementById('wfMentionDropdown');
    if (!dd) return;
    dd.innerHTML = users.map((u, i) => {
        const label = u.full_name || u.username || u.email;
        return `<div class="wf-mention-option${i === _wfMentionActiveIndex ? ' wf-mention-option--active' : ''}"
                     data-idx="${i}" onmousedown="event.preventDefault();wfPickMention(${i})">
                    ${escapeHtml(label)}
                </div>`;
    }).join('');
    dd.dataset.users = JSON.stringify(users);
    dd.style.display = 'block';
}

function wfCloseMentionDropdown() {
    const dd = document.getElementById('wfMentionDropdown');
    if (!dd) return;
    dd.style.display = 'none';
    dd.innerHTML = '';
    _wfMentionMatchStart = -1;
}

function wfPickMention(idx) {
    const dd = document.getElementById('wfMentionDropdown');
    const input = document.getElementById('wfCommentInput');
    if (!dd || !input || _wfMentionMatchStart < 0) return;
    const users = JSON.parse(dd.dataset.users || '[]');
    const u = users[idx];
    if (!u) return;
    const label = u.full_name || u.username || u.email;
    const token = `@[${label}](${u.user_id})`;
    const caret = input.selectionStart;
    const before = input.value.slice(0, _wfMentionMatchStart);
    const after = input.value.slice(caret);
    input.value = `${before}${token} ${after}`;
    const newCaret = (before + token + ' ').length;
    input.focus();
    input.setSelectionRange(newCaret, newCaret);
    wfCloseMentionDropdown();
}

function wfOnCommentKeydown(evt) {
    const dd = document.getElementById('wfMentionDropdown');
    const open = dd && dd.style.display !== 'none';
    if (open) {
        const users = JSON.parse(dd.dataset.users || '[]');
        if (evt.key === 'ArrowDown') {
            evt.preventDefault();
            _wfMentionActiveIndex = Math.min(_wfMentionActiveIndex + 1, users.length - 1);
            wfRenderMentionDropdown(users);
            return;
        }
        if (evt.key === 'ArrowUp') {
            evt.preventDefault();
            _wfMentionActiveIndex = Math.max(_wfMentionActiveIndex - 1, 0);
            wfRenderMentionDropdown(users);
            return;
        }
        if (evt.key === 'Enter' || evt.key === 'Tab') {
            evt.preventDefault();
            wfPickMention(_wfMentionActiveIndex);
            return;
        }
        if (evt.key === 'Escape') {
            wfCloseMentionDropdown();
            return;
        }
    }
    if (evt.key === 'Enter' && !evt.shiftKey && !open) {
        evt.preventDefault();
        wfPostComment();
    }
}

function escapeHtml(str) {
    const d = document.createElement('div');
    d.textContent = str == null ? '' : String(str);
    return d.innerHTML;
}

// ── DOCUMENT QR CODE ─────────────────────────────────────────────────────
let _qrCurrentUrl = null;
let _qrInstance = null;

async function openQrModal() {
    const doc = _viewDocData;
    if (!doc || !doc.id) return;

    const modal = document.getElementById('qrModal');
    const subjEl = document.getElementById('qrModalSubject');
    const statusEl = document.getElementById('qrStatus');
    const container = document.getElementById('qrCodeContainer');
    if (!modal || !container) return;

    // Hide the document viewer behind it so the two dark overlays don't
    // stack on top of each other (that stacking was causing the near-black,
    // double-blurred background).
    const viewDocModal = document.getElementById('viewDocModal');
    if (viewDocModal) viewDocModal.style.display = 'none';

    if (subjEl) subjEl.textContent = doc.subject || ('Document #' + doc.id);
    if (statusEl) statusEl.textContent = '';
    container.innerHTML = '<span id="qrLoadingLabel" style="font-size:.8rem;color:var(--muted)">Generating…</span>';
    _qrCurrentUrl = null;
    modal.style.display = 'flex';

    try {
        const res = await fetch(`/api/documents/${doc.id}/qr`, { method: 'POST' });
        const data = await res.json();
        if (!res.ok || data.error) {
            if (statusEl) statusEl.textContent = data.error || 'Failed to generate QR code.';
            container.innerHTML = '<i class="ph ph-warning-circle" style="font-size:32px;color:#dc2626"></i>';
            return;
        }
        _qrCurrentUrl = data.qr_url;
        container.innerHTML = '';
        if (typeof QRCode !== 'undefined') {
            _qrInstance = new QRCode(container, {
                text: data.qr_url,
                width: 200,
                height: 200,
                correctLevel: QRCode.CorrectLevel.M,
            });
        } else {
            container.innerHTML = `<a href="${data.qr_url}" target="_blank" style="font-size:.8rem;word-break:break-all;padding:8px">${data.qr_url}</a>`;
        }
    } catch (e) {
        if (statusEl) statusEl.textContent = 'Network error generating QR code.';
        container.innerHTML = '<i class="ph ph-warning-circle" style="font-size:32px;color:#dc2626"></i>';
    }
}

function closeQrModal() {
    const modal = document.getElementById('qrModal');
    if (modal) modal.style.display = 'none';

    // Restore the document viewer we hid when the QR modal opened, so
    // closing QR returns you to the document instead of the bare list.
    if (_viewDocData) {
        const viewDocModal = document.getElementById('viewDocModal');
        if (viewDocModal) viewDocModal.style.display = 'grid';
    }
}

function copyQrLink() {
    if (!_qrCurrentUrl) return;
    navigator.clipboard.writeText(_qrCurrentUrl).then(() => {
        const statusEl = document.getElementById('qrStatus');
        if (statusEl) {
            statusEl.style.color = '#16a34a';
            statusEl.textContent = 'Link copied!';
            setTimeout(() => { statusEl.textContent = ''; }, 2000);
        }
    }).catch(() => {});
}

function downloadQrImage() {
    const container = document.getElementById('qrCodeContainer');
    if (!container) return;
    const canvas = container.querySelector('canvas');
    const img = container.querySelector('img');
    let dataUrl = null;
    if (canvas) {
        dataUrl = canvas.toDataURL('image/png');
    } else if (img && img.src) {
        dataUrl = img.src;
    }
    if (!dataUrl) return;
    const a = document.createElement('a');
    a.href = dataUrl;
    a.download = 'document-qr.png';
    document.body.appendChild(a);
    a.click();
    a.remove();
}

// ── NOTIFICATIONS ──────────────────────────────────────────────────────────
const NOTIF_ICONS = {
    ADD: 'ph-file-plus', EDIT: 'ph-pencil-simple', DELETE: 'ph-trash',
    WF_SUBMITTED: 'ph-user-plus', WF_APPROVED: 'ph-check-circle',
    WF_REJECTED: 'ph-x-circle', WF_FORWARDED: 'ph-arrow-bend-up-right',
    WF_COMMENT_ADDED: 'ph-chat-circle-text', WF_MENTIONED: 'ph-at',
};
// Sentence templates per action, in both languages. The server only sends
// the raw document subject (or none) + the action type — the sentence
// itself is always built here so it renders correctly in whichever
// language is currently active, including for notifications received
// while the language was set to the other one.
const NOTIF_TEMPLATES = {
    ADD:    { en_with: s => `Document "${s}" was added`,   en_bare: 'A document was added',
              ar_with: s => `تمت إضافة المستند "${s}"`,     ar_bare: 'تمت إضافة مستند' },
    EDIT:   { en_with: s => `Document "${s}" was edited`,  en_bare: 'A document was edited',
              ar_with: s => `تم تعديل المستند "${s}"`,      ar_bare: 'تم تعديل مستند' },
    DELETE: { en_with: s => `Document "${s}" was deleted`, en_bare: 'A document was deleted',
              ar_with: s => `تم حذف المستند "${s}"`,        ar_bare: 'تم حذف مستند' },
    WF_SUBMITTED: { en_with: s => `A document "${s}" was assigned to you for approval`, en_bare: 'A document was assigned to you for approval',
              ar_with: s => `تم إسناد المستند "${s}" إليك للموافقة`,   ar_bare: 'تم إسناد مستند إليك للموافقة' },
    WF_APPROVED:  { en_with: s => `Your document "${s}" was approved`, en_bare: 'Your document was approved',
              ar_with: s => `تمت الموافقة على المستند "${s}"`,          ar_bare: 'تمت الموافقة على مستندك' },
    WF_REJECTED:  { en_with: s => `Your document "${s}" was rejected`, en_bare: 'Your document was rejected',
              ar_with: s => `تم رفض المستند "${s}"`,                    ar_bare: 'تم رفض مستندك' },
    WF_FORWARDED: { en_with: s => `A document "${s}" was forwarded to you`, en_bare: 'A document was forwarded to you',
              ar_with: s => `تمت إحالة المستند "${s}" إليك`,             ar_bare: 'تمت إحالة مستند إليك' },
    WF_COMMENT_ADDED: { en_with: s => `New comment: ${s}`, en_bare: 'A new comment was added to a document you follow',
              ar_with: s => `تعليق جديد: ${s}`,             ar_bare: 'تمت إضافة تعليق جديد على مستند تتابعه' },
    WF_MENTIONED: { en_with: s => `You were mentioned: ${s}`, en_bare: 'You were mentioned in a comment',
              ar_with: s => `تمت الإشارة إليك: ${s}`,          ar_bare: 'تمت الإشارة إليك في تعليق' },
};

function _notifMessage(n) {
    const tpl = NOTIF_TEMPLATES[n.action_type];
    const subject = (n.subject || '').trim();
    if (!tpl) {
        // Unknown action type — fall back to whatever the server sent, if anything.
        return subject || (currentLang === 'ar' ? 'إشعار' : 'Notification');
    }
    if (currentLang === 'ar') {
        return subject ? tpl.ar_with(subject) : tpl.ar_bare;
    }
    return subject ? tpl.en_with(subject) : tpl.en_bare;
}

let _notifPollTimer = null;
let _lastNotifItems = [];

function toggleNotif() {
    const panel = document.getElementById('notifPanel');
    if (!panel) return;
    const opening = !panel.classList.contains('open');
    panel.classList.toggle('open');
    if (opening) loadNotifications();
}

function closeNotif() {
    const panel = document.getElementById('notifPanel');
    if (panel) panel.classList.remove('open');
}

document.addEventListener('click', e => {
    const btn = document.getElementById('notifBtn');
    const panel = document.getElementById('notifPanel');
    if (panel && btn && !btn.contains(e.target) && !panel.contains(e.target)) closeNotif();
});

function _notifTimeAgo(isoStr) {
    if (!isoStr) return '';
    const diffMs = Date.now() - new Date(isoStr).getTime();
    const mins = Math.floor(diffMs / 60000);
    const isAr = currentLang === 'ar';
    if (mins < 1) return isAr ? 'الآن' : 'now';
    if (mins < 60) return isAr ? `قبل ${mins} د` : `${mins}m ago`;
    const hrs = Math.floor(mins / 60);
    if (hrs < 24) return isAr ? `قبل ${hrs} س` : `${hrs}h ago`;
    return isAr ? `قبل ${Math.floor(hrs / 24)} ي` : `${Math.floor(hrs / 24)}d ago`;
}

function renderNotifications(items) {
    const list = document.getElementById('notifList');
    if (!list) return;
    if (!items || !items.length) {
        const emptyText = currentLang === 'ar' ? 'لا توجد إشعارات حتى الآن' : 'No notifications yet';
        list.innerHTML = `<div class="notif-empty" data-en="No notifications yet" data-ar="لا توجد إشعارات حتى الآن">${emptyText}</div>`;
        return;
    }
    const dismissTitle = currentLang === 'ar' ? 'إزالة' : 'Remove';
    list.innerHTML = items.map(n => {
        const icon = NOTIF_ICONS[n.action_type] || 'ph-bell';
        const unreadClass = n.is_read ? '' : ' notif-item--unread';
        return `<div class="notif-item${unreadClass}" data-id="${n.id}" onclick="onNotifClick(${n.id}, ${n.doc_id || 'null'}, '${n.action_type || ''}')">
            <i class="ph ${icon}" style="font-size:16px;margin-right:6px;flex-shrink:0"></i>
            <div style="flex:1;min-width:0">
                <strong>${_escapeHtml(_notifMessage(n))}</strong>
                <span>${_notifTimeAgo(n.created_on)}</span>
            </div>
            ${n.is_read ? '' : '<span class="notif-dot"></span>'}
            <button type="button" class="notif-dismiss-btn" title="${dismissTitle}" onclick="dismissNotification(event, ${n.id})">
                <i class="ph ph-x"></i>
            </button>
        </div>`;
    }).join('');
}

function _escapeHtml(str) {
    const d = document.createElement('div');
    d.textContent = str;
    return d.innerHTML;
}

function updateNotifBadge(count) {
    const badge = document.getElementById('notifBadge');
    if (!badge) return;
    if (count > 0) {
        badge.textContent = count > 99 ? '99+' : String(count);
        badge.style.display = 'inline-flex';
    } else {
        badge.style.display = 'none';
    }
}

async function loadNotifications() {
    try {
        const res = await fetch('/api/notifications?limit=30');
        if (!res.ok) return;
        const data = await res.json();
        _lastNotifItems = data.items || [];
        renderNotifications(_lastNotifItems);
        updateNotifBadge(data.unread_count || 0);
    } catch (e) {
        console.error('Failed to load notifications', e);
    }
}

const WF_NOTIF_ACTION_TYPES = new Set([
    'WF_SUBMITTED', 'WF_APPROVED', 'WF_REJECTED', 'WF_FORWARDED',
    'WF_COMMENT_ADDED', 'WF_MENTIONED',
]);

async function onNotifClick(notifId, docId, actionType) {
    try {
        await fetch(`/api/notifications/${notifId}/read`, { method: 'POST' });
    } catch (e) { /* non-blocking */ }
    loadNotifications();
    if (!docId) return;
    closeNotif();
    if (WF_NOTIF_ACTION_TYPES.has(actionType)) {
        // Workflow notifications carry a WF_Instances.InstanceID, not an
        // Adco_Transactions id — send these to the Workflow module instead
        // of the Archive "view transaction" viewer.
        wfOpenInstanceDeepLink(docId);
    } else if (typeof viewTransaction === 'function') {
        viewTransaction(docId);
    }
}

async function markAllNotificationsRead() {
    try {
        await fetch('/api/notifications/read-all', { method: 'POST' });
    } catch (e) { /* non-blocking */ }
    loadNotifications();
}

async function dismissNotification(evt, notifId) {
    if (evt) evt.stopPropagation(); // don't trigger onNotifClick/navigation
    try {
        await fetch(`/api/notifications/${notifId}`, { method: 'DELETE' });
    } catch (e) { /* non-blocking */ }
    loadNotifications();
}

async function clearAllNotifications() {
    try {
        await fetch('/api/notifications', { method: 'DELETE' });
    } catch (e) { /* non-blocking */ }
    loadNotifications();
}

function startNotifPolling() {
    loadNotifications();
    if (_notifPollTimer) clearInterval(_notifPollTimer);
    // Safety-net interval now that the 'notification' socket event (see
    // rtInit) triggers loadNotifications() instantly on real events.
    _notifPollTimer = setInterval(loadNotifications, 120000);
}

document.addEventListener('DOMContentLoaded', startNotifPolling);

// ── PROFILE INITIALS ───────────────────────────────────────────────────────
function setProfileInitials() {
    const el = document.getElementById('profileInitials');
    if (!el) return;
    const name = document.body.getAttribute('data-user-full') || 'U';
    const parts = name.trim().split(/\s+/);
    const initials = parts.length >= 2
        ? parts[0][0] + parts[1][0]
        : name.slice(0, 2);
    el.textContent = initials.toUpperCase();
}

// ── COUNT-UP ANIMATION ─────────────────────────────────────────────────────
function countUp(el) {
    const target = parseInt(el.getAttribute('data-target'), 10);
    const suffix = el.getAttribute('data-suffix') || '';
    if (isNaN(target)) return;
    const duration = 800;
    const step = 16;
    const steps = Math.ceil(duration / step);
    let current = 0;
    const inc = target / steps;
    const timer = setInterval(() => {
        current = Math.min(current + inc, target);
        el.textContent = Math.round(current) + suffix;
        if (current >= target) clearInterval(timer);
    }, step);
}

function runCountUps() {
    document.querySelectorAll('[data-target]').forEach(el => countUp(el));
}

// ── FILE UPLOAD (queue: add one-by-one, remove only with ✕) ───────────────
let _archivePendingFiles = [];
let _archiveFileDisplayNames = {}; // index (as string) -> renamed display name, only set when renamed
let _archiveUnsignedOriginals = {}; // index (as string) -> pre-signature File, only set once a file has been signed
let _pendingPreviewUrl = null; // tracks the active blob: URL so it can be revoked

// The name actually used for upload/preview/display: the rename if one was
// set for this file, otherwise the file's original name.
function getArchiveFileName(index) {
    return _archiveFileDisplayNames[index] || _archivePendingFiles[index]?.name || '';
}

function addArchiveFiles(files) {
    let added = 0;
    files.forEach(f => {
        const dup = _archivePendingFiles.some(
            x => x.name === f.name && x.size === f.size && x.lastModified === f.lastModified
        );
        if (!dup) {
            _archivePendingFiles.push(f);
            added++;
        }
    });
    syncArchiveFileInput();
    renderArchiveFileList();
    if (added) {
        goToStep(3);
        showToast(
            currentLang === 'ar'
                ? `تم إضافة ${added} ملف (${_archivePendingFiles.length} إجمالي)`
                : `Added ${added} file(s) (${_archivePendingFiles.length} total)`,
            'success'
        );
    }
}

function syncArchiveFileInput() {
    const fileInput = document.getElementById('fileInput');
    if (!fileInput) return;
    const dt = new DataTransfer();
    _archivePendingFiles.forEach(f => dt.items.add(f));
    fileInput.files = dt.files;
}

function renderArchiveFileList() {
    const fileList = document.getElementById('fileList');
    if (!fileList) return;
    if (!_archivePendingFiles.length) {
        fileList.innerHTML = '';
        _archiveFileDisplayNames = {};
        return;
    }
    // Renaming newly attached files works the same in edit mode as it does
    // when archiving a brand-new document — the upload name (rename, if any)
    // is what both the new-document and edit-mode save endpoints persist as
    // File_Name, so there's no backend reason to hide this in edit mode.
    fileList.innerHTML = _archivePendingFiles.map((f, i) => {
        const displayName = getArchiveFileName(i);
        const renamedBadge = _archiveFileDisplayNames[i]
            ? `<span class="file-item-renamed-badge" title="${currentLang === 'ar' ? `الاسم الأصلي: ${f.name}` : `Original: ${f.name}`}">${currentLang === 'ar' ? 'أُعيدت تسميته' : 'renamed'}</span>`
            : '';
        const renameBtn = `<button type="button" class="file-item-rename" onclick="startRenameArchiveFile(${i})" title="${currentLang === 'ar' ? 'إعادة تسمية' : 'Rename'}"><i class="ph ph-pencil-simple"></i></button>`;
        const signedBadge = _archiveUnsignedOriginals[i]
            ? `<span class="file-item-renamed-badge" title="${currentLang === 'ar' ? 'تم توقيعه' : 'Signed'}">${currentLang === 'ar' ? 'موقّع' : 'signed'}</span>`
            : '';
        const removeSignBtn = _archiveUnsignedOriginals[i]
            ? `<button type="button" class="file-item-preview" onclick="removePendingFileSignature(${i})" title="${currentLang === 'ar' ? 'إزالة التوقيع' : 'Remove signature'}"><i class="ph ph-eraser"></i></button>`
            : '';
        const isPdfFile = /\.pdf$/i.test(displayName);
        const pagesBtn = isPdfFile
            ? `<button type="button" class="file-item-preview" onclick="openPageManagerForPendingFile(${i})" title="${currentLang === 'ar' ? 'إدارة الصفحات' : 'Manage Pages'}"><i class="ph ph-stack"></i></button>`
            : '';
        return `
        <div class="file-item" id="file-item-${i}">
            <span class="file-item-icon">${getFileIcon(displayName)}</span>
            <span class="file-item-name file-item-name--clickable" onclick="previewPendingFile(${i})" title="${currentLang === 'ar' ? 'معاينة' : 'Preview'}">${escAttr(displayName)}</span>
            ${renamedBadge}
            ${signedBadge}
            <span class="file-item-size">${formatBytes(f.size)}</span>
            ${renameBtn}
            <button type="button" class="file-item-preview" onclick="previewPendingFile(${i})" title="${currentLang === 'ar' ? 'معاينة' : 'Preview'}"><i class="ph ph-eye"></i></button>
            ${pagesBtn}
            <button type="button" class="file-item-preview" onclick="openSignatureModalForPendingFile(${i})" title="${currentLang === 'ar' ? 'توقيع' : 'Sign'}"><i class="ph ph-pen-nib"></i></button>
            ${removeSignBtn}
            <button type="button" class="file-item-remove" onclick="removeFile(${i})" title="Remove">✕</button>
        </div>
    `;
    }).join('');
}

// ── Rename a not-yet-uploaded file (archiving step) ─────────────────────────
// Renaming only changes the name used for upload/display — it does NOT touch
// the underlying File object's bytes/type, and the extension is preserved
// automatically so the saved document stays openable.
function startRenameArchiveFile(index) {
    const f = _archivePendingFiles[index];
    if (!f) return;
    const row = document.getElementById(`file-item-${index}`);
    if (!row) return;

    const currentName = getArchiveFileName(index);
    const dot = currentName.lastIndexOf('.');
    const stem = dot > 0 ? currentName.slice(0, dot) : currentName;
    const ext = dot > 0 ? currentName.slice(dot) : '';

    const nameSpan = row.querySelector('.file-item-name');
    if (!nameSpan) return;

    const input = document.createElement('input');
    input.type = 'text';
    input.className = 'file-item-rename-input';
    input.value = stem;
    input.setAttribute('aria-label', currentLang === 'ar' ? 'اسم الملف الجديد' : 'New file name');

    const commit = () => {
        const newStem = input.value.trim();
        if (newStem) {
            const newName = newStem + ext;
            if (newName === f.name) {
                delete _archiveFileDisplayNames[index];
            } else {
                _archiveFileDisplayNames[index] = newName;
            }
        }
        renderArchiveFileList();
    };

    input.addEventListener('keydown', (e) => {
        if (e.key === 'Enter') { e.preventDefault(); commit(); }
        if (e.key === 'Escape') { e.preventDefault(); renderArchiveFileList(); }
    });
    input.addEventListener('blur', commit);

    nameSpan.replaceWith(input);
    input.focus();
    input.select();
}

function removeArchiveFileRename(index) {
    delete _archiveFileDisplayNames[index];
    renderArchiveFileList();
}

// ── Preview a not-yet-uploaded attachment (from the local file picker) ─────
function previewPendingFile(index, filesArr) {
    const f = (filesArr || _archivePendingFiles)[index];
    if (!f) return;
    const displayName = filesArr ? f.name : getArchiveFileName(index);

    const modal       = document.getElementById('pendingPreviewModal');
    const iframe      = document.getElementById('pendingPreviewIframe');
    const placeholder = document.getElementById('pendingPreviewPlaceholder');
    const nameEl      = document.getElementById('pendingPreviewName');
    if (!modal || !iframe || !placeholder) return;

    if (nameEl) nameEl.textContent = displayName;

    // Revoke any previous blob URL before creating a new one
    if (_pendingPreviewUrl) {
        URL.revokeObjectURL(_pendingPreviewUrl);
        _pendingPreviewUrl = null;
    }
    const url = URL.createObjectURL(f);
    _pendingPreviewUrl = url;

    const n = (displayName || '').toLowerCase();
    const isImg   = /\.(png|jpe?g|gif|webp|tiff?|bmp|svg)$/i.test(n);
    const isVideo = /\.(mp4|webm|ogg)$/i.test(n);
    const isAudio = /\.(mp3|wav|ogg)$/i.test(n);
    const isText  = /\.(txt|csv|json|md)$/i.test(n);
    const isPdf   = /\.pdf$/i.test(n) || f.type === 'application/pdf';

    iframe.style.display = 'none';
    iframe.src = '';
    placeholder.style.display = 'flex';
    placeholder.innerHTML = '';

    if (isImg) {
        placeholder.innerHTML = `<img src="${url}" alt="${escAttr(displayName)}"
            style="max-width:100%;max-height:100%;object-fit:contain;border-radius:6px">`;
    } else if (isVideo) {
        placeholder.innerHTML = `<video controls style="max-width:100%;max-height:100%;border-radius:6px">
            <source src="${url}">
            ${currentLang === 'ar' ? 'المتصفح لا يدعم تشغيل الفيديو' : 'Your browser does not support video playback.'}
        </video>`;
    } else if (isAudio) {
        placeholder.innerHTML = `<audio controls style="width:100%;margin:auto">
            <source src="${url}">
            ${currentLang === 'ar' ? 'المتصفح لا يدعم تشغيل الصوت' : 'Your browser does not support audio playback.'}
        </audio>`;
    } else if (isText) {
        f.text().then(text => {
            placeholder.innerHTML = `<pre style="white-space:pre-wrap;word-break:break-all;
                font-size:12px;text-align:left;direction:ltr;overflow:auto;
                width:100%;height:100%;padding:12px;box-sizing:border-box">${text.replace(/</g,'&lt;')}</pre>`;
        }).catch(() => {
            placeholder.innerHTML = `<span style="color:var(--muted)">${currentLang === 'ar' ? 'تعذّر تحميل الملف' : 'Could not load file'}</span>`;
        });
        placeholder.innerHTML = `<span style="color:var(--muted)">Loading…</span>`;
    } else if (isPdf) {
        placeholder.style.display = 'none';
        iframe.src = url;
        iframe.style.display = 'block';
    } else {
        placeholder.innerHTML = `
            <span><i class="ph ph-file" style="font-size:40px;opacity:0.3"></i></span>
            <span>${currentLang === 'ar' ? 'لا تتوفر معاينة لهذا نوع الملف' : 'Preview not available for this file type'}</span>`;
    }

    modal.style.display = 'flex';
}

function closePendingPreview() {
    const modal = document.getElementById('pendingPreviewModal');
    const iframe = document.getElementById('pendingPreviewIframe');
    if (modal) modal.style.display = 'none';
    if (iframe) { iframe.src = ''; iframe.style.display = 'none'; }
    if (_pendingPreviewUrl) {
        URL.revokeObjectURL(_pendingPreviewUrl);
        _pendingPreviewUrl = null;
    }
}

// ── Preview an attachment already saved on the server (edit mode) ──────────
function previewExistingAttachment(url, name) {
    if (!url) return;
    const modal       = document.getElementById('pendingPreviewModal');
    const iframe      = document.getElementById('pendingPreviewIframe');
    const placeholder = document.getElementById('pendingPreviewPlaceholder');
    const nameEl      = document.getElementById('pendingPreviewName');
    if (!modal || !iframe || !placeholder) return;

    if (nameEl) nameEl.textContent = name || '';

    const n = (name || '').toLowerCase();
    const isImg   = /\.(png|jpe?g|gif|webp|tiff?|bmp|svg)$/i.test(n);
    const isVideo = /\.(mp4|webm|ogg)$/i.test(n);
    const isAudio = /\.(mp3|wav|ogg)$/i.test(n);
    const isText  = /\.(txt|csv|json|md)$/i.test(n);

    iframe.style.display = 'none';
    iframe.src = '';
    placeholder.style.display = 'flex';
    placeholder.innerHTML = '';

    if (isImg) {
        placeholder.innerHTML = `<img src="${url}" alt="${escAttr(name)}"
            style="max-width:100%;max-height:100%;object-fit:contain;border-radius:6px">`;
    } else if (isVideo) {
        placeholder.innerHTML = `<video controls style="max-width:100%;max-height:100%;border-radius:6px">
            <source src="${url}">
            ${currentLang === 'ar' ? 'المتصفح لا يدعم تشغيل الفيديو' : 'Your browser does not support video playback.'}
        </video>`;
    } else if (isAudio) {
        placeholder.innerHTML = `<audio controls style="width:100%;margin:auto">
            <source src="${url}">
            ${currentLang === 'ar' ? 'المتصفح لا يدعم تشغيل الصوت' : 'Your browser does not support audio playback.'}
        </audio>`;
    } else if (isText) {
        fetch(url).then(r => r.text()).then(text => {
            placeholder.innerHTML = `<pre style="white-space:pre-wrap;word-break:break-all;
                font-size:12px;text-align:left;direction:ltr;overflow:auto;
                width:100%;height:100%;padding:12px;box-sizing:border-box">${text.replace(/</g,'&lt;')}</pre>`;
        }).catch(() => {
            placeholder.innerHTML = `<span style="color:var(--muted)">${currentLang === 'ar' ? 'تعذّر تحميل الملف' : 'Could not load file'}</span>`;
        });
        placeholder.innerHTML = `<span style="color:var(--muted)">Loading…</span>`;
    } else {
        // PDF and everything else — use iframe
        placeholder.style.display = 'none';
        iframe.src = url;
        iframe.style.display = 'block';
    }

    modal.style.display = 'flex';
}

function onArchiveFileInputChange(input) {
    if (input?.files?.length) addArchiveFiles(Array.from(input.files));
    input.value = '';
}

function showFileList() {
    onArchiveFileInputChange(document.getElementById('fileInput'));
}

let _sigUnsignInFlight = new Set();
function removeAttachmentSignature(attachmentId, refreshFn) {
    if (_sigUnsignInFlight.has(attachmentId)) return; // already in progress, ignore repeat clicks
    if (!confirm(currentLang === 'ar' ? 'إزالة التوقيع من هذا المستند؟' : 'Remove the signature from this document?')) return;
    _sigUnsignInFlight.add(attachmentId);
    fetch(`/api/attachments/${attachmentId}/unsign`, { method: 'POST' })
        .then(r => r.json().then(data => ({ ok: r.ok, data })))
        .then(({ ok, data }) => {
            if (!ok) {
                alert(data.error || 'Could not remove signature.');
                return;
            }
            if (typeof refreshFn === 'function') refreshFn();
        })
        .catch(e => alert('Could not remove signature: ' + e.message))
        .finally(() => _sigUnsignInFlight.delete(attachmentId));
}

function getFileIcon(name) {
    const ext = name.split('.').pop().toLowerCase();
    const icons = { pdf: '<i class="ph ph-file-text"></i>', doc: '<i class="ph ph-file-doc"></i>', docx: '<i class="ph ph-file-doc"></i>', xls: '<i class="ph ph-file-xls"></i>', xlsx: '<i class="ph ph-file-xls"></i>',
                    png: '<i class="ph ph-image"></i>', jpg: '<i class="ph ph-image"></i>', jpeg: '<i class="ph ph-image"></i>', gif: '<i class="ph ph-image"></i>', zip: '<i class="ph ph-file-zip"></i>', rar: '<i class="ph ph-file-zip"></i>' };
    return icons[ext] || '<i class="ph ph-paperclip"></i>';
}

function formatBytes(bytes) {
    if (bytes === 0) return '0 B';
    const k = 1024;
    const sizes = ['B','KB','MB','GB'];
    const i = Math.floor(Math.log(bytes) / Math.log(k));
    return parseFloat((bytes / Math.pow(k, i)).toFixed(1)) + ' ' + sizes[i];
}

function removeFile(index) {
    _archivePendingFiles.splice(index, 1);
    // Reindex the rename map: shift every key above the removed index down by
    // one so renames stay attached to the correct file after the splice.
    const reindexed = {};
    Object.keys(_archiveFileDisplayNames).forEach(k => {
        const i = parseInt(k, 10);
        if (i < index) reindexed[i] = _archiveFileDisplayNames[k];
        else if (i > index) reindexed[i - 1] = _archiveFileDisplayNames[k];
        // i === index: dropped along with the removed file
    });
    _archiveFileDisplayNames = reindexed;

    const reindexedOriginals = {};
    Object.keys(_archiveUnsignedOriginals).forEach(k => {
        const i = parseInt(k, 10);
        if (i < index) reindexedOriginals[i] = _archiveUnsignedOriginals[k];
        else if (i > index) reindexedOriginals[i - 1] = _archiveUnsignedOriginals[k];
    });
    _archiveUnsignedOriginals = reindexedOriginals;

    syncArchiveFileInput();
    renderArchiveFileList();
    if (!_archivePendingFiles.length) {
        goToStep(2);
    }
}

function showFileName() { showFileList(); }

// Restore a not-yet-uploaded file to its pre-signature state.
function removePendingFileSignature(index) {
    const original = _archiveUnsignedOriginals[index];
    if (!original) return;
    _archivePendingFiles[index] = original;
    delete _archiveUnsignedOriginals[index];
    syncArchiveFileInput();
    renderArchiveFileList();
}

function setupDragDrop() {
    const box = document.getElementById('uploadBox');
    if (!box) return;

    box.addEventListener('dragover', e => {
        e.preventDefault();
        box.classList.add('drag-over');
    });
    box.addEventListener('dragleave', () => box.classList.remove('drag-over'));
    box.addEventListener('drop', e => {
        e.preventDefault();
        box.classList.remove('drag-over');
        if (e.dataTransfer.files.length > 0) {
            addArchiveFiles(Array.from(e.dataTransfer.files));
        }
    });
}

// ── FORM HELPERS ───────────────────────────────────────────────────────────
function getFormDocument() {
    return {
        topic: (document.getElementById('topicInput')?.value || '').trim(),
        department: document.getElementById('entitySelect')?.value || '',
        date: document.getElementById('registrationDate')?.value || '',
        number: (document.getElementById('documentNumber')?.value || '').trim(),
        owner: document.body.getAttribute('data-user-full') || 'Current User',
    };
}

// Clear error highlight on a field when user starts filling it
function clearFieldError(el) {
    if (!el) return;
    el.classList.remove('field-error');
}

async function saveDocument() {
    // ── Prevent double-click / duplicate submissions ───────────────────────
    const saveBtn = document.querySelector('[onclick="saveDocument()"]');
    if (saveBtn) {
        if (saveBtn.disabled) return;          // already in-flight — bail out immediately
        saveBtn.disabled = true;
        saveBtn.dataset.origHtml = saveBtn.innerHTML;
        saveBtn.innerHTML = '<i class="ph ph-circle-notch" style="animation:spin .7s linear infinite"></i> <span>' +
            (currentLang === 'ar' ? 'جارٍ الحفظ…' : 'Saving…') + '</span>';
    }

    // Helper to restore the button on any exit path
    function _restoreBtn() {
        if (saveBtn) {
            saveBtn.disabled = false;
            saveBtn.innerHTML = saveBtn.dataset.origHtml ||
                '<i class="ph ph-floppy-disk"></i> <span>Save Document</span>';
            delete saveBtn.dataset.origHtml;
        }
    }

    const formDoc = getFormDocument();

    // ── Required field validation ─────────────────────────────────────────
    const docDate    = (document.getElementById('documentDate')?.value || '').trim();
    const topic      = (formDoc.topic || '').trim();
    const keywords   = (document.getElementById('keywordsInput')?.value || '').trim();
    const statement  = (document.getElementById('statementInput')?.value || '').trim();

    const fields = [
        { val: docDate,   id: 'docDateWrap',     labelEn: 'Document Date',   labelAr: 'تاريخ المستند' },
        { val: topic,     id: 'topicInput',       labelEn: 'Topic / Subject', labelAr: 'الموضوع'       },
        { val: keywords,  id: 'keywordsInput',    labelEn: 'Keywords',        labelAr: 'الكلمات المفتاحية' },
        { val: statement, id: 'statementInput',   labelEn: 'Statement / Notes', labelAr: 'البيان'     },
    ];

    // Clear previous error highlights
    fields.forEach(f => document.getElementById(f.id)?.classList.remove('field-error'));

    const missing = fields.filter(f => !f.val);
    if (missing.length) {
        // Highlight all missing fields
        missing.forEach(f => document.getElementById(f.id)?.classList.add('field-error'));
        const names = missing.map(f => currentLang === 'ar' ? f.labelAr : f.labelEn).join(', ');
        showToast(
            currentLang === 'ar'
                ? `يرجى ملء الحقول المطلوبة: ${names}`
                : `Required fields missing: ${names}`,
            'error'
        );
        // Focus the first missing field
        const firstEl = document.getElementById(missing[0].id);
        if (firstEl) { firstEl.scrollIntoView({ behavior: 'smooth', block: 'center' }); firstEl.focus(); }
        _restoreBtn();   // re-enable so user can fix and retry
        return;
    }

    const saveResult = await saveDocumentToDb();
    if (!saveResult || !saveResult.id) {
        _restoreBtn();   // re-enable on failure so user can retry
        return;
    }
    const id = saveResult.registration_number || ((REG_NUMBER_PREFIX + '-') + saveResult.id);

    const regNum = document.getElementById('registrationNumber');
    if (regNum) regNum.value = id;

    goToStep(4);

    // Clear the uploaded file queue now that they're saved
    _archivePendingFiles = []; _archiveFileDisplayNames = {}; _archiveUnsignedOriginals = {};
    _ocrUsedFileNames = new Set();
    _ocrTextByFileName = {};
    const fileInput = document.getElementById('fileInput');
    if (fileInput) fileInput.value = '';
    const fileList = document.getElementById('fileList');
    if (fileList) fileList.innerHTML = '';

    const attachMsg = saveResult.attachment_count
        ? (currentLang === 'ar' ? ` (${saveResult.attachment_count} مرفق)` : ` (${saveResult.attachment_count} attachment${saveResult.attachment_count > 1 ? 's' : ''})`)
        : '';
    // Re-enable and reset the button so the user can save again if needed
    _restoreBtn();

    // Show success popup modal
    showSaveSuccessModal(id, attachMsg, saveResult.id);
}

function showSaveSuccessModal(id, attachMsg, docId) {
    const isAr = currentLang === 'ar';
    // Remove any existing save success modal
    const existing = document.getElementById('saveSuccessModal');
    if (existing) existing.remove();

    const overlay = document.createElement('div');
    overlay.id = 'saveSuccessModal';
    overlay.style.cssText = `
        position: fixed; inset: 0; background: rgba(0,0,0,0.55);
        display: flex; align-items: center; justify-content: center;
        z-index: 9999; animation: fadeIn .2s ease;
    `;

    overlay.innerHTML = `
        <div style="
            background: var(--bg-card, #1e2533);
            border: 1px solid var(--accent, #4f8ef7);
            border-radius: 14px;
            padding: 36px 40px 28px;
            min-width: 320px;
            max-width: 420px;
            text-align: center;
            box-shadow: 0 8px 40px rgba(0,0,0,0.45);
            animation: slideUp .25s ease;
        ">
            <div style="font-size: 3rem; margin-bottom: 12px;">✅</div>
            <h3 style="margin: 0 0 8px; font-size: 1.2rem; color: var(--text-primary, #fff);">
                ${isAr ? 'تم حفظ المستند بنجاح' : 'Document Saved Successfully'}
            </h3>
            <p style="margin: 0 0 20px; color: var(--text-secondary, #a0aec0); font-size: 0.95rem;">
                ${isAr ? `رقم التسجيل: <strong style="color:var(--accent,#4f8ef7)">${id}</strong>${attachMsg}` 
                        : `Registration: <strong style="color:var(--accent,#4f8ef7)">${id}</strong>${attachMsg}`}
            </p>
            <button onclick="document.getElementById('saveSuccessModal').remove(); openEmailModal(${docId});" style="
                background: transparent;
                color: var(--accent, #4f8ef7);
                border: 1px solid var(--accent, #4f8ef7);
                border-radius: 8px;
                padding: 10px 20px;
                font-size: 0.95rem;
                cursor: pointer;
                font-weight: 600;
                margin-inline-end: 8px;
            "><i class="ph ph-envelope-simple"></i> ${isAr ? 'إرسال بالبريد' : 'Email'}</button>
            <button onclick="document.getElementById('saveSuccessModal').remove(); clearForm();" style="
                background: var(--accent, #4f8ef7);
                color: #fff;
                border: none;
                border-radius: 8px;
                padding: 10px 32px;
                font-size: 0.95rem;
                cursor: pointer;
                font-weight: 600;
            ">${isAr ? 'حسناً' : 'OK'}</button>
        </div>
    `;

    // Close on backdrop click
    overlay.addEventListener('click', e => {
        if (e.target === overlay) { overlay.remove(); clearForm(); }
    });

    document.body.appendChild(overlay);
}

function setRegistrationPlaceholder() {
    const regNum = document.getElementById('registrationNumber');
    if (!regNum) return;
    regNum.value = '';
    regNum.placeholder = currentLang === 'ar' ? 'يُولَّد عند الحفظ' : 'Assigned on save';
}

function clearForm() {
    // Reset edit state — if coming from "Edit" in inquiries, clear the edit ID and restore button label
    const form = document.getElementById('section-archive');
    if (form) delete form.dataset.editId;
    window._removedAttachmentIds = [];
    const saveBtn = document.querySelector('[onclick="saveDocument()"]');
    if (saveBtn) {
        saveBtn.innerHTML = '<i class="ph ph-floppy-disk"></i> <span>' + (currentLang === 'ar' ? 'حفظ المستند' : 'Save Document') + '</span>';
        delete saveBtn.dataset.isUpdate;
        saveBtn.disabled = false;
    }

    ['topicInput', 'documentDate', 'docDateY', 'docDateM', 'docDateD', 'documentNumber', 'keywordsInput', 'statementInput', 'expiryDate', 'shelfNumber'].forEach(id => {
        const f = document.getElementById(id);
        if (f) f.value = '';
    });

    // Reset selects to first option (Normal)
    ['importanceSelect', 'confidentialitySelect'].forEach(id => {
        const s = document.getElementById(id);
        if (s) s.selectedIndex = 0;
    });

    // Reset registration date to today and refresh hijri display
    const today = new Date();
    const todayFmt = `${today.getFullYear()}/${String(today.getMonth()+1).padStart(2,'0')}/${String(today.getDate()).padStart(2,'0')}`;
    const regDate = document.getElementById('registrationDate');
    if (regDate) regDate.value = todayFmt;
    updateHijriDisplay();

    // Reset folder (main) and subfolder selections
    const entitySel = document.getElementById('entitySelect');
    if (entitySel && entitySel.options.length) entitySel.selectedIndex = 0;

    const volumeInput = document.getElementById('volumeInput');
    if (volumeInput) {
        volumeInput.value = '';
        delete volumeInput.dataset.folderId;
        delete volumeInput.dataset.folderDeptId;
    }

    const selectedVolumeLabel = document.getElementById('selectedVolumeLabel');
    if (selectedVolumeLabel) {
        selectedVolumeLabel.textContent = currentLang === 'ar' ? 'لم يتم اختيار مجلد بعد' : 'No folder selected yet';
    }

    // Refresh subfolder options to match the reset entity selection
    updateVolumeOptions();

    _archivePendingFiles = []; _archiveFileDisplayNames = {}; _archiveUnsignedOriginals = {};
    _ocrUsedFileNames = new Set();
    _ocrTextByFileName = {};
    const fileInput = document.getElementById('fileInput');
    if (fileInput) fileInput.value = '';
    const fileList = document.getElementById('fileList');
    if (fileList) fileList.innerHTML = '';
    renderArchiveFileList();

    const existingWrap = document.getElementById('existingAttachmentsWrap');
    const existingList = document.getElementById('existingAttachmentsList');
    if (existingWrap) existingWrap.style.display = 'none';
    if (existingList) existingList.innerHTML = '';

    // Clear scanner / camera / network-scan queues, UI, status messages, and buttons
    netScanClearAll();          // clears _netScanPages, _scanFiles, _cameraPhotos + re-renders all
    _resetNetScanTab();         // hides "Next Page" btn, resets scan btn, clears status text + dot

    setRegistrationPlaceholder();

    // Reset wizard to step 1
    goToStep(1);

    // Clear custom fields zone
    const cfZone = document.getElementById('customFieldsZone');
    if (cfZone) cfZone.style.display = 'none';
    const cfBody = document.getElementById('customFieldsBody');
    if (cfBody) cfBody.innerHTML = '';

    showToast(currentLang === 'ar' ? 'جاهز لمستند جديد' : 'Ready for new document');
}

function downloadDocument() {
    const exportData = {
        registrationNumber: document.getElementById('registrationNumber')?.value || '',
        topic: document.getElementById('topicInput')?.value || 'Untitled',
        department: document.getElementById('entitySelect')?.value,
        registrationDate: document.getElementById('registrationDate')?.value,
        documentNumber: document.getElementById('documentNumber')?.value,
        importance: document.getElementById('importanceSelect')?.value,
        confidentiality: document.getElementById('confidentialitySelect')?.value,
        keywords: document.getElementById('keywordsInput')?.value,
        statement: document.getElementById('statementInput')?.value,
    };

    const blob = new Blob([JSON.stringify(exportData, null, 2)], { type: 'application/json' });
    const url = URL.createObjectURL(blob);
    const link = Object.assign(document.createElement('a'), { href: url, download: (exportData.registrationNumber || 'document') + '.json' });
    document.body.appendChild(link);
    link.click();
    link.remove();
    URL.revokeObjectURL(url);
    showToast(currentLang === 'ar' ? 'جاري التنزيل...' : 'Downloading...', 'success');
}

// ── FOLDER MODAL ───────────────────────────────────────────────────────────
let folderModalParentId = null;

// ── DROPDOWN OPTION CHIP HELPERS ───────────────────────────────────────────
// Manage per-dropdown option lists as individually removable chips.
// All data lives in the DOM (data-value on each chip row) so nothing is lost
// on re-render — the options only change when the user explicitly adds/removes.

function fbRenderOptionChip(feIndex, value) {
    const row = document.createElement('div');
    row.className = 'fb-opt-chip';
    row.setAttribute('data-value', value);
    row.innerHTML = `
        <span class="fb-opt-chip-text">${value.replace(/</g,'&lt;').replace(/>/g,'&gt;')}</span>
        <button type="button" class="fb-opt-chip-remove" title="Remove"
            onclick="fbRemoveOptionChip(this)"><i class="ph ph-x"></i></button>`;
    return row;
}

function fbSetOptionsList(feIndex, options) {
    const list = document.getElementById(`fbFe${feIndex}OptionsList`);
    if (!list) return;
    list.innerHTML = '';
    (options || []).forEach(v => {
        if (v && v.trim()) list.appendChild(fbRenderOptionChip(feIndex, v.trim()));
    });
}

function fbGetOptionsList(feIndex) {
    const list = document.getElementById(`fbFe${feIndex}OptionsList`);
    if (!list) return [];
    return Array.from(list.querySelectorAll('.fb-opt-chip'))
        .map(chip => chip.getAttribute('data-value') || '')
        .filter(Boolean);
}

function fbAddOption(feIndex) {
    const input = document.getElementById(`fbFe${feIndex}NewOpt`);
    if (!input) return;
    const val = input.value.trim();
    if (!val) { input.focus(); return; }
    // Prevent duplicates
    const existing = fbGetOptionsList(feIndex);
    if (existing.includes(val)) {
        input.select();
        showToast(currentLang === 'ar' ? 'هذا الخيار موجود بالفعل' : 'Option already exists', 'warning');
        return;
    }
    const list = document.getElementById(`fbFe${feIndex}OptionsList`);
    if (list) list.appendChild(fbRenderOptionChip(feIndex, val));
    input.value = '';
    input.focus();
}

function fbRemoveOptionChip(btn) {
    btn.closest('.fb-opt-chip')?.remove();
}

// ── FOLDER BUILDER (main folders) ──────────────────────────────────────────
// Fe1–Fe3 are dropdowns — options stored in Sys_DP_DL (FieldName = actual label from Fe1Name/Fe2Name/Fe3Name)
// Fe4–Fe7 are text boxes.
// DB columns on Sys_Department: Fe1Name … Fe7Name  (label the admin gives each field)
// DB table Sys_DP_DL: Dept_Id, FieldName (=label), OptionOrder, OptionValue
// DB columns on Adco_Transactions: Fe1 … Fe7 (value entered per document)

let _fbEntityId = null;

function openFolderBuilder(entityId) {
    _fbEntityId = entityId;
    const isEdit = entityId !== null && entityId !== undefined;
    const entity = isEdit ? allEntities.find(e => e.id === entityId) : null;

    document.getElementById('fbPageTitle').textContent = isEdit
        ? (currentLang === 'ar' ? 'تعديل المجلد: ' + (entity?.name || '') : 'Edit Folder: ' + (entity?.name || ''))
        : (currentLang === 'ar' ? 'مجلد رئيسي جديد' : 'New Main Folder');
    document.getElementById('fbSaveBtnLabel').textContent = isEdit
        ? (currentLang === 'ar' ? 'حفظ التغييرات' : 'Save Changes')
        : (currentLang === 'ar' ? 'إنشاء المجلد' : 'Create Folder');
    document.getElementById('fbBackLabel').textContent = currentLang === 'ar' ? 'رجوع' : 'Back';
    document.getElementById('fbNameInput').value = entity?.name || '';

    // Clear all fields first
    for (let i = 1; i <= 7; i++) {
        const nameEl = document.getElementById(`fbFe${i}Name`);
        if (nameEl) nameEl.value = '';
        if (i <= 3) {
            fbSetOptionsList(i, []);
            const newInput = document.getElementById(`fbFe${i}NewOpt`);
            if (newInput) newInput.value = '';
        }
    }

    // If editing, load field labels and dropdown options from DB
    if (isEdit && entityId) {
        Promise.all([
            fetch(`/api/entities/${entityId}/fields`).then(r => r.ok ? r.json() : {}),
            fetch(`/api/entities/${entityId}/dropdown-options`).then(r => r.ok ? r.json() : {})
        ]).then(([fields, options]) => {
            for (let i = 1; i <= 7; i++) {
                const nameEl = document.getElementById(`fbFe${i}Name`);
                if (nameEl) nameEl.value = fields[`Fe${i}Name`] || '';
                if (i <= 3) {
                    const opts = (options[`Fe${i}`] || {}).options || [];
                    fbSetOptionsList(i, opts);
                }
            }
        }).catch(() => {});
    }

    document.getElementById('folderBuilderOverlay').style.display = 'flex';
    document.getElementById('fbNameInput').focus();
}

function closeFolderBuilder() {
    document.getElementById('folderBuilderOverlay').style.display = 'none';
    _fbEntityId = null;
}

async function saveFolderBuilder() {
    const nameInput = document.getElementById('fbNameInput');
    const name = nameInput.value.trim();
    if (!name) {
        nameInput.focus();
        nameInput.classList.add('field-error');
        showToast(currentLang === 'ar' ? 'أدخل اسم المجلد' : 'Enter a folder name', 'error');
        return;
    }
    nameInput.classList.remove('field-error');

    const isEdit = _fbEntityId !== null;

    try {
        let entityId = _fbEntityId;

        if (isEdit) {
            // Rename the entity if name changed
            const entity = allEntities.find(e => e.id === entityId);
            if (entity && entity.name !== name) {
                const res = await fetch(`/api/entities/${entityId}`, {
                    method: 'PATCH',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ name })
                });
                const data = await res.json();
                if (!res.ok || !data.success) {
                    showToast((currentLang === 'ar' ? 'تعذر تعديل الاسم: ' : 'Failed to rename: ') + (data.error || ''), 'error');
                    return;
                }
            }
        } else {
            // Create new entity
            const res = await fetch('/api/entities', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ name })
            });
            const data = await res.json();
            if (!res.ok || !data.success) {
                showToast((currentLang === 'ar' ? 'تعذر إنشاء المجلد: ' : 'Failed to create folder: ') + (data.error || 'Unknown error'), 'error');
                return;
            }
            entityId = data.id || data.entity_id;
        }

        // Build field names payload for DB
        const fieldsPayload = {};
        for (let i = 1; i <= 7; i++) {
            const nameEl = document.getElementById(`fbFe${i}Name`);
            fieldsPayload[`Fe${i}Name`] = nameEl ? nameEl.value.trim() : '';
        }

        // Save field names to DB
        const fieldsRes = await fetch(`/api/entities/${entityId}/fields`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(fieldsPayload)
        });
        const fieldsData = await fieldsRes.json();
        if (!fieldsRes.ok || !fieldsData.success) {
            showToast((currentLang === 'ar' ? 'تعذر حفظ الحقول: ' : 'Failed to save fields: ') + (fieldsData.error || ''), 'error');
            return;
        }

        // Save dropdown options to Sys_DP_DL (FieldName = actual label from Fe1Name/Fe2Name/Fe3Name)
        const dropdownPayload = {};
        for (let i = 1; i <= 3; i++) {
            dropdownPayload[`Fe${i}`] = fbGetOptionsList(i);
        }
        await fetch(`/api/entities/${entityId}/dropdown-options`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(dropdownPayload)
        });

        closeFolderBuilder();
        await loadEntities();

        // Invalidate the custom fields cache for this entity so the archiving
        // page re-fetches from the DB next time a folder in it is selected.
        delete _feFieldsCache[entityId];

        // If the saved entity is currently selected in the inquiry filter bar,
        // refresh its custom Fe filters immediately — no page reset needed.
        const activeDept = (document.getElementById('adv-dept')?.value || '').trim();
        if (activeDept && String(entityId) === activeDept) {
            _buildCustomFeFilters(activeDept);
        }

        // If the archiving form currently has this entity's folder selected,
        // re-render the custom fields zone live.
        const archiveFolderSel = document.getElementById('folderSelect');
        if (archiveFolderSel?.value) {
            const selectedFolder = allFoldersByDept[entityId]?.find(
                f => String(f.id) === String(archiveFolderSel.value)
            );
            if (selectedFolder) renderCustomFieldsZone(entityId);
        }

        showToast(
            isEdit
                ? (currentLang === 'ar' ? 'تم حفظ التغييرات' : 'Changes saved')
                : (currentLang === 'ar' ? 'تم إنشاء المجلد: ' + name : 'Folder created: ' + name),
            'success'
        );
    } catch (e) {
        showToast(currentLang === 'ar' ? 'خطأ أثناء الحفظ' : 'Error saving folder', 'error');
    }
}

// ── Keep original promptCreateFolder for sub-folders (unchanged logic) ──────
function promptCreateFolder(parentId, deptId = null) {
    folderModalParentId = parentId;
    folderModalDeptId = deptId;
    const modal = document.getElementById('folderModal');
    const title = document.getElementById('folderModalTitle');
    const input = document.getElementById('folderModalInput');
    if (!modal || !title || !input) return;

    if (parentId !== null && deptId) {
        const parent = (allFoldersByDept[deptId] || []).find(f => f.id === parentId);
        title.textContent = currentLang === 'ar'
            ? 'مجلد فرعي في: ' + (parent ? parent.name : '')
            : 'Subfolder in: ' + (parent ? parent.name : '');
    } else {
        title.textContent = currentLang === 'ar' ? 'مجلد فرعي جديد' : 'New subfolder';
    }

    input.value = '';
    modal.style.display = 'grid';
    setTimeout(() => input.focus(), 60);
}

function closeFolderModal() {
    const modal = document.getElementById('folderModal');
    if (modal) modal.style.display = 'none';
    folderModalParentId = null;
    folderModalDeptId = null;
}

async function confirmFolderModal() {
    const input = document.getElementById('folderModalInput');
    if (!input) return;
    const name = input.value.trim();
    if (!name) {
        input.focus();
        showToast(currentLang === 'ar' ? 'أدخل اسم المجلد' : 'Enter a folder name', 'error');
        return;
    }

    try {
        const isMainCreation = folderModalParentId === null;
        let res;
        if (isMainCreation) {
            res = await fetch('/api/entities', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ name })
            });
        } else {
            const entityId = parseInt(folderModalDeptId || '0', 10);
            if (!entityId) {
                showToast(currentLang === 'ar' ? 'اختر المجلد الرئيسي أولاً' : 'Select a main folder first', 'error');
                return;
            }
            // Resolve the real Dept_ID (e.g. 46, 53) from the entity list
            const entity = allEntities.find(e => e.id === entityId);
            const realDeptId = entity ? entity.dept_id : entityId;
            res = await fetch('/api/folders', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({
                    name,
                    dept_id: realDeptId,
                    parent_id: folderModalParentId || 0
                })
            });
        }
        const data = await res.json();
        if (!res.ok || !data.success) {
            showToast((currentLang === 'ar' ? 'تعذر إنشاء المجلد: ' : 'Failed to create folder: ') + (data.error || 'Unknown error'), 'error');
            return;
        }

        closeFolderModal();
        await loadEntities();
        if (!isMainCreation) {
            await updateVolumeOptions();
            const entity = allEntities.find(e => e.id === parseInt(folderModalDeptId || '0', 10));
            const realDeptId = entity ? entity.dept_id : 0;
            selectVolume(name, data.id, realDeptId);

            // Auto-expand parent folder in the tree so the new subfolder is visible
            const parentFolderId = folderModalParentId;
            if (parentFolderId) {
                const parentChildren = document.getElementById(`folder-children-${parentFolderId}`);
                const parentArrow    = document.getElementById(`farrow-${parentFolderId}`);
                if (parentChildren) { parentChildren.style.display = 'block'; }
                if (parentArrow)    { parentArrow.textContent = '▼'; }
            } else {
                // Subfolder of a dept root — expand the dept
                const deptChildren = document.getElementById(`dept-children-${folderModalDeptId}`);
                const deptArrow    = document.getElementById(`arrow-${folderModalDeptId}`);
                if (deptChildren) { deptChildren.style.display = 'block'; }
                if (deptArrow)    { deptArrow.textContent = '▼'; }
            }
        }
        showToast(currentLang === 'ar' ? 'تم إنشاء المجلد: ' + name : 'Folder created: ' + name, 'success');
    } catch (e) {
        showToast(currentLang === 'ar' ? 'خطأ أثناء إنشاء المجلد' : 'Error creating folder', 'error');
    }
}

// ── INQUIRIES: Advanced Search (Task 2) ───────────────────────────────────
let searchDebounceTimer = null;
let _inquirySearchSeq = 0;
let _inquiryAbort = null;
const INQUIRY_FETCH_PAGE_SIZE = 500;

/** Previous calendar year Jan 1 → today (inclusive). */
function getInquiryYearRange() {
    const now = new Date();
    const y = now.getFullYear();
    const py = y - 1;
    const pad = n => String(n).padStart(2, '0');
    return {
        from: `${py}-01-01`,
        to: `${y}-${pad(now.getMonth() + 1)}-${pad(now.getDate())}`,
    };
}

function applyInquiryDefaultDates() {
    const { from, to } = getInquiryYearRange();
    const fromEl = document.getElementById('adv-date-from');
    const toEl   = document.getElementById('adv-date-to');
    if (fromEl) fromEl.value = from;
    if (toEl)   toEl.value = to;
}

function clearInquiryDateInputs() {
    const fromEl = document.getElementById('adv-date-from');
    const toEl   = document.getElementById('adv-date-to');
    if (fromEl) fromEl.value = '';
    if (toEl)   toEl.value = '';
}

function hasInquiryTextFilters() {
    const regNumVal = normalizeRegNumberInput(
        (document.getElementById('adv-reg-number')?.value || '').trim()
    );
    return !!(
        regNumVal ||
        (document.getElementById('adv-doc-number')?.value || '').trim() ||
        (document.getElementById('adv-topic')?.value || '').trim() ||
        (document.getElementById('adv-keywords')?.value || '').trim() ||
        (document.getElementById('adv-notes')?.value || '').trim() ||
        (document.getElementById('adv-statement')?.value || '').trim()
        // Note: dept/folder do NOT clear the date range — they work alongside it
    );
}

/** Empty dates when filtering (all years); fill default range only for browse (no text filters). */
function syncInquiryDateFields() {
    if (hasInquiryTextFilters()) {
        clearInquiryDateInputs();
    } else {
        const fromEl = document.getElementById('adv-date-from');
        const toEl   = document.getElementById('adv-date-to');
        if (!fromEl?.value && !toEl?.value) {
            applyInquiryDefaultDates();
        }
    }
}

function onInquiryFilterInput() {
    syncInquiryDateFields();
    renderSearch();
}

function renderSearch() {
    clearTimeout(searchDebounceTimer);
    searchDebounceTimer = setTimeout(doAdvancedSearch, 400);
}

function runInquirySearchNow() {
    clearTimeout(searchDebounceTimer);
    doAdvancedSearch();
}

function onInquiryFilterKeydown(e) {
    if (e.key === 'Enter') {
        e.preventDefault();
        runInquirySearchNow();
    }
}

/** Strip configured registration-number prefix; keep digits for API. */
function normalizeRegNumberInput(val) {
    let s = (val || '').trim();
    const _prefixRe = new RegExp('^' + REG_NUMBER_PREFIX + '[-\\s]*', 'i');
    if (_prefixRe.test(s)) s = s.replace(_prefixRe, '').trim();
    const digits = s.replace(/\D/g, '');
    return digits || s;
}

/** Build query params from filter bar (single source of truth). */
function buildInquiryParams() {
    const params = new URLSearchParams();
    const addParam = (key, elId) => {
        const val = (document.getElementById(elId)?.value || '').trim();
        if (val) params.set(key, val);
    };

    const regRaw = (document.getElementById('adv-reg-number')?.value || '').trim();
    const regNumVal = normalizeRegNumberInput(regRaw);
    const docNum = (document.getElementById('adv-doc-number')?.value || '').trim();
    const topicVal = (document.getElementById('adv-topic')?.value || '').trim();
    const kwVal = (document.getElementById('adv-keywords')?.value || '').trim();
    const notesVal = (document.getElementById('adv-notes')?.value || '').trim();
    const stmtVal = (document.getElementById('adv-statement')?.value || '').trim();
    const hasTextFilters = !!(regNumVal || docNum || topicVal || kwVal || notesVal || stmtVal);

    if (regNumVal) {
        params.set('reg_number', regNumVal);
        // Full ID match for long numbers; short values (e.g. "93") → partial match
        if (regNumVal.length >= 5) {
            params.set('reg_number_exact', '1');
        }
    }

    addParam('doc_number', 'adv-doc-number');
    addParam('topic', 'adv-topic');
    addParam('keywords', 'adv-keywords');
    addParam('notes', 'adv-notes');
    addParam('statement', 'adv-statement');

    const searchAllYears = hasTextFilters;
    const regExact = regNumVal.length >= 5;

    let dateFrom = '';
    let dateTo   = '';
    if (searchAllYears) {
        // All years — never send dates (date inputs stay empty in the UI)
        params.set('skip_default_dates', '1');
    } else {
        dateFrom = (document.getElementById('adv-date-from')?.value || '').trim();
        dateTo   = (document.getElementById('adv-date-to')?.value   || '').trim();
        if (!dateFrom && !dateTo) {
            const def = getInquiryYearRange();
            dateFrom = def.from;
            dateTo   = def.to;
        }
        if (dateFrom) params.set('reg_date_from', dateFrom);
        if (dateTo)   params.set('reg_date_to', dateTo);
    }

    const deptVal   = (document.getElementById('adv-dept')?.value   || '').trim();
    const folderVal = (document.getElementById('adv-folder')?.value || '').trim();
    if (deptVal)   params.set('dept_id',   deptVal);
    if (folderVal) params.set('folder_id', folderVal);

    // Custom Fe1–Fe7 field filters (only present when a dept is selected)
    for (let i = 1; i <= 7; i++) {
        const el = document.getElementById(`adv-fe${i}`);
        if (!el) continue;
        const v = (el.value || '').trim();
        if (v) params.set(`fe${i}`, v);
    }

    return { params, regNumVal, regExact, docNum, topicVal, kwVal, notesVal, stmtVal,
             dateFrom, dateTo, searchAllYears };
}

/** Client-side safety net so results always match visible filter fields. */
function applyClientInquiryFilters(docs, filters) {
    let out = docs || [];
    const { regNumVal, regExact, docNum, topicVal, kwVal, notesVal, stmtVal } = filters;
    const contains = (hay, needle) => String(hay || '').toLowerCase().includes(needle);

    // Exact reg # already matched on the server
    if (regExact) {
        return out;
    }

    if (regNumVal) {
        out = out.filter(d => {
            const reg = String(d.registration_number ?? d.id ?? '');
            return reg.startsWith(regNumVal);
        });
    }
    if (docNum) {
        const n = docNum.toLowerCase();
        out = out.filter(d => contains(d.doc_number, n));
    }
    if (topicVal) {
        const n = topicVal.toLowerCase();
        out = out.filter(d => contains(d.subject, n) || contains(d.keywords, n) || contains(d.notes, n));
    }
    if (kwVal) {
        const n = kwVal.toLowerCase();
        out = out.filter(d => contains(d.keywords, n) || contains(d.subject, n));
    }
    if (notesVal) {
        const n = notesVal.toLowerCase();
        out = out.filter(d => contains(d.notes, n));
    }
    if (stmtVal) {
        const n = stmtVal.toLowerCase();
        out = out.filter(d => {
            if (contains(d.notes, n)) return true;
            if (d.ocr_snippet) return true; // server already matched this via OCR cache
            return (d.attachments || []).some(a =>
                contains(a.file_name, n) || contains(a.description, n)
            );
        });
    }

    // Folder / department client-side filter (belt-and-suspenders — server also filters)
    const folderVal = (document.getElementById('adv-folder')?.value || '').trim();
    const deptVal   = (document.getElementById('adv-dept')?.value   || '').trim();
    if (folderVal) {
        const fid = parseInt(folderVal, 10);
        out = out.filter(d => d.folder_id === fid || String(d.folder_id) === folderVal);
    } else if (deptVal) {
        const entityId = parseInt(deptVal, 10);
        const deptFolderIds = new Set((allFoldersByDept[entityId] || []).map(f => f.id));
        if (deptFolderIds.size > 0) {
            out = out.filter(d => deptFolderIds.has(d.folder_id));
        }
    }

    // Fe1–Fe7 custom field client-side safety net
    for (let i = 1; i <= 7; i++) {
        const el = document.getElementById(`adv-fe${i}`);
        if (!el) continue;
        const v = (el.value || '').trim().toLowerCase();
        if (!v) continue;
        const feKey = `Fe${i}`;
        if (i <= 3) {
            // Dropdown — exact match
            out = out.filter(d => (d[feKey] || '').toLowerCase() === v);
        } else {
            // Text — contains
            out = out.filter(d => (d[feKey] || '').toLowerCase().includes(v));
        }
    }

    return out;
}

// Called specifically when a date input changes — validates pair before triggering search
function handleDateFilter() {
    if (hasInquiryTextFilters()) {
        clearInquiryDateInputs();
        renderSearch();
        return;
    }

    const dateFrom = (document.getElementById('adv-date-from')?.value || '').trim();
    const dateTo   = (document.getElementById('adv-date-to')?.value   || '').trim();
    const hintEl   = document.getElementById('adv-date-hint');

    if ((dateFrom && !dateTo) || (!dateFrom && dateTo)) {
        // One filled, one empty — show hint, don't search yet
        if (hintEl) {
            hintEl.textContent = currentLang === 'ar'
                ? '⚠ حدد تاريخ البداية والنهاية'
                : '⚠ Fill both dates';
            hintEl.style.color = 'var(--danger, #e05)';
        }
        return;
    }
    // Both filled or both empty — clear hint and search
    if (hintEl) hintEl.textContent = '';
    renderSearch();
}

function clearAdvancedFilters() {
    ['adv-reg-number','adv-doc-number','adv-topic','adv-keywords','adv-notes','adv-statement'].forEach(id => {
        const el = document.getElementById(id);
        if (el) el.value = '';
    });
    currentSearchStatement = '';
    const deptSel   = document.getElementById('adv-dept');
    const folderSel = document.getElementById('adv-folder');
    if (deptSel)   { deptSel.value = '';   _populateInqFolderSelect(''); }
    if (folderSel)   folderSel.value = '';
    // Clear and remove custom Fe filters
    const customContainer = document.getElementById('inq-custom-filters');
    if (customContainer) customContainer.innerHTML = '';
    _lastFeConfigDeptId = null;
    _lastFeConfig = null;
    // Explicit clear button: always reset dates to the default browse range,
    // regardless of what was previously selected (syncInquiryDateFields()
    // only fills defaults when the fields are already empty, so it silently
    // no-ops here if a custom date range was set — that's the bug this fixes).
    clearInquiryDateInputs();
    applyInquiryDefaultDates();
    const hintEl = document.getElementById('adv-date-hint');
    if (hintEl) hintEl.textContent = '';
    const countEl = document.getElementById('searchCount');
    if (countEl) countEl.textContent = '';
    const exportBtn = document.getElementById('exportExcelBtn');
    if (exportBtn) exportBtn.style.display = 'none';
    renderSearch();
}

// ── Pagination state ─────────────────────────────────────────────────────
const RESULTS_PER_PAGE = 10;
let _searchPage    = 1;
let _searchAllDocs = [];   // full result set
let currentSearchStatement = '';   // last "Document Content" (OCR) search term, for snippet highlighting

function searchGoPage(page) {
    const total = Math.ceil(_searchAllDocs.length / RESULTS_PER_PAGE);
    if (page < 1 || page > total) return;
    _searchPage = page;
    _renderSearchPage();
}

function _renderSearchPage() {
    const results = document.getElementById('searchResults');
    const pagination = document.getElementById('searchPagination');
    const pageInfo   = document.getElementById('pageInfo');
    const pagePrev   = document.getElementById('pagePrev');
    const pageNext   = document.getElementById('pageNext');
    if (!results) return;

    const total    = Math.ceil(_searchAllDocs.length / RESULTS_PER_PAGE);
    const start    = (_searchPage - 1) * RESULTS_PER_PAGE;
    const pageData = _searchAllDocs.slice(start, start + RESULTS_PER_PAGE);

    if (_searchView === 'table') {
        const offset = (_searchPage - 1) * RESULTS_PER_PAGE;
        results.innerHTML = renderSearchTable(pageData, offset);
        results.classList.add('search-results--table');
        results.classList.remove('search-results--cards');
    } else {
        results.innerHTML = pageData.map(doc => renderSearchCard(doc)).join('');
        results.classList.add('search-results--cards');
        results.classList.remove('search-results--table');
    }
    // Re-enforce access rights every time results are rendered
    if (typeof applyAccr === 'function') applyAccr();

    if (!_searchAllDocs.length) {
        if (pagination) pagination.style.display = 'none';
        return;
    }

    if (_searchAllDocs.length > RESULTS_PER_PAGE) {
        if (pagination) pagination.style.display = '';
        if (pageInfo)   pageInfo.textContent = currentLang === 'ar'
            ? `${_searchPage} / ${total} — ${_searchAllDocs.length} نتيجة`
            : `Page ${_searchPage} of ${total} — ${_searchAllDocs.length} results`;
        if (pagePrev)   pagePrev.disabled = _searchPage <= 1;
        if (pageNext)   pageNext.disabled = _searchPage >= total;
    } else {
        if (pagination) pagination.style.display = 'none';
    }
}

function hideSearchPagination() {
    const pagination = document.getElementById('searchPagination');
    if (pagination) pagination.style.display = 'none';
}

async function fetchAllInquiryResults(baseParams, signal, seq) {
    const all = [];
    let total = 0;
    let suggestions = [];
    let afterId = null;
    let first = true;

    // Keyset pagination: each page seeks past the last ID we saw
    // (after_id) instead of using OFFSET, which stays fast at any depth
    // instead of getting slower page after page. We only ask for a
    // COUNT(*) on the very first request — the total doesn't change
    // between pages, so there's no reason to keep re-computing it.
    do {
        if (seq !== _inquirySearchSeq) {
            return { results: [], total: 0, cancelled: true };
        }
        const params = new URLSearchParams(baseParams);
        params.set('page_size', String(INQUIRY_FETCH_PAGE_SIZE));
        if (afterId != null) {
            params.set('after_id', String(afterId));
        }
        const res = await fetch('/api/documents/advanced-search?' + params.toString(), { signal });
        const data = await res.json();
        if (!res.ok || data.error) throw new Error(data.error || `HTTP ${res.status}`);
        if (first) {
            total = data.total ?? 0;
            if (data.suggestions?.length) suggestions = data.suggestions;
            first = false;
        }
        if (data.results?.length) all.push(...data.results);
        afterId = data.next_after_id ?? null;
    } while (afterId != null);

    return { results: all, total, suggestions, cancelled: false };
}

async function doAdvancedSearch() {
    const results = document.getElementById('searchResults');
    if (!results) return;

    const hintEl = document.getElementById('adv-date-hint');
    const built = buildInquiryParams();
    const { params, dateFrom, dateTo, searchAllYears } = built;
    currentSearchStatement = built.stmtVal || '';

    if (hintEl && searchAllYears) {
        hintEl.textContent = currentLang === 'ar'
            ? 'بحث مع فلاتر — كل السنوات'
            : 'Filtered search — all years';
        hintEl.style.color = 'var(--muted, #888)';
    } else if (hintEl && dateFrom && dateTo) {
        hintEl.textContent = currentLang === 'ar'
            ? `عرض ${dateFrom} → ${dateTo}`
            : `Showing ${dateFrom} → ${dateTo}`;
        hintEl.style.color = 'var(--muted, #888)';
    }

    if (!searchAllYears && ((dateFrom && !dateTo) || (!dateFrom && dateTo))) {
        if (hintEl) {
            hintEl.textContent = currentLang === 'ar'
                ? '⚠ حدد تاريخ البداية والنهاية'
                : '⚠ Fill both dates';
            hintEl.style.color = 'var(--danger, #e05)';
        }
        return;
    }

    syncInquiryDateFields();

    const seq = ++_inquirySearchSeq;
    _inquiryAbort?.abort();
    _inquiryAbort = new AbortController();

    const loadingMsg = currentLang === 'ar' ? 'جارٍ البحث...' : 'Searching...';
    results.innerHTML = `<div class="search-loading">${loadingMsg}</div>`;
    hideSearchPagination();
    _searchAllDocs = [];
    _searchPage = 1;

    try {
        const { results: allResults, total, suggestions, cancelled } = await fetchAllInquiryResults(
            params, _inquiryAbort.signal, seq
        );
        if (cancelled || seq !== _inquirySearchSeq) return;

        const filtered = applyClientInquiryFilters(allResults, built);
        const countEl = document.getElementById('searchCount');

        if (!filtered.length) {
            if (countEl) countEl.textContent = `0 ${currentLang === 'ar' ? 'نتيجة' : 'result(s)'}`;
            const noResultsMsg = currentLang === 'ar' ? 'لا توجد نتائج' : 'No results found';
            let suggestionsHtml = '';
            if (suggestions && suggestions.length) {
                const label = currentLang === 'ar' ? 'هل تقصد؟' : 'Did you mean:';
                const chips = suggestions.map(s => {
                    const safe = _escapeHtml(String(s));
                    return `<button type="button" class="did-you-mean-chip" onclick="applyDidYouMean('${safe.replace(/'/g, "\\'")}')">${safe}</button>`;
                }).join('');
                suggestionsHtml = `<div class="did-you-mean-row"><span class="did-you-mean-label">${label}</span>${chips}</div>`;
            }
            results.innerHTML = `<div class="search-empty-hint">${noResultsMsg}</div>${suggestionsHtml}`;
            const exportBtn = document.getElementById('exportExcelBtn');
            if (exportBtn) exportBtn.style.display = 'none';
            _searchAllDocs = [];
            _searchPage = 1;
            hideSearchPagination();
            return;
        }

        if (countEl) {
            countEl.textContent = `${filtered.length} ${currentLang === 'ar' ? 'نتيجة' : 'result(s)'}`;
        }
        _searchPage    = 1;
        _searchAllDocs = filtered;
        _renderSearchPage();
        const exportBtn = document.getElementById('exportExcelBtn');
        if (exportBtn) exportBtn.style.display = '';

    } catch (e) {
        if (e.name === 'AbortError' || seq !== _inquirySearchSeq) return;
        results.innerHTML = `<article class="search-error"><strong><i class="ph ph-warning"></i> ${e.message || (currentLang === 'ar' ? 'خطأ في الاتصال' : 'Connection error')}</strong></article>`;
    }
}

// Also keep the legacy renderSearch-driven doSearch (for the basic searchInput bar if used elsewhere)
async function doSearch() { doAdvancedSearch(); }

function applyDidYouMean(term) {
    const topicInput = document.getElementById('adv-topic');
    if (topicInput) topicInput.value = term;
    doAdvancedSearch();
}

// ── Dashboard stat cards (real data from DB) ──────────────────────────────
async function loadStats() {
    try {
        const res  = await fetch('/api/stats');
        const data = await res.json();
        if (data.error) return;
        const animate = (id, value) => {
            const el = document.getElementById(id);
            if (!el) return;
            const target = parseInt(value, 10) || 0;
            const duration = 800;
            const start = performance.now();
            const tick = (now) => {
                const progress = Math.min((now - start) / duration, 1);
                const ease = 1 - Math.pow(1 - progress, 3);
                el.textContent = Math.round(ease * target).toLocaleString();
                if (progress < 1) requestAnimationFrame(tick);
            };
            requestAnimationFrame(tick);
        };
        animate('statTotalDocs',        data.total_docs);
        animate('statThisMonth',        data.this_month);
        animate('statTotalFolders',     data.total_folders);
        animate('statTotalAttachments', data.total_attachments);
    } catch (e) { /* silently fail — cards stay as — */ }
}

// ── Export search results to Excel ───────────────────────────────────────
function exportSearchToExcel() {
    if (!_searchAllDocs || !_searchAllDocs.length) {
        showToast(currentLang === 'ar' ? 'لا توجد نتائج للتصدير' : 'No results to export', 'error');
        return;
    }
    const isAr = currentLang === 'ar';
    const headers = isAr
        ? ['رقم التسجيل', 'رقم المستند', 'التاريخ الميلادي', 'التاريخ الهجري', 'المجلد', 'الموضوع', 'الكلمات المفتاحية', 'البيان / الملاحظات', 'الأهمية', 'السرية', 'المرفقات']
        : ['Reg No.', 'Doc No.', 'Date', 'Hijri Date', 'Folder', 'Subject', 'Keywords', 'Notes', 'Importance', 'Confidentiality', 'Attachments'];
    const importanceLabel = id => ({1: isAr?'عادي':'Normal', 2: isAr?'مهم':'Important', 3: isAr?'عاجل':'Urgent'})[id] || '';
    const secretLabel     = id => ({1: isAr?'عادي':'Normal', 2: isAr?'سري':'Confidential', 3: isAr?'سري للغاية':'High Confidential'})[id] || '';
    const rows = _searchAllDocs.map(doc => {
        const atts = (doc.attachments || []).map(a => a.file_name || '').filter(Boolean).join(', ') || (doc.file_name || '');
        return [doc.registration_number||'', doc.doc_number||'', doc.date||'', doc.hijri_date||'',
                doc.folder_name||'', doc.subject||'', doc.keywords||'', doc.notes||'',
                importanceLabel(doc.importance_id), secretLabel(doc.secret_id), atts];
    });
    const ws = XLSX.utils.aoa_to_sheet([headers, ...rows]);
    ws['!cols'] = [{wch:14},{wch:12},{wch:14},{wch:14},{wch:22},{wch:40},{wch:24},{wch:36},{wch:12},{wch:16},{wch:30}];
    const wb = XLSX.utils.book_new();
    XLSX.utils.book_append_sheet(wb, ws, isAr ? 'نتائج البحث' : 'Search Results');
    const now = new Date();
    const stamp = `${now.getFullYear()}${String(now.getMonth()+1).padStart(2,'0')}${String(now.getDate()).padStart(2,'0')}`;
    XLSX.writeFile(wb, `DocPortal_Export_${stamp}.xlsx`);
    showToast(isAr ? `✓ تم تصدير ${_searchAllDocs.length} نتيجة` : `✓ Exported ${_searchAllDocs.length} result(s)`, 'success');
}

// ── Search view toggle (cards / table) ────────────────────────────────────
let _searchView = 'cards'; // 'cards' | 'table'

function setSearchView(view) {
    _searchView = view;
    document.getElementById('viewBtnCards')?.classList.toggle('inq-view-btn--active', view === 'cards');
    document.getElementById('viewBtnTable')?.classList.toggle('inq-view-btn--active', view === 'table');
    _renderSearchPage();
}

// ── BULK SELECTION ───────────────────────────────────────────────────────
let _bulkSelected = new Set(); // Set of doc IDs currently checked

function _bulkToggle(id, checked) {
    if (checked) _bulkSelected.add(id); else _bulkSelected.delete(id);
    _bulkSyncHeaderCb();
    _bulkBarUpdate();
}

function _bulkToggleAll(checked) {
    document.querySelectorAll('.bulk-cb').forEach(cb => {
        cb.checked = checked;
        const id = parseInt(cb.dataset.id, 10);
        if (checked) _bulkSelected.add(id); else _bulkSelected.delete(id);
    });
    _bulkBarUpdate();
}

function _bulkSyncHeaderCb() {
    const hdr = document.getElementById('bulkSelectAllCb');
    if (!hdr) return;
    const all = document.querySelectorAll('.bulk-cb');
    const checkedCount = [...all].filter(cb => cb.checked).length;
    hdr.checked = all.length > 0 && checkedCount === all.length;
    hdr.indeterminate = checkedCount > 0 && checkedCount < all.length;
}

function _bulkBarUpdate() {
    const bar = document.getElementById('bulkActionBar');
    if (!bar) return;
    const n = _bulkSelected.size;
    bar.style.display = n > 0 ? 'flex' : 'none';
    const lbl = bar.querySelector('.bulk-bar-count');
    if (lbl) {
        const isAr = currentLang === 'ar';
        lbl.textContent = isAr ? `${n} محدد` : `${n} selected`;
    }
}

function bulkExportSelected() {
    const docs = _searchAllDocs.filter(d => _bulkSelected.has(d.id));
    if (!docs.length) return;
    const isAr = currentLang === 'ar';
    const headers = isAr
        ? ['رقم التسجيل', 'رقم المستند', 'التاريخ', 'المجلد', 'الموضوع', 'الكلمات المفتاحية', 'البيان', 'الأهمية', 'السرية']
        : ['Reg No.', 'Doc No.', 'Date', 'Folder', 'Subject', 'Keywords', 'Notes', 'Importance', 'Confidentiality'];
    const importanceLabel = id => ({1: isAr?'عادي':'Normal', 2: isAr?'مهم':'Important', 3: isAr?'عاجل':'Urgent'})[id] || '';
    const secretLabel     = id => ({1: isAr?'عادي':'Normal', 2: isAr?'سري':'Confidential', 3: isAr?'سري للغاية':'High Confidential'})[id] || '';
    const rows = docs.map(d => [
        d.registration_number || '', d.doc_number || '', d.date || '',
        d.folder_name || '', d.subject || '', d.keywords || '',
        d.notes || '', importanceLabel(d.importance_id), secretLabel(d.secret_id),
    ]);
    const ws = XLSX.utils.aoa_to_sheet([headers, ...rows]);
    ws['!cols'] = headers.map(() => ({ wch: 20 }));
    const wb = XLSX.utils.book_new();
    XLSX.utils.book_append_sheet(wb, ws, isAr ? 'المحدد' : 'Selected');
    const stamp = new Date().toISOString().slice(0, 10);
    XLSX.writeFile(wb, `DocPortal_Bulk_${stamp}.xlsx`);
    showToast(isAr ? `✓ تم تصدير ${docs.length} مستند` : `✓ Exported ${docs.length} document(s)`, 'success');
}

async function bulkDeleteSelected() {
    if (!_bulkSelected.size) return;
    const isAr = currentLang === 'ar';
    const n = _bulkSelected.size;
    const msg = isAr
        ? `هل تريد حذف ${n} مستند؟ يمكنك التراجع خلال 10 ثوانٍ.`
        : `Delete ${n} document(s)? You'll have 10 seconds to undo.`;
    if (!confirm(msg)) return;

    const ids = [..._bulkSelected];
    _bulkSelected.clear();

    const doneMsg = isAr ? `تم حذف ${n} مستند` : `Deleted ${n} document(s)`;
    _startDeleteWithUndo(ids, doneMsg, doneMsg);
}

function renderSearchTable(docs, offset = 0) {
    const isAr = currentLang === 'ar';
    if (!docs || !docs.length) return `<div class="search-empty-hint">${isAr ? 'لا توجد نتائج' : 'No results found'}</div>`;

    const importanceLabel = id => ({1: isAr?'عادي':'Normal', 2: isAr?'مهم':'Important', 3: isAr?'عاجل':'Urgent'})[id] || '—';
    const secretLabel     = id => ({1: isAr?'عادي':'Normal', 2: isAr?'سري':'Confidential', 3: isAr?'سري للغاية':'High Conf.'})[id] || '—';

    const canDel = _allowed(1, 'can_del');

    const headers = isAr
        ? ['', '#', 'رقم التسجيل', 'التاريخ', 'المجلد', 'الموضوع', 'الكلمات المفتاحية', 'الأهمية', 'السرية', 'المرفقات', 'إجراء']
        : ['', '#', 'Reg No.', 'Date', 'Folder', 'Subject', 'Keywords', 'Importance', 'Confidentiality', 'Attachments', 'Action'];

    const rows = docs.map((doc, i) => {
        const attCount = (doc.attachments?.length) || (doc.file_name ? 1 : 0);
        const attHtml  = attCount
            ? `<span class="tbl-att-badge"><i class="ph ph-paperclip"></i> ${attCount}</span>`
            : `<span style="color:var(--muted)">—</span>`;
        const isChecked = _bulkSelected.has(doc.id) ? 'checked' : '';
        return `
        <tr class="tbl-row" data-doc-id="${doc.id}" onclick="${_allowed(1,'can_open') ? `viewTransaction(${doc.id})` : ''}" title="${isAr ? 'انقر للعرض' : 'Click to view'}" style="${_allowed(1,'can_open') ? '' : 'cursor:default'}">
            <td class="tbl-cb" onclick="event.stopPropagation()">
                <input type="checkbox" class="bulk-cb" data-id="${doc.id}" ${isChecked}
                    onchange="_bulkToggle(${doc.id}, this.checked)">
            </td>
            <td class="tbl-num">${offset + i + 1}</td>
            <td class="tbl-regnum"><strong>${doc.registration_number || '—'}</strong></td>
            <td class="tbl-date">${doc.date || '—'}</td>
            <td class="tbl-folder"><span class="folder-tag"><i class="ph ph-folder"></i> ${escapeHtml(doc.folder_name) || '—'}</span></td>
            <td class="tbl-subject">${escapeHtml(doc.subject) || '—'}</td>
            <td class="tbl-keywords">${escapeHtml(doc.keywords) || '—'}</td>
            <td class="tbl-imp"><span class="importance-${doc.importance_id}">${importanceLabel(doc.importance_id)}</span></td>
            <td class="tbl-secret">${doc.secret_id > 1 ? `<span class="secret-badge"><i class="ph ph-lock"></i> ${secretLabel(doc.secret_id)}</span>` : secretLabel(doc.secret_id)}</td>
            <td class="tbl-att">${attHtml}</td>
            <td class="tbl-actions" onclick="event.stopPropagation()">
                ${_allowed(1,'can_open') ? `<button class="sr-btn sr-btn-view"   onclick="viewTransaction(${doc.id})">${isAr?'<i class="ph ph-eye"></i> عرض':'<i class="ph ph-eye"></i> View'}</button>` : ''}
                <button class="sr-btn sr-btn-view" onclick="openEmailModal(${doc.id})" title="${isAr ? 'إرسال بالبريد' : 'Email'}"><i class="ph ph-envelope-simple"></i></button>
                ${canDel ? `<button class="sr-btn sr-btn-delete" onclick="confirmDeleteTransaction(${doc.id},'${String(doc.registration_number).replace(/'/g,"\'")}')"><i class="ph ph-trash"></i></button>` : ''}
            </td>
        </tr>`;
    }).join('');

    const allChecked = docs.every(d => _bulkSelected.has(d.id));
    const someChecked = docs.some(d => _bulkSelected.has(d.id));
    const hdrIndeterminate = someChecked && !allChecked;

    return `
    <div id="bulkActionBar" class="bulk-action-bar" style="display:${_bulkSelected.size > 0 ? 'flex' : 'none'}">
        <span class="bulk-bar-count">${_bulkSelected.size} ${isAr ? 'محدد' : 'selected'}</span>
        <button type="button" class="bulk-bar-btn bulk-bar-btn--export" onclick="bulkExportSelected()">
            <i class="ph ph-export"></i> ${isAr ? 'تصدير' : 'Export'}
        </button>
        ${canDel ? `<button type="button" class="bulk-bar-btn bulk-bar-btn--delete" onclick="bulkDeleteSelected()">
            <i class="ph ph-trash"></i> ${isAr ? 'حذف' : 'Delete'}
        </button>` : ''}
        <button type="button" class="bulk-bar-btn bulk-bar-btn--clear" onclick="_bulkToggleAll(false)">
            <i class="ph ph-x"></i> ${isAr ? 'إلغاء التحديد' : 'Deselect all'}
        </button>
    </div>
    <div class="search-table-wrap">
        <table class="search-table">
            <thead>
                <tr>
                    <th class="tbl-cb-head">
                        <input type="checkbox" id="bulkSelectAllCb"
                            title="${isAr ? 'تحديد الصفحة' : 'Select page'}"
                            ${allChecked && docs.length > 0 ? 'checked' : ''}
                            onchange="_bulkToggleAll(this.checked)">
                    </th>
                    ${headers.slice(1).map(h => `<th>${h}</th>`).join('')}
                </tr>
            </thead>
            <tbody>${rows}</tbody>
        </table>
    </div>`;
}
function escapeHtml(text) {
    const div = document.createElement('div');
    div.textContent = text == null ? '' : String(text);
    return div.innerHTML;
}

function highlightKeyword(text, keyword) {
    if (!keyword) return escapeHtml(text);
    const escaped = escapeHtml(text);
    const escapedKw = escapeHtml(keyword);
    const re = new RegExp(`(${escapedKw.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')})`, 'gi');
    return escaped.replace(re, '<mark>$1</mark>');
}

function renderSearchCard(doc) {
    const importanceLabel = id => ({1:'Normal',2:'Important',3:'Urgent'})[id] || '';
    const secretLabel     = id => ({1:'Normal',2:'Confidential',3:'High Confidential'})[id] || '';
    const attachHtml = (() => {
        const list = (doc.attachments?.length) ? doc.attachments
            : (doc.file_name ? [{ file_name: doc.file_name, file_url: doc.file_url }] : []);
        return list.map(a => {
            const name = a.file_name || 'File';
            const attId = a.id != null ? a.id : 'null';
            return `<div class="search-result-attachment"><i class="ph ph-paperclip"></i> <button type="button" class="search-attach-link"
                onclick="event.stopPropagation(); openAttachmentFromSearch(${doc.id}, ${attId})">${escapeHtml(name)}</button></div>`;
        }).join('');
    })();
    const snippet = doc.ocr_snippet || '';
    const snippetHtml = snippet
        ? `<div class="result-ocr-snippet">${highlightKeyword(snippet, currentSearchStatement)}</div>`
        : '';
    return `
        <article class="search-result-card" id="result-card-${doc.id}">
            <div class="search-result-header">
                <strong class="sr-reg-num">${doc.registration_number}</strong>
                ${doc.doc_number ? `<span class="sr-doc-num">Doc: ${escapeHtml(doc.doc_number)}</span>` : ''}
                <span class="folder-tag"><i class="ph ph-folder"></i> ${escapeHtml(doc.folder_name) || '—'}</span>
                <div class="search-result-actions">
                    ${_allowed(1,'can_open') ? `<button class="sr-btn sr-btn-view"
                        title="${currentLang === 'ar' ? 'عرض' : 'View'}"
                        onclick="viewTransaction(${doc.id})">
                        ${currentLang === 'ar' ? 'عرض' : 'View'}
                    </button>` : ''}
                    ${_allowed(1,'can_edit') ? `<button class="sr-btn sr-btn-edit"
                        title="${currentLang === 'ar' ? 'تعديل' : 'Edit'}"
                        onclick="editTransaction(${doc.id})">
                        ${currentLang === 'ar' ? 'تعديل' : 'Edit'}
                    </button>` : ''}
                    ${_allowed(1,'can_del') ? `<button class="sr-btn sr-btn-delete"
                        title="${currentLang === 'ar' ? 'حذف' : 'Delete'}"
                        onclick="confirmDeleteTransaction(${doc.id}, '${String(doc.registration_number).replace(/'/g,"\\'")}')">
                        ${currentLang === 'ar' ? 'حذف' : 'Delete'}
                    </button>` : ''}
                </div>
            </div>
            <p class="search-result-subject">${escapeHtml(doc.subject)}</p>
            <div class="search-result-meta">
                ${doc.form_date ? `<span title="${currentLang === 'ar' ? 'تاريخ المستند' : 'Document date'}"><i class="ph ph-calendar-blank"></i> ${doc.form_date}</span>` : ''}
                ${doc.date ? `<span title="${currentLang === 'ar' ? 'تاريخ التسجيل' : 'Registration date'}"><i class="ph ph-calendar"></i> ${doc.date}</span>` : ''}
                ${doc.hijri_date ? `<span><i class="ph ph-moon"></i> ${doc.hijri_date}</span>` : ''}
                ${doc.keywords ? `<span><i class="ph ph-tag"></i> ${escapeHtml(doc.keywords)}</span>` : ''}
                ${doc.importance_id ? `<span class="importance-${doc.importance_id}">${importanceLabel(doc.importance_id)}</span>` : ''}
                ${doc.secret_id && doc.secret_id > 1 ? `<span class="secret-badge"><i class="ph ph-lock"></i> ${secretLabel(doc.secret_id)}</span>` : ''}
            </div>
            ${snippetHtml}
            ${attachHtml}
        </article>`;
}

// ── RECENT DOCS ────────────────────────────────────────────────────────────
const _RECENT_KEY = 'docportal_recent_docs';
const _RECENT_MAX = 8;

function _recentGet() {
    try { return JSON.parse(localStorage.getItem(_RECENT_KEY) || '[]'); } catch(e) { return []; }
}
function _recentSave(list) {
    try { localStorage.setItem(_RECENT_KEY, JSON.stringify(list)); } catch(e) {}
}
function _recentTrack(doc) {
    if (!doc || !doc.id) return;
    const list = _recentGet().filter(d => d.id !== doc.id);
    list.unshift({
        id:                  doc.id,
        registration_number: doc.registration_number || String(doc.id),
        subject:             (doc.subject || '').substring(0, 80),
        folder_name:         doc.folder_name || '',
        date:                doc.date || '',
    });
    _recentSave(list.slice(0, _RECENT_MAX));
    _recentRender();
}
function _recentClear() {
    _recentSave([]);
    _recentRender();
}
function _recentRender() {
    const widget = document.getElementById('recentDocsWidget');
    if (!widget) return;
    const list = _recentGet();
    if (!list.length) { widget.style.display = 'none'; return; }
    const isAr = currentLang === 'ar';
    widget.style.display = '';
    widget.innerHTML = `
        <div class="rdocs-header">
            <span class="rdocs-label"><i class="ph ph-clock-clockwise"></i> ${isAr ? 'المستندات الأخيرة' : 'Recently Viewed'}</span>
            <button type="button" class="rdocs-clear-btn" onclick="_recentClear()" title="${isAr ? 'مسح' : 'Clear all'}"><i class="ph ph-x"></i></button>
        </div>
        <div class="rdocs-chips">
            ${list.map(d => `
            <button type="button" class="rdocs-chip" onclick="viewTransaction(${d.id})" title="${escapeHtml(d.subject)}">
                <span class="rdocs-chip-num">${escapeHtml(d.registration_number)}</span>
                <span class="rdocs-chip-subj">${escapeHtml(d.subject) || '\u2014'}</span>
                ${d.folder_name ? `<span class="rdocs-chip-folder"><i class="ph ph-folder"></i> ${escapeHtml(d.folder_name)}</span>` : ''}
            </button>`).join('')}
        </div>`;
}

// ── TRANSACTION: View detail (modal with PDF preview) ─────────────────────
let _viewDocData = null;   // currently loaded doc data
let _previewDownloadUrl = null;
let _viewReturnContext = null;  // scroll + card to restore when closing view modal

/** Update data-en / data-ar labels on one element (does not re-run inquiry search). */
function applyI18nToElement(el) {
    if (!el) return;
    el.querySelectorAll('[data-en]').forEach(node => {
        const val = node.getAttribute('data-' + currentLang);
        if (val) node.textContent = val;
    });
    const selfVal = el.getAttribute?.('data-' + currentLang);
    if (el.hasAttribute?.('data-en') && selfVal) el.textContent = selfVal;
}

function escAttr(s) {
    return String(s || '').replace(/&/g, '&amp;').replace(/"/g, '&quot;').replace(/'/g, '&#39;').replace(/</g, '&lt;');
}

function attachmentPreviewUrl(a) {
    if (!a) return '';
    if (a.preview_url) return a.preview_url;
    if (a.id) return `/api/attachments/${a.id}/preview`;
    return a.file_url || '';
}

function attachmentDownloadUrl(a) {
    if (!a) return '';
    if (a.download_url) return a.download_url;
    if (a.id) return `/api/attachments/${a.id}/download`;
    return a.file_url || '';
}

function canPreviewFileName(name) {
    const n = (name || '').toLowerCase();
    return /\.(pdf|png|jpe?g|gif|webp|tiff?|bmp|svg|txt|csv|mp4|webm|ogg|mp3|wav)$/i.test(n);
}

function downloadAttachmentFile(url, name) {
    if (!url) return;
    const link = document.createElement('a');
    link.href = url;
    link.download = name || '';
    link.target = '_blank';
    link.rel = 'noopener';
    document.body.appendChild(link);
    link.click();
    link.remove();
}

const _WF_PREVIEW_IDS = { toolbar: 'wfPreviewToolbar', name: 'wfPreviewFileName', download: 'wfPreviewDownloadBtn', iframe: 'wfPreviewIframe', placeholder: 'wfPreviewPlaceholder' };

function wfPreviewAttachment(previewUrl, name, downloadUrl) {
    previewAttachment(previewUrl, name, downloadUrl, _WF_PREVIEW_IDS);
}

function resetWfPreviewPane() {
    const iframe = document.getElementById('wfPreviewIframe');
    const placeholder = document.getElementById('wfPreviewPlaceholder');
    const toolbar = document.getElementById('wfPreviewToolbar');
    if (iframe) { iframe.src = ''; iframe.style.display = 'none'; }
    if (toolbar) toolbar.style.display = 'none';
    if (placeholder) {
        placeholder.style.display = '';
        placeholder.innerHTML = `<span><i class="ph ph-file" style="font-size:40px;opacity:0.3"></i></span>
            <span data-en="Select an attachment to preview" data-ar="اختر مرفقاً للمعاينة">Select an attachment to preview</span>`;
        applyI18nToElement(placeholder);
    }
}

function onWfAttachItemClick(el) {
    const url = el.dataset.previewUrl;
    const name = el.dataset.name || '';
    const downloadUrl = el.dataset.downloadUrl;
    if (!url) {
        if (downloadUrl) downloadAttachmentFile(downloadUrl, name);
        return;
    }
    wfPreviewAttachment(url, name, downloadUrl);
}

function resetViewPreviewPane() {
    const iframe = document.getElementById('viewDocIframe');
    const placeholder = document.getElementById('viewPreviewPlaceholder');
    const toolbar = document.getElementById('viewPreviewToolbar');
    if (iframe) { iframe.src = ''; iframe.style.display = 'none'; }
    if (toolbar) toolbar.style.display = 'none';
    if (placeholder) {
        placeholder.style.display = '';
        placeholder.innerHTML = `<span><i class="ph ph-file" style="font-size:40px;opacity:0.3"></i></span>
            <span data-en="Select an attachment to preview" data-ar="اختر مرفقاً للمعاينة">Select an attachment to preview</span>`;
        applyI18nToElement(placeholder);
    }
    _previewDownloadUrl = null;
}

// ── Drag-to-resize the details / PDF-preview split in the view modal ───────
// Remembers the chosen width in localStorage so it stays put next time.
const VIEW_DETAILS_WIDTH_KEY = 'viewDocDetailsWidth';
const VIEW_DETAILS_MIN = 200;
const VIEW_DETAILS_MAX = 560;

// After a drag-resize ends, the mouseup/click lands on the overlay and would
// otherwise be read as "clicked outside the box" and close the modal. This
// flag suppresses that one click.
let _suppressViewDocOverlayClose = false;
function suppressViewDocOverlayCloseOnce() {
    _suppressViewDocOverlayClose = true;
    setTimeout(() => { _suppressViewDocOverlayClose = false; }, 0);
}

function applyViewDetailsWidth(px) {
    const body = document.querySelector('#viewDocModal .view-doc-body');
    if (body) body.style.setProperty('--view-details-width', `${px}px`);
}

function initViewDocResizer() {
    const resizer = document.getElementById('viewDocResizer');
    const box = document.querySelector('#viewDocModal .view-doc-box');
    const body = document.querySelector('#viewDocModal .view-doc-body');
    if (!resizer || !box || !body || resizer.dataset.bound) return;
    resizer.dataset.bound = '1';

    const saved = parseInt(localStorage.getItem(VIEW_DETAILS_WIDTH_KEY), 10);
    if (saved && saved >= VIEW_DETAILS_MIN && saved <= VIEW_DETAILS_MAX) {
        applyViewDetailsWidth(saved);
    }

    let startX = 0;
    let startWidth = 0;

    function onPointerMove(e) {
        const dx = (currentLang === 'ar' ? -1 : 1) * (e.clientX - startX);
        const next = Math.min(VIEW_DETAILS_MAX, Math.max(VIEW_DETAILS_MIN, startWidth + dx));
        applyViewDetailsWidth(next);
    }
    function onPointerUp() {
        box.classList.remove('is-resizing');
        resizer.classList.remove('is-dragging');
        document.removeEventListener('pointermove', onPointerMove);
        document.removeEventListener('pointerup', onPointerUp);
        suppressViewDocOverlayCloseOnce();
        const current = parseFloat(getComputedStyle(body).getPropertyValue('--view-details-width')) ||
            document.querySelector('#viewDocModal .view-doc-details')?.getBoundingClientRect().width;
        if (current) localStorage.setItem(VIEW_DETAILS_WIDTH_KEY, String(Math.round(current)));
    }
    resizer.addEventListener('pointerdown', (e) => {
        startX = e.clientX;
        startWidth = document.querySelector('#viewDocModal .view-doc-details')?.getBoundingClientRect().width || 280;
        box.classList.add('is-resizing');
        resizer.classList.add('is-dragging');
        document.addEventListener('pointermove', onPointerMove);
        document.addEventListener('pointerup', onPointerUp);
        e.preventDefault();
    });
    // Double-click the divider to reset to the default width
    resizer.addEventListener('dblclick', () => {
        applyViewDetailsWidth(280);
        localStorage.removeItem(VIEW_DETAILS_WIDTH_KEY);
    });
}

// ── Drag-to-resize the view modal's height ──────────────────────────────
const VIEW_DOC_HEIGHT_KEY = 'viewDocHeight';
const VIEW_DOC_HEIGHT_MIN = 320;

function applyViewDocHeight(px) {
    const box = document.querySelector('#viewDocModal .view-doc-box');
    if (box) box.style.setProperty('--view-doc-height', `${px}px`);
}

function initViewDocVResizer() {
    const resizer = document.getElementById('viewDocVResizer');
    const box = document.querySelector('#viewDocModal .view-doc-box');
    if (!resizer || !box || resizer.dataset.bound) return;
    resizer.dataset.bound = '1';

    const maxH = () => window.innerHeight * 0.95;
    const saved = parseInt(localStorage.getItem(VIEW_DOC_HEIGHT_KEY), 10);
    if (saved && saved >= VIEW_DOC_HEIGHT_MIN && saved <= maxH()) {
        applyViewDocHeight(saved);
    }

    let startY = 0;
    let startHeight = 0;

    function onPointerMove(e) {
        const dy = e.clientY - startY;
        const next = Math.min(maxH(), Math.max(VIEW_DOC_HEIGHT_MIN, startHeight + dy));
        applyViewDocHeight(next);
    }
    function onPointerUp() {
        box.classList.remove('is-resizing');
        resizer.classList.remove('is-dragging');
        document.removeEventListener('pointermove', onPointerMove);
        document.removeEventListener('pointerup', onPointerUp);
        suppressViewDocOverlayCloseOnce();
        const current = box.getBoundingClientRect().height;
        if (current) localStorage.setItem(VIEW_DOC_HEIGHT_KEY, String(Math.round(current)));
    }
    resizer.addEventListener('pointerdown', (e) => {
        startY = e.clientY;
        startHeight = box.getBoundingClientRect().height;
        box.classList.add('is-resizing');
        resizer.classList.add('is-dragging');
        document.addEventListener('pointermove', onPointerMove);
        document.addEventListener('pointerup', onPointerUp);
        e.preventDefault();
    });
    // Double-click the bottom handle to reset to the default (auto) height
    resizer.addEventListener('dblclick', () => {
        box.style.removeProperty('--view-doc-height');
        localStorage.removeItem(VIEW_DOC_HEIGHT_KEY);
    });
}

function restoreViewReturnContext() {
    const ctx = _viewReturnContext;
    _viewReturnContext = null;
    if (!ctx) return;

    requestAnimationFrame(() => {
        const card = document.getElementById(`result-card-${ctx.docId}`);
        const row = document.querySelector(`.tbl-row[data-doc-id="${ctx.docId}"]`);
        const target = card || row;
        if (target) {
            target.scrollIntoView({ block: 'center', behavior: 'smooth' });
            target.classList.add('view-return-highlight');
            setTimeout(() => target.classList.remove('view-return-highlight'), 2200);
        } else if (ctx.scrollY != null) {
            window.scrollTo({ top: ctx.scrollY, behavior: 'smooth' });
        }
    });
}

function onViewAttachItemClick(el) {
    const url = el.dataset.previewUrl;
    const name = el.dataset.name || '';
    const downloadUrl = el.dataset.downloadUrl;
    if (!url) {
        if (downloadUrl) downloadAttachmentFile(downloadUrl, name);
        return;
    }
    previewAttachment(url, name, downloadUrl);
}

async function openAttachmentFromSearch(docId, attachmentId) {
    await viewTransaction(docId);
    const atts = _viewDocData?.attachments || [];
    const att = (attachmentId != null && attachmentId !== 'null')
        ? atts.find(a => a.id === attachmentId)
        : atts[0];
    if (!att) return;
    previewAttachment(attachmentPreviewUrl(att), att.file_name, attachmentDownloadUrl(att));
}

async function viewTransaction(docId) {
    const modal = document.getElementById('viewDocModal');
    if (!modal) return;

    initViewDocResizer();
    initViewDocVResizer();

    _viewReturnContext = {
        docId,
        scrollY: window.scrollY,
        section: document.querySelector('.page-section.active')?.id?.replace('section-', '') || 'inquiries',
    };

    // Reset
    document.getElementById('viewDocTitle').textContent    = '…';
    document.getElementById('viewDocRegNum').textContent   = '';
    document.getElementById('vDate').textContent           = '—';
    document.getElementById('vFolder').textContent         = '—';
    document.getElementById('vSubject').textContent        = '—';
    document.getElementById('vKeywords').textContent       = '—';
    document.getElementById('vNotes').textContent          = '—';
    document.getElementById('vAttachments').innerHTML      = '';
    resetViewPreviewPane();
    modal.style.display = 'grid';

    try {
        const res  = await fetch(`/api/documents/${docId}`);
        const data = await res.json();
        if (!res.ok) { showToast(data.error || 'Failed to load document', 'error'); closeViewDoc(); return; }
        _viewDocData = data;
        _recentTrack(data);

        document.getElementById('viewDocTitle').textContent  = data.subject || 'Document';
        document.getElementById('viewDocRegNum').textContent = '#' + (data.registration_number || data.id);
        document.getElementById('vDate').textContent         = data.date        || '—';
        document.getElementById('vFolder').textContent       = data.folder_name || '—';
        document.getElementById('vSubject').textContent      = data.subject     || '—';
        document.getElementById('vKeywords').textContent     = data.keywords    || '—';
        document.getElementById('vNotes').textContent        = data.notes       || '—';

        // Attachments
        const attachWrap = document.getElementById('vAttachments');
        const atts = data.attachments || [];
        if (!atts.length) {
            attachWrap.innerHTML = `<span class="view-no-attach">${currentLang === 'ar' ? 'لا توجد مرفقات' : 'No attachments'}</span>`;
        } else {
            attachWrap.innerHTML = atts.map((a, i) => {
                const previewUrl = attachmentPreviewUrl(a);
                const downloadUrl = attachmentDownloadUrl(a);
                const name = a.file_name || `File ${i + 1}`;
                return `<div class="view-attach-item view-attach-item--clickable"
                    data-preview-url="${escAttr(previewUrl)}"
                    data-download-url="${escAttr(downloadUrl)}"
                    data-name="${escAttr(name)}"
                    onclick="onViewAttachItemClick(this)">
                    ${getFileIcon(name)}
                    <span class="view-attach-name" title="${escAttr(name)}">${escapeHtml(name)}</span>
                    <button type="button" class="sr-btn sr-btn-view"
                        onclick="event.stopPropagation(); downloadAttachmentFile('${escAttr(downloadUrl)}','${escAttr(name)}')"
                        title="${currentLang === 'ar' ? 'تنزيل' : 'Download'}"><i class="ph ph-download-simple"></i></button>
                    ${a.can_sign === false ? '' : `<button type="button" class="sr-btn sr-btn-view"
                        onclick="event.stopPropagation(); openSignatureModal(${a.id}, { onSigned: () => viewTransaction(${docId}) })"
                        title="${currentLang === 'ar' ? 'توقيع' : 'Sign'}"><i class="ph ph-pen-nib"></i></button>`}
                    ${a.is_signed ? `<button type="button" class="sr-btn sr-btn-view"
                        onclick="event.stopPropagation(); removeAttachmentSignature(${a.id}, () => viewTransaction(${docId}))"
                        title="${currentLang === 'ar' ? 'إزالة التوقيع' : 'Remove signature'}"><i class="ph ph-eraser"></i></button>` : ''}
                </div>`;
            }).join('');
            if (atts.length) {
                const first = atts[0];
                previewAttachment(attachmentPreviewUrl(first), first.file_name, attachmentDownloadUrl(first));
            }
        }
    } catch(e) {
        showToast('Error loading document', 'error');
        closeViewDoc();
    }
    // Re-enforce permissions every time the modal opens
    if (typeof applyAccr === 'function') applyAccr();
}

function closeViewDoc() {
    const modal = document.getElementById('viewDocModal');
    if (modal) modal.style.display = 'none';
    resetViewPreviewPane();
    _viewDocData = null;
    restoreViewReturnContext();
}

// ── Email Document Modal ────────────────────────────────────────────────
let _emDocId = null;
let _emUsers = [];
let _emRecipients = []; // [{full_name, email}]

// Called from the Archive page's action bar. Only works for a document
// that's already saved (either loaded for editing via loadDocIntoForm, or
// just created — the success modal's own Email button covers that case).
// A brand-new, unsaved document has no ID yet, so there's nothing to link.
function emailCurrentArchiveDoc() {
    const docId = _viewDocData && _viewDocData.id;
    if (!docId) {
        showToast(
            currentLang === 'ar'
                ? 'يرجى حفظ المستند أولاً، أو فتح مستند محفوظ لإرساله بالبريد'
                : 'Save the document first, or open an existing one to email it',
            'error'
        );
        return;
    }
    openEmailModal(docId);
}

let _emReturnToViewDoc = false;

async function openEmailModal(docId) {
    if (!docId) { showToast('No document selected', 'error'); return; }
    _emDocId = docId;
    _emRecipients = [];
    _emUsers = [];

    // If the document viewer is open behind this, hide it so the two dark
    // overlays don't stack into a near-black, double-blurred background.
    const viewDocModal = document.getElementById('viewDocModal');
    _emReturnToViewDoc = !!(viewDocModal && viewDocModal.style.display !== 'none' && viewDocModal.style.display !== '');
    if (_emReturnToViewDoc) viewDocModal.style.display = 'none';

    const modal = document.getElementById('emailDocModal');
    document.getElementById('emRecipientPills').innerHTML = '';
    document.getElementById('emRecipientInput').value = '';
    document.getElementById('emRecipientDropdown').style.display = 'none';
    document.getElementById('emSubject').value = '';
    document.getElementById('emBody').value = '';
    document.getElementById('emModeFile').checked = true;
    _emUpdateModeUI();
    document.getElementById('emSelectAll').checked = false;
    document.getElementById('emAttachmentList').innerHTML =
        '<div class="em-recipient-empty">No attachments on this document</div>';
    document.getElementById('emStatus').textContent = '';
    document.getElementById('emDocLabel').textContent = '…';
    modal.style.display = 'grid';

    try {
        // Reuse already-loaded doc data if it's the same document, otherwise fetch it.
        let data = (_viewDocData && _viewDocData.id === docId) ? _viewDocData : null;
        if (!data) {
            const res = await fetch(`/api/documents/${docId}`);
            data = await res.json();
            if (!res.ok) throw new Error(data.error || 'Failed to load document');
        }

        document.getElementById('emDocLabel').textContent =
            `${data.subject || 'Document'} — #${data.registration_number || data.id}`;
        document.getElementById('emSubject').value =
            `${data.subject || 'Document'} — Reg #${data.registration_number || data.id}`;

        const atts = data.attachments || [];
        const listEl = document.getElementById('emAttachmentList');
        if (atts.length) {
            listEl.innerHTML = atts.map(a => `
                <label class="em-attachment-item">
                    <input type="checkbox" class="em-att-cb" value="${a.id}" ${atts.length === 1 ? 'checked' : ''} onchange="_emSyncSelectAll()">
                    ${getFileIcon(a.file_name || '')}
                    <span title="${escAttr(a.file_name || '')}">${escapeHtml(a.file_name || 'File')}</span>
                </label>`).join('');
            _emSyncSelectAll();
        }

        // Recipient list
        const uRes = await fetch('/api/users/list-emails');
        const uData = await uRes.json();
        if (!uRes.ok) throw new Error(uData.error || 'Failed to load users');
        _emUsers = uData.users || [];
    } catch (e) {
        showToast(e.message || 'Failed to open email form', 'error');
    }
}

function closeEmailModal() {
    document.getElementById('emailDocModal').style.display = 'none';
    _emDocId = null;

    if (_emReturnToViewDoc && _viewDocData) {
        const viewDocModal = document.getElementById('viewDocModal');
        if (viewDocModal) viewDocModal.style.display = 'grid';
    }
    _emReturnToViewDoc = false;
}

function _emUpdateModeUI() {
    const isFile = document.getElementById('emModeFile')?.checked;
    document.getElementById('emModeFileLabel')?.classList.toggle('em-mode-active', !!isFile);
    document.getElementById('emModeLinkLabel')?.classList.toggle('em-mode-active', !isFile);
}

async function copyCurrentAttachmentLink() {
    const m = (_previewDownloadUrl || '').match(/\/api\/attachments\/(\d+)\/download/);
    if (!m) { showToast('No attachment selected', 'error'); return; }
    const attachmentId = m[1];
    const fileName = document.getElementById('viewPreviewFileName')?.textContent || 'Document';

    const btn = document.getElementById('viewPreviewCopyLinkBtn');
    const originalHtml = btn ? btn.innerHTML : null;
    if (btn) btn.innerHTML = '<i class="ph ph-spinner" style="animation:spin 0.8s linear infinite"></i>';

    try {
        const res = await fetch(`/api/attachments/${attachmentId}/share-link`, { method: 'POST' });
        const data = await res.json();
        if (!res.ok) throw new Error(data.error || 'Failed to create link');

        await _copyLinkToClipboard(data.share_link, fileName);
        showToast(`Link copied — valid ${data.expires_days} days`, 'success');
    } catch (e) {
        showToast(e.message || 'Failed to copy link', 'error');
    } finally {
        if (btn && originalHtml) btn.innerHTML = originalHtml;
    }
}

// Plain navigator.clipboard.writeText() (or the textarea/execCommand
// fallback) only puts plain text on the clipboard. Pasting a bare URL into
// Outlook/Word/Teams then inserts it as plain text — those apps only
// auto-hyperlink a URL when you TYPE it and hit space/enter, not on paste.
// So the link looks "dead" until the user manually converts it.
//
// Fix: put a real HTML <a> on the clipboard (text/html) alongside a plain
// text/plain fallback, so any paste target that honors rich clipboard
// content (Outlook, Word, Teams, Gmail compose, Slack, etc.) inserts an
// already-clickable hyperlink.
async function _copyLinkToClipboard(url, label) {
    const safeUrl = escAttr(url);
    // The visible/clickable text must be the URL itself, not the file name.
    // Rich-paste targets that honor HTML (Outlook, Word, Teams) render this
    // as a normal clickable hyperlink. But targets like WhatsApp (desktop
    // and web) strip formatting on paste and keep only the visible text,
    // discarding the href entirely — so if the label were the file name,
    // the href silently vanishes and the user is left with unlinked plain
    // text. Using the URL as the visible text means even a formatting-strip
    // paste still yields a real URL, which WhatsApp then auto-linkifies.
    const safeUrlText = escapeHtml(url);
    const html = `<a href="${safeUrl}">${safeUrlText}</a>`;

    // Best path everywhere (works without HTTPS too): select a hidden real
    // <a> element and let execCommand('copy') copy both text/html and
    // text/plain from the selection, exactly like copying from a webpage.
    const container = document.createElement('div');
    container.contentEditable = 'true';
    container.style.position = 'fixed';
    container.style.left = '-9999px';
    container.style.top = '0';
    container.innerHTML = html;
    document.body.appendChild(container);

    const range = document.createRange();
    range.selectNodeContents(container);
    const sel = window.getSelection();
    sel.removeAllRanges();
    sel.addRange(range);

    let copied = false;
    try {
        copied = document.execCommand('copy');
    } catch (e) { /* fall through to the async API below */ }

    sel.removeAllRanges();
    document.body.removeChild(container);
    if (copied) return;

    // Fallback: async Clipboard API with explicit multi-format ClipboardItem
    // (needs a secure context — HTTPS or localhost).
    if (window.isSecureContext && navigator.clipboard && window.ClipboardItem) {
        try {
            await navigator.clipboard.write([
                new ClipboardItem({
                    'text/html': new Blob([html], { type: 'text/html' }),
                    'text/plain': new Blob([url], { type: 'text/plain' }),
                }),
            ]);
            return;
        } catch (e) { /* fall through */ }
    }

    // Last resort: plain text only — still copies, just won't auto-render
    // as a hyperlink on paste.
    if (window.isSecureContext && navigator.clipboard) {
        await navigator.clipboard.writeText(url);
        return;
    }
    const ta = document.createElement('textarea');
    ta.value = url;
    ta.style.position = 'fixed';
    ta.style.opacity = '0';
    document.body.appendChild(ta);
    ta.focus();
    ta.select();
    try { document.execCommand('copy'); } finally { document.body.removeChild(ta); }
}

function _emShowDropdown() {
    _emFilterUsers(document.getElementById('emRecipientInput').value);
}

function _emFilterUsers(query) {
    const dropdown = document.getElementById('emRecipientDropdown');
    const q = (query || '').trim().toLowerCase();
    const chosen = new Set(_emRecipients.map(r => r.email));
    const matches = _emUsers.filter(u =>
        !chosen.has(u.email) &&
        (!q || (u.full_name || '').toLowerCase().includes(q) || (u.email || '').toLowerCase().includes(q))
    ).slice(0, 8);

    let html = '';
    if (matches.length) {
        html += matches.map(u => `
            <div class="em-recipient-option" onclick="_emAddRecipient('${escAttr(u.email)}','${escAttr(u.full_name)}')">
                <span class="em-opt-name">${escapeHtml(u.full_name || u.email)}</span>
                <span class="em-opt-email">${escapeHtml(u.email)}</span>
            </div>`).join('');
    }

    // Typed text isn't a system user — any valid-looking address can still
    // be added directly (Gmail, Outlook.com, any domain). Not restricted
    // to the internal user list.
    const rawQuery = (query || '').trim();
    if (_emIsValidEmail(rawQuery) && !chosen.has(rawQuery.toLowerCase()) &&
        !_emUsers.some(u => u.email.toLowerCase() === rawQuery.toLowerCase())) {
        html += `
            <div class="em-recipient-option em-recipient-option--new" onclick="_emAddRecipient('${escAttr(rawQuery)}','${escAttr(rawQuery)}')">
                <span class="em-opt-name"><i class="ph ph-plus-circle"></i> ${escapeHtml(rawQuery)}</span>
                <span class="em-opt-email" data-en="Add as new recipient" data-ar="إضافة كمستلم جديد">Add as new recipient</span>
            </div>`;
    }

    dropdown.innerHTML = html || `<div class="em-recipient-empty">No matching users</div>`;
    dropdown.style.display = 'block';
}

function _emIsValidEmail(value) {
    // Deliberately permissive — any provider/domain is fine, this is just
    // a basic shape check so obvious typos don't get sent to the server.
    return /^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(value);
}

function _emRecipientKeydown(e) {
    // Enter, comma, or Tab with text typed -> add it as a recipient
    // directly, without requiring a dropdown click. Works for any address,
    // not just users already in the system.
    if (e.key !== 'Enter' && e.key !== ',' && e.key !== 'Tab') return;
    const input = e.target;
    const raw = input.value.trim().replace(/,$/, '');
    if (!raw) return;

    const chosen = new Set(_emRecipients.map(r => r.email.toLowerCase()));
    const existingUser = _emUsers.find(u => u.email.toLowerCase() === raw.toLowerCase());

    if (existingUser && !chosen.has(existingUser.email.toLowerCase())) {
        e.preventDefault();
        _emAddRecipient(existingUser.email, existingUser.full_name);
        return;
    }
    if (_emIsValidEmail(raw) && !chosen.has(raw.toLowerCase())) {
        e.preventDefault();
        _emAddRecipient(raw, raw);
        return;
    }
    // Tab with invalid/empty text: let it move focus normally.
    if (e.key !== 'Tab') e.preventDefault();
}

function _emAddRecipient(email, fullName) {
    if (!email || _emRecipients.some(r => r.email === email)) return;
    _emRecipients.push({ email, full_name: fullName });
    _emRenderPills();
    document.getElementById('emRecipientInput').value = '';
    document.getElementById('emRecipientDropdown').style.display = 'none';
    document.getElementById('emRecipientInput').focus();
}

function _emRemoveRecipient(email) {
    _emRecipients = _emRecipients.filter(r => r.email !== email);
    _emRenderPills();
}

function _emRenderPills() {
    document.getElementById('emRecipientPills').innerHTML = _emRecipients.map(r => `
        <span class="em-recipient-pill">
            ${escapeHtml(r.full_name || r.email)}
            <button type="button" onclick="_emRemoveRecipient('${escAttr(r.email)}')"><i class="ph ph-x"></i></button>
        </span>`).join('');
}

// Close the recipient dropdown when clicking outside it.
document.addEventListener('click', (e) => {
    const box = document.getElementById('emRecipientBox');
    const dropdown = document.getElementById('emRecipientDropdown');
    if (!box || !dropdown) return;
    if (!box.contains(e.target) && !dropdown.contains(e.target)) {
        dropdown.style.display = 'none';
    }
});

function _emToggleSelectAll(checked) {
    document.querySelectorAll('.em-att-cb').forEach(cb => cb.checked = checked);
}

function _emSyncSelectAll() {
    const boxes = Array.from(document.querySelectorAll('.em-att-cb'));
    const allChecked = boxes.length > 0 && boxes.every(cb => cb.checked);
    document.getElementById('emSelectAll').checked = allChecked;
}

async function emSendEmail() {
    if (!_emDocId) return;
    if (!_emRecipients.length) {
        showToast('Add at least one recipient', 'error');
        return;
    }

    const btn = document.getElementById('emSendBtn');
    const status = document.getElementById('emStatus');
    const originalHtml = btn.innerHTML;
    btn.disabled = true;
    btn.innerHTML = '<i class="ph ph-spinner" style="animation:spin 0.8s linear infinite"></i> Sending…';
    status.textContent = '';

    const attachmentIds = Array.from(document.querySelectorAll('.em-att-cb:checked')).map(cb => cb.value);

    const payload = {
        doc_id: _emDocId,
        recipients: _emRecipients.map(r => r.email),
        subject: document.getElementById('emSubject').value.trim(),
        body: document.getElementById('emBody').value.trim(),
        attachment_ids: attachmentIds,
        attach_file: document.querySelector('input[name="emSendMode"]:checked')?.value === 'file',
    };

    try {
        const res = await fetch('/api/email/send', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(payload),
        });
        const data = await res.json();
        if (!res.ok) throw new Error(data.error || 'Failed to send email');
        if (data.skipped_too_large && data.skipped_too_large.length) {
            showToast(`Email sent — ${data.skipped_too_large.join(', ')} was too large to attach, sent as a link instead`, 'info');
        } else {
            showToast('Email sent', 'success');
        }
        closeEmailModal();
    } catch (e) {
        status.textContent = e.message || 'Failed to send email';
        status.style.color = 'var(--danger)';
    } finally {
        btn.disabled = false;
        btn.innerHTML = originalHtml;
    }
}

// ── Edit straight from the results list (Inquiries) ────────────────────────
// Loads the document data, then populates the Archive form for editing —
// same registration number, same folder/subfolder, same attachments.
async function editTransaction(docId) {
    try {
        const res  = await fetch(`/api/documents/${docId}`);
        const data = await res.json();
        if (!res.ok) { showToast(data.error || 'Failed to load document', 'error'); return; }
        _viewDocData = data;
        loadDocIntoForm();
    } catch (e) {
        showToast('Failed to load document', 'error');
    }
}

function printDocument() {
    const isAr = currentLang === 'ar';

    // Hard block — if Can_Print is denied, refuse entirely
    if (!_allowed(1, 'can_print')) {
        showToast(isAr ? 'ليس لديك صلاحية الطباعة' : 'You are not authorized to print', 'error');
        return;
    }

    // Must have a file loaded in the preview pane
    if (!_previewDownloadUrl) {
        showToast(isAr ? 'يرجى تحديد مرفق من القائمة أولاً' : 'Please select an attachment to print first', 'info');
        return;
    }

    const url = _previewDownloadUrl.replace(/\/download$/, '/preview');
    const ext = (url.split('?')[0].split('.').pop() || '').toLowerCase();
    const isImg = /^(png|jpe?g|gif|webp|bmp|tiff?)$/.test(ext);

    if (isImg) {
        const win = window.open('', '_blank', 'width=900,height=900');
        if (!win) { showToast(isAr ? 'يرجى السماح بالنوافذ المنبثقة' : 'Please allow popups for this site', 'error'); return; }
        win.document.write(`<!DOCTYPE html><html><head><meta charset="UTF-8">
<style>*{margin:0;padding:0}body{display:flex;justify-content:center}img{max-width:100%;height:auto}@media print{@page{margin:.5cm}}</style>
</head><body><img src="${url}" onload="window.print()"></body></html>`);
        win.document.close();
    } else {
        // PDF and others — open preview URL directly; browser PDF viewer handles print
        const win = window.open(url, '_blank');
        if (!win) { showToast(isAr ? 'يرجى السماح بالنوافذ المنبثقة' : 'Please allow popups for this site', 'error'); return; }
        win.addEventListener('load', () => { try { win.print(); } catch(e) {} });
    }
}

function previewAttachment(previewUrl, name, downloadUrl, ids) {
    if (!previewUrl) return;
    ids = ids || { toolbar: 'viewPreviewToolbar', name: 'viewPreviewFileName', download: 'viewPreviewDownloadBtn', iframe: 'viewDocIframe', placeholder: 'viewPreviewPlaceholder' };
    _previewDownloadUrl = downloadUrl || previewUrl.replace(/\/preview$/, '/download');

    const toolbar = document.getElementById(ids.toolbar);
    const nameEl  = document.getElementById(ids.name);
    const dlBtn   = document.getElementById(ids.download);
    if (toolbar) toolbar.style.display = 'flex';
    if (nameEl)  nameEl.textContent = name || '';
    if (dlBtn)   dlBtn.onclick = () => downloadAttachmentFile(_previewDownloadUrl, name);

    const iframe      = document.getElementById(ids.iframe);
    const placeholder = document.getElementById(ids.placeholder);
    if (!iframe || !placeholder) return;

    const n = (name || '').toLowerCase();
    const isImg   = /\.(png|jpe?g|gif|webp|tiff?|bmp|svg)$/i.test(n);
    const isVideo = /\.(mp4|webm|ogg)$/i.test(n);
    const isAudio = /\.(mp3|wav|ogg)$/i.test(n);
    const isText  = /\.(txt|csv)$/i.test(n);

    iframe.style.display  = 'none';
    iframe.src = '';
    placeholder.style.display = 'flex';

    if (isImg) {
        placeholder.innerHTML = `<img src="${previewUrl}" alt="${escAttr(name)}"
            style="max-width:100%;max-height:100%;object-fit:contain;border-radius:6px">`;
    } else if (isVideo) {
        placeholder.innerHTML = `<video controls style="max-width:100%;max-height:100%;border-radius:6px">
            <source src="${previewUrl}">
            ${currentLang === 'ar' ? 'المتصفح لا يدعم تشغيل الفيديو' : 'Your browser does not support video playback.'}
        </video>`;
    } else if (isAudio) {
        placeholder.innerHTML = `<audio controls style="width:100%;margin:auto">
            <source src="${previewUrl}">
            ${currentLang === 'ar' ? 'المتصفح لا يدعم تشغيل الصوت' : 'Your browser does not support audio playback.'}
        </audio>`;
    } else if (isText) {
        fetch(previewUrl)
            .then(r => r.text())
            .then(text => {
                placeholder.innerHTML = `<pre style="white-space:pre-wrap;word-break:break-all;
                    font-size:12px;text-align:left;direction:ltr;overflow:auto;
                    width:100%;height:100%;padding:12px;box-sizing:border-box">${text.replace(/</g,'&lt;')}</pre>`;
            })
            .catch(() => {
                placeholder.innerHTML = `<span style="color:var(--muted)">${currentLang === 'ar' ? 'تعذّر تحميل الملف' : 'Could not load file'}</span>`;
            });
        placeholder.innerHTML = `<span style="color:var(--muted)">Loading…</span>`;
    } else {
        // PDF and everything else
        const isPdf = /\.pdf$/i.test(n) || (!isImg && !isVideo && !isAudio && !isText);
        if (isPdf && !_allowed(1, 'can_print')) {
            // Can_Print denied — render via PDF.js canvas so native toolbar is hidden
            placeholder.style.display = 'flex';
            placeholder.innerHTML = `<div id="_pdfCanvasWrap" style="overflow-y:auto;width:100%;height:100%;display:flex;flex-direction:column;align-items:center;gap:8px;padding:8px;box-sizing:border-box">
                <span style="color:var(--text-muted);font-size:.8rem">Loading…</span>
            </div>`;
            iframe.style.display = 'none';
            iframe.src = '';
            _renderPdfInCanvas(previewUrl, document.getElementById('_pdfCanvasWrap'));
        } else {
            placeholder.style.display = 'none';
            iframe.src = previewUrl;
            iframe.style.display = 'block';
        }
    }
}

async function _renderPdfInCanvas(url, container) {
    if (!container) return;
    try {
        let pdfjsLib = window['pdfjs-dist/build/pdf'] || window.pdfjsLib;
        if (!pdfjsLib) {
            await new Promise((resolve, reject) => {
                if (document.getElementById('_pdfjs_script')) { resolve(); return; }
                const s = document.createElement('script');
                s.id  = '_pdfjs_script';
                s.src = 'https://cdnjs.cloudflare.com/ajax/libs/pdf.js/3.11.174/pdf.min.js';
                s.onload = resolve; s.onerror = reject;
                document.head.appendChild(s);
            });
            pdfjsLib = window.pdfjsLib;
            if (pdfjsLib && !pdfjsLib.GlobalWorkerOptions.workerSrc) {
                pdfjsLib.GlobalWorkerOptions.workerSrc =
                    'https://cdnjs.cloudflare.com/ajax/libs/pdf.js/3.11.174/pdf.worker.min.js';
            }
        }
        if (!pdfjsLib) throw new Error('PDF.js unavailable');

        const pdf = await pdfjsLib.getDocument(url).promise;
        container.innerHTML = ''; // clear "Loading…"
        const containerWidth = container.clientWidth || 600;

        for (let i = 1; i <= pdf.numPages; i++) {
            const page = await pdf.getPage(i);
            const vp0  = page.getViewport({ scale: 1 });
            const scale = (containerWidth - 16) / vp0.width;
            const vp   = page.getViewport({ scale });
            const canvas = document.createElement('canvas');
            canvas.width  = vp.width;
            canvas.height = vp.height;
            canvas.style.cssText = 'display:block;box-shadow:0 1px 4px rgba(0,0,0,.15);border-radius:3px;max-width:100%';
            container.appendChild(canvas);
            await page.render({ canvasContext: canvas.getContext('2d'), viewport: vp }).promise;
        }
    } catch(err) {
        container.innerHTML = `<span style="color:var(--text-muted);font-size:.85rem">
            ${currentLang === 'ar' ? 'تعذّر تحميل ملف PDF' : 'Could not render PDF'}</span>`;
    }
}

// ── Load viewed document back into the Archive form for editing ────────────
function loadDocIntoForm() {
    if (!_viewDocData) return;
    const d = _viewDocData;

    _viewReturnContext = null;  // Edit leaves inquiries — do not scroll back to results
    closeViewDoc();
    showSection('archive');     // switch to archive tab

    // Fill form fields
    const set = (id, val) => { const el = document.getElementById(id); if (el) el.value = val || ''; };
    set('topicInput',      d.subject);
    set('keywordsInput',   d.keywords);
    set('statementInput',  d.notes);
    set('registrationNumber', d.registration_number || d.id);

    // ── Restore entity, folder, and expand the tree ───────────────────────────
    // Find the entity (Sys_Department) that owns this document's folder.
    // allFoldersByDept is keyed by entity.id; folder_dept_id from the API is
    // the raw Adco_Folder.Dept_ID value — match via allEntities.dept_id.
    const targetFolderId = d.folder_id ? Number(d.folder_id) : null;

    let ownerEntity = null;
    if (targetFolderId) {
        // Primary: match by dept_id
        if (d.folder_dept_id) {
            ownerEntity = allEntities.find(e => Number(e.dept_id) === Number(d.folder_dept_id));
        }
        // Fallback: scan every entity's folder list
        if (!ownerEntity) {
            for (const e of allEntities) {
                if ((allFoldersByDept[e.id] || []).some(f => Number(f.id) === targetFolderId)) {
                    ownerEntity = e;
                    break;
                }
            }
        }
    }

    // 1. Set the Entity/Department dropdown to the correct entity
    const entitySel = document.getElementById('entitySelect');
    if (entitySel && ownerEntity) {
        entitySel.value = String(ownerEntity.id);
    }

    // 2. Rebuild the volume/subfolder options for this entity, then restore the
    //    folder input values (updateVolumeOptions clears them so we do it after).
    updateVolumeOptions().then(() => {
        const volumeInput = document.getElementById('volumeInput');
        if (volumeInput && targetFolderId) {
            volumeInput.value = d.folder_name || '';
            volumeInput.dataset.folderId = targetFolderId;
            if (d.folder_dept_id) volumeInput.dataset.folderDeptId = d.folder_dept_id;
            else delete volumeInput.dataset.folderDeptId;
        }
        const volLabel = document.getElementById('selectedVolumeLabel');
        if (volLabel && d.folder_name) {
            volLabel.innerHTML = '<i class="ph ph-folder"></i> ' + d.folder_name;
        }

        // 3. Expand the tree to the folder and highlight it
        if (ownerEntity && targetFolderId) {
            const entityId   = ownerEntity.id;
            const allFolders = allFoldersByDept[entityId] || [];
            const targetFolder = allFolders.find(f => Number(f.id) === targetFolderId);

            // Expand department node
            const deptChildren = document.getElementById(`dept-children-${entityId}`);
            const deptArrow    = document.getElementById(`arrow-${entityId}`);
            if (deptChildren) deptChildren.style.display = 'block';
            if (deptArrow)    deptArrow.textContent = '▼';

            // Expand all ancestor folders
            if (targetFolder) {
                let ancestor = targetFolder;
                while (ancestor && ancestor.parent_id) {
                    const parent = allFolders.find(f => Number(f.id) === Number(ancestor.parent_id));
                    if (!parent) break;
                    const childDiv = document.getElementById(`folder-children-${parent.id}`);
                    const arrow    = document.getElementById(`farrow-${parent.id}`);
                    if (childDiv) childDiv.style.display = 'block';
                    if (arrow)    arrow.textContent = '▼';
                    ancestor = parent;
                }
            }

            // Expand this folder's own children
            const ownChildren = document.getElementById(`folder-children-${targetFolderId}`);
            const ownArrow    = document.getElementById(`farrow-${targetFolderId}`);
            if (ownChildren) ownChildren.style.display = 'block';
            if (ownArrow)    ownArrow.textContent = '▼';

            // Highlight and scroll into view
            document.querySelectorAll('.tree-child').forEach(t => t.classList.remove('active'));
            const selectedEl = document.querySelector(`#wrap-${targetFolderId} > .tree-child`);
            if (selectedEl) {
                selectedEl.classList.add('active');
                setTimeout(() => selectedEl.scrollIntoView({ behavior: 'smooth', block: 'nearest' }), 150);
            }
        }
    });

    // ── Existing attachments (with ✕ remove button) ───────────────────────────
    window._removedAttachmentIds = [];
    _ocrUsedFileNames = new Set();
    _ocrTextByFileName = {};
    const existingWrap = document.getElementById('existingAttachmentsWrap');
    const existingList = document.getElementById('existingAttachmentsList');
    const atts = d.attachments || [];
    if (existingWrap && existingList) {
        if (atts.length) {
            existingList.innerHTML = atts.map((a) => {
                const name = a.file_name || 'File';
                const previewUrl = attachmentPreviewUrl(a);
                const attId = a.id != null ? a.id : '';
                const isPdfFile = /\.pdf$/i.test(name);
                const pagesBtn = isPdfFile
                    ? `<button type="button" class="file-item-preview"
                        onclick="openPageManagerForAttachment(${attId},'${escAttr(name)}')"
                        title="${currentLang === 'ar' ? 'إدارة الصفحات' : 'Manage Pages'}"><i class="ph ph-stack"></i></button>`
                    : '';
                return `<div class="file-item" id="existing-att-${attId}">
                    <span class="file-item-icon">${getFileIcon(name)}</span>
                    <span class="file-item-name file-item-name--clickable"
                        onclick="previewExistingAttachment('${escAttr(previewUrl)}','${escAttr(name)}')"
                        title="${currentLang === 'ar' ? 'معاينة' : 'Preview'}">${escapeHtml(name)}</span>
                    <span class="file-item-size">${a.file_size ? formatBytes(a.file_size) : ''}</span>
                    <button type="button" class="file-item-rename"
                        onclick="startRenameExistingAttachment(${attId})"
                        title="${currentLang === 'ar' ? 'إعادة تسمية' : 'Rename'}"><i class="ph ph-pencil-simple"></i></button>
                    <button type="button" class="file-item-preview"
                        onclick="previewExistingAttachment('${escAttr(previewUrl)}','${escAttr(name)}')"
                        title="${currentLang === 'ar' ? 'معاينة' : 'Preview'}"><i class="ph ph-eye"></i></button>
                    ${pagesBtn}
                    <button type="button" class="file-item-remove"
                        onclick="removeExistingAttachment(${attId},'${escAttr(name)}')"
                        title="${currentLang === 'ar' ? 'إزالة' : 'Remove'}">✕</button>
                </div>`;
            }).join('');
            existingWrap.style.display = '';
        } else {
            existingList.innerHTML = '';
            existingWrap.style.display = 'none';
        }
    }

    // Form is fully populated for an existing transaction — show the final step
    goToStep(4);

    // Registration Date — must stay pinned to the day the document was
    // originally archived (H_Date / d.date). It was never populated on edit
    // load, so the field sat blank and the "auto-fill today if empty"
    // fallback in saveDocumentToDb silently overwrote it with today's date
    // on every save. Only the Document Date below should ever change.
    const regDateEl = document.getElementById('registrationDate');
    if (regDateEl && d.date) {
        regDateEl.value = d.date.substring(0, 10).replace(/-/g, '/');
    }

    // Document Date — must come from the document's own Form_Date, not the
    // registration H_Date (d.date). Previously this used d.date, which fed
    // the wrong value into the Document Date boxes and meant the real date
    // never round-tripped through an edit.
    if (d.form_date) setSegDate(d.form_date.substring(0, 10));

    // Store the doc ID so saveDocument knows it's an update
    const form = document.getElementById('section-archive');
    if (form) form.dataset.editId = d.id;

    // Restore Fe1–Fe7 custom field values after zone renders
    const cfDeptId = d.folder_dept_id || null;
    if (cfDeptId) {
        renderCustomFieldsZone(cfDeptId).then(() => {
            setCustomFieldValues({
                Fe1: d.Fe1, Fe2: d.Fe2, Fe3: d.Fe3, Fe4: d.Fe4,
                Fe5: d.Fe5, Fe6: d.Fe6, Fe7: d.Fe7
            });
        });
    }

    document.getElementById('topicInput')?.scrollIntoView({ behavior: 'smooth', block: 'center' });

    // Mark Save button as "Update"
    const saveBtn = document.querySelector('[onclick="saveDocument()"]');
    if (saveBtn) {
        saveBtn.innerHTML = `<i class="ph ph-floppy-disk"></i> <span>${currentLang === 'ar' ? 'تحديث المستند' : 'Update Document'}</span>`;
        saveBtn.dataset.isUpdate = '1';
    }
    showToast(currentLang === 'ar' ? 'تم تحميل المستند للتعديل' : 'Document loaded for editing', 'success');
}

// ── Remove an already-saved attachment while in edit mode ─────────────────
// ── Rename an already-saved attachment while in edit mode ──────────────────
// Unlike the not-yet-uploaded rename (which is purely client-side until
// Save), this one persists immediately via the server, since the file
// already exists on the saved document.
function startRenameExistingAttachment(attId) {
    const row = document.getElementById(`existing-att-${attId}`);
    if (!row) return;
    const nameSpan = row.querySelector('.file-item-name');
    if (!nameSpan) return;

    const currentName = nameSpan.textContent || '';
    const dot = currentName.lastIndexOf('.');
    const stem = dot > 0 ? currentName.slice(0, dot) : currentName;
    const ext = dot > 0 ? currentName.slice(dot) : '';

    const input = document.createElement('input');
    input.type = 'text';
    input.className = 'file-item-rename-input';
    input.value = stem;
    input.setAttribute('aria-label', currentLang === 'ar' ? 'اسم الملف الجديد' : 'New file name');

    let committed = false;
    const commit = async () => {
        if (committed) return;
        committed = true;
        const newStem = input.value.trim();
        if (!newStem || newStem === stem) {
            renderExistingAttachmentName(row, currentName);
            return;
        }
        const newName = newStem + ext;
        try {
            const res = await fetch(`/api/attachments/${attId}/rename`, {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ name: newName })
            });
            const data = await res.json();
            if (!res.ok || !data.success) {
                showToast((currentLang === 'ar' ? 'تعذر تعديل الاسم: ' : 'Failed to rename: ') + (data.error || ''), 'error');
                renderExistingAttachmentName(row, currentName);
                return;
            }
            renderExistingAttachmentName(row, data.name || newName);
            showToast(currentLang === 'ar' ? 'تمت إعادة التسمية' : 'Renamed', 'success');
        } catch (e) {
            showToast(currentLang === 'ar' ? 'تعذر تعديل الاسم' : 'Failed to rename', 'error');
            renderExistingAttachmentName(row, currentName);
        }
    };

    input.addEventListener('keydown', (e) => {
        if (e.key === 'Enter') { e.preventDefault(); commit(); }
        if (e.key === 'Escape') { e.preventDefault(); committed = true; renderExistingAttachmentName(row, currentName); }
    });
    input.addEventListener('blur', commit);

    nameSpan.replaceWith(input);
    input.focus();
    input.select();
}

// Restore the (possibly updated) name span for an existing-attachment row
// after a rename commit/cancel, preserving its preview click handler.
function renderExistingAttachmentName(row, name) {
    const input = row.querySelector('.file-item-rename-input');
    if (!input) return;
    const previewBtn = row.querySelector('.file-item-preview');
    const previewOnclick = previewBtn ? previewBtn.getAttribute('onclick') : '';
    const previewCall = previewOnclick ? previewOnclick.replace(/^event\.stopPropagation\(\);\s*/, '') : '';
    const span = document.createElement('span');
    span.className = 'file-item-name file-item-name--clickable';
    span.title = currentLang === 'ar' ? 'معاينة' : 'Preview';
    span.textContent = name;
    if (previewCall) span.setAttribute('onclick', previewCall);
    input.replaceWith(span);
}

function removeExistingAttachment(attId, name) {
    if (!attId) return;
    if (!window._removedAttachmentIds) window._removedAttachmentIds = [];
    if (!window._removedAttachmentIds.includes(attId)) window._removedAttachmentIds.push(attId);
    const row = document.getElementById(`existing-att-${attId}`);
    if (row) {
        row.style.transition = 'opacity .2s';
        row.style.opacity = '0';
        setTimeout(() => {
            row.remove();
            const list = document.getElementById('existingAttachmentsList');
            const wrap = document.getElementById('existingAttachmentsWrap');
            if (list && !list.children.length && wrap) wrap.style.display = 'none';
        }, 200);
    }
    showToast(currentLang === 'ar' ? `تمت إزالة "${name}"` : `"${name}" removed`, 'success');
}

// ── TRANSACTION: Delete (confirm modal → remove from list → 10s undo) ─────
// Flow: click Delete -> confirm modal -> on confirm, the row disappears from
// both the card view and table view immediately, and a 10-second Undo toast
// appears. Press Undo and it's reinserted exactly where it was, and nothing
// is ever sent to the server. Let the 10 seconds pass and the delete is
// committed to the server for real.
let _deletePendingId    = null;
let _deletePendingLabel = null;

function confirmDeleteTransaction(docId, regNumber) {
    _deletePendingId    = docId;
    _deletePendingLabel = regNumber || ('#' + docId);
    const modal = document.getElementById('deleteConfirmModal');
    const msgEl = document.getElementById('deleteConfirmMsg');
    if (!modal) return;
    if (msgEl) {
        msgEl.innerHTML = currentLang === 'ar'
            ? `هل أنت متأكد من حذف المعاملة <strong>${_deletePendingLabel}</strong>؟<br>ستتم إزالتها من القائمة، مع إمكانية التراجع خلال 10 ثوانٍ.`
            : `Are you sure you want to delete transaction <strong>${_deletePendingLabel}</strong>?<br>It will be removed from the list — you'll have 10 seconds to undo.`;
    }
    modal.style.display = 'grid';
}

function closeDeleteModal() {
    const modal = document.getElementById('deleteConfirmModal');
    if (modal) modal.style.display = 'none';
    _deletePendingId = null; _deletePendingLabel = null;
}

function executeDeleteTransaction() {
    if (!_deletePendingId) return;
    const docId = _deletePendingId;
    const label = _deletePendingLabel;
    closeDeleteModal();
    _startDeleteWithUndo(
        [docId],
        currentLang === 'ar' ? `تم حذف ${label}` : `Deleted ${label}`,
        currentLang === 'ar' ? `تم حذف المعاملة ${label}` : `Transaction ${label} deleted`
    );
}

// ── Shared undo-delete engine (single doc OR bulk) ─────────────────────────
// One pending "batch" at a time: a list of {id, doc, index} items removed
// from _searchAllDocs immediately, with a 10s window to put them all back.
let _undoDeleteTimer = null;
let _undoDeleteItems = [];   // [{ id, doc, index }]
let _undoDoneMsg     = '';   // toast shown after a successful commit

function _refreshSearchCount() {
    const countEl = document.getElementById('searchCount');
    if (countEl) countEl.textContent = `${_searchAllDocs.length} ${currentLang === 'ar' ? 'نتيجة' : 'result(s)'}`;
}

function _startDeleteWithUndo(ids, undoMsg, doneMsg) {
    // A previous batch still pending undo? Commit it now before starting a new one.
    if (_undoDeleteItems.length) _commitPendingDelete();

    const idSet = new Set(ids);
    const items = [];
    _searchAllDocs.forEach((d, i) => { if (idSet.has(d.id)) items.push({ id: d.id, doc: d, index: i }); });
    _undoDeleteItems = items;
    _undoDoneMsg = doneMsg;

    // Remove from the list/table right away — covers both card and table view.
    _searchAllDocs = _searchAllDocs.filter(d => !idSet.has(d.id));
    _renderSearchPage();
    _refreshSearchCount();

    showUndoToast(undoMsg, 10, _cancelPendingDelete);

    clearTimeout(_undoDeleteTimer);
    _undoDeleteTimer = setTimeout(_commitPendingDelete, 10000);
}

function _cancelPendingDelete() {
    if (!_undoDeleteItems.length) return;
    clearTimeout(_undoDeleteTimer);
    _undoDeleteTimer = null;
    hideUndoToast();

    // Reinsert in ascending original-index order so relative order is preserved.
    const items = _undoDeleteItems.slice().sort((a, b) => a.index - b.index);
    items.forEach(it => {
        const insertAt = Math.min(it.index, _searchAllDocs.length);
        _searchAllDocs.splice(insertAt, 0, it.doc);
    });
    _renderSearchPage();
    _refreshSearchCount();

    const n = _undoDeleteItems.length;
    _undoDeleteItems = [];
    _undoDoneMsg = '';

    showToast(
        currentLang === 'ar'
            ? (n > 1 ? `تم التراجع عن حذف ${n} عناصر` : 'تم التراجع عن الحذف')
            : (n > 1 ? `Undid delete of ${n} item(s)` : 'Delete undone'),
        'success'
    );
}

async function _commitPendingDelete() {
    const items = _undoDeleteItems;
    const doneMsg = _undoDoneMsg;
    if (!items.length) return;

    clearTimeout(_undoDeleteTimer);
    _undoDeleteTimer = null;
    _undoDeleteItems = [];
    _undoDoneMsg = '';
    hideUndoToast();

    const failedItems = [];
    await Promise.all(items.map(async it => {
        try {
            const res  = await fetch(`/api/documents/${it.id}`, { method: 'DELETE' });
            const data = await res.json();
            if (!res.ok || !data.success) {
                // Already gone server-side (e.g. deleted elsewhere) — fine, leave it removed.
                if (res.status === 404 || /already deleted/i.test(data.error || '')) return;
                failedItems.push(it);
            }
        } catch {
            failedItems.push(it);
        }
    }));

    if (failedItems.length) {
        // Only put back the ones that genuinely failed — they were never deleted.
        failedItems.slice().sort((a, b) => a.index - b.index).forEach(it => {
            const insertAt = Math.min(it.index, _searchAllDocs.length);
            _searchAllDocs.splice(insertAt, 0, it.doc);
        });
        _renderSearchPage();
        _refreshSearchCount();
        showToast(
            currentLang === 'ar'
                ? `فشل حذف ${failedItems.length} من ${items.length}`
                : `${failedItems.length} of ${items.length} failed to delete`,
            'error'
        );
    }

    if (failedItems.length < items.length) {
        if (typeof loadStats === 'function') loadStats();
        if (doneMsg) showToast(doneMsg, 'success');
    }
}

// Shared cleanup used when a document is deleted from OUTSIDE this flow
// (e.g. the chatbot deletes it directly and pushes a notification) — the
// server has already committed the delete, so this just syncs the UI.
function _afterDocumentDeleted(docId, label) {
    _searchAllDocs = _searchAllDocs.filter(d => d.id !== docId);
    _renderSearchPage();
    _refreshSearchCount();

    if (typeof loadStats === 'function') loadStats();

    showToast(
        label
            ? (currentLang === 'ar' ? `تم حذف المعاملة ${label}` : `Transaction ${label} deleted`)
            : (currentLang === 'ar' ? `تم حذف المستند #${docId}` : `Document #${docId} deleted`),
        'success'
    );
}

// ── Undo toast widget (separate from the plain #toast) ────────────────────
let _undoToastInterval = null;

function showUndoToast(message, seconds, onUndo) {
    let el = document.getElementById('undoToast');
    if (!el) {
        el = document.createElement('div');
        el.id = 'undoToast';
        el.innerHTML = `
            <span id="undoToastMsg"></span>
            <span id="undoToastCount" class="undo-toast-count"></span>
            <button type="button" id="undoToastBtn" class="undo-toast-btn"></button>
        `;
        document.body.appendChild(el);
    }

    document.getElementById('undoToastMsg').textContent = message;

    const btn = document.getElementById('undoToastBtn');
    btn.textContent = currentLang === 'ar' ? 'تراجع' : 'Undo';
    btn.title = /Mac/i.test(navigator.platform) ? '⌘Z' : 'Ctrl+Z';
    btn.onclick = () => { if (typeof onUndo === 'function') onUndo(); };

    let remaining = seconds;
    const countEl = document.getElementById('undoToastCount');
    countEl.textContent = `${remaining}s`;

    clearInterval(_undoToastInterval);
    _undoToastInterval = setInterval(() => {
        remaining -= 1;
        if (remaining <= 0) {
            clearInterval(_undoToastInterval);
            _undoToastInterval = null;
        } else {
            countEl.textContent = `${remaining}s`;
        }
    }, 1000);

    el.className = 'show';
}

function hideUndoToast() {
    const el = document.getElementById('undoToast');
    if (el) el.className = '';
    clearInterval(_undoToastInterval);
    _undoToastInterval = null;
}

// ── SCANNER: open from tree panel header (uses currently selected folder) ──
// Opens scanner modal from the Attachments panel — uses current selected folder
function openAttachScanner() {
    const input      = document.getElementById('volumeInput');
    const folderId   = input?.dataset.folderId   || _scanFolderId;
    const folderName = input?.value              || _scanFolderName || 'Attachments';
    if (!folderId) {
        showToast(currentLang === 'ar' ? 'اختر مجلداً من الشجرة أولاً' : 'Select a folder from the tree first', 'error');
        return;
    }
    openScannerModal(parseInt(folderId, 10), folderName, 'archive');
}

// Opens the scanner modal from the Workflow "New Request" attach strip —
// same upload/scan/camera/network-scanner flow as Archive, but queues
// results into _wfPendingFiles (attached to the wizard, not saved yet).
function openWfScanner() {
    openScannerModal(null, currentLang === 'ar' ? 'طلب سير عمل جديد' : 'New Workflow Request', 'workflow');
}

function openScannerFromTree() {
    const input = document.getElementById('volumeInput');
    const folderId   = input?.dataset.folderId;
    const folderName = input?.value || '';
    if (!folderId) {
        showToast(currentLang === 'ar' ? 'اختر مجلداً أولاً' : 'Select a folder first', 'error');
        return;
    }
    openScannerModal(parseInt(folderId, 10), folderName, 'folder');
}

// ═══════════════════════════════════════════════════════════════════════════
// SCANNER — archive workflow: scan pages → queue → attach to form OR save to folder
// Network scanners via eSCL (Canon, Ricoh, HP, Xerox, Kyocera, etc.)
// ═══════════════════════════════════════════════════════════════════════════

let _scanFolderId   = null;
let _scanFolderName = '';
let _scanMode       = 'folder';   // 'archive' = attach to form; 'folder' = save transaction now
let _scanFiles      = [];
let _cameraStream   = null;
let _cameraPhotos   = [];
let _activeScanTab  = 2;
let _netScannerEndpoint = null;   // { scheme, port, name } after test/discover
let _scanPersisted = false;        // true = close was a hide (not cancel); reopen restores state

function openStandaloneScanner() {
    showSection('archive');
    const input = document.getElementById('volumeInput');
    const folderId   = input?.dataset.folderId;
    const folderName = input?.value || '';
    if (!folderId) {
        showToast(
            currentLang === 'ar' ? 'اختر مجلداً من نموذج الأرشفة أولاً' : 'Select a folder in the archive form first',
            'error'
        );
        input?.focus();
        return;
    }
    openScannerModal(parseInt(folderId, 10), folderName, 'archive');
}

function openScannerModal(folderId, folderName, mode = 'folder') {
    const sameFolderReopen = _scanPersisted && _scanFolderId === (folderId || null) && _scanMode === mode;

    _scanFolderId   = folderId   || null;
    _scanFolderName = folderName || '';
    _scanMode       = mode;
    _netScannerEndpoint = null;

    const subtitle = document.getElementById('scannerModalFolder');
    if (subtitle) subtitle.innerHTML = _scanFolderName ? '<i class="ph ph-folder"></i> ' + escapeHtml(_scanFolderName) : '';

    if (!sameFolderReopen) {
        // Fresh open: reset everything
        _scanFiles      = [];
        _cameraPhotos   = [];
        _resetUploadTab();
        _resetCameraTab();
        _resetNetScanTab();
        _clearScanMetaFields();
        if (mode === 'archive') prefillScanMetaFromArchiveForm();
        if (mode === 'workflow') prefillScanMetaFromWorkflowForm();
    } else {
        // Reopen: restore UI state without clearing
        renderScanFileList();
        _renderCameraPhotos();
        _renderNetScanPages();
    }
    _scanPersisted = false;

    const modal = document.getElementById('scannerModal');
    if (modal) modal.style.display = 'grid';
    updateScanModeUi();
    scanSwitchTab(_activeScanTab || 2);
    setupScanDragDrop();
    netScanRestoreIp();
    _scanRenderExistingPmPages();
    const savedIp = (document.getElementById('netScanIp')?.value || '').trim();
    if (savedIp) setTimeout(() => netScanTest(), 400);
}

function closeScannerModal(discard = false) {
    if (discard) {
        // Hard cancel: clear all state
        scanCameraStop();
        _netScanPages.forEach(p => URL.revokeObjectURL(p.url));
        _netScanPages = [];
        _scanFiles    = [];
        _cameraPhotos = [];
        _scanPersisted = false;
        const wrap = document.getElementById('scanExistingPagesWrap');
        if (wrap) wrap.style.display = 'none';
    } else {
        // Soft close: persist state so reopening restores it
        scanCameraStop();
        _scanPersisted = true;
    }
    const wasPageManagerScan = _scanMode === 'pagemanager';
    const modal = document.getElementById('scannerModal');
    if (modal) modal.style.display = 'none';
    if (discard) {
        _scanFolderId = null;
    }
    const footer = document.getElementById('scanFooterStatus');
    if (footer) { footer.textContent = ''; footer.className = 'scan-status'; }

    // Cancelling a scan that was opened from Manage Pages should return the
    // user to Manage Pages rather than dropping them with no modal open.
    if (wasPageManagerScan && discard) {
        const pmModal = document.getElementById('pageManagerModal');
        if (pmModal && _pmMode) pmModal.style.display = 'flex';
    }
}

function updateScanModeUi() {
    const hint = document.getElementById('scanModeHint');
    const saveLabel = document.getElementById('scanSaveBtnLabel');
    const mergeOptWrap = document.getElementById('scanMergeOptWrap');
    if (mergeOptWrap) mergeOptWrap.style.display = (_scanMode === 'pagemanager') ? 'none' : '';
    if (!hint) return;
    hint.style.display = '';
    if (_scanMode === 'archive') {
        hint.textContent = currentLang === 'ar'
            ? 'الصفحات تُرفق بنموذج الأرشفة — بعد الإغلاق اضغط «حفظ المستند»'
            : 'Pages attach to the archive form — then click Save Document';
        if (saveLabel) saveLabel.textContent = currentLang === 'ar' ? 'إرفاق بالنموذج' : 'Attach to form';
    } else if (_scanMode === 'workflow') {
        hint.textContent = currentLang === 'ar'
            ? 'الصفحات تُرفق بطلب سير العمل — أكمل البيانات ثم أرسل للاعتماد'
            : 'Pages attach to the workflow request — then complete the form and send';
        if (saveLabel) saveLabel.textContent = currentLang === 'ar' ? 'إرفاق بالطلب' : 'Attach to request';
    } else if (_scanMode === 'pagemanager') {
        hint.textContent = currentLang === 'ar'
            ? 'الصفحات الممسوحة ستُضاف إلى المستند الحالي في مدير الصفحات'
            : 'Scanned pages will be added to the document you were editing';
        if (saveLabel) saveLabel.textContent = currentLang === 'ar' ? 'إضافة إلى المستند' : 'Add to document';
    } else {
        hint.textContent = currentLang === 'ar'
            ? 'يُحفظ مباشرة كمعاملة جديدة في المجلد المحدد'
            : 'Saves immediately as a new transaction in this folder';
        if (saveLabel) saveLabel.textContent = currentLang === 'ar' ? 'حفظ في الأرشيف' : 'Save to archive';
    }
}

function prefillScanMetaFromArchiveForm() {
    const subj = document.getElementById('topicInput')?.value || '';
    const kw   = document.getElementById('keywordsInput')?.value || '';
    const notes = document.getElementById('statementInput')?.value || '';
    const s = document.getElementById('scanSubject');   if (s && subj)  s.value = subj;
    const k = document.getElementById('scanKeywords'); if (k && kw)    k.value = kw;
    const n = document.getElementById('scanNotes');    if (n && notes) n.value = notes;
}

function prefillScanMetaFromWorkflowForm() {
    const subj = document.getElementById('wfTopicInput')?.value || '';
    const kw   = document.getElementById('wfKeywordsInput')?.value || '';
    const notes = document.getElementById('wfStatementInput')?.value || '';
    const s = document.getElementById('scanSubject');   if (s && subj)  s.value = subj;
    const k = document.getElementById('scanKeywords'); if (k && kw)    k.value = kw;
    const n = document.getElementById('scanNotes');    if (n && notes) n.value = notes;
}

function _clearScanMetaFields() {
    ['scanSubject', 'scanKeywords', 'scanNotes'].forEach(id => {
        const el = document.getElementById(id);
        if (el) el.value = '';
    });
}

function getScanMeta() {
    return {
        subject:  (document.getElementById('scanSubject')?.value  || '').trim(),
        keywords: (document.getElementById('scanKeywords')?.value || '').trim(),
        notes:    (document.getElementById('scanNotes')?.value    || '').trim(),
        mergePdf: !!document.getElementById('scanMergePdf')?.checked,
    };
}

function applyScanMetaToArchiveForm(meta) {
    const setIfEmpty = (id, val) => {
        const el = document.getElementById(id);
        if (el && val && !el.value.trim()) el.value = val;
    };
    setIfEmpty('topicInput', meta.subject);
    setIfEmpty('keywordsInput', meta.keywords);
    setIfEmpty('statementInput', meta.notes);
}

function applyScanMetaToWorkflowForm(meta) {
    const setIfEmpty = (id, val) => {
        const el = document.getElementById(id);
        if (el && val && !el.value.trim()) el.value = val;
    };
    setIfEmpty('wfTopicInput', meta.subject);
    setIfEmpty('wfKeywordsInput', meta.keywords);
    setIfEmpty('wfStatementInput', meta.notes);
}

function scanSwitchTab(idx) {
    _activeScanTab = idx;
    [0, 1, 2].forEach(i => {
        const pane = document.getElementById(`scanPane${i}`);
        if (pane) pane.style.display = i === idx ? '' : 'none';
    });
    const tabIds = { 0: 'scanTabImport', 1: 'scanTabCamera', 2: 'scanTabScanner' };
    ['scanTabScanner', 'scanTabImport', 'scanTabCamera'].forEach(id => {
        document.getElementById(id)?.classList.remove('active');
    });
    document.getElementById(tabIds[idx])?.classList.add('active');
    if (idx !== 1) scanCameraStop();
    syncScanFooterActions();
}

function syncScanFooterActions() {
    const count = _netScanPages.length + _scanFiles.length + _cameraPhotos.length;
    const clearBtn = document.getElementById('netScanClearBtn');
    const dlBtn    = document.getElementById('netScanLocalDownloadBtn');
    if (clearBtn) clearBtn.style.display = count ? '' : 'none';
    if (dlBtn)    dlBtn.style.display    = _netScanPages.length ? '' : 'none';
    const counter = document.getElementById('netScanPageCounter');
    if (counter) {
        counter.style.display = _netScanPages.length ? '' : 'none';
        counter.textContent = currentLang === 'ar'
            ? `${_netScanPages.length} صفحة`
            : `${_netScanPages.length} page(s)`;
    }
}

function collectAllScanOutputs() {
    const files = [];
    _netScanPages.forEach(p => {
        files.push(new File([p.blob], p.name, { type: p.blob.type || 'application/octet-stream' }));
    });
    _scanFiles.forEach(f => files.push(f));
    _cameraPhotos.forEach((p, i) => {
        const name = p.name || `photo_${i + 1}.jpg`;
        files.push(p instanceof File ? p : new File([p], name, { type: p.type || 'image/jpeg' }));
    });
    return files;
}

// ── PDF FILENAME PROMPT ──────────────────────────────────────────────────
// Shows a small inline prompt in the scanner footer asking the user to name
// the PDF before saving. Returns a Promise<string|null> — null means Cancel.
// NOTE: Does NOT touch any scanner/USB/eSCL logic whatsoever.
function promptPdfFileName(defaultName) {
    return new Promise(resolve => {
        // Remove any existing prompt
        const existing = document.getElementById('_pdfNamePrompt');
        if (existing) existing.remove();

        const isAr = currentLang === 'ar';
        const overlay = document.createElement('div');
        overlay.id = '_pdfNamePrompt';
        overlay.style.cssText = [
            'position:fixed', 'inset:0', 'z-index:9999',
            'display:flex', 'align-items:center', 'justify-content:center',
            'background:rgba(0,0,0,0.45)', 'backdrop-filter:blur(2px)'
        ].join(';');

        overlay.innerHTML = `
            <div style="
                background:var(--bg-card,#fff);
                border:1px solid var(--border,#e2e8f0);
                border-radius:12px;
                padding:24px 28px;
                min-width:320px;
                max-width:90vw;
                box-shadow:0 8px 32px rgba(0,0,0,0.18);
                display:flex;flex-direction:column;gap:14px;
                direction:${isAr ? 'rtl' : 'ltr'}
            ">
                <div style="display:flex;align-items:center;gap:8px;font-weight:600;font-size:15px;color:var(--text-primary,#1e293b)">
                    <i class="ph ph-file-pdf" style="font-size:20px;color:var(--accent,#3b82f6)"></i>
                    <span>${isAr ? 'اسم ملف PDF' : 'Name your PDF file'}</span>
                </div>
                <div style="font-size:13px;color:var(--text-muted,#64748b)">
                    ${isAr ? 'أدخل اسماً للملف قبل الحفظ:' : 'Enter a filename before saving:'}
                </div>
                <div style="display:flex;align-items:center;gap:6px">
                    <input id="_pdfNameInput" type="text"
                        value="${(defaultName || 'document').replace(/\.pdf$/i, '')}"
                        placeholder="${isAr ? 'اسم الملف' : 'filename'}"
                        style="
                            flex:1;padding:8px 12px;border:1px solid var(--border,#e2e8f0);
                            border-radius:7px;font-size:14px;outline:none;
                            background:var(--bg-input,#f8fafc);color:var(--text-primary,#1e293b);
                        "
                        autocomplete="off" spellcheck="false"
                    >
                    <span style="font-size:13px;color:var(--text-muted,#64748b);white-space:nowrap">.pdf</span>
                </div>
                <div style="display:flex;gap:10px;justify-content:flex-end;margin-top:4px">
                    <button id="_pdfNameCancel" type="button" style="
                        padding:8px 18px;border-radius:7px;border:1px solid var(--border,#e2e8f0);
                        background:var(--bg-card,#fff);color:var(--text-primary,#1e293b);
                        cursor:pointer;font-size:14px;
                    ">${isAr ? 'إلغاء' : 'Cancel'}</button>
                    <button id="_pdfNameOk" type="button" style="
                        padding:8px 20px;border-radius:7px;border:none;
                        background:var(--accent,#3b82f6);color:#fff;
                        cursor:pointer;font-size:14px;font-weight:600;
                    ">${isAr ? 'حفظ' : 'Save'}</button>
                </div>
            </div>`;

        document.body.appendChild(overlay);

        const input  = document.getElementById('_pdfNameInput');
        const okBtn  = document.getElementById('_pdfNameOk');
        const canBtn = document.getElementById('_pdfNameCancel');

        // Select all text for easy replacement
        setTimeout(() => { input.focus(); input.select(); }, 60);

        function finish(value) {
            overlay.remove();
            resolve(value);
        }

        okBtn.addEventListener('click', () => {
            const name = input.value.trim();
            if (!name) { input.focus(); return; }
            finish(name.endsWith('.pdf') ? name : name + '.pdf');
        });

        canBtn.addEventListener('click', () => finish(null));

        // Enter = confirm, Escape = cancel
        input.addEventListener('keydown', e => {
            if (e.key === 'Enter')  { okBtn.click(); }
            if (e.key === 'Escape') { finish(null); }
        });

        // Click outside = cancel
        overlay.addEventListener('click', e => { if (e.target === overlay) finish(null); });
    });
}

// ── CLIENT-SIDE PDF MERGE (for Attach to form) ──────────────────────────
async function mergeFilesToPdf(files, pdfName) {
    const { PDFDocument } = PDFLib;
    const merged = await PDFDocument.create();

    for (const file of files) {
        const ab = await file.arrayBuffer();
        const mime = file.type || '';

        if (mime === 'application/pdf' || file.name.toLowerCase().endsWith('.pdf')) {
            try {
                const srcPdf = await PDFDocument.load(ab, { ignoreEncryption: true });
                const pages  = await merged.copyPages(srcPdf, srcPdf.getPageIndices());
                pages.forEach(p => merged.addPage(p));
            } catch(e) {
                console.warn('Could not merge PDF:', file.name, e);
            }
        } else if (mime.startsWith('image/') || /\.(png|jpe?g|gif|webp|bmp)$/i.test(file.name)) {
            try {
                let imgEmbed;
                if (mime === 'image/png' || file.name.toLowerCase().endsWith('.png')) {
                    imgEmbed = await merged.embedPng(ab);
                } else {
                    imgEmbed = await merged.embedJpg(ab);
                }
                const page = merged.addPage([imgEmbed.width, imgEmbed.height]);
                page.drawImage(imgEmbed, { x: 0, y: 0, width: imgEmbed.width, height: imgEmbed.height });
            } catch(e) {
                console.warn('Could not embed image:', file.name, e);
            }
        }
        // Non-image/non-PDF files (docx, xlsx etc) are kept as-is — can't embed them
    }

    // Use the caller-supplied name, or fall back to a timestamp-based default
    const finalName = pdfName || ('document_' + Date.now() + '.pdf');
    const pdfBytes = await merged.save();
    return new File([pdfBytes], finalName, { type: 'application/pdf' });
}

async function scanSaveToArchive() {
    if (_scanMode !== 'workflow' && _scanMode !== 'pagemanager' && !_scanFolderId) {
        showToast(currentLang === 'ar' ? 'لم يُحدد مجلد' : 'No folder selected', 'error');
        return;
    }
    const files = collectAllScanOutputs();
    if (!files.length) {
        showToast(currentLang === 'ar' ? 'امسح أو أضف صفحات أولاً' : 'Scan or add pages first', 'error');
        return;
    }
    const meta = getScanMeta();
    const footer = document.getElementById('scanFooterStatus');
    const setF = (msg, cls) => {
        if (footer) { footer.textContent = msg; footer.className = 'scan-status ' + (cls || ''); }
    };

    // ── Determine a sensible default filename ────────────────────────────────
    // Only prompt for a PDF name when merging. When not merging, each file
    // keeps its own original name and no rename prompt is needed.
    // In pagemanager mode there's no separate file to name — pages are added
    // straight into the document already open in Manage Pages — so skip this.
    let defaultName;
    if (_scanMode === 'pagemanager') {
        defaultName = null;
    } else if (meta.mergePdf) {
        // Will be merged into one PDF — suggest subject or a dated name
        const subj = (meta.subject || '').trim();
        defaultName = subj ? subj : ('document_' + new Date().toISOString().slice(0, 10));
    } else if (files.length === 1) {
        // Single file — suggest its current name (without extension)
        defaultName = files[0].name.replace(/\.[^.]+$/, '');
    } else {
        // Multiple files, no merge — skip the PDF name prompt entirely;
        // each file will be saved under its own original name.
        defaultName = null;
    }

    // ── Prompt the user to name the PDF (only when merging or single file) ──
    let chosenName = null;
    let pdfFileName = null;
    if (defaultName !== null) {
        chosenName = await promptPdfFileName(defaultName);
        if (chosenName === null) {
            // User cancelled — abort save, leave modal open
            setF('', '');
            return;
        }
        pdfFileName = chosenName.endsWith('.pdf') ? chosenName : chosenName + '.pdf';
    }

    // ── ARCHIVE MODE: attach scanned/imported files to the document form ─────
    if (_scanMode === 'archive') {
        setF(currentLang === 'ar' ? 'جارٍ المعالجة…' : 'Processing…', 'scan-status--loading');
        let finalFiles = files;

        if (meta.mergePdf && files.length > 0) {
            try {
                // Pass the user-chosen name to the merge function
                const merged = await mergeFilesToPdf(files, pdfFileName);
                finalFiles = [merged];
            } catch(e) {
                console.error('PDF merge failed:', e);
                setF('⚠ PDF merge failed — attaching original files', 'scan-status--error');
                finalFiles = files;
            }
        } else {
            // Not merging — keep each file under its own original name.
            // For a single file the user was prompted for a name; rename that one.
            // For multiple files (no merge) no prompt was shown — leave names as-is.
            finalFiles = files.map((f, idx) => {
                if (pdfFileName && files.length === 1 &&
                    (f.type === 'application/pdf' || f.name.toLowerCase().endsWith('.pdf'))) {
                    return new File([f], pdfFileName, { type: f.type || 'application/pdf' });
                }
                return f;
            });
        }

        // Only skip adding if the exact same file (name + size) is already queued.
        // Never auto-remove existing files — user must press ✕ to remove manually.
        let added = 0;
        finalFiles.forEach(f => {
            const dup = _archivePendingFiles.some(x => x.name === f.name && x.size === f.size);
            if (!dup) { _archivePendingFiles.push(f); added++; }
        });
        renderArchiveFileList();
        syncArchiveFileInput();
        applyScanMetaToArchiveForm(meta);
        goToStep(3);
        showToast(
            currentLang === 'ar'
                ? `تم إرفاق ${added} ملف — أكمل البيانات واضغط حفظ المستند`
                : `${added} file(s) attached — complete the form and click Save Document`,
            'success'
        );
        closeScannerModal(true);   // hard reset: next open starts with a clean slate
        showSection('archive');
        return;
    }

    // ── PAGE MANAGER MODE: append scanned/imported pages into the currently-
    // open Manage Pages session, then reopen it (instead of going anywhere else) ─
    if (_scanMode === 'pagemanager') {
        setF(currentLang === 'ar' ? 'جارٍ المعالجة…' : 'Processing…', 'scan-status--loading');
        // No merge step and no filename prompt here — every scanned/imported
        // page is simply appended to the document already open in Manage
        // Pages (multi-page PDFs are still expanded page-by-page).
        await _pmAddFilesAsPages(files);
        _pmRenderGrid();
        closeScannerModal(true);   // hard reset: next scan starts with a clean slate
        const pmModal = document.getElementById('pageManagerModal');
        if (pmModal) pmModal.style.display = 'flex';
        showToast(currentLang === 'ar' ? 'تمت إضافة الصفحات الممسوحة' : 'Scanned pages added', 'success');
        return;
    }

    // ── WORKFLOW MODE: attach scanned/imported files to the New Request wizard ─
    if (_scanMode === 'workflow') {
        setF(currentLang === 'ar' ? 'جارٍ المعالجة…' : 'Processing…', 'scan-status--loading');
        let finalFiles = files;

        if (meta.mergePdf && files.length > 0) {
            try {
                const merged = await mergeFilesToPdf(files, pdfFileName);
                finalFiles = [merged];
            } catch(e) {
                console.error('PDF merge failed:', e);
                setF('⚠ PDF merge failed — attaching original files', 'scan-status--error');
                finalFiles = files;
            }
        } else {
            finalFiles = files.map((f) => {
                if (pdfFileName && files.length === 1 &&
                    (f.type === 'application/pdf' || f.name.toLowerCase().endsWith('.pdf'))) {
                    return new File([f], pdfFileName, { type: f.type || 'application/pdf' });
                }
                return f;
            });
        }

        let added = 0;
        finalFiles.forEach(f => {
            const dup = _wfPendingFiles.some(x => x.name === f.name && x.size === f.size);
            if (!dup) { _wfPendingFiles.push(f); added++; }
        });
        _wfRenderPendingFiles();
        if (_wfPendingFiles.length) {
            document.getElementById('wfAttachMainBox')?.classList.add('has-file');
            wfSetStep(2);
        }
        applyScanMetaToWorkflowForm(meta);
        showToast(
            currentLang === 'ar'
                ? `تم إرفاق ${added} ملف — أكمل البيانات وأرسل الطلب`
                : `${added} file(s) attached — complete the form and send the request`,
            'success'
        );
        closeScannerModal(true);   // hard reset: next open starts with a clean slate
        // NOTE: don't call showSection('workflow') here — it unconditionally
        // resets the New Request form (_wfResetNewRequestForm), which would
        // wipe out the files we just attached. We're already on this section,
        // so just make sure the New Request tab is showing.
        switchWfTab('new');
        return;
    }

    // ── FOLDER MODE: save directly to archive folder ─────────────────────────
    setF(currentLang === 'ar' ? 'جارٍ الحفظ…' : 'Saving…', 'scan-status--loading');
    const fd = new FormData();

    if (meta.mergePdf && files.length > 1) {
        // Client-side merge with the user-chosen name, then upload the single merged file
        let mergedFile;
        try {
            mergedFile = await mergeFilesToPdf(files, pdfFileName);
        } catch(e) {
            console.error('PDF merge failed:', e);
            setF('⚠ PDF merge failed — uploading original files', 'scan-status--error');
            mergedFile = null;
        }
        if (mergedFile) {
            fd.append('files', mergedFile);
        } else {
            files.forEach(f => fd.append('files', f));
        }
        // Tell the server NOT to re-merge (we already did it client-side)
        // fd.append('merge_to_pdf', '0');  // omit merge flag
    } else {
        // Not merging — keep each file under its own original name.
        // Only rename if it's a single file and the user was prompted for a name.
        files.forEach((f, idx) => {
            if (pdfFileName && files.length === 1 &&
                (f.type === 'application/pdf' || f.name.toLowerCase().endsWith('.pdf'))) {
                fd.append('files', new File([f], pdfFileName, { type: f.type || 'application/pdf' }));
            } else {
                fd.append('files', f);
            }
        });
        if (meta.mergePdf) fd.append('merge_to_pdf', '1');
    }

    if (meta.subject)  fd.append('subject',  meta.subject);
    if (meta.keywords) fd.append('keywords', meta.keywords);
    if (meta.notes)    fd.append('notes',    meta.notes);

    try {
        const res  = await fetch(`/api/folders/${_scanFolderId}/scan`, { method: 'POST', body: fd });
        const data = await res.json();
        if (!res.ok || !data.success) {
            setF('⚠ ' + (data.error || 'Save failed'), 'scan-status--error');
            return;
        }
        setF(
            currentLang === 'ar'
                ? `✓ معاملة #${data.transaction_id} — ${data.files_saved} ملف`
                : `✓ Transaction #${data.transaction_id} — ${data.files_saved} file(s)`,
            'scan-status--success'
        );
        showToast(currentLang === 'ar' ? 'تم حفظ المسح في الأرشيف' : 'Scan saved to archive', 'success');
        setTimeout(closeScannerModal, 1200);
    } catch (e) {
        setF('⚠ Connection error', 'scan-status--error');
    }
}

// ════════════════════════════════════════════════════════════════════════════
// TAB 0 — UPLOAD FILES
// ════════════════════════════════════════════════════════════════════════════
function _resetUploadTab() {
    const list   = document.getElementById('scanFileList');
    const status = document.getElementById('scanStatus');
    if (list)   list.innerHTML = '';
    if (status) { status.textContent = ''; status.className = 'scan-status'; }
    renderScanFileList();
}

function setupScanDragDrop() {
    const box = document.getElementById('scanDropZone');
    if (!box || box._bound) return;
    box._bound = true;
    box.addEventListener('dragover',  e => { e.preventDefault(); box.classList.add('drag-over'); });
    box.addEventListener('dragleave', ()  => box.classList.remove('drag-over'));
    box.addEventListener('drop', e => {
        e.preventDefault();
        box.classList.remove('drag-over');
        addScanFiles(Array.from(e.dataTransfer.files));
    });
}

function scanFileInputChange(input) {
    addScanFiles(Array.from(input.files));
    input.value = '';
}

function addScanFiles(files) {
    files.forEach(f => _scanFiles.push(f));
    renderScanFileList();
    syncScanFooterActions();
    syncScanOcrVisibility();
    if (_ocrResultFile && _ocrResultFile !== _ocrSupportedFile()) {
        dismissOcrResult();
    }
}

function removeScanFile(idx) {
    const removed = _scanFiles[idx];
    _scanFiles.splice(idx, 1);
    renderScanFileList();
    syncScanFooterActions();
    syncScanOcrVisibility();
    _dismissOcrIfRemoved(removed);
}

// Dismiss the OCR result if it belonged to the item just removed
// (matches either the raw blob/file or its wrapped File counterpart).
// Checks both OCR preview boxes (Upload Files tab and Network Scanner tab)
// since the removed item could be the one either of them is showing.
function _dismissOcrIfRemoved(removedItem) {
    if (!removedItem) return;
    if (_ocrResultFile && (_ocrResultFile === removedItem || _ocrResultFile === removedItem.__ocrFile)) {
        dismissOcrResult();
    }
    if (_netScanOcrResultFile && (_netScanOcrResultFile === removedItem || _netScanOcrResultFile === removedItem.__ocrFile)) {
        dismissNetScanOcrResult();
    }
}

function renderScanFileList() {
    const list = document.getElementById('scanFileList');
    if (!list) return;
    list.innerHTML = _scanFiles.map((f, i) => `
        <div class="scan-file-item">
            <span>${getFileIcon(f.name)}</span>
            <span class="scan-file-name">${f.name}</span>
            <span class="scan-file-size">${formatBytes(f.size)}</span>
            <button type="button" class="file-item-remove" onclick="removeScanFile(${i})">✕</button>
        </div>`).join('');
}

// ── OCR (Extract Text) ───────────────────────────────────────────────────────────────────────────
// OCR is opt-in: only runs when the user clicks "Extract Text".
// Never triggers automatically on every transaction.
const OCR_ENABLED = true;

const _OCR_SUPPORTED_EXT = ['pdf', 'png', 'jpg', 'jpeg', 'tif', 'tiff', 'bmp', 'gif', 'webp'];

function _fileExt(name) {
    return (name || '').split('.').pop().toLowerCase();
}

// Returns the most relevant OCR-eligible file/blob across all scan sources
// (attached files, camera photos, network-scanner pages), as a File/Blob
// with a usable .name. Prefers the most recently added item overall.
function _ocrSupportedFile() {
    // Camera photos and network-scanner pages are always JPEG images —
    // always OCR-eligible. Prefer the most recently captured/scanned page.
    if (_netScanPages.length) {
        const last = _netScanPages[_netScanPages.length - 1];
        return _toOcrFile(last.blob, last.name || 'scan.jpg');
    }
    if (_cameraPhotos.length) {
        const last = _cameraPhotos[_cameraPhotos.length - 1];
        return _toOcrFile(last, last.name || 'photo.jpg');
    }
    const f = _scanFiles.find(f => _OCR_SUPPORTED_EXT.includes(_fileExt(f.name)));
    return f || null;
}

// Wrap a Blob (which may lack .name) into a File-like object with a name,
// suitable for sending to /api/ocr via FormData. Caches the wrapper on the
// blob itself so repeated calls return the same object (stable identity).
function _toOcrFile(blob, name) {
    if (!blob) return null;
    if (blob instanceof File && blob.name) return blob;
    if (blob.__ocrFile) return blob.__ocrFile;
    let wrapped;
    try {
        wrapped = new File([blob], name, { type: blob.type || 'image/jpeg' });
    } catch (_) {
        blob.name = name;
        wrapped = blob;
    }
    try { blob.__ocrFile = wrapped; } catch (_) { /* ignore */ }
    return wrapped;
}

function syncScanOcrVisibility() {
    const hasFile = !!_ocrSupportedFile();
    const wrap = document.getElementById('scanOcrWrap');
    if (wrap) wrap.style.display = hasFile ? '' : 'none';
    const netWrap = document.getElementById('netScanOcrWrap');
    if (netWrap) netWrap.style.display = hasFile ? '' : 'none';
    if (!hasFile) {
        dismissOcrResult();
        dismissNetScanOcrResult();
    }
}

// Tracks which attached File object the current OCR result/preview belongs to
// (separately for the Upload-Files tab and the Network/USB Scanner tab, since
// each has its own preview box — but both share the autofill-undo logic below).
let _ocrResultFile = null;
let _netScanOcrResultFile = null;

// Tracks original filenames the user has actually run "Extract Text" on (and
// got a non-empty result for) during this archive-form session. Sent along
// with the save request so the backend only runs/stores OCR for files the
// user opted into — anything merely attached without using OCR stays a plain
// file with no OCR text saved. Cleared on clearForm() / after a save.
let _ocrUsedFileNames = new Set();

// Holds the actual extracted text per filename, keyed the same way as
// _ocrUsedFileNames above. Sent with the save request so the backend can
// store it directly instead of re-running OCR a second time on the saved
// file (previously the preview ran OCR once here, then save silently ran
// it again — this lets save skip straight to storing the already-seen
// result). Cleared together with _ocrUsedFileNames.
let _ocrTextByFileName = {};

// Tracks the ids of document-form fields that the most recent OCR run actually
// auto-filled (set inside applyOcrAutofill). Pressing "✕ Close" on EITHER OCR
// preview box calls _resetOcrAutofill() so the OCR result is fully removed —
// not just hidden — restoring exactly those fields to empty, no matter which
// "cross" button (Upload Files tab or Network Scanner tab) was pressed.
let _ocrAutofilledFields = null;

function _resetOcrAutofill() {
    if (!_ocrAutofilledFields) return;
    _ocrAutofilledFields.forEach(id => {
        if (id === 'documentDate') {
            const yEl = document.getElementById('docDateY');
            const mEl = document.getElementById('docDateM');
            const dEl = document.getElementById('docDateD');
            if (yEl) yEl.value = '';
            if (mEl) mEl.value = '';
            if (dEl) dEl.value = '';
            if (typeof _syncDocDate === 'function') _syncDocDate();
            return;
        }
        if (id === 'importanceSelect' || id === 'confidentialitySelect') {
            const el = document.getElementById(id);
            if (el) el.value = '1'; // back to the default ("Normal") option
            return;
        }
        const el = document.getElementById(id);
        if (el) el.value = '';
    });
    _ocrAutofilledFields = null;
}

function dismissOcrResult() {
    const result = document.getElementById('scanOcrResult');
    const status = document.getElementById('scanOcrStatus');
    if (result) result.style.display = 'none';
    if (status) status.textContent = '';
    _ocrResultFile = null;
    _resetOcrAutofill();
}

function dismissNetScanOcrResult() {
    const result = document.getElementById('netScanOcrResult');
    const status = document.getElementById('netScanOcrStatus');
    if (result) result.style.display = 'none';
    if (status) status.textContent = '';
    _netScanOcrResultFile = null;
    _resetOcrAutofill();
}

async function runOcrOnScanFile() {
    const status = document.getElementById('scanOcrStatus');
    if (OCR_ENABLED === false) {
        if (status) status.textContent = currentLang === 'ar'
            ? 'ميزة استخراج النص (OCR) معطّلة مؤقتًا'
            : 'Text extraction (OCR) is temporarily disabled';
        return;
    }
    const file = _ocrSupportedFile();
    const btn = document.getElementById('scanOcrBtn');
    if (!file) {
        if (status) status.textContent = currentLang === 'ar'
            ? 'لا يوجد ملف مناسب لاستخراج النص'
            : 'No supported file to extract text from';
        return;
    }

    if (btn) btn.disabled = true;
    if (status) status.textContent = currentLang === 'ar' ? 'جاري استخراج النص...' : 'Extracting text...';

    try {
        const data = await _requestOcr(file);
        const text = (data.text || '').trim();
        if (!text) {
            status.textContent = _ocrEmptyTextMessage(data.reason);
        } else {
            status.textContent = currentLang === 'ar' ? 'تم استخراج النص بنجاح' : 'Text extracted successfully';
            const resultBox = document.getElementById('scanOcrResult');
            const textArea = document.getElementById('scanOcrText');
            if (textArea) textArea.value = text;
            if (resultBox) resultBox.style.display = '';
            _ocrResultFile = file;
            if (file.name) {
                _ocrUsedFileNames.add(file.name);
                _ocrTextByFileName[file.name] = text;
            }

            // Auto-fill Subject, Keywords, Document Date, etc. — only if empty
            applyOcrAutofill(text, file);
        }
    } catch (e) {
        if (status) status.textContent = (currentLang === 'ar' ? 'خطأ: ' : 'Error: ') + (e.message || e);
    } finally {
        if (btn) btn.disabled = false;
    }
}

// Extract Text (OCR) button on the Network/USB Scanner tab — works on the
// same most-recent OCR-eligible source as the Upload Files tab (scanned
// page, camera photo, or uploaded file — whichever is most recent), but
// renders into the Scanner tab's own preview box.
async function runOcrOnScanPages() {
    const status = document.getElementById('netScanOcrStatus');
    if (OCR_ENABLED === false) {
        if (status) status.textContent = currentLang === 'ar'
            ? 'ميزة استخراج النص (OCR) معطّلة مؤقتًا'
            : 'Text extraction (OCR) is temporarily disabled';
        return;
    }
    const file = _ocrSupportedFile();
    const btn = document.getElementById('netScanOcrBtn');
    if (!file) {
        if (status) status.textContent = currentLang === 'ar'
            ? 'لا يوجد ملف مناسب لاستخراج النص'
            : 'No supported file to extract text from';
        return;
    }

    if (btn) btn.disabled = true;
    if (status) status.textContent = currentLang === 'ar' ? 'جاري استخراج النص...' : 'Extracting text...';

    try {
        const data = await _requestOcr(file);
        const text = (data.text || '').trim();
        if (!text) {
            status.textContent = _ocrEmptyTextMessage(data.reason);
        } else {
            status.textContent = currentLang === 'ar' ? 'تم استخراج النص بنجاح' : 'Text extracted successfully';
            const resultBox = document.getElementById('netScanOcrResult');
            const textArea = document.getElementById('netScanOcrText');
            if (textArea) textArea.value = text;
            if (resultBox) resultBox.style.display = '';
            _netScanOcrResultFile = file;
            if (file.name) {
                _ocrUsedFileNames.add(file.name);
                _ocrTextByFileName[file.name] = text;
            }

            // Auto-fill Subject, Keywords, Document Date, etc. — only if empty
            applyOcrAutofill(text, file);
        }
    } catch (e) {
        if (status) status.textContent = (currentLang === 'ar' ? 'خطأ: ' : 'Error: ') + (e.message || e);
    } finally {
        if (btn) btn.disabled = false;
    }
}

// Sends a file/blob to the OCR endpoint and returns the parsed JSON,
// throwing on a failed request. Shared by both OCR entry points above.
async function _requestOcr(file) {
    const fd = new FormData();
    fd.append('file', file);
    const res = await fetch('/api/ocr', { method: 'POST', body: fd });
    const data = await res.json().catch(() => ({}));
    if (!res.ok || data.error) {
        throw new Error(data.error || `OCR failed (${res.status})`);
    }
    return data;
}

// Maps an OCR "no text found" reason code to a user-facing message in the
// current language. Shared by both OCR entry points above.
function _ocrEmptyTextMessage(reason) {
    reason = reason || '';
    let msg = currentLang === 'ar' ? 'لم يتم العثور على نص' : 'No text found';
    if (reason === 'ocr_unavailable' || reason === 'pdf2image_unavailable') {
        msg = currentLang === 'ar'
            ? 'محرك التعرف الضوئي على الحروف غير مهيأ على الخادم'
            : 'OCR engine is not set up on the server (missing Tesseract/Poppler)';
    } else if (reason.startsWith('pdf_render_error')) {
        msg = currentLang === 'ar'
            ? 'تعذر تحويل صفحات PDF إلى صور (تحقق من تثبيت Poppler)'
            : 'Could not render PDF pages for OCR (check Poppler install)';
    } else if (reason.startsWith('ocr_error')) {
        msg = currentLang === 'ar' ? 'فشل التعرف الضوئي على الحروف' : 'OCR failed while reading the file';
    } else if (reason === 'no_text_detected') {
        msg = currentLang === 'ar'
            ? 'لم يتم اكتشاف نص — قد يكون المستند فارغًا أو منخفض الجودة'
            : 'No text detected — the scan may be blank or low quality';
    }
    return msg + (reason ? ` [${reason}]` : '');
}

// Derive a clean Subject line, candidate Keywords, and Notes text from OCR
// output, and fill empty fields only — never overwrites what the user typed.
function applyOcrAutofill(text, file) {
    // Tracks exactly which fields THIS run fills, so a later "✕ Close" on the
    // OCR result can undo precisely those fields (and only those — fields the
    // user had already typed before OCR ran are never touched here or on undo).
    const filled = _ocrAutofilledFields instanceof Set ? _ocrAutofilledFields : new Set();

    const topicEl = document.getElementById('topicInput');
    const keywordsEl = document.getElementById('keywordsInput');
    const statementEl = document.getElementById('statementInput');

    const lines = text.split(/\r?\n/).map(l => l.trim()).filter(Boolean);

    // Subject: first reasonably-sized line of text (skip very short noise lines)
    if (topicEl && !topicEl.value.trim()) {
        let subjectLine = lines.find(l => l.length >= 8) || lines[0] || '';
        if (subjectLine.length > 120) subjectLine = subjectLine.slice(0, 120).trim() + '…';
        if (subjectLine) { topicEl.value = subjectLine; filled.add('topicInput'); }
    }

    // Keywords: frequent, reasonably-long distinct words (any script), deduplicated
    if (keywordsEl && !keywordsEl.value.trim()) {
        // Matches sequences of letters in any language (Unicode-aware)
        const words = text.match(/[\p{L}][\p{L}'-]{2,}/gu) || [];
        const stopwordsEn = new Set(['the','this','that','these','those','from','with','your','dear','sincerely',
            'regards','best','visa','letter','application','officer','currently','department','and','for','are',
            'was','were','have','has','will','would','could','should']);
        const seen = new Set();
        const keywords = [];
        for (const w of words) {
            const key = w.toLowerCase();
            if (stopwordsEn.has(key) || seen.has(key)) continue;
            if (w.length < 3) continue;
            seen.add(key);
            keywords.push(w);
            if (keywords.length >= 8) break;
        }
        if (keywords.length) { keywordsEl.value = keywords.join(', '); filled.add('keywordsInput'); }
    }

    // Statement/Notes: the attached document's filename (without extension)
    if (statementEl && !statementEl.value.trim() && file && file.name) {
        const name = file.name.replace(/\.[^.]+$/, '').trim();
        if (name) { statementEl.value = name; filled.add('statementInput'); }
    }

    // Document Date: find the first recognizable date in the text and fill
    // the Document Date fields, only if currently empty.
    const docDateY = document.getElementById('docDateY');
    const docDateAlreadySet = docDateY && docDateY.value.trim();
    let usedDate = null;
    if (!docDateAlreadySet) {
        usedDate = extractDateFromText(text);
        if (usedDate && typeof setSegDate === 'function') { setSegDate(usedDate); filled.add('documentDate'); }
    }

    // Importance: only fill if a clear urgent/important signal is found
    const importanceEl = document.getElementById('importanceSelect');
    if (importanceEl && importanceEl.value === '1') {
        if (/\burgent\b/i.test(text) || /عاجل/.test(text)) {
            importanceEl.value = '3';
            filled.add('importanceSelect');
        } else if (/\bimportant\b/i.test(text) || /مهم/.test(text)) {
            importanceEl.value = '2';
            filled.add('importanceSelect');
        }
    }

    // Confidentiality: only fill if a clear confidential signal is found
    const confidentialityEl = document.getElementById('confidentialitySelect');
    if (confidentialityEl && confidentialityEl.value === '1') {
        if (/highly\s+confidential/i.test(text) || /سري\s+للغاية/.test(text) || /سري\s+جدا/.test(text)) {
            confidentialityEl.value = '3';
            filled.add('confidentialitySelect');
        } else if (/\bconfidential\b/i.test(text) || /سري/.test(text)) {
            confidentialityEl.value = '2';
            filled.add('confidentialitySelect');
        }
    }

    // Expiry Date: look for a date near "expiry"/"valid until"/"تنتهي"/"صالح حتى",
    // or fall back to a second distinct date in the text (different from the
    // document date already used).
    const expiryEl = document.getElementById('expiryDate');
    if (expiryEl && !expiryEl.value.trim()) {
        let expiryIso = extractDateNearKeywords(text,
            [/expir\w*/i, /valid\s*(until|through|till)?/i, /تنتهي/, /صالح(ة)?\s*حتى/, /ينتهي/]);
        if (!expiryIso) {
            // Fall back: a second date in the text different from the doc date
            const allDates = extractAllDatesFromText(text);
            expiryIso = allDates.find(d => d !== usedDate) || null;
        }
        if (expiryIso) { expiryEl.value = expiryIso; filled.add('expiryDate'); }
    }

    // Shelf Number: look for "shelf"/"رف" followed by a number
    const shelfEl = document.getElementById('shelfNumber');
    if (shelfEl && !shelfEl.value.trim()) {
        let m = text.match(/shelf\s*(?:no\.?|number|#)?\s*[:#]?\s*(\d{1,5})/i);
        if (!m) m = text.match(/رف\s*(?:رقم)?\s*[:#]?\s*(\d{1,5})/);
        if (m) { shelfEl.value = m[1]; filled.add('shelfNumber'); }
    }

    // Doc Number: look for "no."/"number"/"رقم" followed by digits
    const docNumberEl = document.getElementById('documentNumber');
    if (docNumberEl && !docNumberEl.value.trim()) {
        let m = text.match(/\b(?:doc(?:ument)?\s*)?no\.?\s*[:#]?\s*(\d{1,10})\b/i);
        if (!m) m = text.match(/\bnumber\s*[:#]?\s*(\d{1,10})\b/i);
        if (!m) m = text.match(/رقم\s*(?:المستند|الوثيقة|المرجع)?\s*[:#]?\s*(\d{1,10})/);
        if (m) { docNumberEl.value = m[1]; filled.add('documentNumber'); }
    }

    // Persist what this run filled so a later "✕ Close" can undo exactly this.
    _ocrAutofilledFields = filled;
}

// Find a date that appears near one of the given keyword regexes (within ~60
// chars after the keyword). Returns YYYY-MM-DD or null.
function extractDateNearKeywords(text, keywordRegexes) {
    for (const kw of keywordRegexes) {
        const m = text.match(kw);
        if (!m) continue;
        const start = m.index + m[0].length;
        const window = text.slice(start, start + 60);
        const iso = extractDateFromText(window);
        if (iso) return iso;
    }
    return null;
}

// Find all distinct dates in the text, in order of appearance, as YYYY-MM-DD.
function extractAllDatesFromText(text) {
    const results = [];
    const seen = new Set();
    let remaining = text;
    let offset = 0;
    while (remaining.length) {
        const iso = extractDateFromText(remaining);
        if (!iso) break;
        if (!seen.has(iso)) {
            seen.add(iso);
            results.push(iso);
        }
        // Advance past the first match to look for more dates
        const m = remaining.match(/\b\d{1,4}[-\/.]\d{1,2}[-\/.]\d{1,4}\b|\b\d{1,2}\s+\w+\s+\d{4}\b|\b\w+\s+\d{1,2},?\s+\d{4}\b/);
        if (!m) break;
        remaining = remaining.slice(m.index + m[0].length);
        if (results.length >= 5) break; // safety cap
    }
    return results;
}

// Find the first plausible date in OCR text and return it as YYYY-MM-DD,
// or null if none found. Supports numeric (DD/MM/YYYY, YYYY-MM-DD, etc.)
// and month-name formats (e.g. "15 June 2026", "June 15, 2026").
function extractDateFromText(text) {
    const monthNames = {
        jan:1, january:1, feb:2, february:2, mar:3, march:3, apr:4, april:4,
        may:5, jun:6, june:6, jul:7, july:7, aug:8, august:8,
        sep:9, sept:9, september:9, oct:10, october:10,
        nov:11, november:11, dec:12, december:12
    };
    const pad2 = n => String(n).padStart(2, '0');
    const isPlausibleYear = y => y >= 1900 && y <= 2100;

    // 1) ISO-like: YYYY-MM-DD or YYYY/MM/DD
    let m = text.match(/\b(\d{4})[-\/](\d{1,2})[-\/](\d{1,2})\b/);
    if (m) {
        const y = +m[1], mo = +m[2], d = +m[3];
        if (isPlausibleYear(y) && mo >= 1 && mo <= 12 && d >= 1 && d <= 31) {
            return `${y}-${pad2(mo)}-${pad2(d)}`;
        }
    }

    // 2) Numeric DD/MM/YYYY or DD-MM-YYYY (also covers MM/DD/YYYY heuristically
    //    by treating the larger of the first two as the day when unambiguous)
    m = text.match(/\b(\d{1,2})[-\/.](\d{1,2})[-\/.](\d{2,4})\b/);
    if (m) {
        let a = +m[1], b = +m[2], y = +m[3];
        if (y < 100) y += y < 50 ? 2000 : 1900;
        if (isPlausibleYear(y)) {
            let day, month;
            if (a > 12 && b <= 12) { day = a; month = b; }
            else if (b > 12 && a <= 12) { day = b; month = a; }
            else { day = a; month = b; } // ambiguous — assume DD/MM
            if (month >= 1 && month <= 12 && day >= 1 && day <= 31) {
                return `${y}-${pad2(month)}-${pad2(day)}`;
            }
        }
    }

    // 3) Month-name formats: "15 June 2026" or "June 15, 2026" / "June 15 2026"
    const monthPattern = Object.keys(monthNames).join('|');
    m = text.match(new RegExp(`\\b(\\d{1,2})\\s+(${monthPattern})\\.?,?\\s+(\\d{4})\\b`, 'i'));
    if (m) {
        const day = +m[1], mo = monthNames[m[2].toLowerCase()], y = +m[3];
        if (isPlausibleYear(y) && day >= 1 && day <= 31) {
            return `${y}-${pad2(mo)}-${pad2(day)}`;
        }
    }
    m = text.match(new RegExp(`\\b(${monthPattern})\\.?\\s+(\\d{1,2}),?\\s+(\\d{4})\\b`, 'i'));
    if (m) {
        const mo = monthNames[m[1].toLowerCase()], day = +m[2], y = +m[3];
        if (isPlausibleYear(y) && day >= 1 && day <= 31) {
            return `${y}-${pad2(mo)}-${pad2(day)}`;
        }
    }

    return null;
}

// ════════════════════════════════════════════════════════════════════════════
// TAB 1 — CAMERA / PHOTO
// ════════════════════════════════════════════════════════════════════════════
function _resetCameraTab() {
    _cameraPhotos.forEach(p => _dismissOcrIfRemoved(p));
    _cameraPhotos = [];
    _renderCameraPhotos();
    _setCameraStatus('', '');
    const startBtn  = document.getElementById('scanCameraStartBtn');
    const snapBtn   = document.getElementById('scanCameraSnapBtn');
    const stopBtn   = document.getElementById('scanCameraStopBtn');
    const video     = document.getElementById('scanCameraVideo');
    if (startBtn)  startBtn.style.display  = '';
    if (snapBtn)   snapBtn.style.display   = 'none';
    if (stopBtn)   stopBtn.style.display   = 'none';
    if (video)     video.style.display     = 'none';
}

async function scanCameraStart() {
    if (!navigator.mediaDevices?.getUserMedia) {
        _setCameraStatus('⚠ Camera not supported in this browser — use the Phone Camera button below', 'scan-status--error');
        return;
    }
    try {
        _cameraStream = await navigator.mediaDevices.getUserMedia({ video: { facingMode: 'environment' }, audio: false });
        const video = document.getElementById('scanCameraVideo');
        if (video) { video.srcObject = _cameraStream; video.style.display = ''; }
        document.getElementById('scanCameraStartBtn').style.display = 'none';
        document.getElementById('scanCameraSnapBtn').style.display  = '';
        document.getElementById('scanCameraStopBtn').style.display  = '';
        _setCameraStatus(currentLang === 'ar' ? 'الكاميرا تعمل' : 'Camera live', 'scan-status--success');
    } catch(e) {
        _setCameraStatus('⚠ ' + (e.message || 'Camera access denied'), 'scan-status--error');
    }
}

function scanCameraStop() {
    if (_cameraStream) {
        _cameraStream.getTracks().forEach(t => t.stop());
        _cameraStream = null;
    }
    const video = document.getElementById('scanCameraVideo');
    if (video) { video.srcObject = null; video.style.display = 'none'; }
    const startBtn = document.getElementById('scanCameraStartBtn');
    const snapBtn  = document.getElementById('scanCameraSnapBtn');
    const stopBtn  = document.getElementById('scanCameraStopBtn');
    if (startBtn) startBtn.style.display = '';
    if (snapBtn)  snapBtn.style.display  = 'none';
    if (stopBtn)  stopBtn.style.display  = 'none';
}

function scanCameraSnap() {
    const video  = document.getElementById('scanCameraVideo');
    const canvas = document.getElementById('scanCameraCanvas');
    if (!video || !canvas) return;
    canvas.width  = video.videoWidth;
    canvas.height = video.videoHeight;
    canvas.getContext('2d').drawImage(video, 0, 0);
    canvas.toBlob(blob => {
        if (!blob) return;
        _cameraPhotos.push(blob);
        _renderCameraPhotos();
        syncScanFooterActions();
        _setCameraStatus(
            currentLang === 'ar' ? `✓ تم التقاط ${_cameraPhotos.length} صورة` : `✓ ${_cameraPhotos.length} photo(s) captured`,
            'scan-status--success'
        );
    }, 'image/jpeg', 0.92);
}

// Mobile: file input with camera capture
function scanCameraFileInput(input) {
    Array.from(input.files).forEach(f => _cameraPhotos.push(f));
    input.value = '';
    _renderCameraPhotos();
    syncScanFooterActions();
    _setCameraStatus(
        currentLang === 'ar' ? `✓ ${_cameraPhotos.length} صورة جاهزة` : `✓ ${_cameraPhotos.length} photo(s) ready`,
        'scan-status--success'
    );
}

function _renderCameraPhotos() {
    const wrap = document.getElementById('scanCameraPhotos');
    if (!wrap) return;
    if (!_cameraPhotos.length) { wrap.innerHTML = ''; return; }
    wrap.innerHTML = _cameraPhotos.map((p, i) => {
        const url = URL.createObjectURL(p);
        return `<div class="scan-photo-thumb">
            <img src="${url}" onload="URL.revokeObjectURL(this.src)">
            <button type="button" class="scan-photo-del" onclick="scanRemovePhoto(${i})">✕</button>
        </div>`;
    }).join('');
}

function scanRemovePhoto(idx) {
    const removed = _cameraPhotos[idx];
    _cameraPhotos.splice(idx, 1);
    _renderCameraPhotos();
    syncScanFooterActions();
    syncScanOcrVisibility();
    _dismissOcrIfRemoved(removed);
}

function _setCameraStatus(msg, cls) {
    const el = document.getElementById('scanCameraStatus');
    if (!el) return;
    el.textContent = msg;
    el.className   = 'scan-status ' + (cls || '');
}

// ════════════════════════════════════════════════════════════════════════════
// ════════════════════════════════════════════════════════════════════════════
// TAB 2 — NETWORK SCANNER  (eSCL / AirScan protocol)
// Flask backend proxies requests to the scanner IP — no CORS issues.
// Compatible with Canon, Ricoh, HP, Xerox, Kyocera and any eSCL scanner.
// ════════════════════════════════════════════════════════════════════════════

let _netScanPages = [];   // { blob, url, name }

const NET_SCAN_IP_KEY = 'docportalDashNetScanIp';

function netScanRestoreIp() {
    try {
        const saved = localStorage.getItem(NET_SCAN_IP_KEY) || '';
        const el    = document.getElementById('netScanIp');
        if (el && saved) el.value = saved;
    } catch(e) {}
}

function netScanSaveIp(val) {
    try { localStorage.setItem(NET_SCAN_IP_KEY, val.trim()); } catch(e) {}
}

function _setScanConnDot(state) {
    const dot = document.getElementById('netScanConnDot');
    if (!dot) return;
    dot.className = 'scan-conn-dot scan-conn-dot--' + (state || 'idle');
}


async function _autoResetScanner(ip) {
    /** Call /api/scanner/reset, return { ok, log, state }. */
    try {
        const res  = await fetch('/api/scanner/reset', {
            method:  'POST',
            headers: { 'Content-Type': 'application/json' },
            body:    JSON.stringify({ ip }),
        });
        const data = await res.json();
        // Log to console so we can debug if it still fails
        if (data.log && data.log.length) {
            console.group('Scanner reset log');
            data.log.forEach(l => console.log(l));
            console.groupEnd();
        }
        return data.ok || false;
    } catch(e) {
        console.error('Scanner reset error:', e);
        return false;
    }
}

function _showScanResetBtn(ip) {
    /** Show the manual Reset Scanner button in the status area. */
    const sta = document.getElementById('netScanStatus');
    if (!sta) return;
    const existing = document.getElementById('netScanResetBtn');
    if (existing) return;   // already showing
    const btn = document.createElement('button');
    btn.id        = 'netScanResetBtn';
    btn.type      = 'button';
    btn.className = 'btn-sm btn-outline';
    btn.style.marginTop = '6px';
    btn.textContent = currentLang === 'ar' ? '<i class="ph ph-arrow-counter-clockwise"></i> إعادة ضبط الماسح' : '<i class="ph ph-arrow-counter-clockwise"></i> Reset Scanner';
    btn.onclick = async () => {
        btn.disabled = true;
        btn.textContent = currentLang === 'ar' ? '⏳ جارٍ الإعادة...' : '⏳ Resetting…';
        _setNs(sta, currentLang === 'ar' ? '⏳ جارٍ إعادة ضبط الماسح...' : '⏳ Resetting scanner…', 'loading');
        const ok = await _autoResetScanner(ip);
        btn.remove();
        if (ok) {
            _setNs(sta, currentLang === 'ar' ? '✓ تم الضبط — جارٍ إعادة المسح...' : '✓ Reset OK — retrying scan…', 'success');
            setTimeout(() => netScanStart(), 1200);
        } else {
            _setNs(sta,
                currentLang === 'ar' ? '⚠ فشل الضبط — أعد تشغيل الطابعة يدوياً' : '⚠ Reset failed — restart the printer manually',
                'error'
            );
        }
    };
    // Insert the button right below the status span
    if (sta.parentNode) sta.parentNode.insertBefore(btn, sta.nextSibling);
}

async function netScanTest() {
    const ip  = (document.getElementById('netScanIp')?.value || '').trim();
    const btn = document.getElementById('netScanTestBtn');
    const sta = document.getElementById('netScanTestStatus');
    if (!ip) { _setNs(sta, '⚠ Enter an IP address first', 'error'); _setScanConnDot('idle'); return; }
    if (btn) btn.disabled = true;
    _setNs(sta, 'Testing…', 'loading');
    _setScanConnDot('idle');
    try {
        const res  = await fetch(`/api/scanner/test?ip=${encodeURIComponent(ip)}`);
        const data = await res.json();
        if (data.ok) {
            _netScannerEndpoint = { scheme: data.scheme, port: data.port, name: data.name };
            netScanSaveIp(ip);
            _setNs(sta, `✓ ${data.name || ip}`, 'success');
            _setScanConnDot('ok');
        } else {
            _netScannerEndpoint = null;
            _setNs(sta, `⚠ ${data.error || 'Not reachable'}`, 'error');
            _setScanConnDot('error');
        }
    } catch(e) {
        _netScannerEndpoint = null;
        _setNs(sta, '⚠ Could not reach scanner — check IP and network', 'error');
        _setScanConnDot('error');
    } finally {
        if (btn) btn.disabled = false;
    }
}

async function netScanStart() {
    const ip     = (document.getElementById('netScanIp')?.value     || '').trim();
    const color  =  document.getElementById('netScanColor')?.value  || 'RGB24';
    const dpi    =  document.getElementById('netScanDpi')?.value    || '200';
    const fmt    =  document.getElementById('netScanFormat')?.value || 'application/pdf';
    const source =  document.getElementById('netScanSource')?.value || 'Platen';
    const btn    =  document.getElementById('netScanBtn');
    const addBtn =  document.getElementById('netScanAddPageBtn');
    const sta    =  document.getElementById('netScanStatus');

    if (!ip) { _setNs(sta, '⚠ ' + (currentLang === 'ar' ? 'أدخل عنوان IP الماسح أولاً' : 'Enter the scanner IP address first'), 'error'); return; }
    if (btn)    btn.disabled = true;
    if (addBtn) addBtn.disabled = true;
    _setNs(sta, currentLang === 'ar' ? '⏳ جارٍ المسح… يرجى الانتظار' : '⏳ Scanning… please wait', 'loading');

    try {
        const payload = {
            ip, color, dpi: parseInt(dpi, 10), format: fmt, source,
        };
        if (_netScannerEndpoint) {
            payload.scheme = _netScannerEndpoint.scheme;
            payload.port = _netScannerEndpoint.port;
        }
        const res = await fetch('/api/scanner/scan', {
            method:  'POST',
            headers: { 'Content-Type': 'application/json' },
            body:    JSON.stringify(payload),
        });
        const ct = (res.headers.get('content-type') || '').toLowerCase();
        if (!res.ok || ct.includes('application/json')) {
            let errMsg = `Scanner error ${res.status}`;
            let is409 = false;
            try {
                const err = ct.includes('json') ? await res.json() : { error: await res.text() };
                errMsg = err.error || errMsg;
                is409 = res.status === 502 && (errMsg.includes('409') || errMsg.includes('conflict'));
            } catch (_) {}

            if (is409) {
                // Auto-reset on HP 409 conflictWithExisting, then retry once
                _setNs(sta, currentLang === 'ar' ? '⏳ جارٍ إعادة ضبط الماسح...' : '⏳ Scanner busy — resetting…', 'loading');
                const resetOk = await _autoResetScanner(ip);
                if (resetOk) {
                    // Retry the scan once after reset
                    _setNs(sta, currentLang === 'ar' ? '⏳ جارٍ المسح مرة أخرى...' : '⏳ Retrying scan…', 'loading');
                    const res2 = await fetch('/api/scanner/scan', {
                        method: 'POST',
                        headers: { 'Content-Type': 'application/json' },
                        body: JSON.stringify(payload),
                    });
                    const ct2 = (res2.headers.get('content-type') || '').toLowerCase();
                    if (!res2.ok || ct2.includes('application/json')) {
                        let errMsg2 = `Scanner error ${res2.status}`;
                        try {
                            const e2 = ct2.includes('json') ? await res2.json() : { error: await res2.text() };
                            errMsg2 = e2.error || errMsg2;
                        } catch(_) {}
                        _setNs(sta, '⚠ ' + errMsg2 + (currentLang === 'ar' ? ' — اضغط "إعادة الضبط" مجدداً' : ' — press Reset Scanner again'), 'error');
                        _showScanResetBtn(ip);
                        return;
                    }
                    // Success on retry — fall through to blob handling below
                    const blob2 = await res2.blob();
                    if (!blob2.size) {
                        _setNs(sta, '⚠ Empty scan — place document on scanner and try again', 'error');
                        return;
                    }
                    const ext2  = fmt === 'application/pdf' ? 'pdf' : fmt === 'image/jpeg' ? 'jpg' : 'png';
                    const name2 = `scan_${Date.now()}.${ext2}`;
                    const url2  = URL.createObjectURL(blob2);
                    _netScanPages.push({ blob: blob2, url: url2, name: name2 });
                    _renderNetScanPages();
                    syncScanFooterActions();
                    syncScanOcrVisibility();
                    _setNs(sta,
                        currentLang === 'ar' ? `✓ ${_netScanPages.length} صفحة — امسح صفحة أخرى أو احفظ` : `✓ ${_netScanPages.length} page(s) — scan another or Save`,
                        'success'
                    );
                    if (addBtn) addBtn.style.display = '';
                    if (btn)    btn.style.display    = 'none';
                    return;
                } else {
                    _setNs(sta, currentLang === 'ar'
                        ? '⚠ الماسح مشغول — اضغط "إعادة الضبط" أو أعد تشغيل الماسح'
                        : '⚠ Scanner still busy — press Reset Scanner or restart the printer',
                        'error');
                    _showScanResetBtn(ip);
                    return;
                }
            }

            _setNs(sta, '⚠ ' + errMsg, 'error');
            return;
        }
        const blob = await res.blob();
        if (!blob.size) {
            _setNs(sta, '⚠ Empty scan — place document on scanner and try again', 'error');
            return;
        }
        const ext  = fmt === 'application/pdf' ? 'pdf' : fmt === 'image/jpeg' ? 'jpg' : 'png';
        const name = `scan_${Date.now()}.${ext}`;
        const url  = URL.createObjectURL(blob);
        _netScanPages.push({ blob, url, name });
        _renderNetScanPages();
        syncScanFooterActions();
        syncScanOcrVisibility();
        _setNs(sta,
            currentLang === 'ar' ? `✓ ${_netScanPages.length} صفحة — امسح صفحة أخرى أو احفظ` : `✓ ${_netScanPages.length} page(s) — scan another or Save`,
            'success'
        );
        if (addBtn) addBtn.style.display = '';
        if (btn)    btn.style.display    = 'none';
    } catch(e) {
        _setNs(sta, '⚠ ' + e.message, 'error');
    } finally {
        if (btn)    btn.disabled = false;
        if (addBtn) addBtn.disabled = false;
    }
}

// Auto-discover eSCL scanners on the local subnet
async function netScanDiscover() {
    const discBtn = document.getElementById('netScanDiscoverBtn');
    const discSta = document.getElementById('netScanDiscoverStatus');
    const wrap    = document.getElementById('netScanDropdownWrap');
    const sel     = document.getElementById('netScanDiscoveredSelect');
    if (discBtn) discBtn.disabled = true;
    _setNs(discSta, currentLang === 'ar' ? '⏳ جارٍ البحث عن الماسحات...' : '⏳ Scanning network for scanners…', 'loading');
    try {
        const res  = await fetch('/api/scanner/discover');
        const data = await res.json();
        if (data.scanners && data.scanners.length > 0) {
            sel.innerHTML = `<option value="">— ${currentLang === 'ar' ? 'اختر ماسحاً' : 'Select a scanner'} —</option>` +
                data.scanners.map(s =>
                    `<option value="${s.ip}" data-name="${s.name}" data-scheme="${s.scheme}" data-port="${s.port}">${s.name} (${s.ip})</option>`
                ).join('');
            if (wrap) wrap.style.display = '';
            _setNs(discSta,
                currentLang === 'ar' ? `✓ تم العثور على ${data.scanners.length} ماسح` : `✓ Found ${data.scanners.length} scanner(s)`,
                'success'
            );
            // Auto-select if only one found
            if (data.scanners.length === 1) netScanSelectDiscovered(data.scanners[0].ip);
        } else {
            _setNs(discSta,
                currentLang === 'ar' ? '⚠ لم يتم العثور على ماسحات — أدخل IP يدوياً' : '⚠ No scanners found — enter IP manually',
                'error'
            );
        }
    } catch(e) {
        _setNs(discSta, '⚠ ' + e.message, 'error');
    } finally {
        if (discBtn) discBtn.disabled = false;
    }
}

function netScanSelectDiscovered(ip) {
    if (!ip) return;
    const ipInput = document.getElementById('netScanIp');
    const sel = document.getElementById('netScanDiscoveredSelect');
    if (ipInput) { ipInput.value = ip; netScanSaveIp(ip); }
    if (sel) {
        const opt = sel.querySelector(`option[value="${ip}"]`);
        if (opt) {
            _netScannerEndpoint = {
                scheme: opt.dataset.scheme || 'http',
                port: parseInt(opt.dataset.port || '80', 10),
                name: opt.dataset.name || ip,
            };
        }
    }
    netScanTest();
}

function _renderNetScanPages() {
    const wrap = document.getElementById('netScanPages');
    if (!wrap) return;
    wrap.innerHTML = _netScanPages.map((p, i) => {
        const pageNum = i + 1;
        const isPdf   = p.name.endsWith('.pdf');
        if (isPdf) {
            // PDF: render a document-style card with page number and canvas preview
            const canvasId = `scan-pdf-canvas-${i}`;
            return `<div class="scan-page-card scan-page-card--pdf" onclick="openScanPreview(${i})" title="Page ${pageNum} — click to preview">
                <div class="scan-page-card__img-wrap">
                    <canvas id="${canvasId}" class="scan-pdf-canvas"></canvas>
                    <div class="scan-pdf-fallback-icon" id="scan-pdf-fallback-${i}"><i class="ph ph-file-pdf"></i></div>
                </div>
                <div class="scan-page-card__footer">
                    <span class="scan-page-num">${pageNum}</span>
                    <button type="button" class="scan-photo-del scan-photo-del--card" onclick="event.stopPropagation();netScanRemovePage(${i})" title="Remove">✕</button>
                </div>
            </div>`;
        } else {
            // Image: show actual image thumbnail with page number
            return `<div class="scan-page-card" onclick="openScanPreview(${i})" title="Page ${pageNum} — click to preview">
                <div class="scan-page-card__img-wrap">
                    <img src="${p.url}" alt="Page ${pageNum}" loading="lazy">
                </div>
                <div class="scan-page-card__footer">
                    <span class="scan-page-num">${pageNum}</span>
                    <button type="button" class="scan-photo-del scan-photo-del--card" onclick="event.stopPropagation();netScanRemovePage(${i})" title="Remove">✕</button>
                </div>
            </div>`;
        }
    }).join('');

    // After rendering, render PDF first-page previews using PDF.js (if available)
    _netScanPages.forEach((p, i) => {
        if (!p.name.endsWith('.pdf')) return;
        _renderPdfThumbnail(p.url, i);
    });
}

async function _renderPdfThumbnail(url, idx) {
    const canvasEl   = document.getElementById(`scan-pdf-canvas-${idx}`);
    const fallbackEl = document.getElementById(`scan-pdf-fallback-${idx}`);
    if (!canvasEl) return;

    // Try PDF.js if available (loaded via CDN or already present)
    try {
        let pdfjsLib = window['pdfjs-dist/build/pdf'] || window.pdfjsLib;
        if (!pdfjsLib) {
            // Lazy-load PDF.js from CDN
            await new Promise((resolve, reject) => {
                if (document.getElementById('_pdfjs_script')) { resolve(); return; }
                const s = document.createElement('script');
                s.id  = '_pdfjs_script';
                s.src = 'https://cdnjs.cloudflare.com/ajax/libs/pdf.js/3.11.174/pdf.min.js';
                s.onload = resolve;
                s.onerror = reject;
                document.head.appendChild(s);
            });
            pdfjsLib = window.pdfjsLib;
            if (pdfjsLib && !pdfjsLib.GlobalWorkerOptions.workerSrc) {
                pdfjsLib.GlobalWorkerOptions.workerSrc =
                    'https://cdnjs.cloudflare.com/ajax/libs/pdf.js/3.11.174/pdf.worker.min.js';
            }
        }
        if (!pdfjsLib) throw new Error('PDF.js not available');

        const pdf   = await pdfjsLib.getDocument(url).promise;
        const page  = await pdf.getPage(1);
        const vp    = page.getViewport({ scale: 1 });
        // Scale to fit card width (100px)
        const scale = 100 / vp.width;
        const vp2   = page.getViewport({ scale });
        canvasEl.width  = vp2.width;
        canvasEl.height = vp2.height;
        await page.render({ canvasContext: canvasEl.getContext('2d'), viewport: vp2 }).promise;
        if (fallbackEl) fallbackEl.style.display = 'none';
        canvasEl.style.display = 'block';
    } catch (_) {
        // PDF.js unavailable or failed — keep fallback icon visible
        canvasEl.style.display = 'none';
        if (fallbackEl) fallbackEl.style.display = '';
    }
}

function openScanPreview(idx) {
    const p = _netScanPages[idx];
    if (!p) return;
    const isPdf = p.name.endsWith('.pdf');
    const existing = document.getElementById('scanPreviewLightbox');
    if (existing) existing.remove();

    const lb = document.createElement('div');
    lb.id = 'scanPreviewLightbox';
    lb.className = 'scan-preview-lightbox';
    lb.onclick = (e) => { if (e.target === lb) closeScanPreview(); };

    lb.innerHTML = `
        <div class="scan-preview-lightbox-inner">
            <div class="scan-preview-lightbox-toolbar">
                <span class="scan-preview-lightbox-name"><i class="ph ph-${isPdf ? 'file-pdf' : 'image'}"></i> ${p.name}</span>
                <a href="${p.url}" download="${p.name}" class="btn-ghost" style="font-size:12px;padding:4px 10px;text-decoration:none;display:flex;align-items:center;gap:5px;">
                    <i class="ph ph-download-simple"></i> Download
                </a>
                <button class="scan-preview-lightbox-close" onclick="closeScanPreview()" title="Close">
                    <i class="ph ph-x"></i>
                </button>
            </div>
            ${isPdf
                ? `<iframe src="${p.url}" title="${p.name}"></iframe>`
                : `<img src="${p.url}" alt="${p.name}">`
            }
        </div>`;

    document.body.appendChild(lb);

    // Close on Escape
    lb._keyHandler = (e) => { if (e.key === 'Escape') closeScanPreview(); };
    document.addEventListener('keydown', lb._keyHandler);
}

function closeScanPreview() {
    const lb = document.getElementById('scanPreviewLightbox');
    if (lb) {
        document.removeEventListener('keydown', lb._keyHandler);
        lb.remove();
    }
}

function netScanRemovePage(idx) {
    const removed = _netScanPages[idx];
    URL.revokeObjectURL(removed?.url);
    _netScanPages.splice(idx, 1);
    _renderNetScanPages();
    syncScanFooterActions();
    syncScanOcrVisibility();
    if (removed) _dismissOcrIfRemoved(removed.blob);
}

function netScanClearAll() {
    _netScanPages.forEach(p => URL.revokeObjectURL(p.url));
    _netScanPages = [];
    _scanFiles = [];
    _cameraPhotos = [];
    renderScanFileList();
    _renderNetScanPages();
    _renderCameraPhotos();
    syncScanFooterActions();
    syncScanOcrVisibility();
    dismissOcrResult();
    const sta = document.getElementById('netScanStatus');
    _setNs(sta, '', '');
    const addBtn = document.getElementById('netScanAddPageBtn');
    const scanBtn = document.getElementById('netScanBtn');
    if (addBtn) addBtn.style.display = 'none';
    if (scanBtn) { scanBtn.style.display = ''; scanBtn.disabled = false; }
}

function netScanDownloadAll() {
    if (!_netScanPages.length) return;
    _netScanPages.forEach((p, i) => {
        const a = document.createElement('a');
        a.href = p.url;
        a.download = p.name || `scan_page_${i + 1}.pdf`;
        a.click();
    });
}

function _resetNetScanTab() {
    _netScanPages.forEach(p => URL.revokeObjectURL(p.url));
    _netScanPages = [];
    _renderNetScanPages();
    ['netScanStatus','netScanTestStatus','netScanDiscoverStatus'].forEach(id => {
        const el = document.getElementById(id);
        if (el) { el.textContent = ''; el.className = 'scan-status'; }
    });
    const addPageBtn = document.getElementById('netScanAddPageBtn');
    if (addPageBtn) addPageBtn.style.display = 'none';
    const scanBtn = document.getElementById('netScanBtn');
    if (scanBtn) { scanBtn.style.display = ''; scanBtn.disabled = false; }
    _setScanConnDot('idle');
    syncScanFooterActions();
}

function _setNs(el, msg, type) {
    if (!el) return;
    el.textContent = msg;
    el.className   = `scan-status scan-status--${type}`;
}


// ════════════════════════════════════════════════════════════════════════════
// USB / WIRED SCANNER  (SANE on Linux/Mac, WIA on Windows — via Flask backend)
// ════════════════════════════════════════════════════════════════════════════

let _usbScanDeviceId   = null;   // currently selected USB device ID
let _usbScanPages      = [];     // { blob, url, name }

function _setUs(msg, type) {
    const el = document.getElementById('usbScanStatus');
    if (el) { el.textContent = msg; el.className = `scan-status scan-status--${type}`; }
}

async function usbScanList() {
    const btn = document.getElementById('usbScanListBtn');
    if (btn) btn.disabled = true;
    _setUs(currentLang === 'ar' ? '⏳ جارٍ البحث عن الماسحات...' : '⏳ Searching for USB scanners…', 'loading');
    try {
        // First check if the local agent is running
        let agentOnline = false;
        try {
            const ping = await fetch('http://localhost:8765/usb/status', { signal: AbortSignal.timeout(2000) });
            agentOnline = ping.ok;
        } catch(_) { agentOnline = false; }

        if (!agentOnline) {
            _setUs(
                currentLang === 'ar'
                    ? '⚠ عميل الماسح غير مُشغَّل — يرجى تشغيل DocPortal_Scanner_Agent.exe أولاً'
                    : '⚠ USB Agent is not running — please start DocPortal_Scanner_Agent.exe on this PC',
                'error'
            );
            const statusEl = document.getElementById('usbScanStatus');
            if (statusEl && !document.getElementById('usbAgentHelp')) {
                const help = document.createElement('div');
                help.id = 'usbAgentHelp';
                help.style.cssText = 'font-size:12px;margin-top:8px;color:#6b7280;line-height:1.6;';
                help.innerHTML = currentLang === 'ar'
                    ? 'ابحث عن <b>DocPortal_Scanner_Agent.exe</b> على سطح المكتب وانقر نقراً مزدوجاً لتشغيله، ثم حاول مرة أخرى.'
                    : 'Find <b>DocPortal_Scanner_Agent.exe</b> on your Desktop, double-click it to start, then try again.';
                statusEl.after(help);
            }
            return;
        }
        document.getElementById('usbAgentHelp')?.remove();

        const res  = await fetch('http://localhost:8765/usb/scanners');
        const data = await res.json();
        const scanners = data.scanners || [];

        if (!scanners.length) {
            // Show the real server-side error if present
            const errDetail = data.error || (currentLang === 'ar'
                ? 'لم يتم العثور على ماسحات USB'
                : 'No USB scanners found');
            _setUs('⚠ ' + errDetail, 'error');

            // Add a debug link so the user can see raw WIA output
            const statusEl = document.getElementById('usbScanStatus');
            if (statusEl && !document.getElementById('usbDebugLink')) {
                const a = document.createElement('a');
                a.id   = 'usbDebugLink';
                a.href = '/api/scanner/usb/debug';
                a.target = '_blank';
                a.style.cssText = 'display:block;font-size:11px;margin-top:4px;color:#6b7280';
                a.textContent   = currentLang === 'ar' ? '<i class="ph ph-magnifying-glass"></i> عرض تشخيص الماسح' : '<i class="ph ph-magnifying-glass"></i> View scanner diagnostics';
                statusEl.after(a);
            }

            const wrap = document.getElementById('usbScanDropdownWrap');
            if (wrap) wrap.style.display = 'none';
            return;
        }

        // Remove any stale debug link
        document.getElementById('usbDebugLink')?.remove();

        const wrap = document.getElementById('usbScanDropdownWrap');
        const sel  = document.getElementById('usbScanDeviceSelect');
        sel.innerHTML = `<option value="">— ${currentLang === 'ar' ? 'اختر ماسحاً' : 'Select a scanner'} —</option>` +
            scanners.map(s => `<option value="${s.id}">${s.name}</option>`).join('');
        if (wrap) wrap.style.display = '';
        _setUs(
            currentLang === 'ar'
                ? `✓ تم العثور على ${scanners.length} ماسح`
                : `✓ Found ${scanners.length} USB scanner(s) via ${data.backend?.toUpperCase() || 'local'}`,
            'success'
        );
        // Auto-select if only one
        if (scanners.length === 1) usbScanSelectDevice(scanners[0].id);
    } catch(e) {
        _setUs('⚠ ' + e.message, 'error');
    } finally {
        if (btn) btn.disabled = false;
    }
}

function usbScanSelectDevice(id) {
    _usbScanDeviceId = id || null;
    const btnRow = document.getElementById('usbScanBtnRow');
    if (btnRow) btnRow.style.display = id ? '' : 'none';
}

async function usbScanStart() {
    if (!_usbScanDeviceId) {
        _setUs('⚠ ' + (currentLang === 'ar' ? 'اختر ماسحاً أولاً' : 'Select a USB scanner first'), 'error');
        return;
    }
    const color  = document.getElementById('netScanColor')?.value  || 'RGB24';
    const dpi    = document.getElementById('netScanDpi')?.value    || '200';
    const fmt    = document.getElementById('netScanFormat')?.value || 'application/pdf';
    const source = document.getElementById('netScanSource')?.value || 'Platen';
    const btn    = document.getElementById('usbScanBtn');
    const addBtn = document.getElementById('usbScanAddPageBtn');

    if (btn)    btn.disabled = true;
    if (addBtn) addBtn.disabled = true;
    _setUs(currentLang === 'ar' ? '⏳ جارٍ المسح عبر USB...' : '⏳ Scanning via USB… please wait', 'loading');

    try {
        console.log("START SCAN");
        const res = await fetch('http://localhost:8765/usb/scan', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
                device_id: _usbScanDeviceId,
                color_mode: color === 'RGB24' ? 'RGB' : color === 'Grayscale8' ? 'Grayscale' : 'BW',
                dpi: parseInt(dpi, 10),
                format: fmt,
            }),
        });
        const ct = (res.headers.get('content-type') || '').toLowerCase();
        if (!res.ok || ct.includes('application/json')) {
            let errMsg = `Error ${res.status}`;
            try {
                const err = ct.includes('json') ? await res.json() : { error: await res.text() };
                errMsg = err.error || errMsg;
            } catch(_) {}
            _setUs('⚠ ' + errMsg, 'error');
            return;
        }
        const blob = await res.blob();
        if (!blob.size) {
            _setUs('⚠ ' + (currentLang === 'ar' ? 'المسح فارغ' : 'Empty scan — check scanner and try again'), 'error');
            return;
        }
        const ext  = fmt === 'application/pdf' ? 'pdf' : fmt === 'image/jpeg' ? 'jpg' : 'png';
        const name = `scan_usb_${Date.now()}.${ext}`;
        const url  = URL.createObjectURL(blob);
        // Add to the shared _netScanPages array so existing save/preview logic works
        _netScanPages.push({ blob, url, name });
        _renderNetScanPages();
        syncScanFooterActions();
        syncScanOcrVisibility();
        _setUs(
            currentLang === 'ar'
                ? `✓ ${_netScanPages.length} صفحة — امسح صفحة أخرى أو احفظ`
                : `✓ ${_netScanPages.length} page(s) scanned — scan another or Save`,
            'success'
        );
        if (addBtn) addBtn.style.display = '';
        if (btn)    btn.style.display    = 'none';
    } catch(e) {
        _setUs('⚠ ' + e.message, 'error');
    } finally {
        if (btn)    btn.disabled = false;
        if (addBtn) addBtn.disabled = false;
    }
}

// ── TOAST ──────────────────────────────────────────────────────────────────
let toastTimer = null;

function showToast(message, type = 'info') {
    const toast = document.getElementById('toast');
    if (!toast) return;
    clearTimeout(toastTimer);
    toast.textContent = message;
    toast.className = `show toast-${type}`;
    toastTimer = setTimeout(() => { toast.className = ''; }, 2800);
}

// ── KEYBOARD SHORTCUTS ─────────────────────────────────────────────────────
document.addEventListener('keydown', e => {
    if ((e.ctrlKey || e.metaKey) && e.key === 's') {
        e.preventDefault();
        saveDocument();
    }
    if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === 'z') {
        if (_undoDeleteItems.length) {
            e.preventDefault();
            _cancelPendingDelete();
        }
    }
    if (e.key === 'Escape') {
        closeNotif();
        closeFolderModal();
        closeDeleteModal();
        closeScannerModal();
        document.activeElement?.blur();
    }
});

// ── INIT ───────────────────────────────────────────────────────────────────
// ── SEGMENTED DATE INPUT (YYYY / MM / DD auto-advance) ────────────────────
/**
 * Called on every keystroke in a seg-date part.
 * When the field reaches maxLen digits it moves focus to nextId.
 * Also syncs the hidden #documentDate value.
 */
function segDateInput(el, nextId, maxLen) {
    // Strip non-digits
    el.value = el.value.replace(/\D/g, '');
    if (el.value.length >= maxLen && nextId) {
        const next = document.getElementById(nextId);
        if (next) { next.focus(); next.select(); }
    }
    _syncDocDate();
}

/**
 * Backspace on an empty field moves focus back to prevId.
 * Arrow keys also navigate between parts.
 */
function segDateKey(event, prevId, nextId) {
    const el = event.target;
    if (event.key === 'Backspace' && el.value === '' && prevId) {
        event.preventDefault();
        const prev = document.getElementById(prevId);
        if (prev) { prev.focus(); prev.setSelectionRange(prev.value.length, prev.value.length); }
    }
    if (event.key === 'ArrowRight' && el.selectionStart === el.value.length && nextId) {
        event.preventDefault();
        const next = document.getElementById(nextId);
        if (next) { next.focus(); next.select(); }
    }
    if (event.key === 'ArrowLeft' && el.selectionStart === 0 && prevId) {
        event.preventDefault();
        const prev = document.getElementById(prevId);
        if (prev) { prev.focus(); prev.setSelectionRange(prev.value.length, prev.value.length); }
    }
}

/** Build ISO date string from the three seg parts and write to hidden input. */
function _syncDocDate() {
    const y = (document.getElementById('docDateY')?.value || '').padStart(4, '0');
    const m = (document.getElementById('docDateM')?.value || '').padStart(2, '0');
    const d = (document.getElementById('docDateD')?.value || '').padStart(2, '0');
    const hidden = document.getElementById('documentDate');
    if (!hidden) return;
    // Only write if we have a plausibly complete date
    if (y.replace(/^0+/, '').length >= 4 && m !== '00' && d !== '00') {
        hidden.value = `${y}-${m}-${d}`;
    } else {
        hidden.value = '';
    }
}

/** Pre-fill the seg date inputs from an ISO string (YYYY-MM-DD or YYYY/MM/DD). */
function setSegDate(isoStr) {
    if (!isoStr) return;
    const parts = isoStr.replace(/\//g, '-').split('-');
    if (parts.length !== 3) return;
    const yEl = document.getElementById('docDateY');
    const mEl = document.getElementById('docDateM');
    const dEl = document.getElementById('docDateD');
    if (yEl) yEl.value = parts[0] || '';
    if (mEl) mEl.value = parts[1] || '';
    if (dEl) dEl.value = parts[2] || '';
    _syncDocDate();
}

/** Called when user picks a date from the calendar popup — fills all three segments. */
function segDateFromPicker(isoVal) {
    if (!isoVal) return;                   // user cleared / cancelled
    setSegDate(isoVal);                    // reuse existing fill logic
    // Reset the hidden picker so the same date can be re-selected next time
    const picker = document.getElementById('docDatePicker');
    if (picker) picker.value = '';
}

// ── Profile Modal ─────────────────────────────────────────────────────────────
// Tracks the login email as loaded from the server, so saveProfile() can tell
// whether the user actually changed their portal login email (vs. just
// touching the separate "send-as" email/password box below it).
let _profileOriginalEmail = '';

function toggleProfilePwd(inputId, btn) {
    const input = document.getElementById(inputId);
    const icon  = btn.querySelector('i');
    const showing = input.type === 'text';
    input.type = showing ? 'password' : 'text';
    icon.className = showing ? 'ph ph-eye' : 'ph ph-eye-slash';
    btn.setAttribute('aria-label', showing ? 'Show password' : 'Hide password');
}

async function openProfileModal() {
    document.getElementById('profileModal').style.display = 'flex';
    document.getElementById('profileStatus').textContent = '';
    document.getElementById('profileCurrentPassword').value = '';
    document.getElementById('profileNewPassword').value = '';
    document.getElementById('profileConfirmPassword').value = '';
    document.getElementById('profileSmtpPassword').value = '';
    // Reset any show/hide toggles left open from a previous visit
    document.querySelectorAll('#profileModal .profile-input-toggle').forEach(btn => {
        const input = btn.parentElement.querySelector('input');
        if (input) input.type = 'password';
        const icon = btn.querySelector('i');
        if (icon) icon.className = 'ph ph-eye';
    });
    try {
        const res  = await fetch('/api/profile');
        const data = await res.json();
        document.getElementById('profileFullName').value = data.full_name || '';
        document.getElementById('profileUsername').value = data.username  || '';
        document.getElementById('profileEmail').value    = data.email     || '';
        _profileOriginalEmail = data.email || '';
    } catch(e) {
        document.getElementById('profileStatus').textContent = 'Failed to load profile';
    }
    try {
        const res2  = await fetch('/api/settings/email');
        const data2 = await res2.json();
        document.getElementById('profileSmtpEmail').value  = data2.smtp_email || '';
        document.getElementById('profileMailProvider').value = data2.provider || 'office365';
        document.getElementById('profileSmtpServer').value = data2.smtp_server || '';
        document.getElementById('profileSmtpPort').value   = data2.smtp_port || '';
        document.getElementById('profileSmtpSsl').checked  = data2.use_ssl !== false;
        profileApplyProviderPreset();
        _applyGraphModeToProfileModal(!!data2.graph_enabled);
    } catch(e) { /* non-fatal, send-as email is optional */ }
}

// Presets mirror MAIL_PROVIDER_PRESETS in app.py. Each user picks the
// provider THEIR OWN email is with — this is completely independent of
// whatever the admin picked in Control Panel -> Mail Settings. Two users
// can have Gmail and Office 365 configured at the same time and both work.
const PROFILE_MAIL_PROVIDER_PRESETS = {
    office365: { smtp_server: 'smtp.office365.com', smtp_port: 587, use_ssl: true },
    gmail:     { smtp_server: 'smtp.gmail.com',      smtp_port: 587, use_ssl: true },
};

function profileApplyProviderPreset() {
    const provider = document.getElementById('profileMailProvider').value;
    const row = document.getElementById('profileMailServerRow');
    const serverEl = document.getElementById('profileSmtpServer');
    const portEl = document.getElementById('profileSmtpPort');
    const sslEl = document.getElementById('profileSmtpSsl');
    const preset = PROFILE_MAIL_PROVIDER_PRESETS[provider];

    if (preset) {
        row.style.display = 'none';
        serverEl.value = preset.smtp_server;
        portEl.value   = preset.smtp_port;
        sslEl.checked  = preset.use_ssl;
    } else {
        // "Custom" — reveal the fields so the user can enter their
        // company's own mail server (e.g. a hosted Office 365 domain,
        // Zoho, a self-hosted mail server, etc).
        row.style.display = '';
    }
}

// When Microsoft Graph is configured server-side, sending as another
// mailbox needs no password at all — hide that field entirely instead
// of showing something that's no longer relevant.
function _applyGraphModeToProfileModal(graphEnabled) {
    // Password field now stays visible even when Graph is enabled. Graph
    // only auto-covers users whose mailbox lives inside the configured
    // Microsoft 365 tenant; anyone else (Gmail, outside domains, etc.)
    // still needs their own app password here — so hiding this field
    // whenever Graph was on used to block exactly those people.
    const hint = document.querySelector('#profileModal .profile-hint');
    if (hint) {
        hint.textContent = graphEnabled
            ? (currentLang === 'ar'
                ? 'إذا كان بريدك على مايكروسوفت 365 لنفس الشركة، قد يُرسَل تلقائياً دون كلمة مرور. غير ذلك (مثل Gmail)، أدخل كلمة مرور التطبيق أدناه.'
                : "If your email is on the company's Microsoft 365, sending may already work automatically with no password. Otherwise (e.g. Gmail), enter an app password below.")
            : (currentLang === 'ar'
                ? 'أضف حساب بريدك الخاص — أي مزوّد — ليظهر البريد المرسل من البوابة بعنوانك الحقيقي بدلاً من حساب مشترك.'
                : "Add your own email login — any provider — so emails you send from the portal come from your real address instead of a shared account.");
    }
}

function closeProfileModal() {
    document.getElementById('profileModal').style.display = 'none';
}

async function saveProfile() {
    const email       = document.getElementById('profileEmail').value.trim();
    const currentPass = document.getElementById('profileCurrentPassword').value;
    const newPass     = document.getElementById('profileNewPassword').value;
    const confPass    = document.getElementById('profileConfirmPassword').value;
    const smtpEmail    = document.getElementById('profileSmtpEmail').value.trim();
    const smtpPass     = document.getElementById('profileSmtpPassword').value;
    const smtpProvider = document.getElementById('profileMailProvider').value;
    const smtpServer   = document.getElementById('profileSmtpServer').value.trim();
    const smtpPort     = document.getElementById('profileSmtpPort').value.trim();
    const smtpSsl      = document.getElementById('profileSmtpSsl').checked;
    const status      = document.getElementById('profileStatus');

    const setStatus = (msg, type) => {
        status.textContent = msg;
        status.className = `scan-status scan-status--${type}`;
    };

    // The portal login password is only needed when the user is actually
    // changing their portal login email or setting a new portal password —
    // i.e. the Security section. Just saving/updating the send-as email
    // password above it doesn't touch the account login at all, so it
    // shouldn't be blocked on this field.
    const accountChanged = (email !== _profileOriginalEmail) || !!newPass;

    if (accountChanged && !currentPass) {
        setStatus(currentLang === 'ar'
            ? '⚠ كلمة المرور الحالية مطلوبة لتغيير بريد أو كلمة مرور الدخول'
            : '⚠ Current password is required to change your login email or password', 'error');
        return;
    }
    if (!email) {
        setStatus(currentLang === 'ar' ? '⚠ البريد الإلكتروني مطلوب' : '⚠ Email is required', 'error');
        return;
    }
    if (newPass && newPass !== confPass) {
        setStatus(currentLang === 'ar' ? '⚠ كلمتا المرور الجديدة غير متطابقتين' : '⚠ New passwords do not match', 'error');
        return;
    }

    setStatus(currentLang === 'ar' ? 'جارٍ الحفظ...' : 'Saving...', 'loading');

    try {
        if (accountChanged) {
            const body = { email, current_password: currentPass };
            if (newPass) body.password = newPass;

            const res  = await fetch('/api/profile', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify(body),
            });
            const data = await res.json();
            if (!res.ok || !data.success) {
                setStatus('⚠ ' + (data.error || 'Save failed'), 'error');
                return;
            }
        }

        // Optional send-as email — only touch it if the user actually filled something in.
        // No portal password needed: the app/email password entered here is
        // itself the credential being saved, and the request is already
        // authenticated via the logged-in session.
        if (smtpEmail) {
            const body2 = {
                smtp_email: smtpEmail,
                provider: smtpProvider,
                smtp_server: smtpServer,
                smtp_port: smtpPort,
                use_ssl: smtpSsl,
            };
            if (smtpPass) body2.smtp_password = smtpPass;
            const res2  = await fetch('/api/settings/email', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify(body2),
            });
            const data2 = await res2.json();
            if (!res2.ok || !data2.success) {
                setStatus('⚠ ' + (data2.error || 'Send-as email save failed'), 'error');
                return;
            }
        }

        _profileOriginalEmail = email;
        setStatus(currentLang === 'ar' ? '✓ تم الحفظ بنجاح' : '✓ Saved successfully', 'ok');
        setTimeout(closeProfileModal, 1200);
    } catch(e) {
        setStatus('⚠ Network error', 'error');
    }
}

/* ── PROFILE: send-as email test / remove ── */
async function profileTestSmtp() {
    const status = document.getElementById('profileStatus');
    const setStatus = (msg, type) => { status.textContent = msg; status.className = `scan-status scan-status--${type}`; };

    const smtpEmail = document.getElementById('profileSmtpEmail').value.trim();
    const smtpPass  = document.getElementById('profileSmtpPassword').value;
    setStatus(currentLang === 'ar' ? 'جارٍ الاختبار...' : 'Testing connection...', 'loading');
    try {
        const body = {
            provider: document.getElementById('profileMailProvider').value,
            smtp_server: document.getElementById('profileSmtpServer').value.trim(),
            smtp_port: document.getElementById('profileSmtpPort').value.trim(),
            use_ssl: document.getElementById('profileSmtpSsl').checked,
        };
        if (smtpEmail && smtpPass) { body.smtp_email = smtpEmail; body.smtp_password = smtpPass; }
        const res  = await fetch('/api/settings/email/test', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(body),
        });
        const data = await res.json();
        if (!res.ok || !data.success) {
            setStatus('⚠ ' + (data.error || 'Test failed'), 'error');
            return;
        }
        setStatus(currentLang === 'ar' ? '✓ الاتصال ناجح' : '✓ Connection successful', 'ok');
    } catch(e) {
        setStatus('⚠ Network error', 'error');
    }
}

async function profileRemoveSmtp() {
    const status = document.getElementById('profileStatus');
    const setStatus = (msg, type) => { status.textContent = msg; status.className = `scan-status scan-status--${type}`; };

    if (!confirm(currentLang === 'ar'
        ? 'إزالة إعداد البريد الشخصي؟ سيتم استخدام الحساب المشترك بدلاً من ذلك.'
        : 'Remove your personal send-as email? Sending will fall back to the shared account.')) return;

    setStatus(currentLang === 'ar' ? 'جارٍ الإزالة...' : 'Removing...', 'loading');
    try {
        const res  = await fetch('/api/settings/email', { method: 'DELETE' });
        const data = await res.json();
        if (!res.ok || !data.success) {
            setStatus('⚠ ' + (data.error || 'Remove failed'), 'error');
            return;
        }
        document.getElementById('profileSmtpEmail').value = '';
        document.getElementById('profileSmtpPassword').value = '';
        document.getElementById('profileMailProvider').value = 'office365';
        document.getElementById('profileSmtpServer').value = '';
        document.getElementById('profileSmtpPort').value = '';
        document.getElementById('profileSmtpSsl').checked = true;
        profileApplyProviderPreset();
        setStatus(currentLang === 'ar' ? '✓ تمت الإزالة' : '✓ Removed', 'ok');
    } catch(e) {
        setStatus('⚠ Network error', 'error');
    }
}

(function initSessionTimeout() {
    const SESSION_MS   = 3600 * 1000;   // must match Flask PERMANENT_SESSION_LIFETIME
    const WARN_BEFORE  = 5 * 60 * 1000; // warn 5 min before expiry
    const WARN_AT      = SESSION_MS - WARN_BEFORE;

    function showSessionWarning() {
        let banner = document.getElementById('_sessionWarnBanner');
        if (!banner) {
            banner = document.createElement('div');
            banner.id = '_sessionWarnBanner';
            banner.style.cssText = `
                position:fixed;bottom:24px;left:50%;transform:translateX(-50%);
                background:#d97706;color:#fff;padding:12px 24px;border-radius:8px;
                font-size:14px;font-weight:600;z-index:99999;box-shadow:0 4px 16px rgba(0,0,0,.3);
                display:flex;gap:16px;align-items:center;
            `;
            const msg  = document.createElement('span');
            const btn  = document.createElement('button');
            btn.textContent = currentLang === 'ar' ? 'تمديد الجلسة' : 'Stay logged in';
            btn.style.cssText = 'background:#fff;color:#d97706;border:none;padding:6px 14px;border-radius:6px;cursor:pointer;font-weight:700;';
            btn.onclick = () => {
                fetch('/api/stats').finally(() => { banner.remove(); initSessionTimeout(); });
            };
            banner.appendChild(msg);
            banner.appendChild(btn);
            document.body.appendChild(banner);
        }
        const mins = Math.ceil(WARN_BEFORE / 60000);
        banner.querySelector('span').textContent =
            currentLang === 'ar'
                ? `⚠ ستنتهي جلستك خلال ${mins} دقائق`
                : `⚠ Your session expires in ${mins} minutes`;
    }

    // Reset timer on any user activity
    let warnTimer, expireTimer;
    function resetTimers() {
        clearTimeout(warnTimer);
        clearTimeout(expireTimer);
        warnTimer   = setTimeout(showSessionWarning, WARN_AT);
        expireTimer = setTimeout(() => { window.location.href = '/login'; }, SESSION_MS);
    }
    ['click','keydown','mousemove','touchstart'].forEach(e =>
        document.addEventListener(e, resetTimers, { passive: true })
    );
    resetTimers();
})();

(function init() {
    const today = new Date();
    const yyyy = today.getFullYear();
    const mm = String(today.getMonth() + 1).padStart(2, '0');
    const dd = String(today.getDate()).padStart(2, '0');
    const todayFmt = `${yyyy}/${mm}/${dd}`;

    // Task 3: auto-fill Registration Date with today
    const regDate = document.getElementById('registrationDate');
    if (regDate) {
        regDate.value = todayFmt;
        regDate.addEventListener('change', updateHijriDisplay);
    }
    updateHijriDisplay();

    setRegistrationPlaceholder();

    setProfileInitials();
    setupDragDrop();
    setLang(currentLang);

    loadEntities();
    loadStats();
    syncInquiryDateFields();

    setTimeout(runCountUps, 200);
})();


// ══════════════════════════════════════════════════════════════════════════════
// CONTROL PANEL — User Department Permissions
// ══════════════════════════════════════════════════════════════════════════════

let _cpAllDepts = [];        // [{id, name}]
let _cpAllUsers = [];        // [{id, full_name, username, user_type, dep_id_from}]
let _cpEditingUserId = null;
let _cpEditingDepIds = new Set();

async function cpRefreshUsers() {
    const tbody = document.getElementById('cpUserTbody');
    if (tbody) tbody.innerHTML = `<tr><td colspan="5" style="padding:2rem;text-align:center;color:var(--text-muted)">Loading…</td></tr>`;
    cpCloseDrawer();
    try {
        const [usersRes, deptsRes] = await Promise.all([
            fetch('/api/admin/users'),
            fetch('/api/entities?all=1'),
        ]);
        _cpAllUsers = await usersRes.json();
        _cpAllDepts = await deptsRes.json();
        if (!Array.isArray(_cpAllUsers)) _cpAllUsers = [];
        if (!Array.isArray(_cpAllDepts)) _cpAllDepts = [];
        cpRenderTable();
    } catch (e) {
        if (tbody) tbody.innerHTML = `<tr><td colspan="5" style="padding:2rem;text-align:center;color:var(--danger,red)">Failed to load users</td></tr>`;
        console.error('cpRefreshUsers:', e);
    }
}

function cpRenderTable() {
    const tbody = document.getElementById('cpUserTbody');
    if (!tbody) return;
    // Admin has unrestricted access to every department already, so showing
    // it here with a department-picker is just noise — filter it out.
    const nonAdminUsers = _cpAllUsers.filter(u => (u.username || '').toLowerCase() !== 'admin');
    if (!nonAdminUsers.length) {
        tbody.innerHTML = `<tr><td colspan="5" style="padding:2rem;text-align:center;color:var(--text-muted)">No users found</td></tr>`;
        return;
    }
    const deptMap = {};
    _cpAllDepts.forEach(d => { deptMap[d.id] = d.name; });

    const sorted = [...nonAdminUsers].sort((a, b) =>
        (a.full_name || a.username || '').localeCompare(b.full_name || b.username || ''));

    tbody.innerHTML = '';
    sorted.forEach(user => {
        const isAdmin = false;
        const roleLabel = isAdmin
            ? `<span style="background:var(--primary);color:#fff;font-size:.72rem;padding:.15rem .5rem;border-radius:10px">Admin</span>`
            : `<span style="background:var(--bg-subtle,#eee);color:var(--text-muted);font-size:.72rem;padding:.15rem .5rem;border-radius:10px">User</span>`;

        const depIds = (user.dep_id_from || '').split(',').map(s => s.trim()).filter(Boolean);
        const deptNames = depIds.map(id => deptMap[id] || `#${id}`);
        const deptsCell = deptNames.length
            ? deptNames.map(n => `<span style="display:inline-block;background:var(--bg-subtle,#f0f0f0);color:var(--text);font-size:.74rem;padding:.15rem .5rem;border-radius:4px;margin:.1rem">${escapeHtml(n)}</span>`).join(' ')
            : `<span style="color:var(--text-muted);font-size:.82rem;font-style:italic">No access</span>`;

        const tr = document.createElement('tr');
        tr.setAttribute('data-user-id', user.id);
        tr.setAttribute('data-search', `${user.full_name} ${user.username}`.toLowerCase());
        tr.style.cssText = `border-bottom:1px solid var(--border);transition:background .15s${isAdmin ? ';background:var(--bg-subtle,#f8fafc)' : ''}`;
        tr.innerHTML = `
            <td style="padding:.6rem 1rem;font-weight:500">${escapeHtml(user.full_name) || '—'}${isAdmin ? ' <i class="ph ph-shield-check" style="color:var(--primary);font-size:.85rem" title="Administrator"></i>' : ''}</td>
            <td style="padding:.6rem 1rem;color:var(--text-muted);font-family:monospace;font-size:.84rem">${escapeHtml(user.username)}</td>
            <td style="padding:.6rem 1rem">${roleLabel}</td>
            <td style="padding:.6rem 1rem" id="cpDeptCell-${user.id}">${deptsCell}</td>
            <td style="padding:.6rem 1rem;text-align:center">
                <button style="background:var(--bg-subtle,#eee);color:var(--text);border:1px solid var(--border);border-radius:6px;padding:.3rem .85rem;font-size:.8rem;cursor:pointer" onclick="cpToggleEdit(${user.id})">
                    <i class="ph ph-pencil-simple"></i> Edit
                </button>
            </td>`;
        tbody.appendChild(tr);
    });
    cpFilterTable();
}

async function cpToggleEdit(userId) {
    if (_cpEditingUserId === userId) { cpCloseDrawer(); return; }
    _cpEditingUserId = userId;
    cpRenderTable();

    const user = _cpAllUsers.find(u => u.id === userId);
    const fullName = user ? (user.full_name || user.username) : `User ${userId}`;
    const username = user ? (user.username || '') : '';

    const nameEl = document.getElementById('cpPermUserName');
    if (nameEl) nameEl.textContent = fullName;
    const loginEl = document.getElementById('cpPermUserLogin');
    if (loginEl) loginEl.textContent = '@' + username;
    const avatarEl = document.getElementById('cpPermAvatarInitials');
    if (avatarEl) {
        const parts = fullName.trim().split(' ');
        avatarEl.textContent = (parts[0]?.[0] || '') + (parts[1]?.[0] || parts[0]?.[1] || '');
    }

    // Read permissions from already-loaded user data — no extra fetch needed
    _cpEditingDepIds = new Set();
    if (user && user.dep_id_from) {
        user.dep_id_from.split(',').map(s => s.trim()).filter(Boolean)
            .forEach(id => _cpEditingDepIds.add(Number(id)));
    }

    // Show terminate button only for non-admin users
    const isEditingAdmin = (username || '').toLowerCase() === 'admin';
    const terminateBtn = document.getElementById('cpTerminateBtn');
    if (terminateBtn) terminateBtn.style.display = isEditingAdmin ? 'none' : 'flex';

    cpRenderDrawer();
    // Load portal access rights then render
    cpLoadAccr().then(() => cpRenderPortalSections());
    cpSwitchTab('file');
    const modal = document.getElementById('cpPermModal');
    if (modal) modal.style.display = 'grid';
}

async function cpTerminateUser() {
    const user = _cpAllUsers.find(u => u.id === _cpEditingUserId);
    const name = user ? (user.full_name || user.username) : 'this user';
    if (!confirm(`Terminate account for ${name}?\n\nThis will disable their access immediately. This action cannot be undone from here.`)) return;

    try {
        const res  = await fetch(`/api/admin/users/${_cpEditingUserId}/terminate`, { method: 'POST' });
        const data = await res.json();
        if (!res.ok || !data.success) {
            let msg = data.error || 'Unknown error';
            if (Array.isArray(data.pending_approvals) && data.pending_approvals.length) {
                const lines = data.pending_approvals
                    .map(p => `  • #${p.instance_id} — ${p.subject}`)
                    .join('\n');
                msg += `\n\n${lines}`;
            }
            alert('Failed to terminate account: ' + msg);
            return;
        }
        cpCloseDrawer();
        cpRefreshUsers();
        showToast(`Account for ${name} has been terminated.`, 'ok');
    } catch (e) {
        alert('Network error — could not reach server');
    }
}

function cpSwitchTab(tab) {
    const filePanel   = document.getElementById('cpTabFilePanel');
    const portalPanel = document.getElementById('cpTabPortalPanel');
    const fileTab     = document.getElementById('cpTabFileAccess');
    const portalTab   = document.getElementById('cpTabPortalAccess');
    if (!filePanel || !portalPanel) return;
    if (tab === 'file') {
        filePanel.style.display   = 'block';
        portalPanel.style.display = 'none';
        if (fileTab)   fileTab.classList.add('cp-perm-tab--active');
        if (portalTab) portalTab.classList.remove('cp-perm-tab--active');
    } else {
        filePanel.style.display   = 'none';
        portalPanel.style.display = 'block';
        if (fileTab)   fileTab.classList.remove('cp-perm-tab--active');
        if (portalTab) portalTab.classList.add('cp-perm-tab--active');
        // Re-render portal checkboxes (data already loaded on modal open)
        cpRenderPortalSections();
    }
}

// ── Portal Access Rights (Sys_AccR) ──────────────────────────────────────
const _cpPages = [
    { page_id: 1, icon: 'ph-magnifying-glass', label: 'Inquiries',      desc: 'Search & view archived documents' },
    { page_id: 2, icon: 'ph-archive-box',       label: 'Archive',        desc: 'Create & save new documents' },
    { page_id: 3, icon: 'ph-folder-open',       label: 'Folder Browser', desc: 'Browse folder structure' },
    { page_id: 4, icon: 'ph-flow-arrow',        label: 'Workflow',       desc: 'Send, review, and approve documents' },
    { page_id: 5, icon: 'ph-chat-circle-text',  label: 'Messages',       desc: 'View in-app messages' },
];

// Which permissions apply to each page (others are hidden + kept NULL in DB)
const _CP_PAGE_PERMS = {
    1: ['can_open', 'can_prew', 'can_edit', 'can_del', 'can_print', 'can_qr'], // Inquiries
    2: ['can_open', 'can_prew', 'can_add'],                           // Archive
    3: ['can_open'],                                                   // Folder Browser
    4: ['can_open', 'can_approve', 'can_add'],                         // Workflow
    5: ['can_open'],                                                   // Messages
};

const _CP_PERM_DEFS = [
    { field: 'can_open',    icon: 'ph-eye',              label: 'Can Open',    desc: 'Access this page' },
    { field: 'can_prew',    icon: 'ph-magnifying-glass',  label: 'Can Preview', desc: 'Preview document attachments' },
    { field: 'can_edit',    icon: 'ph-pencil-simple',     label: 'Can Edit',    desc: 'Edit transactions' },
    { field: 'can_del',     icon: 'ph-trash',             label: 'Can Delete',  desc: 'Delete transactions' },
    { field: 'can_print',   icon: 'ph-printer',           label: 'Can Print',   desc: 'Print documents' },
    { field: 'can_add',     icon: 'ph-plus-circle',       label: 'Can Add',     desc: 'Add new transactions' },
    { field: 'can_qr',      icon: 'ph-qr-code',           label: 'Can Generate QR', desc: 'Generate a scannable QR code for a document' },
    { field: 'can_approve', icon: 'ph-check-circle',      label: 'Can Approve', desc: 'Approve, reject, or forward workflow items' },
];

// Holds current accr rows keyed by page_id
let _cpAccrData = {}; // { 1: {can_open:0,can_edit:0,...}, ... }

async function cpLoadAccr() {
    if (!_cpEditingUserId) return;
    try {
        const res = await fetch(`/api/admin/users/${_cpEditingUserId}/accr`);
        const rows = await res.json();
        _cpAccrData = {};
        if (Array.isArray(rows)) {
            rows.forEach(r => { _cpAccrData[r.page_id] = r; });
        }
    } catch(e) {
        _cpAccrData = {};
    }
}

function cpRenderPortalSections() {
    const list = document.getElementById('cpPortalSectionList');
    if (!list) return;

    list.innerHTML = _cpPages.map(page => {
        const perms = _cpAccrData[page.page_id] || {};
        const applicablePerms = _CP_PAGE_PERMS[page.page_id] || [];
        const permRows = _CP_PERM_DEFS
            .filter(p => applicablePerms.includes(p.field))
            .map(p => {
                // 0 = allowed (checked), 1 = denied (unchecked)
                const isAllowed = (perms[p.field] ?? 0) === 0;
                return `<label style="display:flex;align-items:center;gap:.5rem;padding:.3rem .4rem;border-radius:5px;cursor:pointer;font-size:.83rem;transition:background .12s" onmouseover="this.style.background='var(--bg-subtle,#f5f5f5)'" onmouseout="this.style.background='transparent'">
                <input type="checkbox" ${isAllowed ? 'checked' : ''}
                    data-page="${page.page_id}" data-field="${p.field}"
                    style="accent-color:var(--primary);width:14px;height:14px;flex-shrink:0;cursor:pointer"
                    onchange="cpToggleAccr(this)">
                <i class="ph ${p.icon}" style="color:var(--primary);font-size:13px;flex-shrink:0"></i>
                <span>
                    <span style="font-weight:500">${p.label}</span>
                    <span style="color:var(--text-muted);font-size:.76rem;display:block;line-height:1.3">${p.desc}</span>
                </span>
            </label>`;
            }).join('');

        return `<div style="border:1px solid var(--border);border-radius:10px;overflow:hidden;margin-bottom:.75rem">
            <div style="display:flex;align-items:center;gap:.6rem;padding:.6rem .9rem;background:var(--surface2,#f8fafc);border-bottom:1px solid var(--border)">
                <i class="ph ${page.icon}" style="color:var(--primary);font-size:1rem"></i>
                <span style="font-weight:600;font-size:.9rem">${page.label}</span>
                <span style="font-size:.76rem;color:var(--text-muted);margin-left:.25rem">${page.desc}</span>
            </div>
            <div style="display:grid;grid-template-columns:1fr 1fr;gap:0 .5rem;padding:.5rem .75rem">
                ${permRows}
            </div>
        </div>`;
    }).join('');
}

async function cpToggleAccr(checkbox) {
    const pageId = Number(checkbox.dataset.page);
    const field  = checkbox.dataset.field;
    // checked = allowed = 0, unchecked = denied = 1
    const value  = checkbox.checked ? 0 : 1;

    // Optimistic local update
    if (!_cpAccrData[pageId]) _cpAccrData[pageId] = {};
    _cpAccrData[pageId][field] = value;

    const msg = document.getElementById('cpSaveMsg');
    try {
        const res = await fetch(`/api/admin/users/${_cpEditingUserId}/accr`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ page_id: pageId, field, value }),
        });
        const data = await res.json();
        if (data.success) {
            if (msg) {
                msg.textContent = '✓ Saved';
                msg.style.display = 'inline';
                clearTimeout(msg._t);
                msg._t = setTimeout(() => { msg.style.display = 'none'; }, 2000);
            }
        } else {
            // Revert checkbox on failure
            checkbox.checked = !checkbox.checked;
            if (msg) { msg.textContent = '✗ ' + (data.error || 'Error'); msg.style.display = 'inline'; }
        }
    } catch(e) {
        checkbox.checked = !checkbox.checked;
        if (msg) { msg.textContent = '✗ Network error'; msg.style.display = 'inline'; }
    }
}

function cpRenderDrawer() {
    const list = document.getElementById('cpEditDeptList');
    if (!list) return;
    list.innerHTML = '';
    _cpAllDepts.forEach(dept => {
        const checked = _cpEditingDepIds.has(Number(dept.id));
        const label = document.createElement('label');
        label.style.cssText = `display:flex;align-items:center;gap:.5rem;padding:.45rem .7rem;border:1px solid ${checked ? 'var(--primary)' : 'var(--border)'};border-radius:6px;cursor:pointer;font-size:.87rem;background:${checked ? 'rgba(var(--primary-rgb,79,70,229),.06)' : 'transparent'};transition:all .15s`;
        label.innerHTML = `
            <input type="checkbox" ${checked ? 'checked' : ''} value="${dept.id}"
                style="accent-color:var(--primary);width:15px;height:15px;flex-shrink:0">
            <span>${escapeHtml(dept.name)}</span>`;

        label.querySelector('input').addEventListener('change', async (e) => {
            const depId = Number(dept.id);
            e.target.checked ? _cpEditingDepIds.add(depId) : _cpEditingDepIds.delete(depId);
            label.style.borderColor = e.target.checked ? 'var(--primary)' : 'var(--border)';
            label.style.background = e.target.checked ? 'rgba(var(--primary-rgb,79,70,229),.06)' : 'transparent';
            await cpPersist();
        });
        list.appendChild(label);
    });
}

async function cpPersist() {
    if (!_cpEditingUserId) return;
    const dep_ids = [..._cpEditingDepIds];
    const msg = document.getElementById('cpSaveMsg');
    try {
        const res = await fetch(`/api/admin/users/${_cpEditingUserId}/permissions`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ dep_ids }),
        });
        const data = await res.json();
        if (data.success) {
            const u = _cpAllUsers.find(u => u.id === _cpEditingUserId);
            if (u) u.dep_id_from = data.dep_id_from;
            cpRefreshDeptCell(_cpEditingUserId);
            if (msg) {
                msg.textContent = '✓ Saved';
                msg.style.display = 'inline';
                clearTimeout(msg._t);
                msg._t = setTimeout(() => { msg.style.display = 'none'; }, 2000);
            }
        } else {
            if (msg) { msg.textContent = '✗ ' + (data.error || 'Error'); msg.style.display = 'inline'; }
        }
    } catch (e) { console.error('cpPersist:', e); }
}

function cpRefreshDeptCell(userId) {
    const cell = document.getElementById(`cpDeptCell-${userId}`);
    if (!cell) return;
    const deptMap = {};
    _cpAllDepts.forEach(d => { deptMap[d.id] = d.name; });
    const user = _cpAllUsers.find(u => u.id === userId);
    const depIds = (user?.dep_id_from || '').split(',').map(s => s.trim()).filter(Boolean);
    const deptNames = depIds.map(id => deptMap[id] || `#${id}`);
    cell.innerHTML = deptNames.length
        ? deptNames.map(n => `<span style="display:inline-block;background:var(--bg-subtle,#f0f0f0);color:var(--text);font-size:.74rem;padding:.15rem .5rem;border-radius:4px;margin:.1rem">${n}</span>`).join(' ')
        : `<span style="color:var(--text-muted);font-size:.82rem;font-style:italic">No access</span>`;
}

function cpCloseDrawer() {
    _cpEditingUserId = null;
    _cpEditingDepIds = new Set();
    const modal = document.getElementById('cpPermModal');
    if (modal) modal.style.display = 'none';
    document.querySelectorAll('#cpUserTbody tr').forEach(tr => {
        tr.style.background = '';
        const btn = tr.querySelector('button');
        if (btn) {
            btn.style.cssText = 'background:var(--bg-subtle,#eee);color:var(--text);border:1px solid var(--border);border-radius:6px;padding:.3rem .85rem;font-size:.8rem;cursor:pointer';
            btn.innerHTML = '<i class="ph ph-pencil-simple"></i> Edit';
        }
    });
}

function cpClosePermModal() { cpCloseDrawer(); }

// ── ADD USER ──────────────────────────────────────────────────────────────
function cpOpenAddUser() {
    document.getElementById('auFullName').value  = '';
    document.getElementById('auEmail').value     = '';
    document.getElementById('auUsername').value  = '';
    document.getElementById('auDepId').value     = '46';
    document.getElementById('auStatus').textContent = '';
    document.getElementById('auStatus').className = '';
    auSetPasswordMode('random');
    document.getElementById('cpAddUserModal').style.display = 'grid';
}

function auSetPasswordMode(mode) {
    const input    = document.getElementById('auPassword');
    const regenBtn = document.getElementById('auRegenBtn');
    const randBtn  = document.getElementById('auModeRandomBtn');
    const manBtn   = document.getElementById('auModeManualBtn');
    randBtn.classList.toggle('active', mode === 'random');
    manBtn.classList.toggle('active', mode === 'manual');
    if (mode === 'manual') {
        input.readOnly = false;
        input.value = '';
        input.placeholder = 'Type a password';
        regenBtn.style.display = 'none';
        input.focus();
    } else {
        input.readOnly = true;
        input.placeholder = 'Set a password';
        regenBtn.style.display = '';
        auGenerateRandomPassword();
    }
}

function auGenerateRandomPassword() {
    const code = Math.floor(10000 + Math.random() * 90000); // always 5 digits
    document.getElementById('auPassword').value = String(code);
}

function cpCloseAddUser() {
    document.getElementById('cpAddUserModal').style.display = 'none';
}

async function cpSaveNewUser() {
    const fullName = document.getElementById('auFullName').value.trim();
    const email    = document.getElementById('auEmail').value.trim();
    const username = document.getElementById('auUsername').value.trim();
    const password = document.getElementById('auPassword').value.trim();
    const depId    = parseInt(document.getElementById('auDepId').value.trim()) || 46;
    const status   = document.getElementById('auStatus');

    const setStatus = (msg, type) => {
        status.textContent = msg;
        status.style.color = type === 'error' ? 'var(--danger)' : type === 'ok' ? 'var(--success)' : 'var(--text-muted)';
    };

    if (!fullName) { setStatus('⚠ Full name is required', 'error'); return; }
    if (!email)    { setStatus('⚠ Email is required', 'error'); return; }
    if (!username) { setStatus('⚠ Username is required', 'error'); return; }
    if (!password) { setStatus('⚠ Password is required', 'error'); return; }

    setStatus('Creating user…', 'info');

    try {
        const res  = await fetch('/api/admin/users/create', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ full_name: fullName, email, username, password, dep_id: depId }),
        });
        const data = await res.json();
        if (!res.ok || !data.success) {
            setStatus('⚠ ' + (data.error || 'Failed to create user'), 'error');
            return;
        }
        setStatus('✓ User created successfully!', 'ok');
        setTimeout(() => {
            cpCloseAddUser();
            cpRefreshUsers();
        }, 900);
    } catch (e) {
        setStatus('⚠ Network error — could not reach server', 'error');
    }
}

function cpFilterTable() {
    const q = (document.getElementById('cpSearch')?.value || '').toLowerCase();
    document.querySelectorAll('#cpUserTbody tr[data-search]').forEach(tr => {
        tr.style.display = !q || tr.getAttribute('data-search').includes(q) ? '' : 'none';
    });
}

// ── CUSTOM FIELDS ZONE (archive form) ──────────────────────────────────────
// Called whenever a folder is selected; deptId = the entity (main folder) ID
// Fields are loaded from the database (Sys_Department.Fe1Name–Fe7Name).

// Cache of entity custom field configs to avoid repeated API calls
const _feFieldsCache = {};

async function renderCustomFieldsZone(deptId) {
    const zone = document.getElementById('customFieldsZone');
    const body = document.getElementById('customFieldsBody');
    if (!zone || !body) return;

    // Try to resolve entity ID from deptId
    let entityId = null;
    if (deptId) {
        const byId     = allEntities.find(e => e.id      === parseInt(deptId, 10));
        const byDeptId = allEntities.find(e => e.dept_id === parseInt(deptId, 10));
        entityId = (byId || byDeptId)?.id || null;
    }

    if (!entityId) { zone.style.display = 'none'; body.innerHTML = ''; return; }

    // Load fields from DB (with cache)
    let fieldDefs;
    if (_feFieldsCache[entityId]) {
        fieldDefs = _feFieldsCache[entityId];
    } else {
        try {
            const res = await fetch(`/api/entities/${entityId}/fields`);
            fieldDefs = res.ok ? await res.json() : {};
            _feFieldsCache[entityId] = fieldDefs;
        } catch {
            fieldDefs = {};
        }
    }

    // Load dropdown options from Sys_DP_DL
    let dropdownOptions = {};
    try {
        const res = await fetch(`/api/entities/${entityId}/dropdown-options`);
        dropdownOptions = res.ok ? await res.json() : {};
    } catch { dropdownOptions = {}; }

    // Build field rows — only show fields that have a label
    const rows = [];
    for (let i = 1; i <= 7; i++) {
        const label = (fieldDefs[`Fe${i}Name`] || '').trim();
        if (!label) continue;
        const feKey = `Fe${i}`;
        if (i <= 3) {
            // Dropdown — options from Sys_DP_DL (keyed by Fe1/Fe2/Fe3)
            const opts = (dropdownOptions[feKey] || {}).options || [];
            const optsHtml = opts.map(o => `<option value="${o}">${o}</option>`).join('');
            rows.push(`
            <div class="field-group cf-field" data-fe-key="${feKey}">
                <label class="field-label">${label}</label>
                <select class="cf-dropdown" data-cf-label="${label}" name="${feKey}">
                    <option value="">— Select —</option>
                    ${optsHtml}
                </select>
            </div>`);
        } else {
            // Text box
            rows.push(`
            <div class="field-group cf-field" data-fe-key="${feKey}">
                <label class="field-label">${label}</label>
                <input type="text" class="cf-text-input" placeholder="${label}…"
                    data-cf-label="${label}" name="${feKey}">
            </div>`);
        }
    }

    if (!rows.length) { zone.style.display = 'none'; body.innerHTML = ''; return; }

    body.innerHTML = '<div class="field-row" style="grid-template-columns:1fr 1fr 1fr;align-items:start">' + rows.join('') + '</div>';
    zone.style.display = '';
}

// Helper: get custom field values as Fe1–Fe7 keyed object (for form submission)
function getCustomFieldValues() {
    const result = {};
    document.querySelectorAll('#customFieldsBody .cf-field').forEach(el => {
        const feKey = el.dataset.feKey;
        const input = el.querySelector('input, select');
        if (feKey && input) result[feKey] = input.value || '';
    });
    return result;
}

// Helper: restore field values when loading a document in edit mode
function setCustomFieldValues(values) {
    if (!values) return;
    document.querySelectorAll('#customFieldsBody .cf-field').forEach(el => {
        const feKey = el.dataset.feKey;
        const input = el.querySelector('input, select');
        if (feKey && input && values[feKey] != null) input.value = values[feKey];
    });
}

// ── FOLDER BROWSER SECTION ──────────────────────────────────────────────────
let _fbBrowserRendered = false;

async function renderFolderBrowser() {
    const tree = document.getElementById('fbTree');
    if (!tree) return;
    if (_fbBrowserRendered && allEntities.length > 0) {
        fbFilter(document.getElementById('fbSearchInput')?.value || '');
        return;
    }
    tree.innerHTML = '<div class="fb-loading"><i class="ph ph-spinner fb-spin"></i> Loading…</div>';
    let tries = 0;
    while ((!allEntities || allEntities.length === 0) && tries < 20) {
        await new Promise(r => setTimeout(r, 300));
        tries++;
    }
    if (!allEntities || allEntities.length === 0) {
        tree.innerHTML = '<div class="fb-empty"><i class="ph ph-folder-dashed"></i><br>No folders found.</div>';
        return;
    }
    tree.innerHTML = allEntities.map(e => fbRenderDeptBrowser(e)).join('');
    _fbBrowserRendered = true;
    fbFilter(document.getElementById('fbSearchInput')?.value || '');
}

function fbRenderDeptBrowser(entity) {
    const folders = (allFoldersByDept[entity.id] || []).filter(f => (f.parent_id || 0) === 0);
    const accessible = !entity.restricted || _accessibleDeptIds.has(entity.id);
    const adminActions = `
        <div class="fb-row-actions">
            <button type="button" class="fb-action-btn" title="Edit main folder"
                onclick="event.stopPropagation(); fbOpenBuilderThenRefresh(${entity.id})">
                <i class="ph ph-pencil-simple"></i></button>
            <button type="button" class="fb-action-btn" title="Add subfolder"
                onclick="event.stopPropagation(); fbPromptCreateFolder(0, ${entity.id})">
                <i class="ph ph-folder-plus"></i></button>
        </div>`;
    return `
    <div class="fb-dept" id="fb-dept-${entity.id}">
        <div class="fb-dept-header" onclick="fbToggleDept(${entity.id})">
            <span class="fb-dept-arrow" id="fb-arrow-dept-${entity.id}">&#9654;</span>
            <i class="ph ph-buildings"></i>
            <span class="fb-dept-name">${escapeHtml(entity.name)}</span>
            ${!accessible ? '<span class="fb-lock"><i class="ph ph-lock-simple"></i></span>' : ''}
            <span class="fb-badge">${folders.length}</span>
            ${adminActions}
        </div>
        <div class="fb-dept-children" id="fb-children-dept-${entity.id}" style="display:none">
            ${folders.length
                ? folders.map(f => fbRenderFolderBrowser(entity.id, f, 0)).join('')
                : '<div class="fb-no-sub">No subfolders</div>'
            }
        </div>
    </div>`;
}

function fbRenderFolderBrowser(deptId, folder, depth) {
    const children = (allFoldersByDept[deptId] || []).filter(f => f.parent_id === folder.id);
    const indent = 16 + depth * 18;
    const icon = children.length > 0 ? 'ph-folder' : 'ph-folder-simple';
    // Namespaced by deptId + folder.id: the same Adco_Folder row can be
    // loaded under more than one department (folders link via Dept_ID, not
    // the department's own primary key — see schema notes in app.py), so
    // folder.id alone is not guaranteed unique across the whole tree.
    // Using folder.id alone here previously caused duplicate DOM ids, so
    // getElementById() (and the id-based lookups below) would silently
    // grab the FIRST matching row in the document — meaning expanding a
    // folder under one department could toggle/reveal a different
    // department's differently-named folder instead.
    const domId = `${deptId}-${folder.id}`;
    const addBtn = `
        <div class="fb-row-actions">
            <button type="button" class="fb-action-btn" title="Add subfolder"
                onclick="event.stopPropagation(); fbPromptCreateFolder(${folder.id}, ${deptId})">
                <i class="ph ph-folder-plus"></i></button>
        </div>`;
    return `
    <div class="fb-folder-wrap" id="fb-wrap-${domId}" data-name="${folder.name.toLowerCase()}" data-depth="${depth}">
        <div class="fb-folder" style="padding-inline-start:${indent}px" onclick="fbToggleFolder(${deptId}, ${folder.id})">
            ${children.length
                ? `<span class="fb-folder-arrow" id="fb-farrow-${domId}">&#9654;</span>`
                : '<span class="fb-folder-arrow fb-folder-arrow--empty"></span>'
            }
            <i class="ph ${icon}"></i>
            <span class="fb-folder-name">${escapeHtml(folder.name)}</span>
            ${children.length ? `<span class="fb-badge fb-badge--sm">${children.length}</span>` : ''}
            ${addBtn}
        </div>
        ${children.length ? `
        <div class="fb-folder-children" id="fb-fchildren-${domId}" style="display:none">
            ${children.map(c => fbRenderFolderBrowser(deptId, c, depth + 1)).join('')}
        </div>` : ''}
    </div>`;
}

function fbToggleDept(deptId) {
    const el = document.getElementById(`fb-children-dept-${deptId}`);
    const arrow = document.getElementById(`fb-arrow-dept-${deptId}`);
    if (!el) return;
    const open = el.style.display !== 'none';
    el.style.display = open ? 'none' : '';
    if (arrow) arrow.innerHTML = open ? '&#9654;' : '&#9660;';
}

function fbToggleFolder(deptId, folderId) {
    const domId = `${deptId}-${folderId}`;
    const el = document.getElementById(`fb-fchildren-${domId}`);
    const arrow = document.getElementById(`fb-farrow-${domId}`);
    if (!el) return;
    const open = el.style.display !== 'none';
    el.style.display = open ? 'none' : '';
    if (arrow) arrow.innerHTML = open ? '&#9654;' : '&#9660;';
}

// ── Folder Browser: create subfolder then re-render tree ───────────────────
// Works by piggybacking on promptCreateFolder / confirmFolderModal — we patch
// the save to also refresh the browser after a successful create.
function fbPromptCreateFolder(parentId, deptId) {
    // Store the ids so our patched confirm can refresh after saving
    _fbPendingParentId = parentId;
    _fbPendingDeptId   = deptId;
    promptCreateFolder(parentId, deptId);
}
let _fbPendingParentId = null;
let _fbPendingDeptId   = null;

// Wrap confirmFolderModal so the folder browser re-renders after each create
// that was triggered from inside the browser.
const _origConfirmFolderModal = confirmFolderModal;
confirmFolderModal = async function() {
    await _origConfirmFolderModal();
    // If the create came from the folder browser, invalidate its cache and
    // re-render so the new folder appears immediately.
    if (_fbPendingParentId !== null || _fbPendingDeptId !== null) {
        _fbBrowserRendered = false;
        _fbPendingParentId = null;
        _fbPendingDeptId   = null;
        if (document.getElementById('section-folders')?.classList.contains('active')) {
            renderFolderBrowser();
        }
    }
};

// Open the Folder Builder (edit main folder) and invalidate the browser
// cache so it re-renders with the updated name when the builder saves.
function fbOpenBuilderThenRefresh(entityId) {
    openFolderBuilder(entityId);
    // Patch saveFolderBuilder to also clear the browser cache after saving
    const _origSave = saveFolderBuilder;
    saveFolderBuilder = async function() {
        await _origSave();
        _fbBrowserRendered = false;
        saveFolderBuilder = _origSave; // restore immediately
        if (document.getElementById('section-folders')?.classList.contains('active')) {
            renderFolderBrowser();
        }
    };
}

function fbFilter(query) {
    const q = (query || '').toLowerCase().trim();
    if (!q) {
        // Full reset: show all depts, collapse all children, reset all arrows
        document.querySelectorAll('.fb-dept').forEach(d => d.style.display = '');
        document.querySelectorAll('.fb-dept-children').forEach(c => { c.style.display = 'none'; });
        document.querySelectorAll('.fb-dept-arrow').forEach(a => a.innerHTML = '&#9654;');
        document.querySelectorAll('.fb-folder-wrap').forEach(w => w.style.display = '');
        document.querySelectorAll('.fb-folder-children').forEach(c => { c.style.display = 'none'; });
        document.querySelectorAll('.fb-folder-arrow:not(.fb-folder-arrow--empty)').forEach(a => a.innerHTML = '&#9654;');
        return;
    }

    // Match subfolders by their own name
    document.querySelectorAll('.fb-folder-wrap').forEach(wrap => {
        const name = wrap.getAttribute('data-name') || '';
        wrap.style.display = name.includes(q) ? '' : 'none';
    });

    // Also match depts (main folders) by their displayed name
    document.querySelectorAll('.fb-dept').forEach(dept => {
        const deptNameEl = dept.querySelector('.fb-dept-name');
        const deptName = (deptNameEl ? deptNameEl.textContent : '').toLowerCase();
        const deptMatches = deptName.includes(q);

        if (deptMatches) {
            // Show the whole dept and all its subfolders
            dept.style.display = '';
            dept.querySelectorAll('.fb-folder-wrap').forEach(w => w.style.display = '');
            const children = dept.querySelector('.fb-dept-children');
            const arrow = dept.querySelector('.fb-dept-arrow');
            if (children) { children.style.display = ''; if (arrow) arrow.innerHTML = '&#9660;'; }
        } else {
            const anyVisible = [...dept.querySelectorAll('.fb-folder-wrap')].some(w => w.style.display !== 'none');
            dept.style.display = anyVisible ? '' : 'none';
            const children = dept.querySelector('.fb-dept-children');
            const arrow = dept.querySelector('.fb-dept-arrow');
            if (anyVisible && children) { children.style.display = ''; if (arrow) arrow.innerHTML = '&#9660;'; }
        }
    });

    // Expand any fb-folder-children that contain a visible match
    document.querySelectorAll('.fb-folder-children').forEach(fc => {
        const anyVisible = [...fc.querySelectorAll('.fb-folder-wrap')].some(w => w.style.display !== 'none');
        fc.style.display = anyVisible ? '' : 'none';
        if (anyVisible) {
            // Scoped to this exact row's own wrapper rather than an
            // id-based lookup — folder DOM ids are namespaced by dept+id
            // now, but staying scoped here is the more robust pattern.
            const arrow = fc.parentElement?.querySelector(':scope > .fb-folder > .fb-folder-arrow');
            if (arrow) arrow.innerHTML = '&#9660;';
        }
    });
}

// ══════════════════════════════════════════════════════════════════════════════
// CONTROL PANEL — Mail Settings (shared/default mailbox, admin only)
// ══════════════════════════════════════════════════════════════════════════════

let _msLoaded = false;
let _msGraphEnabled = false;

// Presets mirror MAIL_PROVIDER_PRESETS in app.py — kept here too so the UI
// can lock/prefill instantly without a round-trip, but the server still
// re-validates everything on save.
const MS_PROVIDER_PRESETS = {
    office365: { smtp_server: 'smtp.office365.com', smtp_port: 587, use_ssl: true },
    gmail:     { smtp_server: 'smtp.gmail.com',      smtp_port: 587, use_ssl: true },
};

function msApplyProviderPreset() {
    const provider = document.getElementById('mailProvider').value;
    const serverEl = document.getElementById('mailSmtpServer');
    const portEl   = document.getElementById('mailSmtpPort');
    const sslEl    = document.getElementById('mailUseSsl');
    const preset   = MS_PROVIDER_PRESETS[provider];

    const locked = !!preset && !_msGraphEnabled;
    [serverEl, portEl].forEach(el => el.disabled = locked);
    sslEl.disabled = locked;

    if (preset) {
        serverEl.value = preset.smtp_server;
        portEl.value   = preset.smtp_port;
        sslEl.checked  = preset.use_ssl;
    }
}

function _msApplyGraphMode(graphEnabled) {
    _msGraphEnabled = graphEnabled;
    const note = document.getElementById('mailSettingsGraphNote');
    const pwdField = document.getElementById('mailAppPasswordField');
    const serverRow = document.getElementById('mailServerRow');
    if (note) note.style.display = graphEnabled ? 'flex' : 'none';
    if (pwdField) pwdField.style.display = graphEnabled ? 'none' : '';
    if (serverRow) serverRow.style.opacity = graphEnabled ? '.5' : '1';
    ['mailSmtpServer', 'mailSmtpPort', 'mailUseSsl'].forEach(id => {
        const el = document.getElementById(id);
        if (el) el.disabled = graphEnabled || el.disabled;
    });
}

function _msSetBadge(configured) {
    const badge = document.getElementById('mailSettingsStatusBadge');
    if (!badge) return;
    if (configured) {
        badge.textContent = currentLang === 'ar' ? 'مُعدّ' : 'Configured';
        badge.style.background = 'var(--blue-glow)';
        badge.style.color = 'var(--blue-dark)';
    } else {
        badge.textContent = currentLang === 'ar' ? 'استخدام .env' : 'Using .env fallback';
        badge.style.background = 'var(--surface3,#e5e7eb)';
        badge.style.color = 'var(--muted)';
    }
}

async function msLoadMailSettings() {
    _msLoaded = true;
    try {
        const res = await fetch('/api/admin/mail-settings');
        const data = await res.json();
        if (!res.ok) throw new Error(data.error || 'Failed to load');

        document.getElementById('mailProvider').value    = data.provider || 'custom';
        document.getElementById('mailSenderEmail').value = data.sender_email || '';
        document.getElementById('mailSmtpServer').value  = data.smtp_server || '';
        document.getElementById('mailSmtpPort').value    = data.smtp_port || '';
        document.getElementById('mailUseSsl').checked    = data.use_ssl !== false;
        document.getElementById('mailAppPassword').value = '';

        _msApplyGraphMode(!!data.graph_enabled);
        msApplyProviderPreset();   // re-locks fields if a preset is selected
        _msSetBadge(!!data.configured);

        const meta = document.getElementById('mailSettingsMeta');
        if (meta) {
            if (data.owner && !data.owner_is_me) {
                meta.innerHTML = (currentLang === 'ar'
                    ? `⚠️ محفوظ حالياً على حساب <b>${data.owner}</b> — الحفظ هنا سينقله إلى حسابك ويستبدل بريده الشخصي إن وُجد.`
                    : `⚠️ Currently saved on <b>${data.owner}</b>'s account — saving here will move it to yours and overwrite their personal email if they had one.`);
                meta.style.color = 'var(--warning,#d97706)';
            } else if (data.owner && data.owner_is_me) {
                meta.textContent = currentLang === 'ar'
                    ? 'محفوظ على حسابك. إن كان لديك بريد شخصي "إرسال باسمك"، فإن هذه القيم تستخدم نفس الحقول.'
                    : "Saved on your own account. If you also use \"Send Documents As Yourself\", it shares these same fields.";
                meta.style.color = 'var(--muted)';
            } else {
                meta.textContent = '';
            }
        }
    } catch (e) {
        console.error('msLoadMailSettings:', e);
        const status = document.getElementById('mailSettingsStatus');
        if (status) status.innerHTML = `<span style="color:var(--danger,red)">Failed to load mail settings.</span>`;
    }
}

function _msCollectPayload() {
    return {
        provider: document.getElementById('mailProvider').value,
        sender_email: document.getElementById('mailSenderEmail').value.trim(),
        smtp_server: document.getElementById('mailSmtpServer').value.trim(),
        smtp_port: document.getElementById('mailSmtpPort').value.trim(),
        app_password: document.getElementById('mailAppPassword').value,
        use_ssl: document.getElementById('mailUseSsl').checked,
    };
}

async function msSaveMailSettings() {
    const status = document.getElementById('mailSettingsStatus');
    if (status) status.innerHTML = '';
    try {
        const res = await fetch('/api/admin/mail-settings', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(_msCollectPayload()),
        });
        const data = await res.json();
        if (!res.ok) throw new Error(data.error || 'Save failed');

        document.getElementById('mailAppPassword').value = '';
        if (status) status.innerHTML = `<span style="color:var(--success,#16a34a)">Saved.</span>`;
        msLoadMailSettings();
    } catch (e) {
        if (status) status.innerHTML = `<span style="color:var(--danger,red)">${e.message}</span>`;
    }
}

async function msSendTestEmail() {
    const status = document.getElementById('mailSettingsStatus');
    if (status) status.innerHTML = `<span style="color:var(--muted)">Sending test email…</span>`;
    try {
        const res = await fetch('/api/admin/mail-settings/test', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(_msCollectPayload()),
        });
        const data = await res.json();
        if (!res.ok) throw new Error(data.error || 'Test failed');
        if (status) status.innerHTML = `<span style="color:var(--success,#16a34a)">Test email sent — check the inbox.</span>`;
    } catch (e) {
        if (status) status.innerHTML = `<span style="color:var(--danger,red)">${e.message}</span>`;
    }
}

async function msClearMailSettings() {
    if (!confirm('Revert to .env fallback? This only removes the "shared mailbox" flag — the saved email/password stay on the owning admin\'s account, they just stop being used as the shared default.')) return;
    const status = document.getElementById('mailSettingsStatus');
    try {
        const res = await fetch('/api/admin/mail-settings', { method: 'DELETE' });
        const data = await res.json();
        if (!res.ok) throw new Error(data.error || 'Clear failed');
        document.getElementById('mailSenderEmail').value = '';
        document.getElementById('mailSmtpServer').value = '';
        document.getElementById('mailSmtpPort').value = '';
        document.getElementById('mailAppPassword').value = '';
        if (status) status.innerHTML = `<span style="color:var(--success,#16a34a)">Cleared — using .env now.</span>`;
        msLoadMailSettings();
    } catch (e) {
        if (status) status.innerHTML = `<span style="color:var(--danger,red)">${e.message}</span>`;
    }
}

// ── Extend showSection for Folder Browser + Control Panel ──────────────────
async function wfLoadApprovalSettings() {
    try {
        const res = await fetch('/api/admin/workflow-settings');
        const data = await res.json();
        if (!res.ok) throw new Error(data.error || 'Failed to load');
        const input = document.getElementById('wfMinApprovalsInput');
        if (input) input.value = data.min_approvals;
    } catch (e) {
        console.error('[Workflow] load approval settings failed', e);
    }
}

async function wfSaveApprovalSettings() {
    const input = document.getElementById('wfMinApprovalsInput');
    const status = document.getElementById('wfApprovalSettingsStatus');
    const minApprovals = parseInt(input?.value, 10);
    if (!minApprovals || minApprovals < 1) {
        if (status) status.innerHTML = `<span style="color:var(--danger,red)">${currentLang === 'ar' ? 'أدخل رقماً صحيحاً 1 أو أكثر' : 'Enter a whole number of 1 or more'}</span>`;
        return;
    }
    try {
        const res = await fetch('/api/admin/workflow-settings', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ min_approvals: minApprovals }),
        });
        const data = await res.json();
        if (!res.ok || !data.success) throw new Error(data.error || 'Failed to save');
        if (status) status.innerHTML = `<span style="color:var(--success,#16a34a)">${currentLang === 'ar' ? 'تم الحفظ' : 'Saved'}</span>`;
    } catch (e) {
        if (status) status.innerHTML = `<span style="color:var(--danger,red)">${e.message}</span>`;
    }
}

const _origShowSection = window.showSection;
window.showSection = function(name) {
    if (typeof _origShowSection === 'function') _origShowSection(name);
    if (name === 'control' && _isAdmin && typeof cpRefreshUsers === 'function' && _cpAllUsers.length === 0) cpRefreshUsers();
    if (name === 'control' && _isAdmin && typeof wfLoadApprovalSettings === 'function') wfLoadApprovalSettings();
    if (name === 'folders') renderFolderBrowser();
};

// ══════════════════════════════════════════════════════════════════════════════
// ACCESS RIGHTS ENFORCEMENT
// 0 = allowed, 1 = denied, NULL = not applicable
// ══════════════════════════════════════════════════════════════════════════════

window._myAccr = {}; // { page_id: {can_open, can_edit, can_del, can_print, can_add, can_prew, can_qr, can_approve} }

function _allowed(pageId, field) {
    const p = window._myAccr[pageId];
    if (!p) return true;             // no row = allowed
    const v = p[field];
    if (v === null || v === undefined) return true; // NULL = not applicable = allowed
    return v === 0;                  // 0 = allowed, 1 = denied
}

async function loadMyAccr() {
    try {
        const res  = await fetch('/api/my/accr');
        const rows = await res.json();
        if (Array.isArray(rows)) rows.forEach(r => { window._myAccr[r.page_id] = r; });
    } catch(e) { console.warn('Could not load access rights:', e); }
    applyAccr();
}

function applyAccr() {
    // ── Nav items: always visible, access checked on click via showSection ──

    // ── Inquiries (page 1) modal buttons ──────────────────────────────────
    // Can_Prew — hide preview pane
    const previewPane = document.getElementById('viewDocPreview');
    if (previewPane) previewPane.style.display = _allowed(1,'can_prew') ? '' : 'none';

    const printBtn = document.querySelector('.view-doc-print-btn');
    if (printBtn) printBtn.style.display = _allowed(1,'can_print') ? '' : 'none';

    const editBtn = document.getElementById('viewDocEditBtn');
    if (editBtn) editBtn.style.display = _allowed(1,'can_edit') ? '' : 'none';

    const qrBtn = document.getElementById('viewDocQrBtn');
    if (qrBtn) qrBtn.style.display = _allowed(1,'can_qr') ? '' : 'none';

    const delConfirmBtn = document.querySelector('.btn-danger-confirm');
    if (delConfirmBtn) delConfirmBtn.style.display = _allowed(1,'can_del') ? '' : 'none';

    // Delete buttons in search results table
    document.querySelectorAll('.sr-btn-delete').forEach(btn => {
        btn.style.display = _allowed(1,'can_del') ? '' : 'none';
    });

    // View buttons + row click in results — block if can_open denied
    document.querySelectorAll('.sr-btn-view, .tbl-row').forEach(el => {
        if (!_allowed(1,'can_open')) {
            el.style.pointerEvents = 'none';
            el.style.opacity = '0.4';
        } else {
            el.style.pointerEvents = '';
            el.style.opacity = '';
        }
    });

    // ── Archive (page 2) ──────────────────────────────────────────────────
    const scanSaveLabel = document.getElementById('scanSaveBtnLabel');
    if (scanSaveLabel) {
        const btn = scanSaveLabel.closest('button');
        if (btn) btn.style.display = _allowed(2,'can_add') ? '' : 'none';
    }
    document.querySelectorAll('[onclick="saveDocument()"]').forEach(btn => {
        btn.style.display = _allowed(2,'can_add') ? '' : 'none';
    });

    // ── Folder Browser (page 3) ───────────────────────────────────────────
    const createFolderBtn = document.querySelector('.create-folder-btn');
    if (createFolderBtn) createFolderBtn.style.display = _allowed(3,'can_open') ? '' : 'none';

    // ── Workflow (page 4) ──────────────────────────────────────────────────
    const wfSendBtn = document.getElementById('wfTopSendBtn');
    if (wfSendBtn && !_allowed(4,'can_add')) wfSendBtn.style.display = 'none';
}

// Wrap viewTransaction to re-apply accr after modal renders
const _realViewTransaction = viewTransaction;
window.viewTransaction = async function(docId) {
    await _realViewTransaction(docId);
    applyAccr();
};

// Wrap confirmDeleteTransaction to enforce Can_Del
const _realConfirmDelete = confirmDeleteTransaction;
window.confirmDeleteTransaction = function(...args) {
    if (!_allowed(1, 'can_del')) {
        const msg = (typeof currentLang !== 'undefined' && currentLang === 'ar')
            ? 'غير مصرح لك بحذف هذه المعاملة'
            : 'You are not authorized to delete this transaction';
        showToast(msg, 'error');
        return;
    }
    _realConfirmDelete(...args);
};

// Re-apply after section switches and search results render
const _accrOrigShowSection = window.showSection;
window.showSection = function(name) {
    if (_accrOrigShowSection) _accrOrigShowSection(name);
    setTimeout(applyAccr, 200);
};

// Re-apply after search results are rendered (results container mutation)
const _accrObserver = new MutationObserver(() => setTimeout(applyAccr, 50));
document.addEventListener('DOMContentLoaded', () => {
    const targets = ['#resultsTableBody', '#resultsGrid', '.results-container', '#searchResults'];
    targets.forEach(sel => {
        const el = document.querySelector(sel);
        if (el) _accrObserver.observe(el, { childList: true, subtree: true });
    });

    // Load permissions then redirect to first accessible page
    loadMyAccr().then(() => {
        // Priority order of pages to try
        const PAGE_ORDER = [
            { section: 'archive',   pageId: 2, field: 'can_open' },
            { section: 'inquiries', pageId: 1, field: 'can_open' },
            { section: 'folders',   pageId: 3, field: 'can_open' },
        ];

        // Find first accessible page
        const first = PAGE_ORDER.find(p => _allowed(p.pageId, p.field));

        if (first) {
            // Navigate to the first accessible page without triggering the guard
            document.querySelectorAll('.page-section').forEach(s => s.classList.remove('active'));
            const sec = document.getElementById('section-' + first.section);
            if (sec) sec.classList.add('active');
            document.querySelectorAll('.nav-item').forEach(b => {
                b.classList.toggle('active', b.getAttribute('data-section') === first.section);
            });
            if (first.section === 'inquiries') { syncInquiryDateFields(); renderSearch(); loadStats(); }
        } else {
            // No pages accessible at all — show full-screen block
            document.querySelectorAll('.page-section').forEach(s => s.classList.remove('active'));
            document.querySelectorAll('.nav-item').forEach(b => b.classList.remove('active'));

            const isAr = typeof currentLang !== 'undefined' && currentLang === 'ar';
            const blocker = document.createElement('div');
            blocker.id = 'fullAccessBlock';
            blocker.style.cssText = `
                position:fixed;inset:0;z-index:9999;
                display:flex;flex-direction:column;align-items:center;justify-content:center;
                background:var(--bg,#f5f7fa);gap:1rem;text-align:center;padding:2rem;
            `;
            blocker.innerHTML = `
                <i class="ph ph-prohibit" style="font-size:4rem;color:var(--danger,#dc2626)"></i>
                <h2 style="font-size:1.4rem;font-weight:700;color:var(--text,#111);margin:0">
                    ${isAr ? 'غير مصرح' : 'Unauthorized'}
                </h2>
                <p style="color:var(--text-muted,#666);max-width:380px;margin:0;font-size:.95rem">
                    ${isAr
                        ? 'ليس لديك صلاحية الوصول إلى أي صفحة في هذا النظام. يرجى التواصل مع المسؤول.'
                        : 'You are not authorized to access any page in this system. Please contact your administrator.'}
                </p>
            `;
            document.body.appendChild(blocker);
        }
    });
});
// ══════════════════════════════════════════════════════════════════════
// SETTINGS PAGE
// ══════════════════════════════════════════════════════════════════════

const STG_STORAGE_KEY_THEME  = 'docportal_user_theme';
const STG_STORAGE_KEY_LABELS = 'docportal_user_labels';
const STG_STORAGE_KEY_PREFS  = 'docportal_user_prefs';

const STG_DEFAULT_LABELS = {
    'registrationDate':      { en: 'Registration Date',   ar: 'تاريخ التسجيل' },
    'hijriDateDisplay':      { en: 'Hijri Date',           ar: 'التاريخ الهجري' },
    'registrationNumber':    { en: 'Registration Number', ar: 'رقم التسجيل' },
    'documentDate':          { en: 'Document Date',        ar: 'التاريخ' },
    'entitySelect':          { en: 'Entity / Department',  ar: 'الجهة / الإدارة' },
    'volumeInput':           { en: 'Volume (Folder)',      ar: 'المجلد' },
    'documentNumber':        { en: 'Doc Number',           ar: 'الرقم' },
    'topicInput':            { en: 'Topic / Subject',      ar: 'الموضوع' },
    'keywordsInput':         { en: 'Keywords',             ar: 'الكلمات المفتاحية' },
    'statementInput':        { en: 'Statement / Notes',    ar: 'البيان' },
    'importanceSelect':      { en: 'Importance',           ar: 'درجة الأهمية' },
    'confidentialitySelect': { en: 'Confidentiality',      ar: 'مستوى السرية' },
    'shelfNumber':           { en: 'Shelf Number',         ar: 'رقم الرف' },
    'expiryDate':            { en: 'Expiry Date',          ar: 'تاريخ الانتهاء' },
    'archiveType':           { en: 'Archive Type',         ar: 'نوع الأرشفة' },
    'documentType':          { en: 'Doc Type ID',          ar: 'نوع المستند' },
};

/* ── Tab switching ── */
function stgSwitchTab(tab) {
    ['theme', 'labels', 'prefs', 'announce'].forEach(t => {
        const key  = t.charAt(0).toUpperCase() + t.slice(1);
        const pane = document.getElementById('stgPane' + key);
        const btn  = document.getElementById('stgTab'  + key);
        if (pane) pane.style.display = t === tab ? '' : 'none';
        if (btn)  btn.classList.toggle('stg-tab--active', t === tab);
    });
    if (tab === 'labels') {
        stgRenderLabelsGrid();
        const overlay = document.getElementById('stgLabelsLockOverlay');
        if (overlay) overlay.style.display = _isAdmin ? 'none' : 'flex';
    }
    if (tab === 'prefs')    { stgLoadPrefsUI(); stgRenderLangButtons(); }
    if (tab === 'announce') {
        const overlay = document.getElementById('stgAnnounceLockOverlay');
        if (overlay) overlay.style.display = _isAdmin ? 'none' : 'flex';
        stgAnnounceLoad();
    }
}

/* ── DEFAULT LANGUAGE ── */
function stgSetDefaultLang(lang) {
    localStorage.setItem('lang', lang);
    setLang(lang);
    stgRenderLangButtons();
}

function stgRenderLangButtons() {
    const lang = localStorage.getItem('lang') || 'en';
    const enBtn   = document.getElementById('stgLangEn');
    const arBtn   = document.getElementById('stgLangAr');
    const enCheck = document.getElementById('stgLangEnCheck');
    const arCheck = document.getElementById('stgLangArCheck');
    if (!enBtn) return;
    const activeStyle = `2px solid var(--primary,#1e6fc4)`;
    const inactiveStyle = `2px solid var(--border,#e2e8f0)`;
    enBtn.style.border = lang === 'en' ? activeStyle : inactiveStyle;
    arBtn.style.border = lang === 'ar' ? activeStyle : inactiveStyle;
    if (enCheck) enCheck.style.display = lang === 'en' ? 'inline' : 'none';
    if (arCheck) arCheck.style.display = lang === 'ar' ? 'inline' : 'none';
}

/* ── ANNOUNCEMENT ── */
const _ANNOUNCE_LABEL_KEY = 'sys_announcement';
const _ANNOUNCE_COLORS = {
    yellow: { bg:'#fffbeb', border:'#fde68a', text:'#92400e' },
    blue:   { bg:'#eff6ff', border:'#bfdbfe', text:'#1e40af' },
    red:    { bg:'#fff1f2', border:'#fecaca', text:'#991b1b' },
    green:  { bg:'#f0fdf4', border:'#bbf7d0', text:'#14532d' },
};
let _announceColor = 'yellow';

function stgAnnounceSetColor(color) {
    _announceColor = color;
    document.querySelectorAll('.announce-color-btn').forEach(btn => {
        btn.style.border = btn.getAttribute('data-color') === color
            ? '2px solid var(--primary,#1e6fc4)'
            : '2px solid transparent';
    });
}

function stgAnnounceLoad() {
    fetch('/api/labels')
        .then(r => r.json())
        .then(data => {
            const raw = data[_ANNOUNCE_LABEL_KEY];
            if (!raw) return;
            const d = typeof raw === 'string' ? JSON.parse(raw) : raw;
            const ta = document.getElementById('stgAnnounceText');
            if (ta) ta.value = d.text || '';
            if (d.color) { _announceColor = d.color; stgAnnounceSetColor(d.color); }
            stgAnnounceRender(d.text, d.color);
        })
        .catch(() => {});
}

function stgAnnounceRender(text, color) {
    const banner = document.getElementById('announcementBanner');
    const span   = document.getElementById('announcementText');
    if (!banner || !span) return;
    if (!text || !text.trim()) { banner.style.display = 'none'; return; }
    const c = _ANNOUNCE_COLORS[color] || _ANNOUNCE_COLORS.yellow;
    banner.style.cssText += `;display:flex;background:${c.bg};border-color:${c.border};color:${c.text}`;
    span.textContent = text;
}

function stgAnnounceSave() {
    if (!_isAdmin) return;
    const text = (document.getElementById('stgAnnounceText') || {}).value || '';
    const payload = { text: text.trim(), color: _announceColor };
    fetch('/api/labels')
        .then(r => r.json())
        .then(existing => {
            const merged = Object.assign({}, existing, { [_ANNOUNCE_LABEL_KEY]: JSON.stringify(payload) });
            return fetch('/api/labels', { method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify(merged) });
        })
        .then(r => r.ok ? r.json() : Promise.reject())
        .then(() => {
            stgAnnounceRender(payload.text, payload.color);
            const st = document.getElementById('stgAnnounceStatus');
            if (st) { st.textContent = '✓ Published'; st.style.color = 'var(--success,#16a34a)'; setTimeout(() => st.textContent = '', 2500); }
        })
        .catch(() => alert('Failed to publish. Please try again.'));
}

function stgAnnounceClear() {
    if (!_isAdmin) return;
    const ta = document.getElementById('stgAnnounceText');
    if (ta) ta.value = '';
    stgAnnounceSave();
}

/* ── THEME ── */
function stgApplyTheme(themeName) {
    document.getElementById('html-root').setAttribute('data-theme', themeName);
    document.querySelectorAll('.stg-theme-card').forEach(c => {
        c.classList.toggle('stg-theme-card--active', c.getAttribute('data-theme') === themeName);
    });
    try { localStorage.setItem(STG_STORAGE_KEY_THEME, themeName); } catch(e) {}
}

function stgLoadTheme() {
    let saved = 'default';
    try { saved = localStorage.getItem(STG_STORAGE_KEY_THEME) || 'default'; } catch(e) {}
    stgApplyTheme(saved);
}

/* ── LABELS (global — admin-only write, server-persisted) ── */

// In-memory cache of labels loaded from server
let _stgServerLabels = {};

/**
 * Load global labels from the server and cache them.
 * Called once on boot and again after a successful save.
 */
async function stgFetchLabels() {
    try {
        const res  = await fetch('/api/labels');
        const data = await res.json();
        if (res.ok && typeof data === 'object' && !data.error) {
            _stgServerLabels = data;
        }
    } catch(e) {
        console.warn('Could not load global labels:', e);
    }
}

function stgGetSavedLabels() {
    return _stgServerLabels;
}

function stgRenderLabelsGrid() {
    const grid = document.getElementById('stgLabelsGrid');
    if (!grid) return;
    const saved = _stgServerLabels;
    const lang  = (typeof currentLang !== 'undefined' ? currentLang : 'en');
    const isAdmin = _isAdmin;
    grid.innerHTML = Object.entries(STG_DEFAULT_LABELS).map(([id, def]) => {
        const cur = (saved[id] && saved[id][lang]) ? saved[id][lang] : def[lang];
        return `<div class="stg-label-row">
            <label>${def.en}</label>
            <input type="text" data-field-id="${id}" data-lang="${lang}"
                value="${cur.replace(/"/g,'&quot;')}"
                placeholder="${def[lang].replace(/"/g,'&quot;')}"
                ${isAdmin ? '' : 'disabled style="opacity:.6;cursor:not-allowed"'}>
            <span class="stg-label-default">Default: ${def[lang]}</span>
        </div>`;
    }).join('');

    // Show/hide lock overlay
    const overlay = document.getElementById('stgLabelsLockOverlay');
    if (overlay) overlay.style.display = isAdmin ? 'none' : 'flex';
}

async function stgSaveLabels() {
    if (!_isAdmin) {
        showPermissionDenied(
            (typeof currentLang !== 'undefined' && currentLang === 'ar') ? 'غير مصرح' : 'Unauthorized',
            (typeof currentLang !== 'undefined' && currentLang === 'ar')
                ? 'تعديل تسميات الحقول متاح للمسؤول فقط.'
                : 'Only administrators can change field labels.'
        );
        return;
    }
    const grid  = document.getElementById('stgLabelsGrid');
    if (!grid) return;
    const lang  = (typeof currentLang !== 'undefined' ? currentLang : 'en');

    // Build updated labels object from grid inputs
    const updated = JSON.parse(JSON.stringify(_stgServerLabels)); // deep copy
    grid.querySelectorAll('input[data-field-id]').forEach(inp => {
        const id  = inp.getAttribute('data-field-id');
        const val = inp.value.trim();
        if (!updated[id]) updated[id] = {};
        updated[id][lang] = val || STG_DEFAULT_LABELS[id]?.[lang] || '';
        // Preserve the other language if already stored
        const otherLang = lang === 'en' ? 'ar' : 'en';
        if (!updated[id][otherLang]) {
            updated[id][otherLang] = (_stgServerLabels[id] && _stgServerLabels[id][otherLang])
                ? _stgServerLabels[id][otherLang]
                : STG_DEFAULT_LABELS[id]?.[otherLang] || '';
        }
    });

    const status = document.getElementById('stgLabelsStatus');
    try {
        const res  = await fetch('/api/labels', {
            method:  'POST',
            headers: { 'Content-Type': 'application/json' },
            body:    JSON.stringify(updated),
        });
        const data = await res.json();
        if (res.ok && data.success) {
            _stgServerLabels = updated;
            stgApplyLabelsToDOM();
            if (status) {
                status.textContent = lang === 'ar' ? '✓ تم الحفظ لجميع المستخدمين' : '✓ Saved — applies to all users';
                status.style.color = 'var(--success, #16a34a)';
                setTimeout(() => { if (status) status.textContent = ''; }, 3000);
            }
        } else {
            if (status) {
                status.textContent = '✗ ' + (data.error || 'Save failed');
                status.style.color = 'var(--danger, #dc2626)';
                setTimeout(() => { if (status) status.textContent = ''; }, 3000);
            }
        }
    } catch(e) {
        if (status) {
            status.textContent = lang === 'ar' ? '✗ خطأ في الاتصال' : '✗ Network error';
            status.style.color = 'var(--danger, #dc2626)';
            setTimeout(() => { if (status) status.textContent = ''; }, 3000);
        }
    }
}

async function stgResetLabels() {
    if (!_isAdmin) {
        showPermissionDenied(
            (typeof currentLang !== 'undefined' && currentLang === 'ar') ? 'غير مصرح' : 'Unauthorized',
            (typeof currentLang !== 'undefined' && currentLang === 'ar')
                ? 'تعديل تسميات الحقول متاح للمسؤول فقط.'
                : 'Only administrators can reset field labels.'
        );
        return;
    }
    const lang = (typeof currentLang !== 'undefined' ? currentLang : 'en');
    // POST empty object = revert to defaults on the server
    try {
        const res  = await fetch('/api/labels', {
            method:  'POST',
            headers: { 'Content-Type': 'application/json' },
            body:    JSON.stringify({}),
        });
        const data = await res.json();
        if (res.ok && data.success) {
            _stgServerLabels = {};
            stgRenderLabelsGrid();
            stgApplyLabelsToDOM();
            const status = document.getElementById('stgLabelsStatus');
            if (status) {
                status.textContent = lang === 'ar' ? '✓ أُعيد الافتراضي لجميع المستخدمين' : '✓ Reset to defaults — applies to all users';
                status.style.color = 'var(--muted)';
                setTimeout(() => { if (status) status.textContent = ''; }, 3000);
            }
        }
    } catch(e) { /* silent */ }
}

function stgApplyLabelsToDOM() {
    const saved = _stgServerLabels;
    const lang  = (typeof currentLang !== 'undefined' ? currentLang : 'en');

    // ── 1. Document form field labels ────────────────────────────────────────
    Object.entries(STG_DEFAULT_LABELS).forEach(([id, def]) => {
        const field = document.getElementById(id);
        if (!field) return;
        const group = field.closest('.field-group');
        if (!group) return;
        const lbl = group.querySelector('.field-label');
        if (!lbl) return;
        const customEn = (saved[id] && saved[id].en) ? saved[id].en : def.en;
        const customAr = (saved[id] && saved[id].ar) ? saved[id].ar : def.ar;
        lbl.setAttribute('data-en', customEn);
        lbl.setAttribute('data-ar', customAr);
        const firstText = [...lbl.childNodes].find(n => n.nodeType === Node.TEXT_NODE);
        if (firstText) firstText.textContent = (lang === 'ar' ? customAr : customEn) + ' ';
        else lbl.textContent = lang === 'ar' ? customAr : customEn;
    });

    // ── 2. Search bar placeholders ───────────────────────────────────────────
    // Map each label key → the corresponding search-bar input id, with a
    // short suffix so the placeholder reads well in a compact search box.
    const SEARCH_BAR_MAP = {
        'registrationNumber': { id: 'adv-reg-number',  suffixEn: ' (starts with…)', suffixAr: ' (يبدأ بـ…)' },
        'documentNumber':     { id: 'adv-doc-number',  suffixEn: '',                suffixAr: '' },
        'topicInput':         { id: 'adv-topic',       suffixEn: '',                suffixAr: '' },
        'keywordsInput':      { id: 'adv-keywords',    suffixEn: '',                suffixAr: '' },
        'statementInput':     { id: 'adv-statement',   suffixEn: '',                suffixAr: '' },
    };

    Object.entries(SEARCH_BAR_MAP).forEach(([labelKey, cfg]) => {
        const def = STG_DEFAULT_LABELS[labelKey];
        if (!def) return;
        const inp = document.getElementById(cfg.id);
        if (!inp) return;
        const customEn = (saved[labelKey] && saved[labelKey].en) ? saved[labelKey].en : def.en;
        const customAr = (saved[labelKey] && saved[labelKey].ar) ? saved[labelKey].ar : def.ar;
        const phEn = customEn + cfg.suffixEn;
        const phAr = customAr + cfg.suffixAr;
        // Update the data attributes so the language-toggle picks them up
        inp.setAttribute('data-en-placeholder', phEn);
        inp.setAttribute('data-ar-placeholder', phAr);
        // Update the live placeholder immediately
        inp.placeholder = lang === 'ar' ? phAr : phEn;
        // Also update the tooltip title to match
        if (cfg.id !== 'adv-reg-number') inp.title = lang === 'ar' ? customAr : customEn;
    });
}

/* ── PREFS ── */
function stgGetPrefs() {
    try { const r = localStorage.getItem(STG_STORAGE_KEY_PREFS); return r ? JSON.parse(r) : { notifications:true, autosave:true, compactView:false }; }
    catch(e) { return { notifications:true, autosave:true, compactView:false }; }
}

function stgLoadPrefsUI() {
    const prefs = stgGetPrefs();
    const n = document.getElementById('prefNotifications');
    const a = document.getElementById('prefAutosave');
    const c = document.getElementById('prefCompactView');
    if (n) n.checked = !!prefs.notifications;
    if (a) a.checked = !!prefs.autosave;
    if (c) c.checked = !!prefs.compactView;
    stgApplyPrefs(prefs);
    stgLoadWfExpiryAlertDays();
}

/* ── Workflow expiry alert days (server-backed, per user) ── */
async function stgLoadWfExpiryAlertDays() {
    const input = document.getElementById('prefWfAlertDays');
    if (!input) return;
    try {
        const res = await fetch('/api/settings/wf-expiry-alert');
        const data = await res.json();
        input.value = (data && data.alert_days !== null && data.alert_days !== undefined) ? data.alert_days : '';
    } catch (e) {
        console.error('[Settings] failed to load workflow expiry alert preference', e);
    }
}

async function stgSaveWfExpiryAlertDays() {
    const input = document.getElementById('prefWfAlertDays');
    const status = document.getElementById('prefWfAlertDaysStatus');
    if (!input) return;
    const raw = input.value.trim();
    const alertDays = raw === '' ? null : parseInt(raw, 10);
    if (raw !== '' && (isNaN(alertDays) || alertDays < 0 || alertDays > 365)) {
        if (status) {
            status.textContent = currentLang === 'ar' ? 'أدخل رقماً بين 0 و365' : 'Enter a number between 0 and 365';
            status.style.color = '#dc2626';
        }
        return;
    }
    try {
        const res = await fetch('/api/settings/wf-expiry-alert', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ alert_days: alertDays }),
        });
        const data = await res.json();
        if (!res.ok || !data.success) {
            if (status) {
                status.textContent = data.error || (currentLang === 'ar' ? 'فشل الحفظ' : 'Save failed');
                status.style.color = '#dc2626';
            }
            return;
        }
        if (status) {
            status.textContent = currentLang === 'ar' ? '✓ تم الحفظ' : '✓ Saved';
            status.style.color = '#16a34a';
            setTimeout(() => { if (status.textContent.includes('✓')) status.textContent = ''; }, 2500);
        }
    } catch (e) {
        console.error('[Settings] failed to save workflow expiry alert preference', e);
        if (status) {
            status.textContent = currentLang === 'ar' ? 'خطأ في الاتصال' : 'Connection error';
            status.style.color = '#dc2626';
        }
    }
}

function stgSavePref(key, value) {
    const prefs = stgGetPrefs();
    prefs[key] = value;
    try { localStorage.setItem(STG_STORAGE_KEY_PREFS, JSON.stringify(prefs)); } catch(e) {}
    stgApplyPrefs(prefs);
}

function stgApplyPrefs(prefs) {
    document.documentElement.classList.toggle('pref-compact', !!prefs.compactView);
}

/* ── Boot ── */
(function stgBoot() {
    stgLoadTheme();
    // Fetch global labels from server then apply to DOM for all users
    stgFetchLabels().then(() => {
        stgApplyLabelsToDOM();
        stgAnnounceLoad();   // render announcement banner for all users on load
    });
    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', () => { stgLoadPrefsUI(); stgRenderLangButtons(); });
    } else {
        stgLoadPrefsUI();
        stgRenderLangButtons();
    }
})();

// ══════════════════════════════════════════════════════════════════════════════
// USER GUIDE — Tab switcher + styles
// ══════════════════════════════════════════════════════════════════════════════

function guideSwitchTab(name) {
    const panes = { scanner:'guidePaneScanner', archive:'guidePaneArchive', inquiries:'guidePaneInquiries', folders:'guidePaneFolders', contact:'guidePaneContact' };
    const tabs  = { scanner:'guideTabScanner',  archive:'guideTabArchive',  inquiries:'guideTabInquiries',  folders:'guideTabFolders',  contact:'guideTabContact'  };
    Object.keys(panes).forEach(k => {
        const pane = document.getElementById(panes[k]);
        const tab  = document.getElementById(tabs[k]);
        if (pane) pane.style.display = (k === name) ? 'block' : 'none';
        if (tab)  tab.classList.toggle('guide-tab--active', k === name);
    });
    if (name === 'contact') guideFetchContacts();
}

// Inject guide CSS once
(function injectGuideStyles() {
    if (document.getElementById('guide-injected-styles')) return;
    const css = `
/* ── Guide: tab bar ── */
.guide-tab-bar {
    display: flex;
    gap: 4px;
    border-bottom: 2px solid var(--border, #e5e7eb);
    margin-bottom: 20px;
}
.guide-tab {
    display: flex;
    align-items: center;
    gap: 6px;
    padding: 9px 18px;
    font-size: 13.5px;
    font-weight: 500;
    color: var(--text-2, #6b7280);
    background: none;
    border: none;
    border-bottom: 2px solid transparent;
    margin-bottom: -2px;
    cursor: pointer;
    border-radius: 6px 6px 0 0;
    transition: color .15s, border-color .15s, background .15s;
}
.guide-tab:hover { color: var(--primary, #1d4ed8); background: var(--bg-subtle, #f3f4f6); }
.guide-tab--active { color: var(--primary, #1d4ed8); border-bottom-color: var(--primary, #1d4ed8); font-weight: 600; }

/* ── Guide: pane ── */
.guide-pane { animation: guideFadeIn .18s ease; }
@keyframes guideFadeIn { from { opacity: 0; transform: translateY(4px); } to { opacity: 1; transform: none; } }

/* ── Guide: intro banner ── */
.guide-intro-banner {
    display: flex;
    align-items: flex-start;
    gap: 14px;
    background: var(--blue-subtle, #eff6ff);
    border: 1px solid var(--blue-border, #bfdbfe);
    border-radius: 10px;
    padding: 14px 18px;
    margin-bottom: 22px;
    color: var(--text, #111827);
}
.guide-intro-banner > i { font-size: 22px; color: var(--primary, #1d4ed8); margin-top: 1px; flex-shrink: 0; }
.guide-intro-banner strong { display: block; font-size: 14px; font-weight: 600; margin-bottom: 3px; }
.guide-intro-banner span  { font-size: 13px; color: var(--text-2, #6b7280); line-height: 1.6; }

/* ── Guide: section label ── */
.guide-section-label {
    font-size: 12px;
    font-weight: 700;
    text-transform: uppercase;
    letter-spacing: .06em;
    color: var(--text-muted, #9ca3af);
    margin: 22px 0 10px;
}

/* ── Guide: steps list ── */
.guide-steps-list { display: flex; flex-direction: column; gap: 8px; margin-bottom: 4px; }
.guide-step-row {
    display: flex;
    align-items: flex-start;
    gap: 12px;
    font-size: 13.5px;
    color: var(--text, #111827);
    line-height: 1.6;
}
.guide-step-num {
    flex-shrink: 0;
    width: 22px; height: 22px;
    background: var(--primary, #1d4ed8);
    color: #fff;
    border-radius: 50%;
    display: flex; align-items: center; justify-content: center;
    font-size: 11px; font-weight: 700;
    margin-top: 2px;
}

/* ── Guide: tip row ── */
.guide-tip-row { display: flex; flex-direction: column; gap: 7px; margin: 14px 0 4px; }
.guide-tip {
    display: flex;
    align-items: flex-start;
    gap: 8px;
    padding: 9px 13px;
    border-radius: 8px;
    font-size: 12.5px;
    line-height: 1.6;
    color: var(--text, #111827);
}
.guide-tip > i { font-size: 15px; margin-top: 1px; flex-shrink: 0; }
.guide-tip--info { background: var(--blue-subtle, #eff6ff); border: 1px solid var(--blue-border, #bfdbfe); }
.guide-tip--info > i { color: var(--primary, #1d4ed8); }
.guide-tip--warn { background: #fffbeb; border: 1px solid #fde68a; }
.guide-tip--warn > i { color: #d97706; }

/* ── Guide: progress strip ── */
.guide-progress-strip {
    display: flex;
    align-items: center;
    gap: 6px;
    flex-wrap: wrap;
    margin-bottom: 16px;
}
.guide-progress-step {
    display: flex; align-items: center; gap: 7px;
    background: var(--bg-subtle, #f3f4f6);
    border: 1px solid var(--border, #e5e7eb);
    border-radius: 20px;
    padding: 5px 13px;
    font-size: 12.5px; font-weight: 500;
    color: var(--text, #111827);
}
.guide-progress-arrow { color: var(--text-muted, #9ca3af); font-size: 16px; }

/* ── Guide: contact pane ── */
.guide-contact-header {
    display: flex;
    align-items: center;
    justify-content: space-between;
    margin-top: 4px;
}
.guide-contact-edit-btn {
    display: flex;
    align-items: center;
    gap: 6px;
    background: var(--primary, #1d4ed8);
    color: #fff;
    border: none;
    border-radius: 7px;
    padding: 7px 14px;
    font-size: 12.5px;
    font-weight: 600;
    cursor: pointer;
    transition: opacity .15s;
}
.guide-contact-edit-btn:hover { opacity: .88; }
.guide-contact-grid {
    display: grid;
    grid-template-columns: repeat(auto-fit, minmax(260px, 1fr));
    gap: 16px;
    margin-top: 12px;
}
.guide-contact-card {
    border: 1px solid var(--border, #e5e7eb);
    border-radius: 12px;
    padding: 16px 18px;
    background: var(--bg-card, #fff);
    display: flex;
    flex-direction: column;
    gap: 10px;
}
.guide-contact-card-badge {
    display: flex;
    align-items: center;
    gap: 7px;
    font-size: 12.5px;
    font-weight: 700;
    text-transform: uppercase;
    letter-spacing: .04em;
    padding-bottom: 8px;
    border-bottom: 1px solid var(--border, #e5e7eb);
}
.guide-contact-card-badge small {
    font-weight: 500;
    text-transform: none;
    letter-spacing: 0;
    font-size: 11px;
    color: var(--text-muted, #9ca3af);
    margin-left: 2px;
}
.guide-contact-card-badge--primary { color: var(--primary, #1d4ed8); }
.guide-contact-card-badge--primary i { color: var(--primary, #1d4ed8); }
.guide-contact-card-badge--secondary { color: var(--text-2, #6b7280); }
.guide-contact-card-badge--secondary i { color: var(--text-2, #6b7280); }
.guide-contact-field {
    display: flex;
    align-items: center;
    gap: 10px;
    font-size: 13.5px;
    color: var(--text, #111827);
}
.guide-contact-field > i { font-size: 16px; color: var(--text-muted, #9ca3af); flex-shrink: 0; width: 18px; text-align: center; }
.guide-contact-field a { color: var(--primary, #1d4ed8); text-decoration: none; }
.guide-contact-field a:hover { text-decoration: underline; }
.guide-contact-field span:empty::before,
.guide-contact-field .gc-empty { color: var(--text-muted, #9ca3af); font-style: italic; }
`;
    const el = document.createElement('style');
    el.id = 'guide-injected-styles';
    el.textContent = css;
    document.head.appendChild(el);
})();

/* ── Guide: Support Contacts pane (admin-only write, server-persisted) ── */

let _guideContactsCache = null;
let _guideContactsLoaded = false;

function _gcEscape(v) {
    return (v || '').toString().replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;');
}

function _gcEmptyLabel() {
    return currentLang === 'ar' ? 'غير محدد' : 'Not set';
}

function guideRenderContacts(data) {
    const c = data || { primary: {}, secondary: {} };
    const map = [
        ['guideContactPrimaryName',   c.primary?.name,   'text'],
        ['guideContactPrimaryRole',   c.primary?.role,   'text'],
        ['guideContactPrimaryEmail',  c.primary?.email,  'email'],
        ['guideContactPrimaryNumber', c.primary?.number, 'tel'],
        ['guideContactSecondaryName',   c.secondary?.name,   'text'],
        ['guideContactSecondaryRole',   c.secondary?.role,   'text'],
        ['guideContactSecondaryEmail',  c.secondary?.email,  'email'],
        ['guideContactSecondaryNumber', c.secondary?.number, 'tel'],
    ];
    map.forEach(([id, val, kind]) => {
        const el = document.getElementById(id);
        if (!el) return;
        const hasVal = val && String(val).trim();
        if (kind === 'email' && el.tagName === 'A') {
            if (hasVal) { el.href = 'mailto:' + val; el.textContent = val; el.classList.remove('gc-empty'); }
            else { el.removeAttribute('href'); el.textContent = _gcEmptyLabel(); el.classList.add('gc-empty'); }
        } else if (kind === 'tel' && el.tagName === 'A') {
            if (hasVal) { el.href = 'tel:' + String(val).replace(/[^+\d]/g, ''); el.textContent = val; el.classList.remove('gc-empty'); }
            else { el.removeAttribute('href'); el.textContent = _gcEmptyLabel(); el.classList.add('gc-empty'); }
        } else {
            el.textContent = hasVal ? val : _gcEmptyLabel();
            el.classList.toggle('gc-empty', !hasVal);
        }
    });
    const note = document.getElementById('guideContactViewOnlyNote');
    if (note) note.style.display = _isAdmin ? 'none' : 'flex';
}

async function guideFetchContacts(force) {
    if (_guideContactsLoaded && !force) { guideRenderContacts(_guideContactsCache); return; }
    try {
        const res  = await fetch('/api/guide-contacts');
        const data = await res.json();
        if (res.ok && data && !data.error) {
            _guideContactsCache = data;
            _guideContactsLoaded = true;
        }
    } catch (e) {
        console.warn('Could not load support contacts:', e);
    }
    guideRenderContacts(_guideContactsCache);
}

function openGuideContactEdit() {
    if (!_isAdmin) {
        showPermissionDenied(
            currentLang === 'ar' ? 'غير مصرح' : 'Unauthorized',
            currentLang === 'ar' ? 'تعديل جهات الاتصال متاح للمسؤول فقط.' : 'Only administrators can edit support contacts.'
        );
        return;
    }
    const c = _guideContactsCache || { primary: {}, secondary: {} };
    const set = (id, val) => { const el = document.getElementById(id); if (el) el.value = val || ''; };
    set('gcPrimaryName',     c.primary?.name);
    set('gcPrimaryRole',     c.primary?.role);
    set('gcPrimaryEmail',    c.primary?.email);
    set('gcPrimaryNumber',   c.primary?.number);
    set('gcSecondaryName',   c.secondary?.name);
    set('gcSecondaryRole',   c.secondary?.role);
    set('gcSecondaryEmail',  c.secondary?.email);
    set('gcSecondaryNumber', c.secondary?.number);
    const status = document.getElementById('gcStatus');
    if (status) status.textContent = '';
    const modal = document.getElementById('guideContactModal');
    if (modal) modal.style.display = 'flex';
}

function closeGuideContactEdit() {
    const modal = document.getElementById('guideContactModal');
    if (modal) modal.style.display = 'none';
}

async function guideSaveContacts() {
    if (!_isAdmin) {
        showPermissionDenied(
            currentLang === 'ar' ? 'غير مصرح' : 'Unauthorized',
            currentLang === 'ar' ? 'تعديل جهات الاتصال متاح للمسؤول فقط.' : 'Only administrators can edit support contacts.'
        );
        return;
    }
    const get = id => (document.getElementById(id)?.value || '').trim();
    const payload = {
        primary: {
            name:   get('gcPrimaryName'),
            role:   get('gcPrimaryRole'),
            email:  get('gcPrimaryEmail'),
            number: get('gcPrimaryNumber'),
        },
        secondary: {
            name:   get('gcSecondaryName'),
            role:   get('gcSecondaryRole'),
            email:  get('gcSecondaryEmail'),
            number: get('gcSecondaryNumber'),
        },
    };
    const status = document.getElementById('gcStatus');
    if (status) { status.textContent = currentLang === 'ar' ? 'جارٍ الحفظ…' : 'Saving…'; status.style.color = 'var(--text-muted)'; }
    try {
        const res  = await fetch('/api/guide-contacts', {
            method:  'POST',
            headers: { 'Content-Type': 'application/json' },
            body:    JSON.stringify(payload),
        });
        const data = await res.json();
        if (res.ok && data.success) {
            _guideContactsCache = data.contacts || payload;
            _guideContactsLoaded = true;
            guideRenderContacts(_guideContactsCache);
            if (status) { status.textContent = currentLang === 'ar' ? '✓ تم الحفظ لجميع المستخدمين' : '✓ Saved — visible to all users'; status.style.color = 'var(--success, #16a34a)'; }
            if (typeof showToast === 'function') showToast(currentLang === 'ar' ? 'تم حفظ جهات الاتصال' : 'Contacts saved', 'success');
            setTimeout(closeGuideContactEdit, 700);
        } else {
            if (status) { status.textContent = data.error || (currentLang === 'ar' ? 'فشل الحفظ' : 'Save failed'); status.style.color = 'var(--danger, #dc2626)'; }
        }
    } catch (e) {
        if (status) { status.textContent = currentLang === 'ar' ? 'تعذر الاتصال بالخادم' : 'Could not reach the server'; status.style.color = 'var(--danger, #dc2626)'; }
    }
}

// ══════════════════════════════════════════════════════════════════════════════
// REPORTS
// ══════════════════════════════════════════════════════════════════════════════

let _reportsState = {
    loaded: false,
    loading: false,
    data: null,
    charts: {},
    // Per-tab filter state (client-side slicing of loaded data)
    auditPage: 1,
    auditPageSize: 50,
};

// ── Helpers ───────────────────────────────────────────────────────────────────

function _reportDateValue(id) {
    return (document.getElementById(id)?.value || '').trim();
}

function _fmtBytes(bytes) {
    let n = Number(bytes || 0);
    const units = ['B', 'KB', 'MB', 'GB', 'TB'];
    let i = 0;
    while (n >= 1024 && i < units.length - 1) { n /= 1024; i++; }
    return i === 0 ? `${Math.round(n)} ${units[i]}` : `${n.toFixed(1)} ${units[i]}`;
}

function _fmtDate(value) {
    if (!value) return '—';
    const d = new Date(value);
    return Number.isNaN(d.getTime()) ? value : d.toISOString().slice(0, 10);
}

function _fmtDateTime(value) {
    if (!value) return '—';
    const d = new Date(value);
    if (Number.isNaN(d.getTime())) return value;
    return d.toISOString().slice(0, 16).replace('T', ' ');
}

function _setTableBody(id, html) {
    const el = document.getElementById(id);
    if (el) el.innerHTML = html;
}

function _destroyChart(key) {
    if (_reportsState.charts[key]) {
        _reportsState.charts[key].destroy();
        delete _reportsState.charts[key];
    }
}

function _drawChart(key, canvasId, config) {
    const canvas = document.getElementById(canvasId);
    if (!canvas || typeof Chart === 'undefined') return;
    _destroyChart(key);
    const ctx = canvas.getContext('2d');
    const baseOptions = {
        responsive: true,
        maintainAspectRatio: false,
        plugins: {
            legend: { display: false },
            tooltip: { mode: 'index', intersect: false },
        },
    };
    config.options = Object.assign({}, baseOptions, config.options || {});
    config.options.plugins = Object.assign({}, baseOptions.plugins, config.options.plugins || {});
    _reportsState.charts[key] = new Chart(ctx, config);
}

// ── Pill/badge helpers ────────────────────────────────────────────────────────

function _rptStatusPill(status) {
    const s = (status || '').toLowerCase();
    if (s.includes('archived') || s.includes('مؤرشف'))
        return `<span class="rpt-pill rpt-pill--archived">${status}</span>`;
    if (s.includes('pending') || s.includes('معلق'))
        return `<span class="rpt-pill rpt-pill--pending">${status}</span>`;
    if (s.includes('urgent') || s.includes('عاجل'))
        return `<span class="rpt-pill rpt-pill--urgent">${status}</span>`;
    return `<span class="rpt-pill rpt-pill--normal">${status || '—'}</span>`;
}

function _rptActionBadge(action) {
    const a = (action || '').toLowerCase();
    if (a.includes('add') || a.includes('create') || a.includes('upload'))
        return `<span class="rpt-badge rpt-badge--add">${action}</span>`;
    if (a.includes('edit') || a.includes('update') || a.includes('modify'))
        return `<span class="rpt-badge rpt-badge--edit">${action}</span>`;
    if (a.includes('delete') || a.includes('remove'))
        return `<span class="rpt-badge rpt-badge--delete">${action}</span>`;
    if (a.includes('login') || a.includes('logout'))
        return `<span class="rpt-badge rpt-badge--login">${action}</span>`;
    return `<span class="rpt-badge rpt-badge--default">${action || '—'}</span>`;
}

// ── Dept filter ───────────────────────────────────────────────────────────────

function rptPopulateDeptFilter() {
    const sel = document.getElementById('rptDeptFilter');
    if (!sel) return;
    const current = sel.value;
    const entities = (typeof allEntities !== 'undefined' && Array.isArray(allEntities)) ? allEntities : [];
    const allowed = entities.filter(e => _canAccessDept(e.id));
    sel.innerHTML = '<option value="">All departments</option>' + allowed
        .map(e => `<option value="${e.id}">${escapeHtml(e.name || e.display_name || e.dept_name || `Dept ${e.id}`)}</option>`)
        .join('');
    if (current) sel.value = current;
}

// ── Build global query params ─────────────────────────────────────────────────

function rptBuildQueryParams() {
    const params = new URLSearchParams();
    const from = _reportDateValue('rptDateFrom');
    const to   = _reportDateValue('rptDateTo');
    const dept = (document.getElementById('rptDeptFilter')?.value || '').trim();
    if (from) params.set('date_from', from);
    if (to)   params.set('date_to',   to);
    if (dept) params.set('dept_id',   dept);
    return params;
}

// ── Reset filters ─────────────────────────────────────────────────────────────

function rptResetFilters() {
    ['rptDateFrom','rptDateTo'].forEach(id => {
        const el = document.getElementById(id);
        if (el) el.value = '';
    });
    const dept = document.getElementById('rptDeptFilter');
    if (dept) dept.value = '';
    rptLoadReports(true);
}

function rptResetAuditFilters() {
    ['rptAuditFrom','rptAuditTo'].forEach(id => {
        const el = document.getElementById(id);
        if (el) el.value = '';
    });
    ['rptAuditAction','rptAuditUser'].forEach(id => {
        const el = document.getElementById(id);
        if (el) el.value = '';
    });
    _reportsState.auditPage = 1;
    rptRenderAudit();
}

// ── Tab switching ─────────────────────────────────────────────────────────────

function rptSwitchTab(tab) {
    const panes = ['overview','volume','depts','workflow','activity','audit','logins'];
    panes.forEach(name => {
        const capName = name.charAt(0).toUpperCase() + name.slice(1);
        const pane = document.getElementById(`rptPane${capName}`);
        if (pane) pane.style.display = name === tab ? '' : 'none';
    });
    document.querySelectorAll('.rpt-tab').forEach(btn => {
        btn.classList.toggle('rpt-tab--active', btn.getAttribute('data-tab') === tab);
    });
    if (_reportsState.loaded) rptRenderTab(tab);
}

// ── Main load (always re-fetches when force=true) ─────────────────────────────

async function rptLoadReports(force = false) {
    if (_reportsState.loading) return;
    if (_reportsState.loaded && !force) {
        const tab = document.querySelector('.rpt-tab--active')?.getAttribute('data-tab') || 'overview';
        rptRenderTab(tab);
        return;
    }
    _reportsState.loading = true;
    try {
        rptPopulateDeptFilter();
        const params = rptBuildQueryParams();
        const qs = params.toString();
        const res = await fetch(`/api/reports${qs ? `?${qs}` : ''}`);
        const data = await res.json();
        if (!res.ok || data.error) {
            showToast(data.error || 'Could not load reports', 'error');
            return;
        }
        _reportsState.data = data;
        _reportsState.loaded = true;
        _reportsState.auditPage = 1;
        rptPopulateAuditFilters();
        rptRenderAll();
        const tab = document.querySelector('.rpt-tab--active')?.getAttribute('data-tab') || 'overview';
        rptRenderTab(tab);
    } catch (e) {
        showToast(currentLang === 'ar' ? 'تعذر تحميل التقارير' : 'Could not load reports', 'error');
    } finally {
        _reportsState.loading = false;
    }
}

// ── Per-tab filter apply (client-side re-render, no new fetch) ────────────────

/** Volume tab: re-slice monthly data by selected months */
function rptApplyVolume() {
    if (!_reportsState.loaded) return;
    rptRenderTab('volume');
}

/** Activity tab: re-slice activity data by selected days */
function rptApplyActivity() {
    if (!_reportsState.loaded) return;
    rptRenderTab('activity');
}

/** Audit tab: re-filter and re-paginate audit rows */
function rptApplyAudit() {
    if (!_reportsState.loaded) return;
    _reportsState.auditPage = 1;
    rptRenderAudit();
}

/** Logins tab: re-slice login data by selected days */
function rptApplyLogins() {
    if (!_reportsState.loaded) return;
    rptRenderTab('logins');
}

// ── Populate audit dropdowns with unique values from data ─────────────────────

function rptPopulateAuditFilters() {
    const data = _reportsState.data || {};
    const rows = data.audit || [];

    const actions = [...new Set(rows.map(r => r.action).filter(Boolean))].sort();
    const users   = [...new Set(rows.map(r => r.user).filter(Boolean))].sort();

    const actionSel = document.getElementById('rptAuditAction');
    if (actionSel) {
        const cur = actionSel.value;
        actionSel.innerHTML = '<option value="">All actions</option>' +
            actions.map(a => `<option value="${a}">${a}</option>`).join('');
        if (cur) actionSel.value = cur;
    }

    const userSel = document.getElementById('rptAuditUser');
    if (userSel) {
        const cur = userSel.value;
        userSel.innerHTML = '<option value="">All users</option>' +
            users.map(u => `<option value="${u}">${u}</option>`).join('');
        if (cur) userSel.value = cur;
    }
}

// ── Render helpers ────────────────────────────────────────────────────────────

function rptRenderAll() {
    const data = _reportsState.data || {};
    const kpi  = data.kpi || {};
    const set  = (id, v) => { const el = document.getElementById(id); if (el) el.textContent = (v !== null && v !== undefined) ? v : '—'; };
    set('kpiTotal',   kpi.total_docs ?? '—');
    set('kpiMonth',   kpi.this_month ?? '—');
    set('kpiPending', kpi.pending    ?? '—');

    // Recent transactions
    const recentRows = (data.recent || []).map(r => `
        <tr>
            <td>${r.id ?? '—'}</td>
            <td>${escapeHtml(r.subject) ?? '—'}</td>
            <td>${escapeHtml(r.dept) ?? '—'}</td>
            <td>${_fmtDate(r.date)}</td>
            <td style="text-align:center">${_rptStatusPill(r.status)}</td>
        </tr>`).join('');
    _setTableBody('rptRecentBody', recentRows ||
        '<tr><td colspan="5" class="rpt-table-empty">No recent transactions</td></tr>');

    // Departments table
    const deptRows = (data.dept_usage || []).map(r => `
        <tr>
            <td>${escapeHtml(r.name) ?? '—'}</td>
            <td style="text-align:center">${r.folders ?? 0}</td>
            <td style="text-align:center">${r.docs ?? 0}</td>
            <td style="text-align:right">${_fmtBytes(r.storage_bytes)}</td>
        </tr>`).join('');
    _setTableBody('rptDeptBody', deptRows ||
        '<tr><td colspan="4" class="rpt-table-empty">No data</td></tr>');

    rptRenderAudit();
    rptRenderActivity();
    rptRenderLogins();
}

/** Audit tab — client-side filter + paginate */
function rptRenderAudit() {
    const data = _reportsState.data || {};
    const filterAction = (document.getElementById('rptAuditAction')?.value || '').trim();
    const filterUser   = (document.getElementById('rptAuditUser')?.value   || '').trim();
    const filterFrom   = _reportDateValue('rptAuditFrom');
    const filterTo     = _reportDateValue('rptAuditTo');

    let rows = (data.audit || []).filter(r => {
        if (filterAction && r.action !== filterAction) return false;
        if (filterUser   && r.user   !== filterUser)   return false;
        if (filterFrom   && r.time   && r.time.slice(0,10) < filterFrom) return false;
        if (filterTo     && r.time   && r.time.slice(0,10) > filterTo)   return false;
        return true;
    });

    const total = rows.length;
    const ps    = _reportsState.auditPageSize;
    const page  = _reportsState.auditPage;
    const pages = Math.max(1, Math.ceil(total / ps));

    // Clamp page
    if (page > pages) _reportsState.auditPage = pages;
    const sliced = rows.slice((page - 1) * ps, page * ps);

    const count = document.getElementById('rptAuditCount');
    if (count) count.textContent = total > 0 ? `${total} rows` : '';

    const html = sliced.map(r => `
        <tr>
            <td>${_fmtDateTime(r.time)}</td>
            <td>${escapeHtml(r.user) || '—'}</td>
            <td>${_rptActionBadge(r.action)}</td>
            <td style="max-width:260px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis" title="${escAttr(r.notes || '')}">${escapeHtml(r.notes) || '—'}</td>
            <td><code style="font-size:11px">${escapeHtml(r.ip) || '—'}</code></td>
        </tr>`).join('');
    _setTableBody('rptAuditBody', html ||
        '<tr><td colspan="5" class="rpt-table-empty">No audit records match your filters</td></tr>');

    // Pagination
    const pager = document.getElementById('rptAuditPager');
    if (pager) {
        if (pages <= 1) { pager.innerHTML = ''; return; }
        const cur = _reportsState.auditPage;
        let btns = '';
        // Prev
        btns += `<button class="rpt-page-btn" onclick="rptAuditGoPage(${cur-1})" ${cur === 1 ? 'disabled' : ''}>‹ Prev</button>`;
        // Page numbers (show window around current)
        const win = 2;
        for (let p = 1; p <= pages; p++) {
            if (p === 1 || p === pages || (p >= cur - win && p <= cur + win)) {
                btns += `<button class="rpt-page-btn ${p === cur ? 'rpt-page-btn--active' : ''}" onclick="rptAuditGoPage(${p})">${p}</button>`;
            } else if (p === cur - win - 1 || p === cur + win + 1) {
                btns += `<span style="padding:0 4px;color:var(--muted)">…</span>`;
            }
        }
        // Next
        btns += `<button class="rpt-page-btn" onclick="rptAuditGoPage(${cur+1})" ${cur === pages ? 'disabled' : ''}>Next ›</button>`;
        pager.innerHTML = btns;
    }
}

function rptAuditGoPage(p) {
    const data = _reportsState.data || {};
    const total = (data.audit || []).length;
    const pages = Math.max(1, Math.ceil(total / _reportsState.auditPageSize));
    _reportsState.auditPage = Math.min(pages, Math.max(1, p));
    rptRenderAudit();
}

/** Activity tab — client-side filter by days */
function rptRenderActivity() {
    const data = _reportsState.data || {};
    const days = parseInt(document.getElementById('rptActDays')?.value || '30', 10);
    const cutoff = new Date();
    cutoff.setDate(cutoff.getDate() - days);
    const cutStr = cutoff.toISOString().slice(0, 10);

    // The server returns pre-aggregated data per user; filter by last_active if days selected
    let rows = (data.activity || []).filter(r => {
        if (!r.last_active) return true;
        return r.last_active.slice(0, 10) >= cutStr;
    });

    const html = rows.map(r => `
        <tr>
            <td>${escapeHtml(r.user) ?? '—'}</td>
            <td style="text-align:center"><strong>${r.total ?? 0}</strong></td>
            <td style="text-align:center">${r.added ?? 0}</td>
            <td style="text-align:center">${r.edited ?? 0}</td>
            <td style="text-align:center">${r.deleted ?? 0}</td>
            <td style="text-align:center">${r.downloaded ?? 0}</td>
            <td>${_fmtDate(r.last_active)}</td>
        </tr>`).join('');
    _setTableBody('rptActivityBody', html ||
        '<tr><td colspan="7" class="rpt-table-empty">No activity data for this period</td></tr>');

    _drawChart('activity', 'rptChartActivity', {
        type: 'bar',
        data: {
            labels: rows.map(r => r.user),
            datasets: [{ label: 'Actions', data: rows.map(r => r.total), backgroundColor: '#1e6fc4' }]
        },
        options: { indexAxis: 'y', scales: { x: { beginAtZero: true, ticks: { precision: 0 } }, y: { grid: { display: false } } } }
    });
}

/** Logins tab — client-side filter by days */
function rptRenderLogins() {
    const data = _reportsState.data || {};
    const days = parseInt(document.getElementById('rptLoginDays')?.value || '30', 10);
    const cutoff = new Date();
    cutoff.setDate(cutoff.getDate() - days);
    const cutStr = cutoff.toISOString().slice(0, 10);

    let rows = (data.logins || []).filter(r => {
        if (!r.last_active) return true;
        return r.last_active.slice(0, 10) >= cutStr;
    });

    const html = rows.map(r => `
        <tr>
            <td>${escapeHtml(r.user) ?? '—'}</td>
            <td>${_fmtDate(r.last_active)}</td>
            <td style="text-align:center">${r.logins ?? 0}</td>
            <td style="text-align:center">${r.failed > 0 ? `<span style="color:var(--danger);font-weight:600">${r.failed}</span>` : '0'}</td>
            <td><code style="font-size:11px">${escapeHtml(r.ip) || '—'}</code></td>
        </tr>`).join('');
    _setTableBody('rptLoginBody', html ||
        '<tr><td colspan="5" class="rpt-table-empty">No login data for this period</td></tr>');

    _drawChart('logins', 'rptChartLogins', {
        type: 'bar',
        data: {
            labels: rows.map(r => r.user),
            datasets: [
                { label: 'Logins', data: rows.map(r => r.logins ?? 0), backgroundColor: '#16a34a' },
                { label: 'Failed', data: rows.map(r => r.failed ?? 0), backgroundColor: '#dc2626' },
            ]
        },
        options: { indexAxis: 'y', plugins: { legend: { display: true, position: 'top' } }, scales: { x: { beginAtZero: true, ticks: { precision: 0 } }, y: { grid: { display: false } } } }
    });
}

/** Workflow tab — KPIs, monthly trend, status breakdown, top approvers, pending list */
function rptRenderWorkflow() {
    const data = _reportsState.data || {};
    const wf = data.workflow || {};
    const kpi = wf.kpi || {};
    const set = (id, v) => { const el = document.getElementById(id); if (el) el.textContent = (v !== null && v !== undefined) ? v : '—'; };

    set('kpiWfPending',    kpi.pending ?? '—');
    set('kpiWfApproved',   kpi.approved ?? '—');
    set('kpiWfRejected',   kpi.rejected ?? '—');
    set('kpiWfOverdue',    kpi.overdue ?? '—');
    set('kpiWfTurnaround', kpi.avg_turnaround_days ?? '—');

    // Monthly submitted vs approved vs rejected
    const monthly = wf.monthly || [];
    const monthLabels = monthly.map(r => `${r.year}-${String(r.month).padStart(2,'0')}`);
    _drawChart('wfMonthly', 'rptChartWfMonthly', {
        type: 'bar',
        data: {
            labels: monthLabels,
            datasets: [
                { label: 'Submitted', data: monthly.map(r => r.submitted ?? 0), backgroundColor: '#1e6fc4' },
                { label: 'Approved',  data: monthly.map(r => r.approved  ?? 0), backgroundColor: '#16a34a' },
                { label: 'Rejected',  data: monthly.map(r => r.rejected  ?? 0), backgroundColor: '#dc2626' },
            ]
        },
        options: {
            plugins: { legend: { display: true, position: 'top' } },
            scales: { x: { grid: { display: false } }, y: { beginAtZero: true, ticks: { precision: 0 } } }
        }
    });

    // Current status breakdown (doughnut)
    const statusBreakdown = wf.status_breakdown || {};
    const statusLabels = Object.keys(statusBreakdown);
    _drawChart('wfStatus', 'rptChartWfStatus', {
        type: 'doughnut',
        data: {
            labels: statusLabels,
            datasets: [{
                data: statusLabels.map(k => statusBreakdown[k]),
                backgroundColor: ['#1e6fc4','#d97706','#16a34a','#dc2626','#64748b','#7c3aed','#0284c7','#0f766e']
            }]
        },
        options: { cutout: '68%', plugins: { legend: { display: true, position: 'bottom' } } }
    });

    // Top approvers
    const approvers = wf.top_approvers || [];
    _drawChart('wfApprovers', 'rptChartWfApprovers', {
        type: 'bar',
        data: {
            labels: approvers.map(r => r.user),
            datasets: [{ label: 'Approvals', data: approvers.map(r => r.count), backgroundColor: '#16a34a' }]
        },
        options: { indexAxis: 'y', scales: { x: { beginAtZero: true, ticks: { precision: 0 } }, y: { grid: { display: false } } } }
    });

    // Pending list table
    const pendingRows = (wf.pending_list || []).map(r => {
        const overdue = r.expiry_date && new Date(r.expiry_date) < new Date(new Date().toDateString());
        return `
        <tr>
            <td>${escapeHtml(r.subject) ?? '—'}</td>
            <td>${escapeHtml(r.submitted_by) ?? '—'}</td>
            <td>${_fmtDate(r.submitted_on)}</td>
            <td style="text-align:center">${r.days_waiting ?? 0}</td>
            <td>${r.expiry_date ? `<span style="${overdue ? 'color:var(--danger);font-weight:600' : ''}">${_fmtDate(r.expiry_date)}</span>` : '—'}</td>
            <td style="text-align:center">${_rptStatusPill(r.status)}</td>
        </tr>`;
    }).join('');
    _setTableBody('rptWfPendingBody', pendingRows ||
        '<tr><td colspan="6" class="rpt-table-empty">No pending workflow items</td></tr>');
}

function rptRenderTab(tab) {
    const data = _reportsState.data || {};

    if (tab === 'overview') {
        const monthLabels = (data.monthly || []).map(r => `${r.year}-${String(r.month).padStart(2,'0')}`);
        _drawChart('monthly', 'rptChartMonthly', {
            type: 'bar',
            data: { labels: monthLabels, datasets: [{ label: 'Documents', data: (data.monthly || []).map(r => r.count), backgroundColor: '#1e6fc4' }] },
            options: { scales: { x: { grid: { display: false } }, y: { beginAtZero: true, ticks: { precision: 0 } } } }
        });
        _drawChart('depts', 'rptChartDepts', {
            type: 'doughnut',
            data: {
                labels: (data.dept_breakdown || []).map(r => r.name),
                datasets: [{ data: (data.dept_breakdown || []).map(r => r.count), backgroundColor: ['#1e6fc4','#16a34a','#d97706','#dc2626','#0f766e','#7c3aed','#0284c7','#64748b'] }]
            },
            options: { cutout: '68%', plugins: { legend: { display: true, position: 'bottom' } } }
        });
    }

    if (tab === 'volume') {
        const months = parseInt(document.getElementById('rptVolMonths')?.value || '12', 10);
        const allMonthly = data.monthly || [];
        const sliced = allMonthly.slice(-months);
        const labels = sliced.map(r => `${r.year}-${String(r.month).padStart(2,'0')}`);
        _drawChart('volume', 'rptChartVolume', {
            type: 'bar',
            data: { labels, datasets: [{ label: 'Documents added', data: sliced.map(r => r.count), backgroundColor: '#1e6fc4' }] },
            options: { scales: { x: { grid: { display: false } }, y: { beginAtZero: true, ticks: { precision: 0 } } } }
        });
    }

    if (tab === 'depts') {
        _drawChart('deptFull', 'rptChartDeptFull', {
            type: 'bar',
            data: {
                labels: (data.dept_usage || []).map(r => r.name),
                datasets: [{ label: 'Documents', data: (data.dept_usage || []).map(r => r.docs), backgroundColor: '#1e6fc4' }]
            },
            options: { indexAxis: 'y', scales: { x: { beginAtZero: true, ticks: { precision: 0 } }, y: { grid: { display: false } } } }
        });
    }

    if (tab === 'workflow') rptRenderWorkflow();
    if (tab === 'activity') rptRenderActivity();
    if (tab === 'audit')    rptRenderAudit();
    if (tab === 'logins')   rptRenderLogins();
}

// ── Export audit ──────────────────────────────────────────────────────────────

function rptExportAudit() {
    const data = _reportsState.data || {};
    const rows = data.audit || [];
    if (!rows.length) { showToast('No audit data to export', 'warning'); return; }
    const csv = ['Time,User,Action,Notes,IP']
        .concat(rows.map(r =>
            [_fmtDateTime(r.time), r.user, r.action, (r.notes || '').replace(/,/g,'；'), r.ip]
            .map(v => `"${(v||'').replace(/"/g,'""')}"`)
            .join(',')
        ))
        .join('\n');
    const blob = new Blob(['\ufeff' + csv], { type: 'text/csv;charset=utf-8;' });
    const a = document.createElement('a');
    a.href = URL.createObjectURL(blob);
    a.download = `audit-log-${new Date().toISOString().slice(0,10)}.csv`;
    a.click();
}

// ── Hook showSection ──────────────────────────────────────────────────────────

const _origShowSectionReports = window.showSection;
window.showSection = function(name) {
    if (typeof _origShowSectionReports === 'function') _origShowSectionReports(name);
    if (name === 'reports') rptLoadReports();
};

document.addEventListener('DOMContentLoaded', () => {
    const reportsPane = document.getElementById('section-reports');
    if (reportsPane && reportsPane.classList.contains('active')) rptLoadReports();
});
// ── BASIC CHATBOT WIDGET ─────────────────────────────────────────────────
let _chatbotOpen = false;
let _chatbotHistoryLoaded = false;
let _chatbotLastQueryWords = [];

function toggleChatbot(forceOpen) {
    const panel = document.getElementById('chatbotPanel');
    if (!panel) return;
    _chatbotOpen = (typeof forceOpen === 'boolean') ? forceOpen : !_chatbotOpen;
    panel.style.display = _chatbotOpen ? 'flex' : 'none';

    if (_chatbotOpen && !_chatbotHistoryLoaded) {
        _chatbotHistoryLoaded = true;
        const list = document.getElementById('chatbotMessages');
        if (list) {
            const div = document.createElement('div');
            div.className = 'chatbot-msg chatbot-msg--bot';
            const enText = "Hi! I'm a simple assistant. Try 'find <keyword>' to search documents, 'open <page>' to jump to a page (e.g. 'open reports'), 'email all documents in <folder>' or 'download all attachments in <folder>' for bulk actions, or ask me things like 'how to scan', 'how to email', or type 'help' for more.";
            const arText = "مرحبًا! أنا مساعد بسيط. اكتب 'ابحث عن <كلمة>' للبحث في المستندات، أو 'افتح <صفحة>' للانتقال إلى صفحة (مثال: 'افتح التقارير')، أو 'أرسل كل المستندات في <مجلد>' أو 'نزّل كل المرفقات في <مجلد>' للإجراءات الجماعية، أو اسألني مثل 'كيف أمسح مستندًا' أو 'كيف أرسل بالبريد'، أو اكتب 'مساعدة' لمزيد من الخيارات.";
            div.setAttribute('data-en', enText);
            div.setAttribute('data-ar', arText);
            div.textContent = currentLang === 'ar' ? arText : enText;
            list.appendChild(div);
        }
    }
    if (_chatbotOpen) {
        const input = document.getElementById('chatbotInput');
        if (input) input.focus();
    }
}

function _chatbotAppendMessage(role, text) {
    const list = document.getElementById('chatbotMessages');
    if (!list) return;
    const div = document.createElement('div');
    div.className = 'chatbot-msg chatbot-msg--' + (role === 'user' ? 'user' : 'bot');
    div.textContent = text;
    list.appendChild(div);
    list.scrollTop = list.scrollHeight;
}

// Renders clickable quick-reply buttons (e.g. Yes/No, Attachment/Link) under
// the latest bot message. Clicking one sends its value like the user typed it.
function _chatbotAppendQuickReplies(quickReplies) {
    const list = document.getElementById('chatbotMessages');
    if (!list || !quickReplies || !quickReplies.length) return;
    const row = document.createElement('div');
    row.className = 'chatbot-quick-replies';
    quickReplies.forEach(qr => {
        const btn = document.createElement('button');
        btn.type = 'button';
        btn.className = 'chatbot-quick-reply-btn';
        btn.textContent = qr.label;
        btn.onclick = () => {
            // Once any option is picked, remove the whole button row so it
            // can't be clicked twice (and so old prompts don't linger).
            row.remove();
            _chatbotSubmitMessage(qr.value, qr.label);
        };
        row.appendChild(btn);
    });
    list.appendChild(row);
    list.scrollTop = list.scrollHeight;
}

// Shows an animated "bot is typing" bubble; returns a function that removes it.
function _chatbotShowTyping() {
    const list = document.getElementById('chatbotMessages');
    const avatar = document.querySelector('.chatbot-header-icon');
    if (avatar) avatar.classList.add('cb-thinking');
    if (!list) return () => { if (avatar) avatar.classList.remove('cb-thinking'); };
    const div = document.createElement('div');
    div.className = 'chatbot-msg chatbot-msg--typing';
    div.innerHTML = '<span class="cb-dot"></span><span class="cb-dot"></span><span class="cb-dot"></span>';
    list.appendChild(div);
    list.scrollTop = list.scrollHeight;
    return () => { div.remove(); if (avatar) avatar.classList.remove('cb-thinking'); };
}

// Escapes then re-highlights any word from the user's last query that
// appears in a result snippet, so the match is visually obvious.
function _cbHighlightSnippet(snippetHtml) {
    if (!_chatbotLastQueryWords.length) return snippetHtml;
    let out = snippetHtml;
    _chatbotLastQueryWords.forEach(word => {
        if (!word || word.length < 3) return;
        const escaped = word.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
        const re = new RegExp('(' + escaped + ')', 'ig');
        out = out.replace(re, '<mark>$1</mark>');
    });
    return out;
}

// Returns a Tabler icon class based on a result's file type/subject, so the
// result card reads at a glance instead of every row looking the same.
function _cbResultIconClass(r) {
    const hint = ((r.file_type || '') + ' ' + (r.subject || '')).toLowerCase();
    if (hint.includes('pdf')) return 'ti-file-type-pdf';
    if (hint.includes('invoice') || hint.includes('فاتورة')) return 'ti-file-invoice';
    if (hint.includes('image') || hint.includes('jpg') || hint.includes('png')) return 'ti-photo';
    if (hint.includes('excel') || hint.includes('xls')) return 'ti-file-spreadsheet';
    return 'ti-file-text';
}

function _chatbotAppendResults(results) {
    const list = document.getElementById('chatbotMessages');
    if (!list || !results) return;
    const noFolderLabel = currentLang === 'ar' ? 'لا يوجد مجلد' : 'No folder';
    results.forEach(r => {
        const div = document.createElement('div');
        div.className = 'chatbot-msg--result';
        const rawSnippet = r.content_snippet
            ? `… ${escapeHtml(r.content_snippet)} …`
            : '';
        const snippetHtml = rawSnippet
            ? `<div class="cb-result-snippet">${_cbHighlightSnippet(rawSnippet)}</div>`
            : '';
        div.innerHTML = `<div class="cb-result-icon"><i class="ti ${_cbResultIconClass(r)}" aria-hidden="true"></i></div>
                          <div class="cb-result-body">
                              <div class="cb-result-title">#${r.id} — ${escapeHtml(r.subject || (currentLang === 'ar' ? '(بدون موضوع)' : '(no subject)'))}</div>
                              <div class="cb-result-sub">${escapeHtml(r.folder_name || noFolderLabel)} · ${escapeHtml(r.date || '')}</div>
                              ${snippetHtml}
                          </div>`;
        div.onclick = () => {
            toggleChatbot(false);
            if (typeof viewTransaction === 'function') viewTransaction(r.id);
        };
        list.appendChild(div);
    });
    if (results.length) _chatbotAppendActionChips(results[0].id);
    list.scrollTop = list.scrollHeight;
}

// Quick-action chips shown after a result so the user doesn't have to
// retype a follow-up. Adjust the message strings below if your backend's
// chatbot parser expects different phrasing for these commands.
function _chatbotAppendActionChips(resultId) {
    const list = document.getElementById('chatbotMessages');
    if (!list) return;
    const row = document.createElement('div');
    row.className = 'chatbot-action-chips';
    const chips = [
        {
            icon: 'ti-file-text',
            label: currentLang === 'ar' ? 'لخص مرة أخرى' : 'Summarize again',
            value: `summarize ${resultId}`
        },
        {
            icon: 'ti-mail',
            label: currentLang === 'ar' ? 'أرسل هذا بالبريد' : 'Email this',
            value: `email document ${resultId}`
        }
    ];
    chips.forEach(chip => {
        const btn = document.createElement('button');
        btn.type = 'button';
        btn.className = 'chatbot-action-chip';
        btn.innerHTML = `<i class="ti ${chip.icon}" aria-hidden="true" style="font-size:13px"></i>${escapeHtml(chip.label)}`;
        btn.onclick = () => {
            row.remove();
            _chatbotSubmitMessage(chip.value, chip.label);
        };
        row.appendChild(btn);
    });
    list.appendChild(row);
    list.scrollTop = list.scrollHeight;
}

// ── Voice input (Web Speech API) ─────────────────────────────────────────
// No backend involved: browser does speech-to-text and we drop the transcript
// into the existing chatbot input. Silently no-ops on unsupported browsers.
let _chatbotRecognition = null;
let _chatbotListening = false;

function _initChatbotVoice() {
    const SpeechRecognitionCtor = window.SpeechRecognition || window.webkitSpeechRecognition;
    const micBtn = document.getElementById('chatbotMicBtn');
    if (!SpeechRecognitionCtor || !micBtn) return;

    micBtn.style.display = 'flex';

    _chatbotRecognition = new SpeechRecognitionCtor();
    _chatbotRecognition.continuous = false;
    _chatbotRecognition.interimResults = true;
    _chatbotRecognition.maxAlternatives = 1;

    _chatbotRecognition.onresult = (event) => {
        const input = document.getElementById('chatbotInput');
        if (!input) return;
        let transcript = '';
        for (let i = 0; i < event.results.length; i++) {
            transcript += event.results[i][0].transcript;
        }
        input.value = transcript;
    };

    _chatbotRecognition.onerror = (event) => {
        _setChatbotListening(false);
        if (event.error === 'not-allowed' || event.error === 'service-not-allowed') {
            _chatbotAppendMessage('bot', currentLang === 'ar'
                ? 'الرجاء السماح بالوصول إلى الميكروفون لاستخدام الإدخال الصوتي.'
                : 'Please allow microphone access to use voice input.');
        }
    };

    _chatbotRecognition.onend = () => {
        _setChatbotListening(false);
        const input = document.getElementById('chatbotInput');
        // Auto-send if we captured something and the user didn't manually stop empty.
        if (input && input.value.trim()) {
            chatbotSend();
        }
    };
}

function _setChatbotListening(isListening) {
    _chatbotListening = isListening;
    const micBtn = document.getElementById('chatbotMicBtn');
    if (micBtn) micBtn.classList.toggle('listening', isListening);
}

function toggleChatbotVoice() {
    if (!_chatbotRecognition) return;
    if (_chatbotListening) {
        _chatbotRecognition.stop();
        return;
    }
    _chatbotRecognition.lang = (currentLang === 'ar') ? 'ar-SA' : 'en-US';
    try {
        _chatbotRecognition.start();
        _setChatbotListening(true);
    } catch (e) {
        // start() throws if already started; ignore.
    }
}

document.addEventListener('DOMContentLoaded', _initChatbotVoice);

async function chatbotSend(evt) {
    if (evt) evt.preventDefault();
    const input = document.getElementById('chatbotInput');
    if (!input) return false;
    const message = input.value.trim();
    if (!message) return false;
    input.value = '';
    await _chatbotSubmitMessage(message);
    return false;
}

// Core send routine shared by the text input (chatbotSend) and by clicking
// a quick-reply button. `displayText` lets a button show its short label
// (e.g. "Yes, delete") as the user bubble instead of the raw value sent to
// the backend (e.g. "yes") when the two differ.
async function _chatbotSubmitMessage(message, displayText) {
    _chatbotAppendMessage('user', displayText || message);
    _chatbotLastQueryWords = message.split(/\s+/).filter(w => w.length >= 3);

    const hideTyping = _chatbotShowTyping();

    try {
        const res = await fetch('/api/chatbot', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ message, lang: currentLang })
        });
        const data = await res.json();
        hideTyping();
        if (!res.ok || data.error) {
            _chatbotAppendMessage('bot', data.error || (currentLang === 'ar' ? 'حدث خطأ ما. حاول مرة أخرى.' : 'Something went wrong. Please try again.'));
            return;
        }
        _chatbotAppendMessage('bot', data.reply || '…');
        if (data.results && data.results.length) _chatbotAppendResults(data.results);
        if (data.quick_replies && data.quick_replies.length) _chatbotAppendQuickReplies(data.quick_replies);
        if (data.action === 'open_section' && data.section) {
            toggleChatbot(false);
            if (typeof showSection === 'function') showSection(data.section);
        }
        if (data.action === 'download_zip' && data.url) {
            // Trigger the browser's normal download flow — a same-tab
            // navigation is enough since the response is Content-Disposition:
            // attachment, so it won't replace the dashboard page.
            const a = document.createElement('a');
            a.href = data.url;
            a.rel = 'noopener';
            document.body.appendChild(a);
            a.click();
            a.remove();
        }
        if (data.action === 'document_deleted' && typeof _afterDocumentDeleted === 'function') {
            _afterDocumentDeleted(data.id, data.registration_number);
        }
    } catch (e) {
        hideTyping();
        _chatbotAppendMessage('bot', currentLang === 'ar' ? 'خطأ في الشبكة. حاول مرة أخرى.' : 'Network error. Please try again.');
    }
}
// Opens a workflow instance's history/timeline detail — shared by the
// email deep-link handler below and by clicking a WF_* notification in
// the bell dropdown, so both paths land in the same place.
function wfOpenInstanceDeepLink(instanceId, attempts = 0) {
    if (typeof showSection !== 'function' || typeof switchWfTab !== 'function' || typeof wfViewTimeline !== 'function') {
        if (attempts > 20) return;
        setTimeout(() => wfOpenInstanceDeepLink(instanceId, attempts + 1), 250);
        return;
    }
    showSection('workflow');
    switchWfTab('history');
    wfViewTimeline(instanceId);
}

// ── WORKFLOW EMAIL DEEP LINK ─────────────────────────────────────────────
// Notification emails link to /dashboard?wf_open=<instanceId>. On load, if
// that query param is present, jump straight to the Workflow section and
// open the instance's history/timeline detail so the link is actually
// useful instead of dropping the user on a generic dashboard.
document.addEventListener('DOMContentLoaded', () => {
    try {
        const params = new URLSearchParams(window.location.search);
        const wfOpenId = params.get('wf_open');
        if (!wfOpenId) return;

        wfOpenInstanceDeepLink(parseInt(wfOpenId, 10));

        // Clean the query string so a page refresh doesn't keep reopening it.
        const cleanUrl = window.location.pathname;
        window.history.replaceState({}, document.title, cleanUrl);
    } catch (e) {
        console.error('[Workflow] deep link handling failed', e);
    }
});
// ─────────────────────────────────────────────────────────────────────
// Signature pad — draw or type a signature, stamp it onto a PDF attachment.
// Entry point: openSignatureModal(attachmentId, { onSigned: (result) => {} })
// ─────────────────────────────────────────────────────────────────────
(function () {
    let _sigCanvas, _sigCtx, _sigDrawing = false, _sigHasStroke = false;
    let _sigCurrentAttachmentId = null;
    let _sigPendingFileIndex = null;
    let _sigOnSignedCallback = null;

    // ── Manual drag-to-place state ──────────────────────────────────────
    let _sigPlacementActive = false;
    let _sigPlacePct = null; // {x, y} top-left fraction (0..1) of the page, once dragged
    let _sigPlacePdfDoc = null; // cached pdf.js document for the currently open preview
    let _sigPlacePdfKey = null; // identifies which source _sigPlacePdfDoc belongs to
    let _sigThumbDragging = false, _sigThumbOffset = { x: 0, y: 0 };

    function _sigBuildModalOnce() {
        if (document.getElementById('signatureModal')) return;

        const wrap = document.createElement('div');
        wrap.id = 'signatureModal';
        wrap.className = 'sig-modal-overlay';
        wrap.style.display = 'none';
        wrap.innerHTML = `
            <div class="sig-modal">
                <div class="sig-modal-header">
                    <h3><i class="ph ph-pen-nib"></i> <span>${typeof currentLang !== 'undefined' && currentLang === 'ar' ? 'توقيع المستند' : 'Sign Document'}</span></h3>
                    <button type="button" class="sig-modal-close" onclick="closeSignatureModal()">
                        <i class="ph ph-x"></i>
                    </button>
                </div>

                <div class="sig-tabs">
                    <button type="button" class="sig-tab active" data-mode="draw" onclick="_sigSetMode('draw')">Draw</button>
                    <button type="button" class="sig-tab" data-mode="type" onclick="_sigSetMode('type')">Type</button>
                </div>

                <div class="sig-pad-area" id="sigDrawArea">
                    <canvas id="sigCanvas" width="500" height="180"></canvas>
                </div>

                <div class="sig-type-area" id="sigTypeArea" style="display:none;">
                    <input type="text" id="sigTypedName" placeholder="Type your name" maxlength="60" />
                    <div id="sigTypedPreview" class="sig-typed-preview"></div>
                </div>

                <div class="sig-options">
                    <label>
                        Position:
                        <select id="sigPosition">
                            <option value="bottom-right" selected>Bottom right</option>
                            <option value="bottom-left">Bottom left</option>
                            <option value="top-right">Top right</option>
                            <option value="top-left">Top left</option>
                        </select>
                    </label>
                    <label>
                        Page:
                        <select id="sigPage">
                            <option value="-1" selected>Last page</option>
                            <option value="1">First page</option>
                        </select>
                    </label>
                    <button type="button" class="sig-place-toggle" id="sigPlaceToggle" onclick="_sigTogglePlacement()">
                        <i class="ph ph-hand-pointing"></i> Place manually
                    </button>
                </div>

                <div class="sig-place-area" id="sigPlaceArea" style="display:none;">
                    <div class="sig-place-hint">Drag your signature anywhere on the document, then confirm.</div>
                    <div class="sig-place-canvas-wrap" id="sigPlaceCanvasWrap">
                        <canvas id="sigPageCanvas"></canvas>
                        <img id="sigDragThumb" alt="signature" draggable="false" />
                    </div>
                    <div class="sig-place-status" id="sigPlaceStatus"></div>
                </div>

                <div class="sig-modal-footer">
                    <button type="button" class="btn-secondary btn-sm" onclick="_sigClear()">
                        <i class="ph ph-eraser"></i> Clear
                    </button>
                    <div class="sig-modal-footer-right">
                        <button type="button" class="btn-secondary btn-sm" onclick="closeSignatureModal()">Cancel</button>
                        <button type="button" class="btn-primary btn-sm" id="sigConfirmBtn" onclick="_sigConfirm()">
                            <i class="ph ph-check"></i> Sign &amp; Stamp
                        </button>
                    </div>
                </div>
                <div id="sigErrorMsg" class="sig-error" style="display:none;"></div>
            </div>
        `;
        document.body.appendChild(wrap);

        _sigCanvas = document.getElementById('sigCanvas');
        _sigCtx = _sigCanvas.getContext('2d');
        _sigCtx.lineWidth = 2.5;
        _sigCtx.lineCap = 'round';
        _sigCtx.strokeStyle = '#1a1a2e';

        _sigBindCanvasEvents();
        document.getElementById('sigTypedName').addEventListener('input', _sigRenderTypedPreview);
        document.getElementById('sigPage').addEventListener('change', () => {
            if (_sigPlacementActive) {
                _sigPlacePct = null;
                _sigRenderPlacementPreview();
            }
        });
    }

    function _sigBindCanvasEvents() {
        const getPos = (e) => {
            const rect = _sigCanvas.getBoundingClientRect();
            const clientX = e.touches ? e.touches[0].clientX : e.clientX;
            const clientY = e.touches ? e.touches[0].clientY : e.clientY;
            return {
                x: (clientX - rect.left) * (_sigCanvas.width / rect.width),
                y: (clientY - rect.top) * (_sigCanvas.height / rect.height),
            };
        };
        const start = (e) => {
            e.preventDefault();
            _sigDrawing = true;
            const p = getPos(e);
            _sigCtx.beginPath();
            _sigCtx.moveTo(p.x, p.y);
        };
        const move = (e) => {
            if (!_sigDrawing) return;
            e.preventDefault();
            const p = getPos(e);
            _sigCtx.lineTo(p.x, p.y);
            _sigCtx.stroke();
            _sigHasStroke = true;
        };
        const end = () => { _sigDrawing = false; };

        _sigCanvas.addEventListener('mousedown', start);
        _sigCanvas.addEventListener('mousemove', move);
        window.addEventListener('mouseup', end);
        _sigCanvas.addEventListener('touchstart', start, { passive: false });
        _sigCanvas.addEventListener('touchmove', move, { passive: false });
        _sigCanvas.addEventListener('touchend', end);
    }

    function _sigRenderTypedPreview() {
        const name = document.getElementById('sigTypedName').value;
        document.getElementById('sigTypedPreview').textContent = name;
    }

    window._sigSetMode = function (mode) {
        document.querySelectorAll('.sig-tab').forEach(t =>
            t.classList.toggle('active', t.dataset.mode === mode));
        document.getElementById('sigDrawArea').style.display = mode === 'draw' ? 'block' : 'none';
        document.getElementById('sigTypeArea').style.display = mode === 'type' ? 'block' : 'none';
    };

    window._sigClear = function () {
        _sigCtx.clearRect(0, 0, _sigCanvas.width, _sigCanvas.height);
        _sigHasStroke = false;
        document.getElementById('sigTypedName').value = '';
        _sigRenderTypedPreview();
        _sigHideError();
        _sigExitPlacementMode();
    };

    function _sigHideError() {
        const el = document.getElementById('sigErrorMsg');
        el.style.display = 'none';
        el.textContent = '';
    }
    function _sigShowError(msg) {
        const el = document.getElementById('sigErrorMsg');
        el.textContent = msg;
        el.style.display = 'block';
    }

    function _sigGetPngDataUrl() {
        const drawMode = document.querySelector('.sig-tab.active').dataset.mode === 'draw';
        if (drawMode) {
            if (!_sigHasStroke) return null;
            return _sigCanvas.toDataURL('image/png');
        }
        const name = document.getElementById('sigTypedName').value.trim();
        if (!name) return null;
        const off = document.createElement('canvas');
        off.width = 500; off.height = 180;
        const octx = off.getContext('2d');
        octx.clearRect(0, 0, off.width, off.height);
        octx.fillStyle = '#1a1a2e';
        octx.font = "56px 'Brush Script MT', cursive";
        octx.textBaseline = 'middle';
        octx.fillText(name, 20, off.height / 2);
        return off.toDataURL('image/png');
    }

    function _sigExitPlacementMode() {
        _sigPlacementActive = false;
        _sigPlacePct = null;
        _sigPlacePdfDoc = null;
        _sigPlacePdfKey = null;
        const area = document.getElementById('sigPlaceArea');
        if (area) area.style.display = 'none';
        const toggle = document.getElementById('sigPlaceToggle');
        if (toggle) toggle.classList.remove('active');
        const status = document.getElementById('sigPlaceStatus');
        if (status) status.textContent = '';
    }

    function _sigPreviewSourceUrl() {
        if (_sigCurrentAttachmentId !== null) {
            return `/api/attachments/${_sigCurrentAttachmentId}/preview`;
        }
        if (_sigPendingFileIndex !== null) {
            const file = _archivePendingFiles[_sigPendingFileIndex];
            return file ? URL.createObjectURL(file) : null;
        }
        return null;
    }

    function _sigBindThumbDrag() {
        const thumb = document.getElementById('sigDragThumb');
        const wrap = document.getElementById('sigPlaceCanvasWrap');

        const clientPos = (e) => e.touches ? e.touches[0] : e;

        const start = (e) => {
            e.preventDefault();
            const p = clientPos(e);
            const thumbRect = thumb.getBoundingClientRect();
            _sigThumbDragging = true;
            _sigThumbOffset.x = p.clientX - thumbRect.left;
            _sigThumbOffset.y = p.clientY - thumbRect.top;
        };
        const move = (e) => {
            if (!_sigThumbDragging) return;
            e.preventDefault();
            const p = clientPos(e);
            const wrapRect = wrap.getBoundingClientRect();
            let left = p.clientX - wrapRect.left - _sigThumbOffset.x;
            let top = p.clientY - wrapRect.top - _sigThumbOffset.y;
            const maxLeft = wrap.clientWidth - thumb.offsetWidth;
            const maxTop = wrap.clientHeight - thumb.offsetHeight;
            left = Math.min(Math.max(left, 0), Math.max(maxLeft, 0));
            top = Math.min(Math.max(top, 0), Math.max(maxTop, 0));
            thumb.style.left = left + 'px';
            thumb.style.top = top + 'px';
            _sigPlacePct = {
                x: wrap.clientWidth ? left / wrap.clientWidth : 0,
                y: wrap.clientHeight ? top / wrap.clientHeight : 0,
            };
        };
        const end = () => { _sigThumbDragging = false; };

        thumb.addEventListener('mousedown', start);
        window.addEventListener('mousemove', move);
        window.addEventListener('mouseup', end);
        thumb.addEventListener('touchstart', start, { passive: false });
        thumb.addEventListener('touchmove', move, { passive: false });
        thumb.addEventListener('touchend', end);
        thumb._sigDragBound = true;
    }

    async function _sigRenderPlacementPreview() {
        const status = document.getElementById('sigPlaceStatus');
        if (typeof pdfjsLib === 'undefined') {
            status.textContent = 'Document preview library failed to load.';
            return;
        }
        const src = _sigPreviewSourceUrl();
        if (!src) {
            status.textContent = 'No document available to preview.';
            return;
        }
        const pageSel = parseInt(document.getElementById('sigPage').value, 10);
        const key = src + '|' + pageSel;
        try {
            status.textContent = 'Loading document…';
            if (_sigPlacePdfKey !== src) {
                _sigPlacePdfDoc = await pdfjsLib.getDocument(src).promise;
                _sigPlacePdfKey = src;
            }
            const pageNum = pageSel === -1 ? _sigPlacePdfDoc.numPages : Math.min(Math.max(pageSel, 1), _sigPlacePdfDoc.numPages);
            const page = await _sigPlacePdfDoc.getPage(pageNum);
            const targetWidth = 480;
            const baseViewport = page.getViewport({ scale: 1 });
            const scale = targetWidth / baseViewport.width;
            const viewport = page.getViewport({ scale });

            const canvas = document.getElementById('sigPageCanvas');
            canvas.width = viewport.width;
            canvas.height = viewport.height;
            const ctx = canvas.getContext('2d');
            await page.render({ canvasContext: ctx, viewport }).promise;

            const wrap = document.getElementById('sigPlaceCanvasWrap');
            wrap.style.width = viewport.width + 'px';
            wrap.style.height = viewport.height + 'px';

            // Signature box is fixed at 160x60pt on the server; mirror that
            // proportion here so the drag preview matches the real stamp.
            const thumb = document.getElementById('sigDragThumb');
            thumb.src = _sigGetPngDataUrl() || thumb.src;
            const pdfPointScale = viewport.width / (baseViewport.width); // px per pt at this scale (already 1:1 with pt since scale=1 base)
            thumb.style.width = (160 * scale) + 'px';
            thumb.style.height = (60 * scale) + 'px';

            if (!_sigPlacePct) {
                // Default to bottom-right, matching the preset default.
                _sigPlacePct = {
                    x: Math.max((viewport.width - 160 * scale - 20) / viewport.width, 0),
                    y: Math.max((viewport.height - 60 * scale - 20) / viewport.height, 0),
                };
            }
            thumb.style.left = (_sigPlacePct.x * viewport.width) + 'px';
            thumb.style.top = (_sigPlacePct.y * viewport.height) + 'px';

            if (!thumb._sigDragBound) _sigBindThumbDrag();
            status.textContent = '';
        } catch (e) {
            status.textContent = 'Could not load document preview: ' + e.message;
        }
    }

    window._sigTogglePlacement = function () {
        _sigHideError();
        if (_sigPlacementActive) {
            _sigExitPlacementMode();
            return;
        }
        const dataUrl = _sigGetPngDataUrl();
        if (!dataUrl) {
            _sigShowError('Draw or type your signature first, then place it manually.');
            return;
        }
        _sigPlacementActive = true;
        _sigPlacePct = null;
        document.getElementById('sigPlaceArea').style.display = 'block';
        document.getElementById('sigPlaceToggle').classList.add('active');
        _sigRenderPlacementPreview();
    };

    window.openSignatureModal = function (attachmentId, opts) {
        _sigBuildModalOnce();
        _sigCurrentAttachmentId = attachmentId;
        _sigPendingFileIndex = null;
        _sigOnSignedCallback = (opts && opts.onSigned) || null;
        window._sigClear();
        window._sigSetMode('draw');
        document.getElementById('signatureModal').style.display = 'flex';
    };

    // Same pad, but for a file that hasn't been uploaded yet (archiving step
    // 1/2). Stamps the PDF client-side with pdf-lib and swaps the stamped
    // File back into _archivePendingFiles — nothing hits the server until
    // the normal "Save"/archive submit happens.
    window.openSignatureModalForPendingFile = function (index) {
        _sigBuildModalOnce();
        _sigCurrentAttachmentId = null;
        _sigPendingFileIndex = index;
        _sigOnSignedCallback = null;
        window._sigClear();
        window._sigSetMode('draw');
        document.getElementById('signatureModal').style.display = 'flex';
    };

    window.closeSignatureModal = function () {
        const modal = document.getElementById('signatureModal');
        if (modal) modal.style.display = 'none';
        _sigCurrentAttachmentId = null;
        _sigPendingFileIndex = null;
        _sigOnSignedCallback = null;
    };

    async function _sigStampPendingFile(index, pngDataUrl, position, pageNumber, placePct) {
        if (typeof PDFLib === 'undefined') {
            throw new Error('PDF library not loaded');
        }
        const file = _archivePendingFiles[index];
        if (!file) throw new Error('File no longer exists');
        if (!/\.pdf$/i.test(file.name)) throw new Error('Only PDF files can be signed');

        const existingBytes = await file.arrayBuffer();
        const pdfDoc = await PDFLib.PDFDocument.load(existingBytes);
        const pages = pdfDoc.getPages();
        if (!pages.length) throw new Error('PDF has no pages');
        const targetIndex = pageNumber === -1 ? pages.length - 1 : Math.min(Math.max(pageNumber - 1, 0), pages.length - 1);
        const page = pages[targetIndex];
        const { width: pw, height: ph } = page.getSize();

        const pngBytes = Uint8Array.from(atob(pngDataUrl.split(',')[1]), c => c.charCodeAt(0));
        const pngImage = await pdfDoc.embedPng(pngBytes);
        const sigW = 160, sigH = 60, margin = 36;
        let x, y;
        if (placePct) {
            // Same mapping as the server: x_pct/y_pct are the box's top-left
            // corner as a fraction of the page, y measured from the top.
            x = Math.min(Math.max(placePct.x * pw, 0), Math.max(pw - sigW, 0));
            y = Math.min(Math.max(ph - (placePct.y * ph) - sigH, 0), Math.max(ph - sigH, 0));
        } else if (position === 'bottom-left') { x = margin; y = margin; }
        else if (position === 'top-right') { x = pw - sigW - margin; y = ph - sigH - margin; }
        else if (position === 'top-left') { x = margin; y = ph - sigH - margin; }
        else { x = pw - sigW - margin; y = margin; } // bottom-right

        page.drawImage(pngImage, { x, y, width: sigW, height: sigH });

        const stampedBytes = await pdfDoc.save();
        const stampedFile = new File([stampedBytes], file.name, { type: 'application/pdf' });
        if (!_archiveUnsignedOriginals[index]) {
            _archiveUnsignedOriginals[index] = file; // keep the pre-signature original for undo
        }
        _archivePendingFiles[index] = stampedFile;
        syncArchiveFileInput();
        renderArchiveFileList();
    }

    window._sigConfirm = async function () {
        _sigHideError();
        const dataUrl = _sigGetPngDataUrl();
        if (!dataUrl) {
            _sigShowError('Please draw or type a signature first.');
            return;
        }
        const position = document.getElementById('sigPosition').value;
        const pageNumber = parseInt(document.getElementById('sigPage').value, 10);
        const placePct = (_sigPlacementActive && _sigPlacePct) ? _sigPlacePct : null;

        const btn = document.getElementById('sigConfirmBtn');
        const originalHtml = btn.innerHTML;
        btn.disabled = true;
        btn.innerHTML = '<i class="ph ph-spinner ph-spin"></i> Signing...';

        // ── Mode 1: not-yet-uploaded file (archiving step) — stamp client-side ──
        if (_sigPendingFileIndex !== null) {
            try {
                await _sigStampPendingFile(_sigPendingFileIndex, dataUrl, position, pageNumber, placePct);
                closeSignatureModal();
            } catch (e) {
                _sigShowError(e.message || 'Signing failed.');
            } finally {
                btn.disabled = false;
                btn.innerHTML = originalHtml;
            }
            return;
        }

        // ── Mode 2: already-archived attachment — stamp server-side ─────────────
        try {
            const body = { signature: dataUrl, position, page_number: pageNumber };
            if (placePct) { body.x_pct = placePct.x; body.y_pct = placePct.y; }
            const resp = await fetch(`/api/attachments/${_sigCurrentAttachmentId}/sign`, {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify(body),
            });
            const data = await resp.json();
            if (!resp.ok) {
                _sigShowError(data.error || 'Signing failed.');
                return;
            }
            if (_sigOnSignedCallback) _sigOnSignedCallback(data);
            closeSignatureModal();
        } catch (e) {
            _sigShowError('Signing failed: ' + e.message);
        } finally {
            btn.disabled = false;
            btn.innerHTML = originalHtml;
        }
    };
})();
/* ── Polish layer: mobile sidebar drawer toggle ───────────────────────────
   Pairs with the CSS breakpoint that turns the sidebar into an off-canvas
   drawer under 720px. Tapping the ☰ area (topbar-left, rendered via CSS
   ::before) opens/closes it; tapping outside or picking a nav item closes it. */
(function polishMobileNav() {
    const topbarLeft = document.querySelector('.topbar-left');
    const sidebar = document.querySelector('.sidebar');
    if (!topbarLeft || !sidebar) return;

    function isMobile() { return window.matchMedia('(max-width: 720px)').matches; }

    topbarLeft.addEventListener('click', (e) => {
        if (!isMobile()) return;
        // Only react to taps in the ☰ hotzone (left edge), not the breadcrumb text
        const rect = topbarLeft.getBoundingClientRect();
        if (e.clientX - rect.left > 56) return;
        sidebar.classList.toggle('open');
    });

    document.addEventListener('click', (e) => {
        if (!isMobile() || !sidebar.classList.contains('open')) return;
        if (sidebar.contains(e.target) || topbarLeft.contains(e.target)) return;
        sidebar.classList.remove('open');
    });

    sidebar.querySelectorAll('.nav-item').forEach(btn => {
        btn.addEventListener('click', () => {
            if (isMobile()) sidebar.classList.remove('open');
        });
    });

    window.addEventListener('resize', () => {
        if (!isMobile()) sidebar.classList.remove('open');
    });
})();

// ══════════════════════════════════════════════════════════════════════════════
// PDF PAGE MANAGER — add and remove pages on a PDF, both for a not-yet-uploaded
// scan/upload (archiving step) and for an already-saved attachment (edit mode).
// Mirrors the dual-mode pattern used by the signature modal: everything is
// rebuilt client-side with pdf-lib + pdf.js. Pending files are swapped back
// into _archivePendingFiles; saved attachments are re-uploaded to replace the
// file on disk via /api/attachments/<id>/replace-pdf.
// ══════════════════════════════════════════════════════════════════════════════
let _pmMode = null;          // 'pending' | 'existing'
let _pmIndex = null;         // index into _archivePendingFiles (pending mode)
let _pmAttachmentId = null;  // attachment ID (existing mode)
let _pmFileNameValue = '';
let _pmSources = [];         // [{ key, kind:'pdf'|'image', pdfjsDoc?, bytes, mime?, pageCount }]
let _pmPages = [];           // [{ uid, sourceKey, pageIndex }] — current, in-order page list
let _pmUidSeq = 0;

async function _pmLoadPdfJs() {
    let pdfjsLib = window['pdfjs-dist/build/pdf'] || window.pdfjsLib;
    if (pdfjsLib) return pdfjsLib;
    await new Promise((resolve, reject) => {
        if (document.getElementById('_pdfjs_script')) {
            const check = setInterval(() => {
                if (window.pdfjsLib) { clearInterval(check); resolve(); }
            }, 100);
            return;
        }
        const s = document.createElement('script');
        s.id = '_pdfjs_script';
        s.src = 'https://cdnjs.cloudflare.com/ajax/libs/pdf.js/3.11.174/pdf.min.js';
        s.onload = resolve; s.onerror = reject;
        document.head.appendChild(s);
    });
    pdfjsLib = window.pdfjsLib;
    if (pdfjsLib && !pdfjsLib.GlobalWorkerOptions.workerSrc) {
        pdfjsLib.GlobalWorkerOptions.workerSrc =
            'https://cdnjs.cloudflare.com/ajax/libs/pdf.js/3.11.174/pdf.worker.min.js';
    }
    return pdfjsLib;
}

async function _pmAddPdfSource(bytes, keyPrefix) {
    const pdfjsLib = await _pmLoadPdfJs();
    const pdfjsDoc = await pdfjsLib.getDocument({ data: bytes.slice(0) }).promise;
    const key = keyPrefix + '_' + (_pmUidSeq++);
    _pmSources.push({ key, kind: 'pdf', pdfjsDoc, bytes, pageCount: pdfjsDoc.numPages });
    return key;
}

function _pmAddImageSource(bytes, mime, keyPrefix) {
    const key = keyPrefix + '_' + (_pmUidSeq++);
    _pmSources.push({ key, kind: 'image', bytes, mime, pageCount: 1 });
    return key;
}

async function _pmResetAndLoad(bytes, fileName) {
    _pmSources = [];
    _pmPages = [];
    _pmUidSeq = 0;
    _pmFileNameValue = fileName;
    const key = await _pmAddPdfSource(bytes, 'orig');
    const src = _pmSources.find(s => s.key === key);
    for (let i = 0; i < src.pageCount; i++) {
        _pmPages.push({ uid: _pmUidSeq++, sourceKey: key, pageIndex: i });
    }
}

function openPageManagerForPendingFile(index) {
    const file = _archivePendingFiles[index];
    if (!file) return;
    _pmMode = 'pending';
    _pmIndex = index;
    _pmAttachmentId = null;
    const name = getArchiveFileName(index);
    document.getElementById('pageManagerModal').style.display = 'flex';
    document.getElementById('pmFileName').textContent = name;
    document.getElementById('pmStatus').textContent = currentLang === 'ar' ? 'جارٍ التحميل…' : 'Loading…';
    document.getElementById('pmPageGrid').innerHTML = '';
    document.getElementById('pmEmptyMsg').style.display = 'none';
    file.arrayBuffer().then(async (buf) => {
        await _pmResetAndLoad(buf, name);
        document.getElementById('pmStatus').textContent = '';
        _pmRenderGrid();
    }).catch(() => {
        document.getElementById('pmStatus').textContent = currentLang === 'ar' ? 'تعذّر تحميل الملف' : 'Could not load file';
    });
}

async function openPageManagerForAttachment(attachmentId, name) {
    _pmMode = 'existing';
    _pmIndex = null;
    _pmAttachmentId = attachmentId;
    document.getElementById('pageManagerModal').style.display = 'flex';
    document.getElementById('pmFileName').textContent = name;
    document.getElementById('pmStatus').textContent = currentLang === 'ar' ? 'جارٍ التحميل…' : 'Loading…';
    document.getElementById('pmPageGrid').innerHTML = '';
    document.getElementById('pmEmptyMsg').style.display = 'none';
    try {
        const res = await fetch(`/api/attachments/${attachmentId}/download`);
        if (!res.ok) throw new Error('download failed');
        const buf = await res.arrayBuffer();
        await _pmResetAndLoad(buf, name);
        document.getElementById('pmStatus').textContent = '';
        _pmRenderGrid();
    } catch (e) {
        document.getElementById('pmStatus').textContent = currentLang === 'ar' ? 'تعذّر تحميل الملف' : 'Could not load file';
    }
}

function closePageManager() {
    const modal = document.getElementById('pageManagerModal');
    if (modal) modal.style.display = 'none';
    _pmMode = null; _pmIndex = null; _pmAttachmentId = null;
    _pmSources = []; _pmPages = [];
}

function _pmRenderGrid() {
    const grid = document.getElementById('pmPageGrid');
    const empty = document.getElementById('pmEmptyMsg');
    const countEl = document.getElementById('pmPageCount');
    if (!grid) return;
    if (countEl) {
        countEl.textContent = currentLang === 'ar'
            ? `${_pmPages.length} صفحة`
            : `${_pmPages.length} ${_pmPages.length === 1 ? 'page' : 'pages'}`;
    }
    if (!_pmPages.length) {
        grid.innerHTML = '';
        if (empty) empty.style.display = 'block';
        return;
    }
    if (empty) empty.style.display = 'none';
    grid.innerHTML = _pmPages.map((p, idx) => `
        <div class="pm-page-thumb" draggable="true" data-pm-uid="${p.uid}"
            ondragstart="pmDragStart(event, ${p.uid})"
            ondragover="pmDragOver(event, ${p.uid})"
            ondragleave="pmDragLeave(event)"
            ondrop="pmDrop(event, ${p.uid})"
            ondragend="pmDragEnd(event)"
            style="position:relative;border:1px solid var(--border);border-radius:8px;
            overflow:hidden;background:var(--bg-subtle,#f5f5f5);aspect-ratio:3/4;display:flex;align-items:center;justify-content:center;
            cursor:grab;transition:transform .12s,box-shadow .12s">
            <div class="pm-thumb-content" id="pm-thumb-content-${p.uid}" style="width:100%;height:100%;display:flex;align-items:center;justify-content:center;pointer-events:none">
                <span style="font-size:.7rem;color:var(--muted)">…</span>
            </div>
            <span style="position:absolute;top:4px;left:6px;background:rgba(0,0,0,.55);color:#fff;font-size:.68rem;padding:1px 6px;border-radius:8px;pointer-events:none">${idx + 1}</span>
            <span title="${currentLang === 'ar' ? 'اسحب لإعادة الترتيب' : 'Drag to reorder'}"
                style="position:absolute;top:2px;left:50%;transform:translateX(-50%);background:rgba(0,0,0,.4);color:#fff;
                font-size:.85rem;line-height:1;padding:1px 6px;border-radius:6px;pointer-events:none">⠿</span>
            <button type="button" onclick="pmRemovePage(${p.uid})" title="${currentLang === 'ar' ? 'إزالة' : 'Remove'}"
                style="position:absolute;top:2px;right:2px;width:22px;height:22px;border-radius:50%;border:none;
                background:var(--danger,#dc2626);color:#fff;cursor:pointer;font-size:.75rem;line-height:1">✕</button>
            <div style="position:absolute;bottom:2px;left:2px;right:2px;display:flex;justify-content:space-between;pointer-events:none">
                <button type="button" onclick="pmMovePage(${p.uid},-1)" ${idx === 0 ? 'disabled' : ''}
                    title="${currentLang === 'ar' ? 'تحريك لليسار' : 'Move earlier'}"
                    style="pointer-events:auto;width:22px;height:22px;border-radius:50%;border:none;
                    background:rgba(0,0,0,.55);color:#fff;cursor:pointer;font-size:.7rem;line-height:1;${idx === 0 ? 'opacity:.3;cursor:default' : ''}">‹</button>
                <button type="button" onclick="pmMovePage(${p.uid},1)" ${idx === _pmPages.length - 1 ? 'disabled' : ''}
                    title="${currentLang === 'ar' ? 'تحريك لليمين' : 'Move later'}"
                    style="pointer-events:auto;width:22px;height:22px;border-radius:50%;border:none;
                    background:rgba(0,0,0,.55);color:#fff;cursor:pointer;font-size:.7rem;line-height:1;${idx === _pmPages.length - 1 ? 'opacity:.3;cursor:default' : ''}">›</button>
            </div>
        </div>`).join('');
    _pmPages.forEach(p => _pmRenderThumb(p));
}

// ── Reordering: HTML5 drag-and-drop (desktop) + move-arrow buttons (touch/fallback) ──
let _pmDragUid = null;

function pmDragStart(ev, uid) {
    _pmDragUid = uid;
    ev.dataTransfer.effectAllowed = 'move';
    try { ev.dataTransfer.setData('text/plain', String(uid)); } catch (e) {}
    ev.currentTarget.style.opacity = '.4';
}

function pmDragOver(ev, uid) {
    ev.preventDefault();
    ev.dataTransfer.dropEffect = 'move';
    if (uid !== _pmDragUid) {
        ev.currentTarget.style.boxShadow = 'inset 0 0 0 2px var(--primary, #2563eb)';
    }
}

function pmDragLeave(ev) {
    ev.currentTarget.style.boxShadow = '';
}

function pmDrop(ev, targetUid) {
    ev.preventDefault();
    ev.currentTarget.style.boxShadow = '';
    const draggedUid = _pmDragUid;
    _pmDragUid = null;
    if (draggedUid == null || draggedUid === targetUid) return;
    const fromIdx = _pmPages.findIndex(p => p.uid === draggedUid);
    const toIdx = _pmPages.findIndex(p => p.uid === targetUid);
    if (fromIdx === -1 || toIdx === -1) return;
    const [moved] = _pmPages.splice(fromIdx, 1);
    _pmPages.splice(toIdx, 0, moved);
    _pmRenderGrid();
}

function pmDragEnd(ev) {
    ev.currentTarget.style.opacity = '';
    document.querySelectorAll('#pmPageGrid .pm-page-thumb').forEach(el => el.style.boxShadow = '');
    _pmDragUid = null;
}

// Fallback reordering for touch devices / accessibility: shift a page one
// position earlier (dir=-1) or later (dir=1) in the page list.
function pmMovePage(uid, dir) {
    const idx = _pmPages.findIndex(p => p.uid === uid);
    if (idx === -1) return;
    const newIdx = idx + dir;
    if (newIdx < 0 || newIdx >= _pmPages.length) return;
    const [moved] = _pmPages.splice(idx, 1);
    _pmPages.splice(newIdx, 0, moved);
    _pmRenderGrid();
}


async function _pmRenderThumb(p) {
    const content = document.getElementById(`pm-thumb-content-${p.uid}`);
    if (!content) return;
    const src = _pmSources.find(s => s.key === p.sourceKey);
    if (!src) return;
    try {
        if (src.kind === 'image') {
            const blob = new Blob([src.bytes], { type: src.mime });
            const url = URL.createObjectURL(blob);
            content.innerHTML = `<img src="${url}" style="max-width:100%;max-height:100%;object-fit:contain">`;
        } else {
            const page = await src.pdfjsDoc.getPage(p.pageIndex + 1);
            const vp0 = page.getViewport({ scale: 1 });
            const scale = 130 / vp0.width;
            const vp = page.getViewport({ scale });
            const canvas = document.createElement('canvas');
            canvas.width = vp.width;
            canvas.height = vp.height;
            canvas.style.cssText = 'max-width:100%;max-height:100%';
            await page.render({ canvasContext: canvas.getContext('2d'), viewport: vp }).promise;
            content.innerHTML = '';
            content.appendChild(canvas);
        }
    } catch (e) {
        content.innerHTML = `<span style="font-size:.65rem;color:var(--muted)">—</span>`;
    }
}

// Shared helper: turn a list of File objects into pm pages (PDF pages get
// expanded one-per-page, images become a single page each). Used both by the
// "Add Pages" file picker and by the scanner-return flow below.
async function _pmAddFilesAsPages(files) {
    for (const f of files) {
        try {
            const buf = await f.arrayBuffer();
            const isPdf = /\.pdf$/i.test(f.name) || f.type === 'application/pdf';
            if (isPdf) {
                const key = await _pmAddPdfSource(buf, 'new');
                const src = _pmSources.find(s => s.key === key);
                for (let i = 0; i < src.pageCount; i++) {
                    _pmPages.push({ uid: _pmUidSeq++, sourceKey: key, pageIndex: i });
                }
            } else {
                const mime = f.type || (/\.png$/i.test(f.name) ? 'image/png' : 'image/jpeg');
                const key = _pmAddImageSource(buf, mime, 'new');
                _pmPages.push({ uid: _pmUidSeq++, sourceKey: key, pageIndex: 0 });
            }
        } catch (e) {
            console.error('_pmAddFilesAsPages:', e);
        }
    }
}

async function pmAddPagesFromInput(input) {
    const files = Array.from(input.files || []);
    input.value = '';
    if (!files.length) return;
    const statusEl = document.getElementById('pmStatus');
    if (statusEl) statusEl.textContent = currentLang === 'ar' ? 'جارٍ الإضافة…' : 'Adding…';
    await _pmAddFilesAsPages(files);
    if (statusEl) statusEl.textContent = '';
    _pmRenderGrid();
}

// Opens the network/USB scanner so the user can scan more pages straight
// into the currently-open Manage Pages session (instead of only being able
// to pick files already saved on the laptop). The page manager modal is
// hidden (not closed — its state is preserved) while the scanner is open,
// and scanned/imported pages are appended to _pmPages when the user attaches
// them, then the page manager reopens automatically.
function pmOpenScanner() {
    const pmModal = document.getElementById('pageManagerModal');
    if (pmModal) pmModal.style.display = 'none';
    const name = document.getElementById('pmFileName')?.textContent || '';
    openScannerModal(null, name, 'pagemanager');
}

// Renders read-only thumbnails of the pages already in the current Manage
// Pages session, inside the scanner modal, so the user can see what's
// already attached to the PDF while they scan/add more pages.
async function _scanRenderExistingPmPages() {
    const wrap = document.getElementById('scanExistingPagesWrap');
    const strip = document.getElementById('scanExistingPagesThumbs');
    if (!wrap || !strip) return;
    if (_scanMode !== 'pagemanager' || !_pmPages.length) {
        wrap.style.display = 'none';
        strip.innerHTML = '';
        return;
    }
    wrap.style.display = 'block';
    strip.innerHTML = _pmPages.map((p, idx) => `
        <div style="flex:0 0 auto;width:70px;height:92px;border:1px solid var(--border);border-radius:6px;
            overflow:hidden;background:var(--bg-subtle,#f5f5f5);display:flex;align-items:center;justify-content:center;position:relative">
            <div id="scan-existing-thumb-${p.uid}" style="width:100%;height:100%;display:flex;align-items:center;justify-content:center">
                <span style="font-size:.62rem;color:var(--muted)">…</span>
            </div>
            <span style="position:absolute;bottom:2px;right:3px;background:rgba(0,0,0,.55);color:#fff;font-size:.6rem;padding:0 4px;border-radius:6px">${idx + 1}</span>
        </div>`).join('');
    for (const p of _pmPages) {
        const content = document.getElementById(`scan-existing-thumb-${p.uid}`);
        if (!content) continue;
        const src = _pmSources.find(s => s.key === p.sourceKey);
        if (!src) continue;
        try {
            if (src.kind === 'image') {
                const blob = new Blob([src.bytes], { type: src.mime });
                const url = URL.createObjectURL(blob);
                content.innerHTML = `<img src="${url}" style="max-width:100%;max-height:100%;object-fit:contain">`;
            } else {
                const page = await src.pdfjsDoc.getPage(p.pageIndex + 1);
                const vp0 = page.getViewport({ scale: 1 });
                const scale = 70 / vp0.width;
                const vp = page.getViewport({ scale });
                const canvas = document.createElement('canvas');
                canvas.width = vp.width;
                canvas.height = vp.height;
                canvas.style.cssText = 'max-width:100%;max-height:100%';
                await page.render({ canvasContext: canvas.getContext('2d'), viewport: vp }).promise;
                content.innerHTML = '';
                content.appendChild(canvas);
            }
        } catch (e) {
            content.innerHTML = `<span style="font-size:.6rem;color:var(--muted)">—</span>`;
        }
    }
}

function pmRemovePage(uid) {
    _pmPages = _pmPages.filter(p => p.uid !== uid);
    _pmRenderGrid();
}

async function pmSaveChanges() {
    if (!_pmPages.length) {
        showToast(currentLang === 'ar' ? 'أضف صفحة واحدة على الأقل' : 'Add at least one page first', 'error');
        return;
    }
    if (typeof PDFLib === 'undefined') {
        showToast('PDF library not loaded', 'error');
        return;
    }
    const btn = document.getElementById('pmSaveBtn');
    const originalHtml = btn ? btn.innerHTML : '';
    if (btn) {
        btn.disabled = true;
        btn.innerHTML = `<i class="ph ph-spinner ph-spin"></i> ${currentLang === 'ar' ? 'جارٍ الحفظ…' : 'Saving…'}`;
    }
    try {
        // Load (and cache) a pdf-lib document for every distinct PDF source —
        // needed for copyPages; image sources are embedded directly.
        const pdfLibDocs = {};
        for (const src of _pmSources) {
            if (src.kind === 'pdf' && !pdfLibDocs[src.key]) {
                pdfLibDocs[src.key] = await PDFLib.PDFDocument.load(src.bytes, { ignoreEncryption: true });
            }
        }
        const outDoc = await PDFLib.PDFDocument.create();
        for (const p of _pmPages) {
            const src = _pmSources.find(s => s.key === p.sourceKey);
            if (!src) continue;
            if (src.kind === 'pdf') {
                const [copied] = await outDoc.copyPages(pdfLibDocs[src.key], [p.pageIndex]);
                outDoc.addPage(copied);
            } else {
                const img = src.mime === 'image/png'
                    ? await outDoc.embedPng(src.bytes)
                    : await outDoc.embedJpg(src.bytes);
                const page = outDoc.addPage([img.width, img.height]);
                page.drawImage(img, { x: 0, y: 0, width: img.width, height: img.height });
            }
        }
        const outBytes = await outDoc.save();

        if (_pmMode === 'pending') {
            const newFile = new File([outBytes], _pmFileNameValue, { type: 'application/pdf' });
            _archivePendingFiles[_pmIndex] = newFile;
            delete _archiveUnsignedOriginals[_pmIndex]; // page edits invalidate any stored pre-signature original
            syncArchiveFileInput();
            renderArchiveFileList();
            closePageManager();
            showToast(currentLang === 'ar' ? 'تم تحديث الصفحات' : 'Pages updated', 'success');
        } else {
            const fd = new FormData();
            fd.append('file', new Blob([outBytes], { type: 'application/pdf' }), _pmFileNameValue);
            const res = await fetch(`/api/attachments/${_pmAttachmentId}/replace-pdf`, { method: 'POST', body: fd });
            const data = await res.json();
            if (!res.ok || data.error) {
                showToast(data.error || 'Could not save changes', 'error');
                return;
            }
            const sizeEl = document.querySelector(`#existing-att-${_pmAttachmentId} .file-item-size`);
            if (sizeEl && data.file_size) sizeEl.textContent = formatBytes(data.file_size);
            closePageManager();
            showToast(currentLang === 'ar' ? 'تم حفظ الصفحات' : 'Pages saved', 'success');
        }
    } catch (e) {
        console.error('pmSaveChanges:', e);
        showToast(currentLang === 'ar' ? 'تعذّر حفظ الصفحات' : 'Could not save pages', 'error');
    } finally {
        if (btn) {
            btn.disabled = false;
            btn.innerHTML = originalHtml;
        }
    }
}