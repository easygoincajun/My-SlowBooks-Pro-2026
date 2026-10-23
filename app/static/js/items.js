/**
 * Item list — QB2003 supported a parent/sub-item tree with inherited
 * accounts. We skipped the hierarchy. Life is too short.
 */
const ItemsPage = {
    // "To reorder" (#261): the items at or under their reorder point, which
    // GET /api/items/low-stock has always returned and no page showed. Off
    // by default, and not drawn at all when nothing is short, so a company
    // that doesn't track stock sees nothing new.
    _reorderOnly: false,

    toggleReorder() {
        ItemsPage._reorderOnly = !ItemsPage._reorderOnly;
        App.navigate(location.hash || '#/items');
    },

    async render() {
        const all = await API.get(`/items${ActiveLists.query('items')}`);
        ActiveLists.remember('items', all);
        let low = [];
        try { low = await API.get('/items/low-stock'); } catch (e) { low = []; }
        const lowById = Object.fromEntries(low.map(l => [l.id, l]));
        const reorderOnly = ItemsPage._reorderOnly && low.length > 0;
        const items = reorderOnly ? all.filter(i => lowById[i.id]) : all;
        const reorderBtn = low.length
            ? `<button type="button" class="btn btn-sm ${reorderOnly ? 'btn-primary' : 'btn-secondary'}" aria-pressed="${reorderOnly}" onclick="ItemsPage.toggleReorder()" title="Items at or under their reorder point">To reorder (${low.length})</button>`
            : '';
        let html = `
            <div class="page-header">
                <h2>Items & Services</h2>
                <button class="btn btn-primary" onclick="ItemsPage.showForm()">+ New Item</button>
            </div>
            <div class="toolbar">${ActiveLists.pickerHtml('items')} ${reorderBtn}</div>`;

        if (reorderOnly && items.length === 0) {
            html += `<div class="empty-state"><p>Nothing to reorder in this list.</p></div>`;
        } else if (items.length === 0 && ActiveLists.emptyText('items')) {
            html += `<div class="empty-state"><p>${ActiveLists.emptyText('items')}</p></div>`;
        } else if (items.length === 0) {
            html += `<div class="empty-state">
                <p>No items yet.</p>
                <button class="btn btn-primary" onclick="ItemsPage.showForm()" style="margin-top:10px;">+ Create your first item</button>
            </div>`;
        } else {
            html += `<div class="table-container"><table>
                <thead><tr>
                    <th scope="col">Name</th><th scope="col">Type</th><th scope="col">Description</th>
                    <th scope="col" class="amount">Rate</th><th scope="col" class="amount">Cost</th>
                    <th scope="col" class="amount">Qty on Hand</th>${reorderOnly ? '<th scope="col" class="amount">Reorder point</th><th scope="col" class="amount">Short by</th>' : ''}<th scope="col">Actions</th>
                </tr></thead><tbody>`;
            for (const item of items) {
                const short = lowById[item.id];
                const qtyCell = item.track_inventory
                    ? `<strong>${Number(item.quantity_on_hand || 0).toLocaleString()}</strong>${short && !reorderOnly ? ' <span style="color:var(--text-danger); font-weight:700;" title="At or under the reorder point">↓</span>' : ''}`
                    : `<span style="color:var(--text-muted)">—</span>`;
                const reorderCells = reorderOnly
                    ? `<td class="amount">${Number(short.reorder_point || 0).toLocaleString()}</td><td class="amount" style="color:var(--text-danger); font-weight:700;">${Number(short.shortage || 0).toLocaleString()}</td>`
                    : '';
                const adjustBtn = item.track_inventory
                    ? `<button class="btn btn-sm btn-secondary" onclick="ItemsPage.showAdjust(${item.id})" title="Adjust quantity on hand">Adjust</button>
                       <button class="btn btn-sm btn-secondary" onclick="ItemsPage.showMovements(${item.id})" title="Inventory movement history">History</button>`
                    : '';
                // An inactive item stays in the list, marked, so it can be
                // opened and made active again; the pickers leave it out.
                const inactive = item.is_active === false;
                html += `<tr${inactive ? ' class="row--dim"' : ''}>
                    <td><strong>${escapeHtml(item.name)}</strong>${inactive ? ' <span class="badge badge-draft">inactive</span>' : ''}</td>
                    <td>${statusBadge(item.item_type)}</td>
                    <td>${escapeHtml(item.description) || ''}</td>
                    <td class="amount">${formatCurrency(item.rate)}</td>
                    <td class="amount">${formatCurrency(item.cost)}</td>
                    <td class="amount">${qtyCell}</td>${reorderCells}
                    <td class="actions">
                        <button class="btn btn-sm btn-secondary" onclick="ItemsPage.showForm(${item.id})">Edit</button>
                        ${ActiveLists.buttonHtml('items', item)}
                        ${adjustBtn}
                    </td>
                </tr>`;
            }
            html += `</tbody></table></div>`;
        }
        return html;
    },

    async showForm(id = null) {
        let item = { name:'', item_type:'service', description:'', rate:0, cost:0,
            income_account_id:'', expense_account_id:'', is_taxable:true,
            track_inventory:false, quantity_on_hand:0, reorder_point:0,
            asset_account_id:'', avg_cost:0 };
        if (id) item = await API.get(`/items/${id}`);

        const accounts = await API.get('/accounts');
        // Income means income accounts only — the list used to offer COGS
        // and expense accounts too, and a business's 4400 In-Kind
        // Contributions (W-L13). The account an item already uses stays
        // listed, so saving the form never drops it.
        const incomeAccts = pickerAccounts(accounts, ['income'], item.income_account_id);
        const expenseAccts = pickerAccounts(accounts, ['expense','cogs'], item.expense_account_id);
        const assetAccts = pickerAccounts(accounts, ['asset'], item.asset_account_id);
        const acctLabel = a => `${a.account_number ? escapeHtml(a.account_number) + ' - ' : ''}${escapeHtml(a.name)}`;

        // Phase 11 inventory only makes sense for tangible items.
        const trackable = ['product','material'].includes(item.item_type);
        const showInv = trackable && item.track_inventory;
        const qtyEditableOnCreate = !id;

        openModal(id ? 'Edit Item' : 'New Item', `
            <form id="item-form" onsubmit="ItemsPage.save(event, ${id})">
                <div class="form-grid">
                    <div class="form-group"><label>Name *</label>
                        <input name="name" required maxlength="200" value="${escapeHtml(item.name)}"></div>
                    <div class="form-group"><label>Type *</label>
                        <select name="item_type" id="item-type-sel">
                            ${['service','product','material','labor'].map(t =>
                                `<option value="${t}" ${item.item_type===t?'selected':''}>${t.charAt(0).toUpperCase()+t.slice(1)}</option>`).join('')}
                        </select></div>
                    <div class="form-group full-width"><label>Description</label>
                        <textarea name="description">${escapeHtml(item.description || '')}</textarea></div>
                    <div class="form-group"><label>Rate (sell price)</label>
                        <input name="rate" type="number" step="0.0001" value="${item.rate}"></div>
                    <div class="form-group"><label>Cost</label>
                        <input name="cost" type="number" step="0.0001" value="${item.cost}"></div>
                    <div class="form-group"><label>${T('Income')} Account</label>
                        <select name="income_account_id">
                            <option value="">-- None --</option>
                            ${incomeAccts.map(a => `<option value="${a.id}" ${item.income_account_id==a.id?'selected':''}>${acctLabel(a)}</option>`).join('')}
                        </select></div>
                    <div class="form-group"><label>Expense or COGS Account</label>
                        <select name="expense_account_id">
                            <option value="">-- None --</option>
                            ${PurchaseAccounts.options(expenseAccts, item.expense_account_id)}
                        </select></div>
                    <div class="form-group"><label>
                        <input type="checkbox" name="is_taxable" ${item.is_taxable?'checked':''}>
                        Taxable</label></div>
                    ${id ? `<div class="form-group"><label>
                        <input type="checkbox" name="is_active" ${item.is_active !== false ? 'checked' : ''}>
                        Active</label>
                        <div style="font-size:10px; color:var(--text-muted);">An inactive item stays on the documents that use it but is left out of every item picker.</div></div>` : ''}
                </div>

                <fieldset id="inv-fieldset" style="margin-top:16px; padding:10px 14px; border:1px solid var(--panel-border); border-radius:4px; ${trackable ? '' : 'display:none;'}">
                    <legend style="font-size:11px; font-weight:700; text-transform:uppercase; letter-spacing:0.5px; color:var(--text-muted); padding:0 6px;">Inventory Tracking</legend>
                    <div class="form-group">
                        <label>
                            <input type="checkbox" name="track_inventory" id="track-inv-chk" ${item.track_inventory?'checked':''}>
                            Track inventory for this item
                        </label>
                        <div style="font-size:11px; color:var(--text-muted); margin-top:2px;">
                            Posting an invoice or bill that uses this item will adjust quantity on hand and post to the asset account below.
                        </div>
                    </div>
                    <div id="inv-detail-grid" class="form-grid" style="${showInv ? '' : 'display:none;'}">
                        <div class="form-group"><label>Quantity on Hand
                            ${id ? '<span style="font-size:10px;color:var(--text-muted)">(saving a change posts an inventory adjustment)</span>' : ''}
                        </label>
                            <input name="quantity_on_hand" type="number" step="1" min="0"
                                   value="${item.quantity_on_hand || 0}"
                                   data-original="${item.quantity_on_hand || 0}"></div>
                        <div class="form-group"><label>Reorder Point</label>
                            <input name="reorder_point" type="number" step="1" min="0" value="${item.reorder_point || 0}"></div>
                        <div class="form-group"><label>Asset Account ${id ? '' : '<span style="font-size:10px;color:var(--text-muted)">(blank = Inventory 1300)</span>'}</label>
                            <select name="asset_account_id">
                                <option value="">-- Default (Inventory 1300) --</option>
                                ${assetAccts.map(a => `<option value="${a.id}" ${item.asset_account_id==a.id?'selected':''}>${acctLabel(a)}</option>`).join('')}
                            </select></div>
                        ${id ? `<div class="form-group"><label>Avg Cost <span style="font-size:10px;color:var(--text-muted)">(weighted, computed from movements)</span></label>
                            <div style="padding:8px 10px; border:1px solid var(--panel-border); border-radius:4px; background:transparent; color:var(--text-primary); font-variant-numeric:tabular-nums;">${formatCurrency(item.avg_cost || 0)}</div></div>` : ''}
                    </div>
                </fieldset>

                <div class="form-actions">
                    <button type="button" class="btn btn-secondary" onclick="closeModal()">Cancel</button>
                    <button type="submit" class="btn btn-primary">${id ? 'Update' : 'Create'} Item</button>
                </div>
            </form>`);

        // Wire conditional visibility: fieldset shown only for product/material;
        // detail grid shown only when the track checkbox is on.
        const typeSel = document.getElementById('item-type-sel');
        const fs = document.getElementById('inv-fieldset');
        const trackChk = document.getElementById('track-inv-chk');
        const detailGrid = document.getElementById('inv-detail-grid');

        const syncFieldset = () => {
            const isTrackable = ['product','material'].includes(typeSel.value);
            fs.style.display = isTrackable ? '' : 'none';
            if (!isTrackable) trackChk.checked = false;
            detailGrid.style.display = (isTrackable && trackChk.checked) ? '' : 'none';
        };
        if (typeSel) typeSel.addEventListener('change', syncFieldset);
        if (trackChk) trackChk.addEventListener('change', syncFieldset);
    },

    async save(e, id) {
        e.preventDefault();
        const form = e.target;
        const trackInv = form.track_inventory ? form.track_inventory.checked : false;
        const data = {
            name: form.name.value,
            item_type: form.item_type.value,
            description: form.description.value,
            rate: parseFloat(form.rate.value) || 0,
            cost: parseFloat(form.cost.value) || 0,
            income_account_id: form.income_account_id.value ? parseInt(form.income_account_id.value) : null,
            expense_account_id: form.expense_account_id.value ? parseInt(form.expense_account_id.value) : null,
            is_taxable: form.is_taxable.checked,
            track_inventory: trackInv,
            reorder_point: trackInv && form.reorder_point ? parseFloat(form.reorder_point.value) || 0 : 0,
            asset_account_id: trackInv && form.asset_account_id && form.asset_account_id.value
                ? parseInt(form.asset_account_id.value) : null,
        };
        // Active/inactive is an edit (a new item is active).
        if (id && form.is_active) data.is_active = form.is_active.checked;
        // Initial quantity is only settable on create — backend recomputes
        // it from inventory_movements after that, so PUT silently ignores it.
        if (!id && trackInv && form.quantity_on_hand) {
            data.quantity_on_hand = parseFloat(form.quantity_on_hand.value) || 0;
        }

        // For an existing item, detect a manual qty edit and convert it to
        // an /adjust call so the inventory_movements ledger stays coherent
        // (PUT alone can't change qty_on_hand by design).
        let pendingAdjust = null;
        if (id && trackInv && form.quantity_on_hand) {
            const original = parseFloat(form.quantity_on_hand.dataset.original) || 0;
            const requested = parseFloat(form.quantity_on_hand.value) || 0;
            if (requested < 0) {
                toast('Quantity on hand cannot be negative', 'error');
                return;
            }
            const delta = requested - original;
            if (Math.abs(delta) > 1e-9) {
                pendingAdjust = { quantity_delta: delta, memo: 'Direct edit via item form' };
            }
        }

        try {
            if (id) { await API.put(`/items/${id}`, data); }
            else { await API.post('/items', data); }
            if (pendingAdjust) {
                await API.post(`/items/${id}/adjust`, pendingAdjust);
                toast('Item updated and quantity adjusted');
            } else {
                toast(id ? 'Item updated' : 'Item created');
            }
            App.refresh();
        } catch (err) { toast(err.message, 'error'); }
    },

    // ----- Phase 11: manual inventory adjustment -----
    // POST /items/{id}/adjust accepts {quantity_delta, unit_cost?, memo?}.
    // The form lets the user think in three modes (Add / Remove / Set to)
    // and computes the delta for them. unit_cost is only relevant on Add
    // (i.e., a receipt that changes the weighted-average cost).

    async showMovements(id) {
        const [item, movements] = await Promise.all([
            API.get(`/items/${id}`),
            API.get(`/items/${id}/movements`),
        ]);
        // Movement type → human label + color. Receipts add stock, sales +
        // negative adjustments remove it.
        const typeLabel = {
            purchase: ['Purchase', 'badge-paid'],
            sale: ['Sale', 'badge-sent'],
            adjustment: ['Adjustment', 'badge-draft'],
            return: ['Return', 'badge-paid'],
        };
        let rows = movements.map(m => {
            const [label, cls] = typeLabel[m.movement_type] || [m.movement_type, 'badge-draft'];
            const qty = Number(m.quantity);
            const qtyStr = (qty > 0 ? '+' : '') + qty.toLocaleString();
            const src = m.source_type
                ? `${escapeHtml(m.source_type)}${m.source_id ? ' #' + m.source_id : ''}`
                : '';
            return `<tr>
                <td style="white-space:nowrap;font-size:11px;">${formatDate(m.date)}</td>
                <td><span class="badge ${cls}">${label}</span></td>
                <td class="amount" style="${qty < 0 ? 'color:var(--qb-red);' : ''}">${qtyStr}</td>
                <td class="amount">${formatCurrency(m.unit_cost)}</td>
                <td class="amount"><strong>${Number(m.balance_qty).toLocaleString()}</strong></td>
                <td class="amount">${formatCurrency(m.balance_avg_cost)}</td>
                <td style="font-size:11px;">${src}${m.memo ? '<br><span style="color:var(--text-muted);">' + escapeHtml(m.memo) + '</span>' : ''}</td>
            </tr>`;
        }).join('');
        if (!rows) {
            rows = '<tr><td colspan="7" style="text-align:center;color:var(--gray-400);">No movements recorded yet</td></tr>';
        }
        openModal(`Inventory History — ${escapeHtml(item.name)}`, `
            <div style="margin-bottom:10px;font-size:12px;color:var(--text-muted);">
                On hand: <strong>${Number(item.quantity_on_hand || 0).toLocaleString()}</strong>
                &nbsp;·&nbsp; Weighted avg cost: <strong>${formatCurrency(item.avg_cost || 0)}</strong>
            </div>
            <div class="table-container" style="max-height:60vh;overflow:auto;"><table>
                <thead><tr>
                    <th scope="col">Date</th><th scope="col">Type</th><th scope="col" class="amount">Qty</th><th scope="col" class="amount">Unit Cost</th>
                    <th scope="col" class="amount">Bal Qty</th><th scope="col" class="amount">Bal Avg</th><th scope="col">Source</th>
                </tr></thead>
                <tbody>${rows}</tbody>
            </table></div>
            <div class="form-actions"><button class="btn btn-secondary" onclick="closeModal()">Close</button></div>`);
    },

    async showAdjust(id) {
        const item = await API.get(`/items/${id}`);
        if (!item.track_inventory) {
            toast('This item is not inventory-tracked', 'error');
            return;
        }
        const currentQty = Number(item.quantity_on_hand || 0);
        const currentAvg = Number(item.avg_cost || 0);

        openModal('Adjust Inventory — ' + item.name, `
            <form id="adjust-form" onsubmit="ItemsPage.submitAdjust(event, ${id})">
                <div class="form-grid">
                    <div class="form-group full-width">
                        <label>Current</label>
                        <div style="font-size:14px;">
                            <strong>${currentQty.toLocaleString()}</strong> on hand
                            &middot; avg cost <strong>${formatCurrency(currentAvg)}</strong>
                        </div>
                    </div>
                    <div class="form-group">
                        <label>Mode *</label>
                        <select name="mode" id="adjust-mode">
                            <option value="add">Add stock (receipt / found)</option>
                            <option value="remove">Remove stock (shrinkage / spoilage)</option>
                            <option value="set">Set to count (physical-count correction)</option>
                        </select>
                    </div>
                    <div class="form-group">
                        <label id="adjust-qty-label">Quantity to add *</label>
                        <input name="quantity" id="adjust-qty" type="number" step="1" min="0" required>
                    </div>
                    <div class="form-group" id="adjust-cost-wrap">
                        <label>Unit cost (optional)
                            <span style="font-size:10px;color:var(--text-muted)">— affects avg cost</span>
                        </label>
                        <input name="unit_cost" type="number" step="0.0001" min="0"
                               placeholder="${currentAvg.toFixed(2)}">
                    </div>
                    <div class="form-group full-width">
                        <label>Memo</label>
                        <input name="memo" placeholder="e.g. Annual physical count, vendor return, etc.">
                    </div>
                    <div class="form-group full-width" id="adjust-preview"
                         style="font-size:12px; color:var(--text-muted); padding:8px 10px;
                                background:var(--gray-100,#f7f7fa); border-radius:4px;">
                        Enter a quantity to see the resulting balance.
                    </div>
                </div>
                <div class="form-actions">
                    <button type="button" class="btn btn-secondary" onclick="closeModal()">Cancel</button>
                    <button type="submit" class="btn btn-primary">Post Adjustment</button>
                </div>
            </form>`);

        // Live label/preview wiring
        const modeSel = document.getElementById('adjust-mode');
        const qtyInp = document.getElementById('adjust-qty');
        const lbl = document.getElementById('adjust-qty-label');
        const costWrap = document.getElementById('adjust-cost-wrap');
        const preview = document.getElementById('adjust-preview');

        const sync = () => {
            const mode = modeSel.value;
            const qty = parseFloat(qtyInp.value) || 0;
            // Label
            if (mode === 'add') lbl.textContent = 'Quantity to add *';
            else if (mode === 'remove') lbl.textContent = 'Quantity to remove *';
            else lbl.textContent = 'New quantity *';
            // Cost only meaningful for receipts
            costWrap.style.display = mode === 'add' ? '' : 'none';
            // Preview
            let delta = 0, newQty = currentQty;
            if (mode === 'add') { delta = qty; newQty = currentQty + qty; }
            else if (mode === 'remove') { delta = -qty; newQty = currentQty - qty; }
            else { delta = qty - currentQty; newQty = qty; }
            const sign = delta >= 0 ? '+' : '';
            preview.innerHTML =
                `Delta: <strong>${sign}${delta.toLocaleString()}</strong>` +
                ` &nbsp;&middot;&nbsp; New on-hand: <strong>${newQty.toLocaleString()}</strong>`;
        };
        modeSel.addEventListener('change', sync);
        qtyInp.addEventListener('input', sync);
    },

    async submitAdjust(e, id) {
        e.preventDefault();
        const form = e.target;
        const mode = form.mode.value;
        const qty = parseFloat(form.quantity.value);
        if (!isFinite(qty) || qty < 0) {
            toast('Quantity must be a non-negative number', 'error');
            return;
        }

        // Recompute current qty fresh — modal value could be stale if user
        // sat on it for a while.
        const item = await API.get(`/items/${id}`);
        const currentQty = Number(item.quantity_on_hand || 0);

        let delta;
        if (mode === 'add') delta = qty;
        else if (mode === 'remove') delta = -qty;
        else delta = qty - currentQty;

        if (delta === 0) {
            toast('No change to post (delta is zero)', 'error');
            return;
        }

        const payload = { quantity_delta: delta };
        if (mode === 'add' && form.unit_cost.value) {
            const c = parseFloat(form.unit_cost.value);
            if (isFinite(c) && c >= 0) payload.unit_cost = c;
        }
        if (form.memo.value.trim()) payload.memo = form.memo.value.trim();

        try {
            await API.post(`/items/${id}/adjust`, payload);
            toast('Inventory adjusted');
            App.refresh();
        } catch (err) {
            toast(err.message || 'Adjustment failed', 'error');
        }
    },
};

/**
 * "+ New Item" in a sales form's line Item picker: create an item without
 * leaving the form, as "+ New Customer" does for the customer. The panel
 * sits under the lines table (a row inside it would be counted as a line),
 * remembers which line asked, and on Save creates the item, adds it to every
 * line's picker and picks it on that line, whose change handler fills the
 * description, price and tax flag. Income goes to the default income
 * account until one is set in Items.
 *
 * A form gives the panel an id prefix: panelHtml('inv'), then
 * open('inv', select, items, tbody) when a line's picker reads NEW. `items`
 * is the form's item list (the new item joins it, so the line's fill finds
 * it); `tbody` holds the lines whose pickers get the new option.
 */
const ItemQuickAdd = {
    NEW: '__new__',
    _for: {},  // prefix -> {select, items, tbody} of the line that asked

    option() { return `<option value="${this.NEW}">+ New Item</option>`; },

    panelHtml(p) {
        const types = [['service', 'Service'], ['product', 'Product'], ['material', 'Material'], ['labor', 'Labor']];
        return `
            <div id="${p}-new-item" style="display:none; margin-top:8px; padding:8px; border:1px solid var(--gray-300); border-radius:4px; background:var(--primary-light);">
                <div style="font-weight:700; font-size:11px; margin-bottom:6px;">Quick Add Item</div>
                <div style="display:flex; gap:6px; flex-wrap:wrap; margin-bottom:4px;">
                    <input id="${p}-new-item-name" placeholder="Name *" aria-label="Item name" aria-required="true" style="flex:2; min-width:160px;">
                    <select id="${p}-new-item-type" aria-label="Item type" data-no-search>
                        ${types.map(([v, t]) => `<option value="${v}">${t}</option>`).join('')}</select>
                    <input id="${p}-new-item-rate" type="number" step="0.01" min="0" placeholder="Price" aria-label="Item price" style="width:100px;">
                    <label style="font-weight:normal; display:flex; align-items:center; gap:4px;">
                        <input type="checkbox" id="${p}-new-item-taxable" checked> Taxable</label>
                </div>
                <input id="${p}-new-item-desc" placeholder="Description" aria-label="Item description" style="width:100%; margin-bottom:4px;">
                <div style="display:flex; gap:6px;">
                    <button type="button" class="btn btn-sm btn-primary" onclick="ItemQuickAdd.save('${p}')">Save</button>
                    <button type="button" class="btn btn-sm btn-secondary" onclick="ItemQuickAdd.cancel('${p}')">Cancel</button>
                </div>
            </div>`;
    },

    open(p, select, items, tbody) {
        this._for[p] = { select, items, tbody };
        $(`#${p}-new-item`).style.display = 'block';
        $(`#${p}-new-item-name`).focus();
    },

    // A line's picker changed. True when it asked for "+ New Item": the
    // panel opens and the line waits for it. Otherwise a panel open for this
    // line closes, and the form fills the line from the item as before.
    picked(p, row, items, tbody) {
        const select = row?.querySelector('.line-item');
        if (!select) return false;
        if (select.value === this.NEW) {
            this.open(p, select, items, tbody);
            return true;
        }
        this.close(p, select);
        return false;
    },

    // A line that picks something else while the panel is open for it.
    close(p, select) {
        if (select && this._for[p]?.select !== select) return;
        const panel = $(`#${p}-new-item`);
        if (panel) panel.style.display = 'none';
        delete this._for[p];
    },

    cancel(p) {
        const select = this._for[p]?.select;
        this.close(p);
        if (select && select.value === this.NEW) {
            select.value = '';
            select.dispatchEvent(new Event('change'));
        }
    },

    async save(p) {
        if (!this._for[p]) return null;
        const { select, items, tbody } = this._for[p];
        const name = $(`#${p}-new-item-name`).value.trim();
        if (!name) { toast('Item name is required', 'error'); return null; }
        let item = items.find(i => (i.name || '').toLowerCase() === name.toLowerCase());
        if (item) {
            toast(`Using existing item "${item.name}"`);
        } else {
            try {
                item = await API.post('/items', {
                    name,
                    item_type: $(`#${p}-new-item-type`).value,
                    rate: parseFloat($(`#${p}-new-item-rate`).value) || 0,
                    is_taxable: $(`#${p}-new-item-taxable`).checked,
                    description: $(`#${p}-new-item-desc`).value.trim() || null,
                });
            } catch (err) { toast(err.message, 'error'); return null; }
            items.push(item);
            for (const sel of tbody.querySelectorAll('.line-item')) {
                sel.insertAdjacentHTML('beforeend', `<option value="${item.id}">${escapeHtml(item.name)}</option>`);
            }
            toast(`Item "${item.name}" created`);
        }
        for (const id of ['name', 'rate', 'desc']) $(`#${p}-new-item-${id}`).value = '';
        $(`#${p}-new-item-type`).value = 'service';
        $(`#${p}-new-item-taxable`).checked = true;
        this.close(p);
        if (select && select.isConnected) {
            select.value = item.id;
            select.dispatchEvent(new Event('change'));
        }
        return item;
    },
};
