"use strict";

const tg = window.Telegram?.WebApp;
const content = document.querySelector("#content");
const navigation = document.querySelector("#navigation");
const notice = document.querySelector("#notice");
const confirmDialog = document.querySelector("#confirm-dialog");
const statusLabels = {running: "进行中", finished: "已开奖", canceled: "已取消", all: "全部"};
const modeLabels = {none: "主办方自行联系", unique: "独立码", shared: "共享文本"};
let activeFilter = "all";
let displayTimeZone = "";
let navigationVersion = 0;
let backAction = null;
let mutationInProgress = false;

function element(tag, text, classes) {
    const node = document.createElement(tag);
    if (text !== undefined) node.textContent = text;
    if (classes) node.className = classes;
    return node;
}

function button(text, classes, onClick) {
    const control = element("button", text, ["button", classes].filter(Boolean).join(" "));
    control.type = "button";
    if (onClick) control.addEventListener("click", onClick);
    return control;
}

function panel(title, ...children) {
    const section = element("section", undefined, "panel stack");
    if (title) section.append(element("h3", title));
    section.append(...children);
    return section;
}

function lines(values, fallback) {
    if (!values.length) return element("p", fallback, "hint");
    const list = element("ul", undefined, "plain-list");
    for (const value of values) {
        const line = element("li", undefined, "line-item");
        line.append(element("span", value));
        list.append(line);
    }
    return list;
}

function stats(values, classes = "") {
    const list = element("dl", undefined, ["details", classes].filter(Boolean).join(" "));
    for (const [label, value, valueClass] of values) {
        const item = element("div");
        item.append(element("dt", label), element("dd", String(value), valueClass));
        list.append(item);
    }
    return list;
}

function statusBadge(kind, label = statusLabels[kind]) {
    return element("span", label, "status " + kind);
}

function announce(message, warning = false) {
    notice.classList.toggle("warning", warning);
    notice.textContent = message;
}

function formError(node, message = "") {
    node.textContent = message;
    node.hidden = !message;
    if (message) node.focus();
}

async function api(path, payload) {
    const headers = new Headers({Authorization: "tma " + tg.initData});
    const options = {headers, cache: "no-store", credentials: "omit"};
    if (payload !== undefined) {
        headers.set("Content-Type", "application/json");
        options.method = "POST";
        options.body = JSON.stringify(payload);
    }
    let response;
    try {
        response = await fetch("/admin/api" + path, options);
    } catch {
        throw new Error("连接中断，请检查网络后重试。");
    }
    const data = await response.json().catch(() => null);
    if (!response.ok || !data) {
        const fallback = response.status === 401
            ? "登录已过期，请从机器人私聊菜单重新打开小程序。"
            : "暂时无法完成操作，请稍后重试。";
        const error = new Error(data?.error || fallback);
        error.status = response.status;
        throw error;
    }
    return data;
}

async function withButton(control, pendingText, callback, errorBox) {
    if (mutationInProgress) return;
    mutationInProgress = true;
    control.disabled = true;
    const original = control.textContent;
    control.textContent = pendingText;
    announce("");
    if (errorBox) formError(errorBox);
    try {
        await callback();
    } catch (error) {
        if (errorBox) formError(errorBox, error.message);
        else announce(error.message, true);
    } finally {
        mutationInProgress = false;
        control.textContent = original;
        control.disabled = false;
    }
}

async function confirmAction(title, description, action, danger = false) {
    if (typeof confirmDialog.showModal !== "function") {
        return window.confirm(title + "\n\n" + description);
    }
    document.querySelector("#confirm-title").textContent = title;
    document.querySelector("#confirm-body").textContent = description;
    const control = document.querySelector("#confirm-action");
    control.textContent = action;
    control.className = danger ? "button danger" : "button primary";
    confirmDialog.returnValue = "cancel";
    return new Promise(resolve => {
        confirmDialog.addEventListener("close", () => {
            resolve(confirmDialog.returnValue === "confirm");
        }, {once: true});
        confirmDialog.showModal();
    });
}

function setBack(action) {
    backAction = action;
    if (action) tg?.BackButton?.show();
    else tg?.BackButton?.hide();
}

function doBack() {
    if (confirmDialog.open) {
        confirmDialog.close("cancel");
    } else if (!mutationInProgress) {
        backAction?.();
    }
}

function moveToTop() {
    window.scrollTo(0, 0);
    content.querySelector("h2[tabindex]")?.focus({preventScroll: true});
}

function sectionHeading(title, backLabel) {
    const heading = element("div", undefined, "section-heading");
    if (backLabel) heading.append(button("‹ " + backLabel, "quiet back", doBack));
    const name = element("h2", title);
    name.tabIndex = -1;
    heading.append(name);
    return heading;
}

function showPageError(error, retry) {
    const heading = sectionHeading("暂时无法加载", backAction ? "返回" : null);
    const info = panel(null, element("p", error.message));
    if (![401, 403].includes(error.status) && retry) {
        info.append(button("重新加载", "secondary", retry));
    }
    content.replaceChildren(heading, info);
    moveToTop();
}

async function openPage(page, id) {
    const version = ++navigationVersion;
    announce("");
    const topLevel = page === "list" || page === "access";
    navigation.hidden = !topLevel;
    for (const tab of navigation.querySelectorAll("button")) {
        tab.setAttribute("aria-pressed", String(tab.dataset.page === page));
    }
    setBack(topLevel ? null : () => openPage("list"));
    content.setAttribute("aria-busy", "true");
    content.replaceChildren(panel(null, element("p", "正在加载…", "hint")));
    window.scrollTo(0, 0);
    try {
        if (page === "list") {
            renderList(id);
        } else if (page === "access") {
            const data = await api("/access");
            if (version === navigationVersion) renderAccess(data);
        } else if (page === "detail") {
            const data = await api("/giveaways/" + id);
            if (version === navigationVersion) renderDetail(data);
        } else if (page === "create") {
            renderCreate();
        }
    } catch (error) {
        if (version === navigationVersion) showPageError(error, () => openPage(page, id));
    } finally {
        if (version === navigationVersion) {
            content.setAttribute("aria-busy", "false");
            moveToTop();
        }
    }
}

function renderList(initial) {
    const hero = element("section", undefined, "hero");
    const intro = element("div");
    intro.append(
        element("h2", "发起一场抽奖"),
        element("p", "设置奖项、报名条件和发布位置。"),
    );
    hero.append(intro, button("＋ 新建抽奖", "primary", () => openPage("create")));
    const filters = element("div", undefined, "filters");
    filters.setAttribute("role", "group");
    filters.setAttribute("aria-label", "按抽奖状态筛选");
    const records = element("div", undefined, "record-list");
    const heading = sectionHeading("抽奖列表");
    const refresh = button("刷新", "quiet", () => loadRecords());
    heading.append(refresh);
    content.replaceChildren(hero, heading, filters, records);
    let fetchVersion = 0;

    for (const status of ["all", "running", "finished", "canceled"]) {
        const filter = button(statusLabels[status], "", () => {
            if (status === activeFilter) return;
            activeFilter = status;
            loadRecords();
        });
        filter.dataset.status = status;
        filters.append(filter);
    }

    async function loadRecords(before, more, snapshot) {
        const version = ++fetchVersion;
        for (const filter of filters.children) {
            filter.setAttribute("aria-pressed", String(filter.dataset.status === activeFilter));
        }
        if (!before) records.replaceChildren(panel(null, element("p", "正在加载…", "hint")));
        if (more) {
            more.disabled = true;
            more.textContent = "正在加载…";
        }
        records.setAttribute("aria-busy", "true");
        const query = new URLSearchParams({status: activeFilter});
        if (before) query.set("before", before);
        try {
            const result = snapshot || await api("/giveaways?" + query);
            if (!records.isConnected || version !== fetchVersion) return;
            displayTimeZone = result.tz;
            if (!before) records.replaceChildren();
            else more.remove();
            for (const giveaway of result.items) records.append(giveawayRecord(giveaway));
            if (!records.children.length) {
                records.append(element("p", "暂无这一状态的抽奖。", "panel empty"));
            }
            if (result.next_before) {
                const next = button("加载更多", "secondary", () => loadRecords(result.next_before, next));
                records.append(next);
            }
        } catch (error) {
            if (!records.isConnected || version !== fetchVersion) return;
            if (more) {
                more.disabled = false;
                more.textContent = "加载更多";
                announce(error.message, true);
            } else {
                records.replaceChildren(panel(null, element("p", error.message)));
            }
        } finally {
            if (version === fetchVersion) records.setAttribute("aria-busy", "false");
        }
    }

    loadRecords(undefined, undefined, initial);
}

function giveawayRecord(giveaway) {
    const card = button("", "panel record", () => openPage("detail", giveaway.id));
    const top = element("span", undefined, "record-top");
    top.append(element("strong", giveaway.title), statusBadge(
        giveaway.publishing && giveaway.status === "running" ? "publishing" : giveaway.status,
        giveaway.publishing && giveaway.status === "running" ? "发布中" : statusLabels[giveaway.status],
    ));
    card.append(
        top,
        element("span", "#" + giveaway.id + " · " + giveaway.participants + " 人有效报名", "hint"),
        element("span", giveaway.condition, "hint"),
    );
    return card;
}

function renderDetail(giveaway) {
    const header = sectionHeading(giveaway.title, "抽奖列表");
    const published = giveaway.posts.filter(post => post.sent).length;
    const pending = giveaway.posts.length - published;
    const isOpen = giveaway.status === "running";
    const status = isOpen && giveaway.publishing ? "发布中" : statusLabels[giveaway.status];
    const detail = panel(null, stats([
        ["抽奖编号", "#" + giveaway.id],
        ["状态", status],
        ["有效报名", giveaway.participants + " 人", "data-number"],
        ["开奖条件", giveaway.condition],
    ], "detail-stats"));
    if (giveaway.description) detail.append(element("p", giveaway.description, "prewrap"));

    const publications = element("ul", undefined, "plain-list");
    for (const post of giveaway.posts) {
        const row = element("li", undefined, "line-item");
        row.append(element("span", post.name), statusBadge(
            post.sent ? "sent" : "failed", post.sent ? "已发布" : "未发布",
        ));
        publications.append(row);
    }
    const destinations = panel("发布状态", publications.children.length
        ? publications
        : element("p", "没有自动发布记录。", "hint"));
    const requirements = panel("报名须加入");
    if (giveaway.channels.length) requirements.append(element("p", "频道", "hint"), lines(giveaway.channels, ""));
    if (giveaway.groups.length) requirements.append(element("p", "群组", "hint"), lines(giveaway.groups, ""));
    if (!giveaway.channels.length && !giveaway.groups.length) {
        requirements.append(element("p", "无", "hint"));
    }

    const actions = element("div", undefined, "button-row");
    if (isOpen) {
        if (pending) actions.append(giveawayAction(
            giveaway, "republish", "补发帖子", "将补发 " + pending + " 个尚未发布的位置。",
        ));
        actions.append(giveawayAction(
            giveaway, "cancel", "取消抽奖",
            "取消后将停止报名，这场抽奖不会开奖。", {danger: true},
        ));
    }
    if (giveaway.status === "finished" && giveaway.pending_notifications) {
        actions.append(giveawayAction(
            giveaway, "retry", "补发中奖私信",
            "将重试发送 " + giveaway.pending_notifications + " 条待补发的中奖私信。",
        ));
    }

    const actionPanel = panel("抽奖操作");
    if (actions.children.length) actionPanel.append(actions);
    if (giveaway.status === "finished") {
        if (!giveaway.pending_notifications) actionPanel.append(element("p", "没有待补发的中奖私信。", "hint"));
        if (giveaway.tiers.length) {
            const tier = tierSelect(giveaway.tiers, "选择要重抽的奖项");
            const reroll = giveawayAction(
                giveaway, "reroll", "重抽该奖项",
                () => "将替换「" + tier.value + "」的中奖名单，机器人会发送新通知。",
                {danger: true, tier: () => tier.value},
            );
            const selector = element("label", "重抽奖项");
            selector.append(tier);
            actionPanel.append(selector, reroll);
        }
    }
    const sections = [header, detail, destinations, requirements, renderStock(giveaway)];
    if (actionPanel.children.length > 1) sections.push(actionPanel);
    sections.push(renderGate(giveaway.gate));
    content.replaceChildren(...sections);
}

function giveawayAction(giveaway, action, title, description, settings = {}) {
    const control = button(title, settings.danger ? "danger" : "secondary", async () => {
        const explanation = typeof description === "function" ? description() : description;
        if (!await confirmAction(title, explanation, title, settings.danger)) return;
        await withButton(control, "正在处理…", async () => {
            const payload = {action};
            if (settings.tier) payload.tier = settings.tier();
            const result = await api("/giveaways/" + giveaway.id + "/actions", payload);
            await openPage("detail", giveaway.id);
            announce(result.message);
        });
    });
    return control;
}

function tierSelect(tiers, name) {
    const select = element("select");
    select.setAttribute("aria-label", name);
    for (const tier of tiers) {
        const option = element("option", tier.name);
        option.value = tier.name;
        select.append(option);
    }
    return select;
}

function renderGate(gate) {
    const values = [
        ["用户编号检查", gate.enabled ? "已开启" : "已关闭"],
        ["编号下限", gate.min_user_id || "未设置"],
    ];
    const distribution = gate.distribution;
    if (distribution) values.push(
        ["样本人数", distribution.count],
        ["最小编号", distribution.low],
        ["中位编号", distribution.median],
        ["最大编号", distribution.high],
        ["低于下限", distribution.below],
    );
    return panel("报名编号门槛", stats(values, "gate-grid"));
}

function renderStock(giveaway) {
    const section = panel("奖池库存", element("p", modeLabels[giveaway.prize_mode], "hint"));
    if (!giveaway.tiers.length) {
        section.append(element("p", "暂无奖项。", "hint"));
        return section;
    }
    const tierLines = giveaway.tiers.map(tier => {
        const planned = tier.name + " · " + tier.count + " 个名额";
        if (giveaway.prize_mode === "none") return planned;
        if (giveaway.prize_mode === "shared") {
            return planned + " · " + (tier.total ? "已录入共享文本" : "暂无共享文本")
                + " · " + tier.claimed + " 人已领取";
        }
        return planned + " · 剩余 " + tier.remaining + " 条 / 共 " + tier.total
            + " 条 · 已领 " + tier.claimed + " 条";
    });
    section.append(lines(tierLines, ""));
    if (giveaway.prize_mode === "none") return section;

    const details = element("details", undefined, "stock-controls");
    details.append(element("summary", "补充库存", "button secondary"));
    const form = element("form", undefined, "stack");
    form.autocomplete = "off";
    const choice = element("label", "奖项");
    const tier = tierSelect(giveaway.tiers, "选择要补货的奖项");
    choice.append(tier);
    const codeLabel = element("label", giveaway.prize_mode === "shared" ? "共享文本" : "独立码，每行一条");
    const codes = element("textarea");
    codes.rows = 4;
    codes.required = true;
    codes.spellcheck = false;
    codeLabel.append(codes);
    const submit = button("核对补货数量", "secondary");
    submit.type = "submit";
    form.append(choice, codeLabel, submit);
    form.addEventListener("submit", event => {
        event.preventDefault();
        withButton(submit, "正在核对…", async () => {
            const path = "/giveaways/" + giveaway.id + "/actions";
            const payload = {action: "stock-preview", tier: tier.value, codes: codes.value};
            const preview = await api(path, payload);
            const confirmed = await confirmAction("确认补货", preview.message, "确认入库");
            if (!confirmed) return;
            submit.textContent = "正在入库…";
            const result = await api(path, {...payload, action: "add-stock"});
            await openPage("detail", giveaway.id);
            announce(result.message);
        });
    });
    details.append(form);
    section.append(details);
    return section;
}

function renderCreate() {
    const fragment = document.querySelector("#create-template").content.cloneNode(true);
    const form = fragment.querySelector("form");
    const draft = {dirty: false, preview: null, pending: false};
    const errorBox = form.querySelector("#create-error");
    const tierList = form.querySelector("#tier-list");
    const addTier = form.querySelector("#add-tier");
    const resetCodes = form.querySelector("#reset-codes");
    const previewButton = form.querySelector("#preview-button");
    const fields = form.elements;
    form.querySelector("#timezone").textContent = displayTimeZone ? "（" + displayTimeZone + "）" : "";
    form.querySelector(".section-heading").prepend(button("‹ 抽奖列表", "quiet back", doBack));
    content.replaceChildren(form);

    function markDirty() {
        draft.dirty = true;
        tg?.enableClosingConfirmation?.();
    }

    function updateTime() {
        const timed = fields.draw_mode.value !== "count";
        const count = fields.draw_mode.value !== "time";
        const relative = fields.time_mode.value === "relative";
        form.querySelector("#time-mode-field").hidden = !timed;
        form.querySelector("#deadline-field").hidden = !timed || relative;
        form.querySelector("#duration-field").hidden = !timed || !relative;
        form.querySelector("#target-field").hidden = !count;
        fields.deadline.required = timed && !relative;
        fields.deadline.disabled = !timed || relative;
        fields.duration.required = timed && relative;
        fields.duration.disabled = !timed || !relative;
        fields.target.required = count;
        fields.target.disabled = !count;
    }

    function updateTiers() {
        const mode = fields.prize_mode.value;
        const tiers = [...tierList.children];
        resetCodes.hidden = mode === "none";
        for (const [index, tier] of tiers.entries()) {
            tier.querySelector("legend").textContent = "奖项 " + (index + 1);
            const remove = tier.querySelector(".remove-tier");
            remove.setAttribute("aria-label", "移除奖项 " + (index + 1));
            remove.hidden = tiers.length === 1;
            const codesLabel = tier.querySelector(".codes-field");
            codesLabel.hidden = mode === "none";
            codesLabel.querySelector("span").textContent = mode === "unique" ? "独立码，每行一条" : "共享文本";
            const raw = tier.querySelector("[name=tier_codes]").value.trim();
            const planned = Number(tier.querySelector("[name=tier_count]").value);
            let inventory = "";
            if (mode === "unique") {
                const count = raw === "-" ? 0 : new Set(raw
                    .split(/[\r\n\v\f\x1c-\x1e\x85\u2028\u2029]+/)
                    .map(line => line.trim()).filter(Boolean)).size;
                inventory = "已识别 " + count + " 条";
                if (planned > 0 && count < planned) inventory += " · 少 " + (planned - count) + " 条";
                if (planned > 0 && count > planned) inventory += " · 多 " + (count - planned) + " 条";
            } else if (mode === "shared") {
                inventory = raw && raw !== "-" ? "已录入 1 份共享文本" : "尚未录入共享文本";
            }
            tier.querySelector(".inventory").textContent = inventory;
        }
    }

    function addTierRow(focus) {
        const tier = document.querySelector("#tier-template").content.firstElementChild.cloneNode(true);
        tier.querySelector(".remove-tier").addEventListener("click", () => {
            tier.remove();
            updateTiers();
            markDirty();
            addTier.focus();
        });
        tierList.append(tier);
        updateTiers();
        if (focus) tier.querySelector("[name=tier_name]").focus();
    }

    function returnToForm() {
        if (draft.pending) {
            announce("发布状态尚未确认，请先重试本次发布。", true);
            return;
        }
        announce("");
        draft.preview = null;
        content.replaceChildren(form);
        moveToTop();
        setBack(async () => {
            if (draft.dirty && !await confirmAction(
                "离开新建抽奖？", "当前草稿会随页面关闭而丢失。", "离开", true,
            )) return;
            tg?.disableClosingConfirmation?.();
            openPage("list");
        });
    }

    function collectDraft() {
        const timed = fields.draw_mode.value !== "count";
        const count = fields.draw_mode.value !== "time";
        const relative = fields.time_mode.value === "relative";
        const deadline = relative ? Number(fields.duration.value) + fields.duration_unit.value : fields.deadline.value;
        const condition = [timed ? deadline : "", count ? "满" + Number(fields.target.value) + "人" : ""]
            .filter(Boolean).join("或");
        return {
            title: fields.title.value.trim(),
            description: fields.description.value,
            channels: fields.channels.value,
            groups: fields.groups.value,
            condition,
            prize_mode: fields.prize_mode.value,
            tiers: Array.from(tierList.children, tier => ({
                name: tier.querySelector("[name=tier_name]").value.trim(),
                count: Number(tier.querySelector("[name=tier_count]").value),
                codes: fields.prize_mode.value === "none" ? "" : tier.querySelector("[name=tier_codes]").value,
            })),
        };
    }

    form.addEventListener("input", () => {
        markDirty();
        updateTime();
        updateTiers();
    });
    addTier.addEventListener("click", () => {
        addTierRow(true);
        markDirty();
    });
    resetCodes.addEventListener("click", async () => {
        if (!await confirmAction(
            "清空全部奖品内容？", "各奖项名称和中奖人数会保留。", "清空奖品内容", true,
        )) return;
        for (const field of tierList.querySelectorAll("[name=tier_codes]")) field.value = "";
        updateTiers();
        markDirty();
    });
    form.addEventListener("submit", event => {
        event.preventDefault();
        withButton(previewButton, "正在核对…", async () => {
            const request = collectDraft();
            const result = await api("/create/preview", request);
            draft.preview = {request: {...request, condition: result.fixed_condition, request_id: crypto.randomUUID()}};
            showPreview(draft, result, returnToForm);
        }, errorBox);
    });

    addTierRow(false);
    updateTime();
    returnToForm();
}

function showPreview(draft, preview, returnToForm) {
    const {request} = draft.preview;
    const summary = panel(request.title, stats([
        ["开奖条件", preview.condition],
        ["发奖方式", modeLabels[request.prize_mode]],
    ]));
    if (request.description.trim()) summary.append(element("p", request.description, "prewrap"));
    const list = panel("奖项核对", lines(preview.tiers.map(tier => {
        const seats = tier.name + " · " + tier.count + " 个名额";
        const mode = request.prize_mode;
        if (mode === "none") return seats;
        const inventory = mode === "shared" ? " 份共享文本" : " 条独立码";
        return seats + " · 已录入 " + tier.inventory + inventory;
    }), ""));
    if (preview.tiers.some(tier =>
        (request.prize_mode === "unique" && tier.inventory !== tier.count)
        || (request.prize_mode === "shared" && !tier.inventory),
    )) {
        list.append(element("p", "部分奖项的奖品库存与中奖名额需核对。", "inventory-warning hint"));
    }
    const destinations = panel("将发布到", lines(preview.destinations, "没有发布位置。"));
    const errorBox = element("p", "", "form-error");
    errorBox.hidden = true;
    errorBox.setAttribute("role", "alert");
    errorBox.tabIndex = -1;
    const submit = button("确认并发布", "primary full", () => {
        withButton(submit, "正在发布…", async () => {
            let result;
            try {
                result = await api("/create", request);
            } catch (error) {
                if (!error.status || error.status >= 500 || error.status < 400 || error.status === 408) {
                    draft.pending = true;
                    throw new Error("发布状态暂未确认。请留在本页，点击「确认并发布」重试同一次发布。");
                }
                if (error.status === 400) draft.pending = false;
                throw error;
            }
            draft.pending = false;
            tg?.disableClosingConfirmation?.();
            await openPage("detail", result.id);
            if (result.canceled) {
                announce("所有位置发布失败，抽奖已取消。", true);
            } else if (result.failed.length) {
                announce("已发布 " + result.sent + " 处。未发布：" + result.failed.join("、"), true);
            } else {
                announce("抽奖已发布到 " + result.sent + " 处。");
            }
        }, errorBox);
    });
    setBack(returnToForm);
    content.replaceChildren(
        sectionHeading("预览抽奖", "继续编辑"), summary, destinations, list, errorBox, submit,
    );
    moveToTop();
}

function textField(title, {name, pattern, maxLength, required, type = "text", inputMode}) {
    const control = element("input");
    Object.assign(control, {type, name, required: Boolean(required)});
    if (pattern) control.pattern = pattern;
    if (maxLength) control.maxLength = maxLength;
    if (inputMode) control.inputMode = inputMode;
    const label = element("label", title);
    label.append(control);
    return label;
}

function makeAccessForm(title, options) {
    const form = element("form", undefined, "stack");
    form.autocomplete = "off";
    form.append(textField("Telegram 用户编号", {
        name: "user_id", pattern: "[1-9][0-9]{0,18}", inputMode: "numeric", maxLength: 19, required: true,
    }));
    if (options.some(option => option.action === "ban")) {
        form.append(textField("拉黑原因（选填）", {name: "reason", maxLength: 512}));
    }
    const controls = element("div", undefined, "button-row");
    const errorBox = element("p", "", "form-error");
    errorBox.hidden = true;
    errorBox.setAttribute("role", "alert");
    errorBox.tabIndex = -1;
    for (const option of options) {
        const control = button(option.label, option.danger ? "danger" : "secondary");
        Object.assign(control, {type: "submit", value: option.action});
        controls.append(control);
    }
    form.append(errorBox, controls);
    form.addEventListener("submit", async event => {
        event.preventDefault();
        if (mutationInProgress) return;
        const submitter = event.submitter || controls.firstElementChild;
        const option = options.find(item => item.action === submitter.value);
        const userId = form.elements.user_id.value;
        const payload = {action: option.action, user_id: userId};
        if (option.action === "ban") payload.reason = form.elements.reason.value.trim();
        if (!await confirmAction(option.label + "？", "用户编号 " + userId + "。"
            + option.description, option.label, option.danger)) return;
        withButton(submitter, "正在处理…", async () => {
            const result = await api("/access", payload);
            if (option.action === "add-admin") await openPage("access");
            else form.reset();
            announce(result.message);
        }, errorBox);
    });
    return panel(title, form);
}

function renderAccess(data) {
    const administrators = panel("管理员", lines(data.admins.map(admin => admin.id
        + (admin.configured ? " · 部署配置" : "")), "暂无管理员记录。"));
    const add = makeAccessForm("添加管理员", [{
        action: "add-admin", label: "添加管理员",
        description: "该账号将获得抽奖管理权限。", danger: true,
    }]);
    const blacklist = makeAccessForm("黑名单", [{
        action: "ban", label: "拉黑用户",
        description: "将清除该用户已有的报名记录，并阻止继续报名。", danger: true,
    }, {
        action: "unban", label: "解除拉黑",
        description: "解除后可重新报名。",
    }]);
    content.replaceChildren(sectionHeading("权限管理"), administrators, add, blacklist, renderGate(data.gate));
}

function applyTheme() {
    document.documentElement.dataset.theme = tg?.colorScheme ||
        (window.matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light");
    const background = getComputedStyle(document.documentElement).getPropertyValue("--canvas").trim();
    if (tg?.isVersionAtLeast?.("6.1")) tg.setBackgroundColor?.(background);
    if (tg?.isVersionAtLeast?.("6.9")) tg.setHeaderColor?.(background);
}

async function initialize() {
    applyTheme();
    tg?.onEvent?.("themeChanged", applyTheme);
    tg?.BackButton?.onClick(doBack);
    tg?.ready?.();
    tg?.expand?.();
    if (!tg?.initData) {
        content.setAttribute("aria-busy", "false");
        showPageError(new Error("请从 Telegram 机器人私聊里的「抽奖管理」菜单打开。"));
        return;
    }
    announce("");
    content.replaceChildren(panel(null, element("p", "正在加载抽奖…", "hint")));
    content.setAttribute("aria-busy", "true");
    try {
        const initial = await api("/giveaways?status=" + activeFilter);
        await openPage("list", initial);
    } catch (error) {
        navigation.hidden = true;
        content.setAttribute("aria-busy", "false");
        showPageError(error, initialize);
    }
}

navigation.addEventListener("click", event => {
    const tab = event.target.closest("button[data-page]");
    if (tab && !mutationInProgress && tab.getAttribute("aria-pressed") !== "true") {
        openPage(tab.dataset.page);
    }
});

initialize();
