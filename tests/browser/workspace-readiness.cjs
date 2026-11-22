// Frappe can finish a shortcut count without rendering a badge, as it does for zero.
function workspaceShortcutsReady(target) {
    const app = window.frappe;
    const workspace = app?.workspace;
    const panel = document.querySelector("#page-Workspaces .layout-main-section");
    const shortcuts = workspace?.page_data?.shortcuts?.items;
    if (workspace?._page?.name !== target || !panel || !Array.isArray(shortcuts)
        || !Array.isArray(workspace.content) || app.request?.ajax_count !== 0) return false;

    const unescape = value => app.utils.unescape_html(value);
    const expected = workspace.content.filter(block => block.type === "shortcut")
        .map(block => block.data.shortcut_name)
        .filter(name => shortcuts.some(item => unescape(item.label) === unescape(__(name))));
    const rendered = Array.from(panel.querySelectorAll(".ce-block [shortcut_name]"));
    if (!expected.length || rendered.length !== expected.length) return false;

    return expected.every(name => {
        const matches = rendered.filter(block => block.getAttribute("shortcut_name") === name);
        return matches.length === 1 && Boolean(matches[0].querySelector(".shortcut-widget-box"));
    });
}

module.exports = { workspaceShortcutsReady };
