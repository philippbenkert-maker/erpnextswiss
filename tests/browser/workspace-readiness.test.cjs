const assert = require("node:assert/strict");
const { before, after, test } = require("node:test");
const { chromium } = require("playwright");
const { workspaceShortcutsReady } = require("./workspace-readiness.cjs");

let browser;
before(async () => { browser = await chromium.launch({ headless: true }); });
after(async () => { if (browser) await browser.close(); });

async function fixture(t, shortcuts) {
    const page = await browser.newPage();
    t.after(() => page.close());
    await page.setContent('<div id="page-Workspaces"><div class="layout-main-section"></div></div>');
    await page.evaluate(items => {
        window.__ = value => value;
        window.frappe = {
            request: { ajax_count: 0 },
            utils: { unescape_html: value => {
                const decoder = document.createElement("textarea");
                decoder.innerHTML = value;
                return decoder.value;
            } },
            workspace: {
                _page: { name: "Payments" },
                page_data: { shortcuts: { items } },
                content: items.map(item => ({ type: "shortcut", data: { shortcut_name: item.label } }))
            }
        };
        const panel = document.querySelector(".layout-main-section");
        for (const item of items) {
            const block = document.createElement("div");
            block.className = "ce-block";
            const wrapper = document.createElement("div");
            wrapper.setAttribute("shortcut_name", item.label);
            const widget = document.createElement("div");
            widget.className = "shortcut-widget-box";
            wrapper.append(widget);
            block.append(wrapper);
            panel.append(block);
        }
    }, shortcuts);
    return page;
}

const counted = label => ({ label, type: "DocType", doc_view: "List", link_to: label });

test("waits for requests to finish and accepts native zero-count shortcuts with no badge", async t => {
    const page = await fixture(t, [counted("Invoices"), counted("Proposals")]);
    await page.evaluate(() => {
        frappe.request.ajax_count = 2;
    });
    assert.equal(await page.evaluate(workspaceShortcutsReady, "Payments"), false);
    await page.evaluate(() => {
        frappe.request.ajax_count = 1; // The zero result leaves its shortcut without a badge.
    });
    assert.equal(await page.evaluate(workspaceShortcutsReady, "Payments"), false);
    await page.evaluate(() => {
        setTimeout(() => {
            document.querySelectorAll(".shortcut-widget-box")[1].innerHTML = '<div class="count-badge">17</div>';
            frappe.request.ajax_count = 0;
        }, 75);
    });
    await page.waitForFunction(workspaceShortcutsReady, "Payments", { timeout: 2000 });
    assert.equal(await page.locator(".shortcut-widget-box").first().innerText(), "");
    assert.deepEqual(await page.locator(".count-badge").allTextContents(), ["17"]);
});

test("unknown or unsettled request state fails the readiness gate", async t => {
    const page = await fixture(t, [counted("Invoices")]);
    for (const pending of [1, -1, null]) {
        await page.evaluate(value => { frappe.request.ajax_count = value; }, pending);
        assert.equal(await page.evaluate(workspaceShortcutsReady, "Payments"), false);
    }
});

test("Page, Report, New and Single shortcuts do not request a count", async t => {
    const page = await fixture(t, [
        { label: "Import", type: "Page", link_to: "bankimport" },
        { label: "Report", type: "Report", link_to: "General Ledger" },
        { ...counted("New invoice"), doc_view: "New" },
        { ...counted("Settings"), link_to: "Bank Settings" }
    ]);
    assert.equal(await page.evaluate(workspaceShortcutsReady, "Payments"), true);
    assert.equal(await page.evaluate(workspaceShortcutsReady, "Another workspace"), false);
});

test("missing metadata, rendered blocks and widgets never pass as ready", async t => {
    const page = await fixture(t, [{ label: "Import", type: "Page" }]);
    await page.locator(".shortcut-widget-box").evaluate(widget => widget.remove());
    assert.equal(await page.evaluate(workspaceShortcutsReady, "Payments"), false);
    await page.locator(".ce-block").evaluate(block => block.remove());
    assert.equal(await page.evaluate(workspaceShortcutsReady, "Payments"), false);
    await page.evaluate(() => { delete frappe.workspace.page_data; });
    assert.equal(await page.evaluate(workspaceShortcutsReady, "Payments"), false);
});

test("only configured blocks with native permitted data need to render", async t => {
    const page = await fixture(t, [{ label: "Bank &amp; import", type: "Page" }]);
    await page.evaluate(() => {
        frappe.workspace.content[0].data.shortcut_name = "Bank & import";
        document.querySelector("[shortcut_name]").setAttribute("shortcut_name", "Bank & import");
        frappe.workspace.page_data.shortcuts.items.push({ label: "Unused", type: "DocType" });
        frappe.workspace.content.push({ type: "shortcut", data: { shortcut_name: "Not permitted" } });
        frappe.workspace.content.push({ type: "header", data: { text: "Overview" } });
    });
    assert.equal(await page.evaluate(workspaceShortcutsReady, "Payments"), true);
});

test("a named workspace with layout chrome and no shortcut data is not ready", async t => {
    const page = await fixture(t, []);
    assert.equal(await page.evaluate(workspaceShortcutsReady, "Payments"), false);
});
