"use strict";

const tg = window.Telegram?.WebApp;
const content = document.querySelector("#content");
const navigation = document.querySelector("#navigation");
const notice = document.querySelector("#notice");
const noticeIcon = notice.querySelector(".notice-icon");
const noticeText = notice.querySelector(".notice-text");
const noticeClose = document.querySelector("#notice-close");
const themeToggle = document.querySelector("#theme-toggle");
const tzBadge = document.querySelector("#tz-badge");
const confirmDialog = document.querySelector("#confirm-dialog");

const statusLabels = {running: "进行中", finished: "已开奖", canceled: "已取消", all: "全部"};
const modeLabels = {none: "主办方自行联系", unique: "独立码", shared: "共享文本"};
const defaultTierNames = ["一等奖", "二等奖", "三等奖", "四等奖", "五等奖"];
const SVG_NS = "http://www.w3.org/2000/svg";

let activeFilter = "all";
let activeSearch = "";
let displayTimeZone = "";
let themeOverride = null;
let navigationVersion = 0;
let backAction = null;
let mutationInProgress = false;
let noticeTimer = null;
let tierDomCounter = 0;

function element(tag, text, classes) {
    const node = document.createElement(tag);
    if (text !== undefined && text !== null) node.textContent = text;
    if (classes) node.className = classes;
    return node;
}

function svgIcon(kind) {
    const svg = document.createElementNS(SVG_NS, "svg");
    svg.setAttribute("viewBox", "0 0 24 24");
    svg.setAttribute("fill", "none");
    svg.setAttribute("stroke", "currentColor");
    svg.setAttribute("stroke-width", "2");
    svg.setAttribute("stroke-linecap", "round");
    svg.setAttribute("stroke-linejoin", "round");
    svg.setAttribute("aria-hidden", "true");
    const paths = {
        check: ["M20 6 9 17l-5-5"],
        warn: [
            "M10.29 3.86 1.82 18a2 2 0 0 0 1.71 3h16.94a2 2 0 0 0 1.71-3L13.71 3.86a2 2 0 0 0-3.42 0z",
            "M12 9v4",
            "M12 17h.01",
        ],
        refresh: [
            "M21 12a9 9 0 0 0-9-9 9.75 9.75 0 0 0-6.74 2.74L3 8",
            "M3 3v5h5",
            "M3 12a9 9 0 0 0 9 9 9.75 9.75 0 0 0 6.74-2.74L21 16",
            "M16 16h5v5",
        ],
        copy: [
            "M8 8h12v12H8z",
            "M4 16c-1.1 0-2-.9-2-2V4c0-1.1.9-2 2-2h10c1.1 0 2 .9 2 2",
        ],
        external: [
            "M15 3h6v6",
            "M10 14 21 3",
            "M18 13v6a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V8a2 2 0 0 1 2-2h6",
        ],
        empty: [
            "M20 12v10H4V12",
            "M2 7h20v5H2z",
            "M12 22V7",
            "M12 7H7.5a2.5 2.5 0 0 1 0-5C11 2 12 7 12 7z",
            "M12 7h4.5a2.5 2.5 0 0 0 0-5C13 2 12 7 12 7z",
        ],
        chevron: ["m6 9 6 6 6-6"],
    }[kind] || [];
    for (const d of paths) {
        const path = document.createElementNS(SVG_NS, "path");
        path.setAttribute("d", d);
        svg.append(path);
    }
    return svg;
}

function haptic(type) {
    try {
        const api = tg?.HapticFeedback;
        if (!api) return;
        if (type === "selection") api.selectionChanged?.();
        else if (type === "success" || type === "error" || type === "warning") {
            api.notificationOccurred?.(type);
        } else {
            api.impactOccurred?.(type || "light");
        }
    } catch {
        // Ignore on unsupported Telegram clients.
    }
}

function button(text, classes, onClick, iconKind) {
    const control = element("button", undefined, ["button", classes].filter(Boolean).join(" "));
    control.type = "button";
    if (iconKind) control.append(svgIcon(iconKind));
    if (text) control.append(element("span", text));
    if (onClick) {
        control.addEventListener("click", event => {
            haptic("light");
            onClick(event);
        });
    }
    return control;
}

function setButtonBusy(control, busyText) {
    const labelNode = control.querySelector("span") || control;
    const original = labelNode.textContent;
    labelNode.textContent = busyText;
    control.disabled = true;
    return () => {
        labelNode.textContent = original;
        control.disabled = false;
    };
}

function panel(title, ...children) {
    const section = element("section", undefined, "panel stack");
    if (title) section.append(element("h3", title));
    section.append(...children);
    return section;
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

function progressBar(value, max, variant = "") {
    const bar = document.createElement("progress");
    bar.className = ["progress", variant].filter(Boolean).join(" ");
    bar.max = Math.max(1, Number(max) || 1);
    bar.value = Math.min(bar.max, Math.max(0, Number(value) || 0));
    return bar;
}

function statusBadge(kind, label = statusLabels[kind]) {
    return element("span", label, "status " + kind);
}

function uuid4() {
    if (window.crypto?.randomUUID) return window.crypto.randomUUID();
    const bytes = new Uint8Array(16);
    window.crypto.getRandomValues(bytes);
    bytes[6] = (bytes[6] & 0x0f) | 0x40;
    bytes[8] = (bytes[8] & 0x3f) | 0x80;
    const hex = [...bytes].map(b => b.toString(16).padStart(2, "0")).join("");
    return `${hex.slice(0, 8)}-${hex.slice(8, 12)}-${hex.slice(12, 16)}-${hex.slice(16, 20)}-${hex.slice(20)}`;
}

function updateTzBadge() {
    tzBadge.textContent = displayTimeZone
        ? "古诗词防脚本 · " + displayTimeZone
        : "古诗词防脚本抽奖";
}

function formatDateTime(iso) {
    if (!iso) return "";
    const date = new Date(iso);
    if (Number.isNaN(date.getTime())) return "";
    try {
        return new Intl.DateTimeFormat("zh-CN", {
            timeZone: displayTimeZone || undefined,
            year: "numeric",
            month: "2-digit",
            day: "2-digit",
            hour: "2-digit",
            minute: "2-digit",
            hour12: false,
        }).format(date);
    } catch {
        return date.toLocaleString("zh-CN");
    }
}

function formatLocalDateTimeInput(date, timeZone) {
    try {
        const parts = new Intl.DateTimeFormat("en-CA", {
            timeZone: timeZone || undefined,
            year: "numeric",
            month: "2-digit",
            day: "2-digit",
            hour: "2-digit",
            minute: "2-digit",
            hourCycle: "h23",
        }).formatToParts(date);
        const map = Object.fromEntries(parts.map(part => [part.type, part.value]));
        const hour = map.hour === "24" ? "00" : map.hour;
        return `${map.year}-${map.month}-${map.day}T${hour}:${map.minute}`;
    } catch {
        const local = new Date(date.getTime() - date.getTimezoneOffset() * 60000);
        return local.toISOString().slice(0, 16);
    }
}

function formatCountdown(iso) {
    if (!iso) return null;
    const ms = new Date(iso).getTime() - Date.now();
    if (Number.isNaN(ms)) return null;
    if (ms <= 0) return {text: "已到开奖时间", overdue: true};
    const minutes = Math.ceil(ms / 60000);
    if (minutes < 60) return {text: `剩余 ${minutes} 分钟`, overdue: false};
    const hours = Math.floor(minutes / 60);
    const remMins = minutes % 60;
    if (hours < 24) {
        return {
            text: remMins ? `剩余 ${hours} 小时 ${remMins} 分` : `剩余 ${hours} 小时`,
            overdue: false,
        };
    }
    const days = Math.floor(hours / 24);
    const remHours = hours % 24;
    return {
        text: remHours ? `剩余 ${days} 天 ${remHours} 小时` : `剩余 ${days} 天`,
        overdue: false,
    };
}

function formatPostError(code) {
    if (!code) return "请检查机器人是否已加入该位置并拥有发送消息权限。";
    const hints = {
        TelegramForbiddenError: "机器人未加入该频道/群组或已被移出。",
        TelegramBadRequest: "目标地址无效或缺少发送消息权限。",
        TelegramRetryAfter: "触发 Telegram 发送频率限制，请稍后重试。",
    };
    return hints[code] || `发送异常（${code}）`;
}

function announce(message, warning = false) {
    if (noticeTimer) {
        clearTimeout(noticeTimer);
        noticeTimer = null;
    }
    if (!message) {
        notice.hidden = true;
        notice.classList.remove("warning");
        noticeText.textContent = "";
        noticeIcon.replaceChildren();
        return;
    }
    notice.classList.toggle("warning", warning);
    noticeIcon.replaceChildren(svgIcon(warning ? "warn" : "check"));
    noticeText.textContent = message;
    notice.hidden = false;
    if (warning) haptic("error");
    else {
        haptic("success");
        noticeTimer = setTimeout(() => announce(""), 5000);
    }
}

async function copyText(value, label) {
    try {
        if (navigator.clipboard?.writeText) {
            await navigator.clipboard.writeText(value);
            announce(`已复制${label}：${value}`);
            return;
        }
    } catch {
        // Fallback below.
    }
    const helper = document.createElement("textarea");
    helper.value = value;
    helper.setAttribute("readonly", "");
    helper.className = "sr-copy";
    document.body.append(helper);
    helper.select();
    try {
        document.execCommand("copy");
        announce(`已复制${label}：${value}`);
    } catch {
        announce(`无法自动复制，请长按手动复制：${value}`, true);
    } finally {
        helper.remove();
    }
}

function formError(node, message = "") {
    node.textContent = message;
    node.hidden = !message;
    if (message) {
        haptic("error");
        node.focus();
    }
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
    const restore = setButtonBusy(control, pendingText);
    announce("");
    if (errorBox) formError(errorBox);
    try {
        await callback();
    } catch (error) {
        if (errorBox) formError(errorBox, error.message);
        else announce(error.message, true);
    } finally {
        mutationInProgress = false;
        restore();
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

function skeletonCards(count = 3) {
    const box = element("div", undefined, "record-list");
    for (let i = 0; i < count; i += 1) {
        const card = element("div", undefined, "panel skeleton-card");
        card.append(
            element("div", undefined, "skeleton-line wide"),
            element("div", undefined, "skeleton-line medium"),
            element("div", undefined, "skeleton-line short"),
        );
        box.append(card);
    }
    return box;
}

function showPageError(error, retry) {
    const heading = sectionHeading("暂时无法加载", backAction ? "返回" : null);
    const info = panel(null, element("p", error.message));
    if (![401, 403].includes(error.status) && retry) {
        info.append(button("重新加载", "secondary", retry, "refresh"));
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
    content.replaceChildren(skeletonCards(page === "list" ? 3 : 2));
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
    const intro = element("div", undefined, "hero-copy");
    const heroStats = element("div", undefined, "hero-stats");
    intro.append(
        element("h2", "抽奖控制台"),
        element("p", "古诗词答题防脚本 · 多频道同步发布 · 可验证开奖"),
        heroStats,
    );
    hero.append(intro, button("＋ 新建抽奖", "primary", () => openPage("create")));

    const heading = sectionHeading("抽奖列表");
    const refresh = button("刷新", "quiet compact", () => loadRecords(), "refresh");
    heading.append(refresh);

    const toolbar = element("div", undefined, "toolbar");
    const searchWrap = element("div", undefined, "search-wrap");
    const searchIcon = element("span", undefined, "search-icon");
    const searchSvg = document.createElementNS(SVG_NS, "svg");
    searchSvg.setAttribute("viewBox", "0 0 24 24");
    searchSvg.setAttribute("fill", "none");
    searchSvg.setAttribute("stroke", "currentColor");
    searchSvg.setAttribute("stroke-width", "2");
    searchSvg.setAttribute("aria-hidden", "true");
    const circle = document.createElementNS(SVG_NS, "circle");
    circle.setAttribute("cx", "11");
    circle.setAttribute("cy", "11");
    circle.setAttribute("r", "8");
    const line = document.createElementNS(SVG_NS, "path");
    line.setAttribute("d", "m21 21-4.3-4.3");
    searchSvg.append(circle, line);
    searchIcon.append(searchSvg);

    const searchInput = element("input", undefined, "search-input");
    searchInput.type = "search";
    searchInput.placeholder = "搜索抽奖标题或 #编号…";
    searchInput.value = activeSearch;
    searchInput.setAttribute("aria-label", "搜索抽奖");

    const searchClear = element("button", "×", "search-clear");
    searchClear.type = "button";
    searchClear.setAttribute("aria-label", "清除搜索");
    searchClear.hidden = !activeSearch;
    searchWrap.append(searchIcon, searchInput, searchClear);

    const filters = element("div", undefined, "filters");
    filters.setAttribute("role", "group");
    filters.setAttribute("aria-label", "按抽奖状态筛选");

    const filterButtons = {};
    for (const status of ["all", "running", "finished", "canceled"]) {
        const filter = element("button");
        filter.type = "button";
        filter.dataset.status = status;
        const labelSpan = element("span", statusLabels[status]);
        const countSpan = element("span", "", "filter-count");
        filter.append(labelSpan, countSpan);
        filter.addEventListener("click", () => {
            if (status === activeFilter) return;
            haptic("selection");
            activeFilter = status;
            loadRecords();
        });
        filterButtons[status] = {button: filter, count: countSpan};
        filters.append(filter);
    }

    toolbar.append(searchWrap, filters);
    const records = element("div", undefined, "record-list");
    content.replaceChildren(hero, heading, toolbar, records);

    let fetchVersion = 0;
    let searchTimer = null;

    function updateSummaryCounts(counts) {
        if (!counts) return;
        for (const [key, refs] of Object.entries(filterButtons)) {
            const count = counts[key];
            refs.count.textContent = count !== undefined ? String(count) : "";
        }
        heroStats.replaceChildren(
            element("span", `进行中 ${counts.running ?? 0}`, "hero-pill"),
            element("span", `已开奖 ${counts.finished ?? 0}`, "hero-pill"),
            element("span", `总场次 ${counts.all ?? 0}`, "hero-pill"),
        );
    }

    searchInput.addEventListener("input", () => {
        activeSearch = searchInput.value;
        searchClear.hidden = !activeSearch;
        if (searchTimer) clearTimeout(searchTimer);
        searchTimer = setTimeout(() => loadRecords(), activeSearch ? 260 : 0);
    });
    searchClear.addEventListener("click", () => {
        activeSearch = "";
        searchInput.value = "";
        searchClear.hidden = true;
        if (searchTimer) clearTimeout(searchTimer);
        loadRecords();
        searchInput.focus();
    });

    async function loadRecords(before, more, snapshot) {
        const version = ++fetchVersion;
        for (const filter of filters.children) {
            filter.setAttribute("aria-pressed", String(filter.dataset.status === activeFilter));
        }
        if (!before && !snapshot) records.replaceChildren(skeletonCards(3));
        if (more) {
            more.disabled = true;
            more.textContent = "正在加载…";
        }
        records.setAttribute("aria-busy", "true");
        const query = new URLSearchParams({status: activeFilter});
        if (before) query.set("before", before);
        if (activeSearch.trim()) query.set("q", activeSearch.trim());
        try {
            const result = snapshot || await api("/giveaways?" + query);
            if (!records.isConnected || version !== fetchVersion) return;
            displayTimeZone = result.tz || displayTimeZone;
            updateTzBadge();
            updateSummaryCounts(result.counts);
            if (!before) records.replaceChildren();
            else more.remove();
            for (const giveaway of result.items) records.append(giveawayRecord(giveaway));
            if (!records.children.length) {
                const empty = element("div", undefined, "panel empty-state");
                const icon = element("span", undefined, "empty-icon");
                icon.append(svgIcon("empty"));
                const msg = activeSearch.trim()
                    ? `没有找到包含「${activeSearch.trim()}」的抽奖。`
                    : "暂无这一状态的抽奖。";
                empty.append(icon, element("p", msg, "hint"));
                if (activeSearch.trim()) {
                    empty.append(button("清除搜索", "secondary compact", () => searchClear.click()));
                } else if (activeFilter === "all") {
                    empty.append(button("立即新建抽奖", "secondary compact", () => openPage("create")));
                }
                records.append(empty);
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

    loadRecords(undefined, undefined, initial && !activeSearch.trim() ? initial : undefined);
}

function giveawayRecord(giveaway) {
    const card = element("button", undefined, "panel record");
    card.type = "button";
    card.addEventListener("click", () => {
        haptic("light");
        openPage("detail", giveaway.id);
    });

    const top = element("div", undefined, "record-top");
    const badge = statusBadge(
        giveaway.publishing && giveaway.status === "running" ? "publishing" : giveaway.status,
        giveaway.publishing && giveaway.status === "running" ? "发布中" : statusLabels[giveaway.status],
    );
    top.append(element("strong", giveaway.title), badge);

    const badges = element("div", undefined, "record-badges");
    badges.append(element("span", "#" + giveaway.id, "meta-pill"));
    if (giveaway.prize_mode) {
        badges.append(element("span", modeLabels[giveaway.prize_mode] || giveaway.prize_mode, "meta-pill"));
    }
    if (giveaway.tier_count) {
        badges.append(
            element("span", `${giveaway.tier_count} 档 · ${giveaway.winner_slots || 0} 名额`, "meta-pill"),
        );
    }
    if (giveaway.status === "running" && giveaway.end_at) {
        const countdown = formatCountdown(giveaway.end_at);
        if (countdown) {
            badges.append(
                element(
                    "span",
                    countdown.text,
                    "meta-pill " + (countdown.overdue ? "overdue" : "countdown"),
                ),
            );
        }
    }

    card.append(top, badges);

    if (
        (giveaway.draw_mode === "count" || giveaway.draw_mode === "either")
        && giveaway.target_participants > 0
    ) {
        const wrap = element("div", undefined, "progress-wrap");
        const pct = Math.min(100, Math.round((giveaway.participants / giveaway.target_participants) * 100));
        const label = element("div", undefined, "progress-label");
        label.append(
            element("span", `${giveaway.participants} / ${giveaway.target_participants} 人有效报名`),
            element("span", pct + "%"),
        );
        wrap.append(label, progressBar(giveaway.participants, giveaway.target_participants, pct >= 100 ? "is-good" : ""));
        card.append(wrap);
    } else {
        card.append(element("span", `${giveaway.participants} 人有效报名`, "hint"));
    }

    const bottom = element("div", undefined, "record-bottom");
    bottom.append(
        element("span", giveaway.condition, "hint"),
        element("span", "›", "record-arrow"),
    );
    card.append(bottom);
    return card;
}

function renderDetail(giveaway) {
    if (giveaway.tz) displayTimeZone = giveaway.tz;
    const isOpen = giveaway.status === "running";
    const statusKind = isOpen && giveaway.publishing ? "publishing" : giveaway.status;
    const statusText = isOpen && giveaway.publishing ? "发布中" : statusLabels[giveaway.status];

    const header = element("div", undefined, "detail-header");
    const backRow = element("div", undefined, "back-row");
    backRow.append(button("‹ 抽奖列表", "quiet back", doBack));
    const titleNode = element("h2", giveaway.title);
    titleNode.tabIndex = -1;
    const metaRow = element("div", undefined, "title-meta-row");
    const idBtn = button("#" + giveaway.id, "quiet compact", () => copyText("#" + giveaway.id, "抽奖编号"), "copy");
    metaRow.append(statusBadge(statusKind, statusText), idBtn);
    if (isOpen && giveaway.end_at) {
        const countdown = formatCountdown(giveaway.end_at);
        if (countdown) {
            metaRow.append(element("span", countdown.text, "meta-pill " + (countdown.overdue ? "overdue" : "countdown")));
        }
    }
    header.append(backRow, titleNode, metaRow);

    const detailItems = [
        ["有效报名", giveaway.participants + " 人", "data-number"],
        ["开奖条件", giveaway.condition],
        ["发奖方式", modeLabels[giveaway.prize_mode] || giveaway.prize_mode],
        ["创建时间", formatDateTime(giveaway.created_at) || "—"],
    ];
    const detail = panel(null, stats(detailItems, "detail-stats"));

    if (
        (giveaway.draw_mode === "count" || giveaway.draw_mode === "either")
        && giveaway.target_participants > 0
    ) {
        const wrap = element("div", undefined, "progress-wrap");
        const pct = Math.min(100, Math.round((giveaway.participants / giveaway.target_participants) * 100));
        const label = element("div", undefined, "progress-label");
        label.append(element("span", "目标人数进度"), element("span", `${giveaway.participants} / ${giveaway.target_participants} (${pct}%)`));
        wrap.append(label, progressBar(giveaway.participants, giveaway.target_participants, pct >= 100 ? "is-good" : ""));
        detail.append(wrap);
    }

    if (giveaway.description) {
        detail.append(element("div", giveaway.description, "desc-box prewrap"));
    }

    if (giveaway.draw_commit || giveaway.draw_seed) {
        const verifyBox = element("div", undefined, "stack-sm");
        if (giveaway.draw_commit) {
            const row = element("div", undefined, "code-pill-row");
            row.append(
                element("span", "承诺哈希", "hint"),
                element("span", giveaway.draw_commit, "mono-truncate"),
                button("复制", "quiet compact", () => copyText(giveaway.draw_commit, "承诺哈希"), "copy"),
            );
            verifyBox.append(row);
        }
        if (giveaway.draw_seed) {
            const row = element("div", undefined, "code-pill-row");
            row.append(
                element("span", "开奖种子", "hint"),
                element("span", giveaway.draw_seed, "mono-truncate"),
                button("复制", "quiet compact", () => copyText(giveaway.draw_seed, "开奖种子"), "copy"),
            );
            verifyBox.append(row);
        }
        detail.append(verifyBox);
    }

    const published = giveaway.posts.filter(post => post.sent).length;
    const pending = giveaway.posts.length - published;
    const publications = element("ul", undefined, "plain-list");
    for (const post of giveaway.posts) {
        const row = element("li", undefined, "line-item");
        const left = element("div", undefined, "line-item-stack");
        left.append(element("span", post.name));
        if (!post.sent) {
            left.append(element("span", formatPostError(post.error), "hint"));
        }
        row.append(left, statusBadge(post.sent ? "sent" : "failed", post.sent ? "已发布" : "未发布"));
        publications.append(row);
    }
    const destinations = panel(
        "发布状态",
        publications.children.length ? publications : element("p", "没有自动发布记录。", "hint"),
    );
    if (isOpen && pending > 0) {
        destinations.append(
            giveawayAction(
                giveaway,
                "republish",
                `补发未成功位置（${pending} 处）`,
                `将补发 ${pending} 个尚未发布的频道或群组。`,
            ),
        );
    }

    const reqData = giveaway.requirements || {
        channels: (giveaway.channels || []).map(name => ({name, url: null})),
        groups: (giveaway.groups || []).map(name => ({name, url: null})),
    };
    const requirements = panel("报名须加入");
    if (reqData.channels.length) {
        requirements.append(element("p", "频道", "hint"), requirementChips(reqData.channels));
    }
    if (reqData.groups.length) {
        requirements.append(element("p", "群组", "hint"), requirementChips(reqData.groups));
    }
    if (!reqData.channels.length && !reqData.groups.length) {
        requirements.append(element("p", "无", "hint"));
    }

    const actions = element("div", undefined, "button-row");
    if (isOpen) {
        actions.append(
            giveawayAction(
                giveaway,
                "cancel",
                "取消抽奖",
                "取消后将停止报名，这场抽奖不会开奖。",
                {danger: true},
            ),
        );
    }
    const actionPanel = panel("抽奖操作");
    if (actions.children.length && isOpen) actionPanel.append(actions);
    if (giveaway.status === "finished" && giveaway.tiers.length) {
        const rerollSelect = tierSelect(giveaway.tiers, "选择要重抽的奖项", true);
        const tier = rerollSelect.select;
        const rerollHint = element("p", "", "hint");
        const reroll = giveawayAction(
            giveaway,
            "reroll",
            "重抽该奖项",
            () => "将替换「" + tier.value + "」的中奖名单，机器人会发送新通知。",
            {danger: true, tier: () => tier.value},
        );
        function syncRerollState() {
            const chosen = giveaway.tiers.find(item => item.name === tier.value);
            const blocked = chosen && chosen.rerollable === false;
            reroll.disabled = Boolean(blocked);
            rerollHint.textContent = blocked
                ? "该奖项已有中奖者领取奖品，为保证公平无法重抽。"
                : "重抽仅针对当前未领奖的奖项，会顺延排除其他奖项中奖者。";
        }
        tier.addEventListener("change", syncRerollState);
        syncRerollState();
        const selector = element("label", "重抽奖项");
        selector.append(rerollSelect.wrap);
        actionPanel.append(selector, rerollHint, reroll);
    }

    const sections = [header, detail, destinations, requirements, renderStock(giveaway)];
    if (giveaway.status === "finished") sections.push(renderWinners(giveaway));
    if (actionPanel.children.length > 1) sections.push(actionPanel);
    sections.push(renderGate(giveaway.gate));
    content.replaceChildren(...sections);
}

function requirementChips(items) {
    const wrap = element("div", undefined, "chip-links");
    for (const item of items) {
        if (item.url) {
            const anchor = element("a", undefined, "link-chip");
            anchor.href = item.url;
            anchor.target = "_blank";
            anchor.rel = "noopener noreferrer";
            anchor.append(element("span", item.name), svgIcon("external"));
            wrap.append(anchor);
        } else {
            wrap.append(element("span", item.name, "link-chip static"));
        }
    }
    return wrap;
}

function renderWinners(giveaway) {
    const winners = giveaway.winners || [];
    const section = panel("中奖名单与通知");
    if (!winners.length) {
        section.append(element("p", "暂无中奖记录（无人参与或暂无合格参与者）。", "hint"));
        return section;
    }
    const list = element("ul", undefined, "plain-list");
    for (const winner of winners) {
        const row = element("li", undefined, "winner-row");
        const left = element("div", undefined, "winner-main");
        const nameText = winner.username ? `${winner.user_id} (@${winner.username})` : winner.user_id;
        const userBtn = element("button", nameText, "link-btn");
        userBtn.type = "button";
        userBtn.title = "点击复制用户编号";
        userBtn.addEventListener("click", () => copyText(winner.user_id, "用户编号"));

        const sub = element("span", winner.tier, "hint");
        left.append(userBtn, sub);
        if (!winner.notified_ok && winner.notify_error) {
            left.append(element("span", "失败原因：" + winner.notify_error, "hint"));
        }

        const right = element("div", undefined, "winner-badges");
        if (winner.notified_ok) {
            right.append(statusBadge("sent", "私信已达"));
        } else if (winner.notified_at) {
            right.append(statusBadge("failed", "私信失败"));
        } else {
            right.append(statusBadge("publishing", "待发送"));
        }
        if (giveaway.prize_mode !== "none") {
            right.append(statusBadge(winner.claimed ? "good" : "info", winner.claimed ? "已领奖" : "未领奖"));
        }
        row.append(left, right);
        list.append(row);
    }
    section.append(list);
    if (giveaway.pending_notifications > 0) {
        section.append(
            giveawayAction(
                giveaway,
                "retry",
                `补发 ${giveaway.pending_notifications} 条失败私信`,
                `将重试发送 ${giveaway.pending_notifications} 条待补发的中奖私信。`,
            ),
        );
    }
    return section;
}

function giveawayAction(giveaway, action, title, description, settings = {}) {
    return button(title, settings.danger ? "danger" : "secondary", async event => {
        const control = event.currentTarget;
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
}

function tierSelect(tiers, name, annotateReroll = false) {
    const wrap = element("span", undefined, "select-wrap");
    const select = element("select");
    select.setAttribute("aria-label", name);
    for (const tier of tiers) {
        const suffix = annotateReroll && tier.rerollable === false ? "（已领奖，不可重抽）" : "";
        const option = element("option", tier.name + suffix);
        option.value = tier.name;
        select.append(option);
    }
    const chevron = svgIcon("chevron");
    chevron.setAttribute("class", "select-chevron");
    wrap.append(select, chevron);
    return {wrap, select};
}

function renderGate(gate) {
    const values = [
        ["用户编号检查", gate.enabled ? "已开启" : "已关闭"],
        ["编号下限", gate.min_user_id || "未设置"],
    ];
    const distribution = gate.distribution;
    if (distribution) {
        values.push(
            ["样本人数", distribution.count],
            ["最小编号", distribution.low],
            ["中位编号", distribution.median],
            ["最大编号", distribution.high],
            ["低于下限", distribution.below],
        );
    }
    const section = panel("报名编号门槛", stats(values, "gate-grid"));
    if (distribution && distribution.count > 0 && gate.min_user_id) {
        const wrap = element("div", undefined, "progress-wrap");
        const pct = Math.round((distribution.below / distribution.count) * 100);
        const label = element("div", undefined, "progress-label");
        label.append(element("span", "低于编号下限占比"), element("span", `${distribution.below} / ${distribution.count} (${pct}%)`));
        wrap.append(label, progressBar(distribution.below, distribution.count, pct > 20 ? "is-warn" : ""));
        section.append(wrap);
    }
    return section;
}

function countUniqueLines(raw) {
    const trimmed = raw.trim();
    if (!trimmed || trimmed === "-") return [];
    const seen = new Set();
    const out = [];
    for (const part of trimmed.split(/[\r\n\v\f\x1c-\x1e\x85\u2028\u2029]+/)) {
        const value = part.trim();
        if (value && !seen.has(value)) {
            seen.add(value);
            out.push(value);
        }
    }
    return out;
}

function renderStock(giveaway) {
    const section = panel("奖池库存", element("p", modeLabels[giveaway.prize_mode], "hint"));
    if (!giveaway.tiers.length) {
        section.append(element("p", "暂无奖项。", "hint"));
        return section;
    }

    const details = giveaway.prize_mode !== "none" ? element("details", undefined, "stock-controls") : null;
    let tierControl = null;
    let codesControl = null;

    const cards = element("div", undefined, "stock-cards");
    for (const tier of giveaway.tiers) {
        const card = element("div", undefined, "stock-card");
        const head = element("div", undefined, "stock-card-head");
        head.append(
            element("span", `${tier.name} · ${tier.count} 名额`, "stock-card-title"),
        );

        let badge = null;
        if (giveaway.prize_mode === "none") {
            badge = statusBadge("info", "人工发奖");
        } else if (giveaway.prize_mode === "shared") {
            badge = tier.total ? statusBadge("good", "已录入") : statusBadge("warn", "待录入");
        } else if (tier.remaining === 0 && tier.total === 0) {
            badge = statusBadge("warn", "待补货");
        } else if (tier.remaining < Math.max(0, tier.count - tier.claimed)) {
            badge = statusBadge("danger", "库存偏少");
        } else {
            badge = statusBadge("good", "库存充足");
        }
        head.append(badge);
        card.append(head);

        if (giveaway.prize_mode === "unique") {
            const neededRemaining = Math.max(0, tier.count - tier.claimed);
            const maxBar = Math.max(tier.total, tier.count, 1);
            card.append(
                progressBar(
                    tier.remaining,
                    maxBar,
                    tier.remaining < neededRemaining ? "is-warn" : "is-good",
                ),
            );
            const footer = element("div", undefined, "tier-footer");
            footer.append(
                element("span", `剩余 ${tier.remaining} 条 · 已领 ${tier.claimed} 条 · 共 ${tier.total} 条`, "hint"),
            );
            if (details) {
                const restockBtn = button("补货", "secondary compact", () => {
                    details.open = true;
                    tierControl.value = tier.name;
                    codesControl.focus();
                });
                footer.append(restockBtn);
            }
            card.append(footer);
        } else if (giveaway.prize_mode === "shared") {
            const footer = element("div", undefined, "tier-footer");
            footer.append(
                element(
                    "span",
                    (tier.total ? "已录入共享文本" : "尚未录入共享文本") + ` · ${tier.claimed} 人已领取`,
                    "hint",
                ),
            );
            if (details) {
                const restockBtn = button(tier.total ? "追加/更新" : "录入文本", "secondary compact", () => {
                    details.open = true;
                    tierControl.value = tier.name;
                    codesControl.focus();
                });
                footer.append(restockBtn);
            }
            card.append(footer);
        }
        cards.append(card);
    }
    section.append(cards);
    if (!details) return section;

    details.append(element("summary", "补充库存", "button secondary"));
    const form = element("form", undefined, "stack");
    form.autocomplete = "off";
    const choice = element("label", "奖项");
    const stockSelect = tierSelect(giveaway.tiers, "选择要补货的奖项");
    tierControl = stockSelect.select;
    choice.append(stockSelect.wrap);

    const codeLabel = element("label", giveaway.prize_mode === "shared" ? "共享文本" : "独立码，每行一条");
    codesControl = element("textarea");
    codesControl.rows = 4;
    codesControl.required = true;
    codesControl.spellcheck = false;
    codesControl.placeholder = giveaway.prize_mode === "shared"
        ? "输入中奖者将领取的共享文本…"
        : "粘贴独立码，每行一条，空行和重复项会自动忽略…";
    codeLabel.append(codesControl);

    const liveHint = element("span", "", "hint");
    codesControl.addEventListener("input", () => {
        if (giveaway.prize_mode === "shared") {
            liveHint.textContent = codesControl.value.trim() ? "已输入 1 份共享文本" : "";
        } else {
            const lines = countUniqueLines(codesControl.value);
            liveHint.textContent = lines.length ? `本地已去重识别 ${lines.length} 条独立码` : "";
        }
    });

    const submit = button("核对补货数量", "primary");
    submit.type = "submit";
    form.append(choice, codeLabel, liveHint, submit);
    form.addEventListener("submit", event => {
        event.preventDefault();
        withButton(submit, "正在核对…", async () => {
            const path = "/giveaways/" + giveaway.id + "/actions";
            const payload = {action: "stock-preview", tier: tierControl.value, codes: codesControl.value};
            const preview = await api(path, payload);
            const confirmed = await confirmAction("确认补货", preview.message, "确认入库");
            if (!confirmed) return;
            const span = submit.querySelector("span") || submit;
            span.textContent = "正在入库…";
            const result = await api(path, {...payload, action: "add-stock"});
            await openPage("detail", giveaway.id);
            announce(result.message);
        });
    });
    details.append(form);
    section.append(details);
    return section;
}

function parseLocationLine(entry) {
    const trimmed = entry.trim();
    if (!trimmed) return null;
    if (!trimmed.includes("|")) {
        const ok = /^@[A-Za-z][A-Za-z0-9_]{0,31}$/.test(trimmed)
            || /^-[1-9][0-9]{0,18}$/.test(trimmed)
            || /^https:\/\/t\.me\/[A-Za-z][A-Za-z0-9_]{0,31}\/?$/.test(trimmed);
        return {
            label: trimmed,
            ok,
            reason: ok ? "" : "私有链接请用「名称 | 链接 | -100编号」",
        };
    }
    const parts = trimmed.split("|").map(part => part.trim());
    if (parts.length !== 2 && parts.length !== 3) {
        return {label: trimmed, ok: false, reason: "格式应为：名称 | 链接 | 可选编号"};
    }
    const [name, link, chatId] = parts;
    const isUrl = /^https:\/\/t\.me\/.+/.test(link);
    if (!name || !isUrl) {
        return {label: name || trimmed, ok: false, reason: "需要名称与 https://t.me/ 链接"};
    }
    const isPrivateInvite = /^https:\/\/t\.me\/(\+|joinchat\/)/.test(link);
    if (parts.length === 2 && isPrivateInvite) {
        return {label: name, ok: false, reason: "私有邀请链接需附带 | -100编号"};
    }
    if (parts.length === 3 && !/^-[1-9][0-9]{0,18}$/.test(chatId)) {
        return {label: name, ok: false, reason: "编号须为负数（如 -100…）"};
    }
    return {
        label: parts.length === 3 ? `${name} (${chatId})` : name,
        ok: true,
        reason: "",
    };
}

function renderLocationChips(rawText, container) {
    container.replaceChildren();
    const raw = rawText.trim();
    if (!raw || raw === "-") return;
    for (const line of raw.split(/\r?\n/)) {
        const chunks = line.includes("|") ? [line] : line.split(/[\s,，]+/);
        for (const chunk of chunks) {
            const parsed = parseLocationLine(chunk);
            if (!parsed) continue;
            const chip = element(
                "span",
                parsed.ok ? `✓ ${parsed.label}` : `⚠ ${parsed.label}：${parsed.reason}`,
                "parsed-chip" + (parsed.ok ? "" : " is-error"),
            );
            container.append(chip);
        }
    }
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
    const titleCounter = form.querySelector("#title-counter");
    const descCounter = form.querySelector("#desc-counter");
    const channelsPreview = form.querySelector("#channels-preview");
    const groupsPreview = form.querySelector("#groups-preview");
    const prizeSummaryPill = form.querySelector("#prize-summary-pill");
    const stepPills = [...form.querySelectorAll(".step-pill")];

    form.querySelector("#timezone").textContent = displayTimeZone ? "时区：" + displayTimeZone : "";
    form.querySelector(".section-heading").prepend(button("‹ 抽奖列表", "quiet back", doBack));
    content.replaceChildren(form);

    function markDirty() {
        draft.dirty = true;
        tg?.enableClosingConfirmation?.();
    }

    function syncCountersAndChips() {
        titleCounter.textContent = `${fields.title.value.length} / 256`;
        descCounter.textContent = `${fields.description.value.length} / 4096`;
        renderLocationChips(fields.channels.value, channelsPreview);
        renderLocationChips(fields.groups.value, groupsPreview);
    }

    const stepSections = stepPills
        .map(pill => ({pill, node: form.querySelector("#" + pill.dataset.step)}))
        .filter(item => item.node);
    function syncActiveStep() {
        if (!form.isConnected || !stepSections.length) return;
        let current = stepSections[0];
        for (const item of stepSections) {
            if (item.node.getBoundingClientRect().top <= 140) current = item;
        }
        for (const item of stepSections) {
            item.pill.classList.toggle("is-active", item === current);
        }
    }
    window.addEventListener("scroll", syncActiveStep, {passive: true});

    for (const pill of stepPills) {
        pill.addEventListener("click", event => {
            event.preventDefault();
            const target = form.querySelector("#" + pill.dataset.step);
            if (!target) return;
            for (const item of stepPills) item.classList.toggle("is-active", item === pill);
            target.scrollIntoView({behavior: "smooth", block: "start"});
        });
    }

    const builderToggle = form.querySelector("#location-builder");
    const builderKind = form.querySelector("#builder-kind");
    const builderName = form.querySelector("#builder-name");
    const builderUrl = form.querySelector("#builder-url");
    const builderChatId = form.querySelector("#builder-chat-id");
    const builderHint = form.querySelector("#builder-hint");
    form.querySelector("#builder-insert").addEventListener("click", () => {
        const name = builderName.value.trim();
        let ref = builderUrl.value.trim();
        const chatId = builderChatId.value.trim();
        if (!ref) {
            builderHint.textContent = "请填写邀请链接或 @用户名。";
            builderUrl.focus();
            return;
        }
        if (ref.startsWith("@") && !name && !chatId) {
            // Keep @username format as-is.
        } else {
            if (ref.startsWith("@")) ref = "https://t.me/" + ref.slice(1);
            if (!name) {
                builderHint.textContent = "请填写显示名称（例如：官方频道）。";
                builderName.focus();
                return;
            }
            if (/^https:\/\/t\.me\/(\+|joinchat\/)/.test(ref) && !/^-[1-9][0-9]{0,18}$/.test(chatId)) {
                builderHint.textContent = "私有邀请链接必须填入 -100… 负数聊天编号。";
                builderChatId.focus();
                return;
            }
            ref = chatId ? `${name} | ${ref} | ${chatId}` : `${name} | ${ref}`;
        }
        const target = fields[builderKind.value];
        target.value = target.value.trim() ? `${target.value.trim()}\n${ref}` : ref;
        builderName.value = "";
        builderUrl.value = "";
        builderChatId.value = "";
        builderHint.textContent = "已添加到列表中。";
        builderToggle.open = false;
        markDirty();
        syncCountersAndChips();
        haptic("success");
    });

    function syncPresetHighlights() {
        const durVal = fields.duration.value;
        const durUnit = fields.duration_unit.value;
        for (const btn of form.querySelectorAll("#duration-presets .chip-btn")) {
            btn.classList.toggle("is-selected", btn.dataset.duration === durVal && btn.dataset.unit === durUnit);
        }
        const targetVal = fields.target.value;
        for (const btn of form.querySelectorAll("#target-presets .chip-btn")) {
            btn.classList.toggle("is-selected", btn.dataset.target === targetVal);
        }
    }

    for (const btn of form.querySelectorAll("#duration-presets .chip-btn")) {
        btn.addEventListener("click", () => {
            fields.duration.value = btn.dataset.duration;
            fields.duration_unit.value = btn.dataset.unit;
            haptic("selection");
            markDirty();
            syncPresetHighlights();
        });
    }
    for (const btn of form.querySelectorAll("#deadline-presets .chip-btn")) {
        btn.addEventListener("click", () => {
            const target = new Date();
            if (btn.dataset.hours) {
                target.setTime(target.getTime() + Number(btn.dataset.hours) * 3600000);
            } else if (btn.dataset.evening !== undefined) {
                const days = Number(btn.dataset.evening);
                const nowLocal = formatLocalDateTimeInput(target, displayTimeZone);
                const baseDate = new Date(nowLocal.slice(0, 10) + "T20:00");
                if (!Number.isNaN(baseDate.getTime())) {
                    const ymd = nowLocal.slice(0, 10);
                    if (days === 0) {
                        fields.deadline.value = `${ymd}T20:00`;
                    } else {
                        const tomorrow = new Date(target.getTime() + days * 86400000);
                        const tLocal = formatLocalDateTimeInput(tomorrow, displayTimeZone);
                        fields.deadline.value = `${tLocal.slice(0, 10)}T20:00`;
                    }
                    haptic("selection");
                    markDirty();
                    return;
                }
            }
            fields.deadline.value = formatLocalDateTimeInput(target, displayTimeZone);
            haptic("selection");
            markDirty();
        });
    }
    for (const btn of form.querySelectorAll("#target-presets .chip-btn")) {
        btn.addEventListener("click", () => {
            fields.target.value = btn.dataset.target;
            haptic("selection");
            markDirty();
            syncPresetHighlights();
        });
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
        if (timed && !relative && !fields.deadline.value) {
            fields.deadline.value = formatLocalDateTimeInput(
                new Date(Date.now() + 24 * 3600000),
                displayTimeZone,
            );
        }
        syncPresetHighlights();
    }

    function updateTiers() {
        const mode = fields.prize_mode.value;
        const tiers = [...tierList.children];
        resetCodes.hidden = mode === "none";

        const seenNames = new Map();
        const globalCodes = new Set();
        const duplicateCodes = new Set();
        let totalSeats = 0;
        let totalStock = 0;

        for (const tier of tiers) {
            const name = tier.querySelector("[name=tier_name]").value.trim();
            if (name) seenNames.set(name, (seenNames.get(name) || 0) + 1);
            if (mode === "unique") {
                for (const code of countUniqueLines(tier.querySelector("[name=tier_codes]").value)) {
                    if (globalCodes.has(code)) duplicateCodes.add(code);
                    globalCodes.add(code);
                }
            }
        }

        for (const [index, tier] of tiers.entries()) {
            tier.querySelector("legend").textContent = "奖项 " + (index + 1);
            const remove = tier.querySelector(".remove-tier");
            remove.setAttribute("aria-label", "移除奖项 " + (index + 1));
            remove.hidden = tiers.length === 1;

            const codesField = tier.querySelector(".codes-field");
            codesField.hidden = mode === "none";
            codesField.querySelector(".codes-label span").textContent =
                mode === "unique" ? "独立码，每行一条" : "共享文本";

            const cleanBtn = tier.querySelector(".clean-codes");
            const codesArea = tier.querySelector("[name=tier_codes]");
            codesArea.placeholder = mode === "unique"
                ? "粘贴卡密或兑换码，每行一条（自动去重和忽略空行）"
                : "输入所有中奖者领取的同一份文本（保留换行）";
            cleanBtn.hidden = mode !== "unique" || !codesArea.value.trim();

            const tierName = tier.querySelector("[name=tier_name]").value.trim();
            const raw = codesArea.value.trim();
            const planned = Number(tier.querySelector("[name=tier_count]").value) || 0;
            totalSeats += Math.max(0, planned);

            const badge = tier.querySelector(".inventory-badge");
            const inventoryNode = tier.querySelector(".inventory");
            badge.hidden = true;
            let inventory = "";

            if (tierName && seenNames.get(tierName) > 1) {
                badge.hidden = false;
                badge.className = "status danger inventory-badge";
                badge.textContent = "名称重复";
            }

            if (mode === "unique") {
                const uniqueList = countUniqueLines(codesArea.value);
                const count = uniqueList.length;
                totalStock += count;
                const crossDupes = uniqueList.filter(code => duplicateCodes.has(code)).length;
                const rawNonEmpty = raw && raw !== "-"
                    ? raw.split(/[\r\n\v\f\x1c-\x1e\x85\u2028\u2029]+/).map(s => s.trim()).filter(Boolean).length
                    : 0;
                const selfDupes = Math.max(0, rawNonEmpty - count);

                inventory = `已识别 ${count} 条`;
                if (selfDupes) inventory += `（忽略 ${selfDupes} 重复）`;
                if (crossDupes) inventory += ` · 有 ${crossDupes} 条与其他奖项重复`;
                else if (planned > 0 && count < planned) inventory += ` · 少 ${planned - count} 条`;
                else if (planned > 0 && count > planned) inventory += ` · 多 ${count - planned} 条`;

                if (badge.hidden) {
                    badge.hidden = false;
                    if (crossDupes) {
                        badge.className = "status danger inventory-badge";
                        badge.textContent = "跨档重复";
                    } else if (planned > 0 && count === planned) {
                        badge.className = "status good inventory-badge";
                        badge.textContent = "库存匹配";
                    } else if (planned > 0 && count < planned) {
                        badge.className = "status warn inventory-badge";
                        badge.textContent = `缺 ${planned - count} 条`;
                    } else if (planned > 0 && count > planned) {
                        badge.className = "status info inventory-badge";
                        badge.textContent = `余 ${count - planned} 条`;
                    } else {
                        badge.className = "status info inventory-badge";
                        badge.textContent = `${count} 条`;
                    }
                }
            } else if (mode === "shared") {
                const hasText = Boolean(raw && raw !== "-");
                if (hasText) totalStock += 1;
                inventory = hasText ? "已录入 1 份共享文本" : "尚未录入共享文本";
                if (badge.hidden) {
                    badge.hidden = false;
                    badge.className = hasText ? "status good inventory-badge" : "status warn inventory-badge";
                    badge.textContent = hasText ? "已录入" : "待录入";
                }
            }
            inventoryNode.textContent = inventory;
        }

        if (mode === "none") {
            prizeSummaryPill.textContent = `共 ${tiers.length} 档 · ${totalSeats} 个中奖名额`;
        } else {
            const unit = mode === "shared" ? "份共享文本" : "条独立码";
            prizeSummaryPill.textContent = `共 ${tiers.length} 档 · ${totalSeats} 名额 · 已录 ${totalStock} ${unit}`;
        }
    }

    function addTierRow(focus) {
        const tier = document.querySelector("#tier-template").content.firstElementChild.cloneNode(true);
        const index = tierList.children.length;
        const nameInput = tier.querySelector("[name=tier_name]");
        if (!nameInput.value && index < defaultTierNames.length) {
            nameInput.value = defaultTierNames[index];
        }
        const codeArea = tier.querySelector("[name=tier_codes]");
        const codeId = "tier-codes-" + (++tierDomCounter);
        codeArea.id = codeId;
        tier.querySelector(".codes-label").setAttribute("for", codeId);

        tier.querySelector(".clean-codes").addEventListener("click", () => {
            const cleaned = countUniqueLines(codeArea.value).join("\n");
            if (cleaned !== codeArea.value) {
                codeArea.value = cleaned;
                markDirty();
                updateTiers();
                announce("已去除空行与重复独立码。");
            }
        });
        tier.querySelector(".remove-tier").addEventListener("click", () => {
            tier.remove();
            updateTiers();
            markDirty();
            addTier.focus();
        });
        tierList.append(tier);
        updateTiers();
        if (focus) nameInput.focus();
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
        syncCountersAndChips();
        updateTime();
        updateTiers();
    });
    form.addEventListener("change", () => {
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
        withButton(previewButton, "正在校验目标与库存…", async () => {
            const request = collectDraft();
            const result = await api("/create/preview", request);
            draft.preview = {
                request: {...request, condition: result.fixed_condition, request_id: uuid4()},
            };
            showPreview(draft, result, returnToForm);
        }, errorBox);
    });

    addTierRow(false);
    syncCountersAndChips();
    updateTime();
    returnToForm();
}

function showPreview(draft, preview, returnToForm) {
    const {request} = draft.preview;

    const mockup = element("section", undefined, "tg-mockup");
    mockup.append(element("span", "Telegram 频道/群组帖子预览", "hint"));
    const bubble = element("div", undefined, "tg-bubble");
    bubble.append(element("div", "🎁 抽奖 #预览", "hint"));
    bubble.append(element("div", request.title, "tg-bubble-title"));
    if (request.description.trim()) {
        bubble.append(element("div", request.description.trim(), "prewrap"));
    }

    const prizeSec = element("div", undefined, "tg-bubble-section");
    prizeSec.append(element("strong", "🏆 奖品："));
    for (const tier of preview.tiers) {
        prizeSec.append(element("span", `• ${tier.name} × ${tier.count}`));
    }
    bubble.append(prizeSec);

    if (preview.channels?.length) {
        const chSec = element("div", undefined, "tg-bubble-section");
        chSec.append(element("strong", "📢 必加频道："));
        for (const ch of preview.channels) chSec.append(element("span", "• " + ch));
        bubble.append(chSec);
    }
    if (preview.groups?.length) {
        const gpSec = element("div", undefined, "tg-bubble-section");
        gpSec.append(element("strong", "👥 必加群组："));
        for (const gp of preview.groups) gpSec.append(element("span", "• " + gp));
        bubble.append(gpSec);
    }

    bubble.append(
        element("span", "📝 参加须答对古诗词选择题，每题限时 60 秒，最多错 5 次。", "hint"),
        element("span", "🏁 开奖条件：" + preview.condition),
        element("span", "👥 已报名：0 人"),
    );

    const fakeJoin = element("div", "✅ 参加", "tg-inline-btn");
    mockup.append(bubble, fakeJoin);

    if (preview.char_count !== undefined) {
        const charLimit = preview.char_limit || 4096;
        const wrap = element("div", undefined, "progress-wrap");
        const pct = Math.min(100, Math.round((preview.char_count / charLimit) * 100));
        const label = element("div", undefined, "progress-label");
        label.append(element("span", "帖子字数长度"), element("span", `${preview.char_count} / ${charLimit} 字符`));
        wrap.append(label, progressBar(preview.char_count, charLimit, pct > 85 ? "is-warn" : ""));
        mockup.append(wrap);
    }

    const tierLines = element("ul", undefined, "plain-list");
    for (const tier of preview.tiers) {
        const row = element("li", undefined, "line-item");
        const mode = request.prize_mode;
        let badge;
        if (mode === "none") {
            badge = statusBadge("info", "人工发奖");
        } else if (mode === "shared") {
            badge = tier.inventory ? statusBadge("good", "已录入共享文本") : statusBadge("warn", "待补充文本");
        } else if (tier.inventory === tier.count) {
            badge = statusBadge("good", `已录 ${tier.inventory} 条`);
        } else if (tier.inventory < tier.count) {
            badge = statusBadge("warn", `已录 ${tier.inventory} / 缺 ${tier.count - tier.inventory}`);
        } else {
            badge = statusBadge("info", `已录 ${tier.inventory} / 余 ${tier.inventory - tier.count}`);
        }
        row.append(element("span", `${tier.name} · ${tier.count} 名额`), badge);
        tierLines.append(row);
    }

    const list = panel("奖项与发奖方式", element("p", "发奖方式：" + modeLabels[request.prize_mode], "hint"), tierLines);
    if (preview.tiers.some(tier =>
        (request.prize_mode === "unique" && tier.inventory !== tier.count)
        || (request.prize_mode === "shared" && !tier.inventory),
    )) {
        list.append(element("p", "部分奖项的奖品库存与中奖名额不一致，发布后仍可随时补货。", "inventory-warning"));
    }

    const destList = element("ul", undefined, "plain-list");
    for (const dest of preview.destinations) {
        const row = element("li", undefined, "line-item");
        row.append(element("span", dest), statusBadge("sent", "权限校验通过"));
        destList.append(row);
    }
    const destinations = panel(
        `将自动发布到 ${preview.destinations.length} 处`,
        destList.children.length ? destList : element("p", "没有发布位置。", "hint"),
    );

    const errorBox = element("p", "", "form-error");
    errorBox.hidden = true;
    errorBox.setAttribute("role", "alert");
    errorBox.tabIndex = -1;

    const submit = button("确认并发布抽奖", "primary full", event => {
        const control = event.currentTarget;
        withButton(control, "正在发布…", async () => {
            let result;
            try {
                result = await api("/create", request);
            } catch (error) {
                if (!error.status || error.status >= 500 || error.status < 400 || error.status === 408) {
                    draft.pending = true;
                    throw new Error("发布状态暂未确认。请留在本页，点击「确认并发布抽奖」重试同一次发布。");
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

    const editBack = button("返回继续修改", "secondary full", returnToForm);
    setBack(returnToForm);
    content.replaceChildren(
        sectionHeading("预览抽奖", "继续编辑"),
        mockup,
        destinations,
        list,
        errorBox,
        submit,
        editBack,
    );
    moveToTop();
}

function textField(title, {name, pattern, maxLength, required, type = "text", inputMode, placeholder}) {
    const control = element("input");
    Object.assign(control, {type, name, required: Boolean(required)});
    if (pattern) control.pattern = pattern;
    if (maxLength) control.maxLength = maxLength;
    if (inputMode) control.inputMode = inputMode;
    if (placeholder) control.placeholder = placeholder;
    const label = element("label", title);
    label.append(control);
    return label;
}

function makeAccessForm(title, descriptionText, options) {
    const form = element("form", undefined, "stack");
    form.autocomplete = "off";
    form.append(textField("Telegram 用户编号", {
        name: "user_id",
        pattern: "[1-9][0-9]{0,18}",
        inputMode: "numeric",
        maxLength: 19,
        required: true,
        placeholder: "例如：123456789",
    }));
    if (options.some(option => option.action === "ban")) {
        form.append(textField("拉黑原因（选填）", {
            name: "reason",
            maxLength: 512,
            placeholder: "例如：脚本刷号 / 恶意小号",
        }));
    }
    const controls = element("div", undefined, "button-row");
    const errorBox = element("p", "", "form-error");
    errorBox.hidden = true;
    errorBox.setAttribute("role", "alert");
    errorBox.tabIndex = -1;
    for (const option of options) {
        const variant = option.primary ? "primary" : (option.danger ? "danger" : "secondary");
        const control = button(option.label, variant);
        control.type = "submit";
        control.value = option.action;
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
        if (!await confirmAction(
            option.label + "？",
            "用户编号 " + userId + "。" + option.description,
            option.label,
            option.danger,
        )) return;
        withButton(submitter, "正在处理…", async () => {
            const result = await api("/access", payload);
            await openPage("access");
            announce(result.message);
        }, errorBox);
    });
    const section = panel(title);
    if (descriptionText) section.append(element("p", descriptionText, "hint"));
    section.append(form);
    return section;
}

function renderAccess(data) {
    const adminList = element("ul", undefined, "plain-list");
    for (const admin of data.admins) {
        const row = element("li", undefined, "line-item");
        const left = element("div", undefined, "line-item-stack");
        const idLine = element("div", undefined, "record-badges");
        const copyIdBtn = element("button", admin.id, "link-btn");
        copyIdBtn.type = "button";
        copyIdBtn.addEventListener("click", () => copyText(admin.id, "管理员编号"));
        idLine.append(copyIdBtn);
        if (admin.configured) idLine.append(statusBadge("info", "部署配置"));
        else idLine.append(statusBadge("good", "动态添加"));
        if (data.current_admin_id && admin.id === data.current_admin_id) {
            idLine.append(statusBadge("running", "当前账号"));
        }
        left.append(idLine);
        row.append(left);

        if (!admin.configured && admin.id !== data.current_admin_id) {
            row.append(button("移除", "danger compact", async event => {
                const control = event.currentTarget;
                if (!await confirmAction(
                    "移除管理员？",
                    `用户编号 ${admin.id} 将失去抽奖管理小程序和管理员命令权限。`,
                    "确认移除",
                    true,
                )) return;
                await withButton(control, "移除中…", async () => {
                    const result = await api("/access", {action: "remove-admin", user_id: admin.id});
                    await openPage("access");
                    announce(result.message);
                });
            }));
        }
        adminList.append(row);
    }
    const administrators = panel(
        `管理员列表（${data.admins.length}）`,
        adminList.children.length ? adminList : element("p", "暂无管理员记录。", "hint"),
    );

    const add = makeAccessForm(
        "添加管理员",
        "新增管理员须先私聊机器人发送过 /start，才能显示「抽奖管理」菜单按钮。",
        [{
            action: "add-admin",
            label: "添加管理员",
            description: "该账号将获得抽奖管理权限。",
            primary: true,
        }],
    );

    const blacklistForm = makeAccessForm(
        "黑名单操作",
        "拉黑会立即清除该用户在所有抽奖中的报名记录，且禁止再次参与。",
        [{
            action: "ban",
            label: "拉黑用户",
            description: "将清除该用户已有的报名记录，并阻止继续报名。",
            danger: true,
        }, {
            action: "unban",
            label: "解除拉黑",
            description: "解除后可重新报名。",
        }],
    );

    const banItems = data.blacklist || [];
    const banList = element("ul", undefined, "plain-list");
    for (const item of banItems) {
        const row = element("li", undefined, "line-item");
        const left = element("div", undefined, "line-item-stack");
        const idBtn = element("button", item.user_id, "link-btn");
        idBtn.type = "button";
        idBtn.addEventListener("click", () => copyText(item.user_id, "用户编号"));
        left.append(idBtn);
        const detailParts = [];
        if (item.reason) detailParts.push("原因：" + item.reason);
        if (item.banned_at) detailParts.push("时间：" + formatDateTime(item.banned_at));
        if (detailParts.length) left.append(element("span", detailParts.join(" · "), "hint"));

        row.append(left, button("解封", "secondary compact", async event => {
            const control = event.currentTarget;
            if (!await confirmAction(
                "解除拉黑？",
                `用户编号 ${item.user_id} 解除后可重新参与抽奖。`,
                "确认解除",
            )) return;
            await withButton(control, "处理中…", async () => {
                const result = await api("/access", {action: "unban", user_id: item.user_id});
                await openPage("access");
                announce(result.message);
            });
        }));
        banList.append(row);
    }
    const blacklistPanel = panel(
        `当前黑名单（${banItems.length}）`,
        banList.children.length ? banList : element("p", "暂无已拉黑的用户。", "hint"),
    );

    content.replaceChildren(
        sectionHeading("权限与风控"),
        administrators,
        add,
        blacklistForm,
        blacklistPanel,
        renderGate(data.gate),
    );
}

function applyTheme() {
    const resolved = themeOverride
        || tg?.colorScheme
        || (window.matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light");
    document.documentElement.dataset.theme = resolved;
    const background = getComputedStyle(document.documentElement).getPropertyValue("--canvas").trim();
    if (tg?.isVersionAtLeast?.("6.1")) tg.setBackgroundColor?.(background);
    if (tg?.isVersionAtLeast?.("6.9")) tg.setHeaderColor?.(background);
}

themeToggle.addEventListener("click", () => {
    const current = document.documentElement.dataset.theme === "dark" ? "dark" : "light";
    themeOverride = current === "dark" ? "light" : "dark";
    haptic("selection");
    applyTheme();
});

noticeClose.addEventListener("click", () => announce(""));

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
    content.replaceChildren(skeletonCards(3));
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
        haptic("selection");
        openPage(tab.dataset.page);
    }
});

initialize();
