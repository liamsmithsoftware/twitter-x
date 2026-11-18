// Evaluated in the rendered profile. Read the DOM, not scripts or network responses.
({ seen, profilePath, immediate }) => {
    const visible = el => !el.closest("[inert], [hidden]") &&
        el.checkVisibility({checkOpacity: true, checkVisibilityCSS: true});
    const primary = document.querySelector('[data-testid="primaryColumn"]') || document.querySelector("main");
    const find = (root, selector) => root ? [...root.querySelectorAll(selector)].filter(visible) : [];
    const articles = find(primary, 'article[data-testid="tweet"]').filter(el => !el.parentElement.closest("article"));
    // Quote cards can contain another author's timestamp, text and media.
    const own = (article, selector) => find(article, selector).find(el =>
        el.closest('article, [data-testid="quoteTweet"], div[role="link"]') === article);
    const times = articles.map(el => own(el, 'a[href*="/status/"] time'));
    const hrefs = times.map(time => time?.closest("a").href || "");
    const dialog = find(document, '[role="dialog"], [aria-modal="true"]')[0];
    const emptySelector = '[data-testid="emptyState"], [data-testid="empty_state_header_text"]';
    const notices = find(primary || document, `${emptySelector}, [data-testid="error-detail"], [data-testid="retry"]`)
        .filter(el => !el.closest("article"));

    if (!immediate && location.pathname.toLowerCase().replace(/\/$/, "") === profilePath &&
        !dialog && !notices.length && !hrefs.some(href => href && !seen.includes(href))) return false;

    // innerText alone drops emoji rendered as images. Keep line breaks and emoji alt text.
    const text = node => {
        if (node.nodeType === Node.TEXT_NODE) return node.textContent;
        if (node.nodeType !== Node.ELEMENT_NODE || !visible(node)) return "";
        if (node.tagName === "IMG") return node.alt;
        if (node.tagName === "BR") return "\n";
        return [...node.childNodes].map(text).join("");
    };
    return {
        url: location.href,
        title: document.title,
        notice: notices.map(el => el.innerText).join("\n"),
        hasNotice: notices.length > 0,
        emptyState: notices.length > 0 && notices.every(el => el.matches(emptySelector)),
        dialog: dialog ? dialog.innerText : null,
        // Used only to diagnose an otherwise unrecognisable page, never saved or logged.
        emptyText: immediate && !articles.length ? document.body?.innerText.slice(0, 2000) : "",
        loading: find(primary, '[role="progressbar"]').length > 0,
        // Advance from the last article we actually read, not a sidebar or hidden quote.
        nextY: window.scrollY + Math.max(window.innerHeight, articles.at(-1)?.getBoundingClientRect().bottom || 0),
        atEnd: window.scrollY + window.innerHeight >= document.documentElement.scrollHeight - 2,
        cards: articles.map((el, i) => {
            const body = own(el, '[data-testid="tweetText"]');
            const video = own(el, "video");
            const isVideo = video || own(el, '[data-testid="videoPlayer"], [data-testid="videoComponent"]');
            const photo = own(el, '[data-testid="tweetPhoto"] img');
            const quote = find(el, '[data-testid="quoteTweet"], div[role="link"] time')[0];
            const analytics = own(el, 'a[href$="/analytics"]');
            return {
                href: hrefs[i], datetime: times[i]?.getAttribute("datetime"),
                pinned: own(el, '[data-testid="socialContext"]')?.innerText.trim() === "Pinned",
                // Null means unrecognisable markup, as distinct from a media/quote-only post.
                content: body ? text(body).trim() : (isVideo || photo || quote ? "" : null),
                media_type: isVideo ? "video" : photo ? "photo" : "text",
                media_url: (video?.currentSrc.startsWith("https://") && video.currentSrc) || video?.poster || photo?.src || null,
                metrics: analytics?.getAttribute("aria-label") || own(el, '[role="group"]')?.getAttribute("aria-label") || "",
            };
        }),
    };
}
