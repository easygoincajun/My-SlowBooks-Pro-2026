/**
 * Bank Rules — auto-categorize imported bank transactions by payee pattern
 * Phase 10: Quick Wins + Medium Effort Features
 */
const BankRulesPage = {
    async render() {
        const rules = await API.get('/bank-rules');
        let html = `
            <div class="page-header">
                <h2>Bank Rules</h2>
                <div class="btn-group">
                    <button class="btn btn-primary" onclick="BankRulesPage.showForm()">+ New Rule</button>
                    <button class="btn btn-secondary" onclick="BankRulesPage.applyAll()">Apply Rules Now</button>
                </div>
            </div>
            <p style="font-size:11px; color:var(--text-muted); margin-bottom:12px;">
                Rules auto-match imported bank transactions by payee name. Higher priority rules are checked first.
            </p>`;

        if (rules.length === 0) {
            html += '<div class="empty-state"><p>No bank rules yet. Create one to auto-categorize transactions.</p></div>';
        } else {
            html += `<div class="table-container"><table>
                <thead><tr>
                    <th scope="col">Name</th><th scope="col">Pattern</th><th scope="col">Match Type</th>
                    <th scope="col">Category Account</th><th scope="col">Priority</th><th scope="col">Active</th><th scope="col">Actions</th>
                </tr></thead><tbody>`;
            for (const r of rules) {
                html += `<tr>
                    <td><strong>${escapeHtml(r.name)}</strong></td>
                    <td><code>${escapeHtml(r.pattern)}</code></td>
                    <td>${escapeHtml(r.rule_type)}</td>
                    <td>${r.account_id || '—'}</td>
                    <td>${r.priority}</td>
                    <td>${r.is_active ? 'Yes' : 'No'}</td>
                    <td class="actions">
                        <button class="btn btn-sm btn-secondary" onclick="BankRulesPage.showForm(${r.id})">Edit</button>
                        <button data-destructive class="btn btn-sm btn-danger" onclick="BankRulesPage.deleteRule(${r.id})">Delete</button>
                    </td>
                </tr>`;
            }
            html += '</tbody></table></div>';
        }
        return html;
    },

    // What a rule made from elsewhere does when saved (the banking page's
    // "Rule…" on a statement line, #259): run, then return there. The
    // rules list is the default.
    _afterSave: null,

    // prefill: a rule's fields filled in from another screen (#259).
    async showForm(id = null, prefill = null) {
        let rule = { name: '', pattern: '', account_id: '', vendor_id: '', rule_type: 'contains', priority: 0, is_active: true };
        if (id) rule = await API.get(`/bank-rules/${id}`);
        if (prefill) {
            const { afterSave, from, ...fields } = prefill;
            rule = { ...rule, ...fields };
            BankRulesPage._afterSave = afterSave || null;
        } else {
            BankRulesPage._afterSave = null;
        }

        const accounts = await API.get('/accounts');
        const acctOpts = accounts
            .filter(a => ['expense','income','asset','liability','cogs'].includes(a.account_type))
            .map(a => `<option value="${a.id}" ${rule.account_id==a.id?'selected':''}>${accountLabel(a)}</option>`).join('');

        openModal(id ? 'Edit Bank Rule' : 'New Bank Rule', `
            <form onsubmit="BankRulesPage.save(event, ${id})">
                <div class="form-grid">
                    <div class="form-group"><label>Rule Name *</label>
                        <input name="name" required value="${escapeHtml(rule.name)}"></div>
                    <div class="form-group"><label>Payee Pattern *</label>
                        <input name="pattern" required value="${escapeHtml(rule.pattern)}" placeholder="e.g. AMZN or Amazon"></div>
                    ${prefill && prefill.from ? `<div class="form-group full-width form-hint" style="font-size:11px; color:var(--gray-500);">From ${escapeHtml(prefill.from)}: the pattern is its payee, matched by Contains in any case. A shorter pattern catches other spellings, and a very short one ("CO") catches too much.</div>` : ''}
                    <div class="form-group"><label>Match Type</label>
                        <select name="rule_type">
                            <option value="contains" ${rule.rule_type==='contains'?'selected':''}>Contains</option>
                            <option value="starts_with" ${rule.rule_type==='starts_with'?'selected':''}>Starts With</option>
                            <option value="exact" ${rule.rule_type==='exact'?'selected':''}>Exact Match</option>
                        </select></div>
                    <div class="form-group"><label>Category Account</label>
                        <select name="account_id"><option value="">-- None --</option>${acctOpts}</select></div>
                    <div class="form-group"><label>Priority</label>
                        <input name="priority" type="number" value="${rule.priority}" title="Higher = checked first"></div>
                    <div class="form-group"><label>Active</label>
                        <select name="is_active">
                            <option value="true" ${rule.is_active !== false ? 'selected' : ''}>Yes</option>
                            <option value="false" ${rule.is_active === false ? 'selected' : ''}>No</option>
                        </select></div>
                </div>
                <div class="form-actions">
                    <button type="button" class="btn btn-secondary" onclick="closeModal()">Cancel</button>
                    <button type="submit" class="btn btn-primary">${id ? 'Update' : 'Create'} Rule</button>
                </div>
            </form>`);
    },

    async save(e, id) {
        e.preventDefault();
        const data = Object.fromEntries(new FormData(e.target).entries());
        data.account_id = data.account_id ? parseInt(data.account_id) : null;
        data.vendor_id = data.vendor_id ? parseInt(data.vendor_id) : null;
        data.priority = parseInt(data.priority) || 0;
        data.is_active = data.is_active === 'true';
        try {
            if (id) { await API.put(`/bank-rules/${id}`, data); toast('Rule updated'); }
            else { await API.post('/bank-rules', data); toast('Rule created'); }
            const after = BankRulesPage._afterSave;
            BankRulesPage._afterSave = null;
            if (after) { await after(); return; }
            closeModal();
            App.navigate('#/bank-rules');
        } catch (err) { toast(err.message, 'error'); }
    },

    async deleteRule(id) {
        if (!confirm('Delete this rule?')) return;
        try {
            await API.del(`/bank-rules/${id}`);
            toast('Rule deleted');
            App.navigate('#/bank-rules');
        } catch (err) { toast(err.message, 'error'); }
    },

    async applyAll() {
        try {
            const result = await API.post('/bank-rules/apply');
            toast(`Matched ${result.matched} of ${result.total_unmatched} unmatched transactions`);
        } catch (err) { toast(err.message, 'error'); }
    },
};
