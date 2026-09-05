// Dashboard interaction regression tests for dashboard_v2/script.js.
//
// Run:  node tests/test_dropdowns.js       (exit 0 = pass)
//
// Loads the real script.js against a minimal DOM stub; the bootstrap never
// fires because the stub swallows DOMContentLoaded.
//
// Covers the two things that broke in this area: QtWebEngine growing a <select>
// whose options are swapped while its native popup is open, and the saved preset
// stable id being restored across the two separate signals that build the dropdown.
const fs = require("fs");
const path = require("path");
const vm = require("vm");

const SELECT_IDS = ["preset-select", "program-select", "monitor-select", "mono-output-select"];

class Option {
    constructor() { this.value = ""; this.textContent = ""; }
}

class Select {
    constructor(id) { this.id = id; this.tagName = "SELECT"; this.options = []; this._idx = -1; this.listeners = {}; }
    set innerHTML(v) { if (v === "") { this.options = []; this._idx = -1; } }
    get innerHTML() { return ""; }
    appendChild(o) { this.options.push(o); if (this._idx === -1) this._idx = 0; }
    get value() { return this._idx >= 0 && this.options[this._idx] ? this.options[this._idx].value : ""; }
    set value(v) {
        const i = this.options.findIndex(o => String(o.value) === String(v));
        this._idx = i;                      // -1 when absent: the blank-dropdown state
    }
    get selectedIndex() { return this._idx; }
    set selectedIndex(i) { this._idx = i; }
    addEventListener(ev, cb) { (this.listeners[ev] = this.listeners[ev] || []).push(cb); }
    fire(ev) { const l = this.listeners[ev] || []; this.listeners[ev] = []; l.forEach(cb => cb()); }
    labels() { return this.options.map(o => o.textContent); }
}

class Generic {
    constructor(id) {
        this.id = id; this.value = ""; this.textContent = ""; this.checked = false;
        this.style = { setProperty(name, value) { this[name] = value; } };
        const classes = new Set(id.includes("modal") ? ["is-hidden"] : []);
        this.classList = {
            toggle(name, on) { if (on) classes.add(name); else classes.delete(name); },
            add(name) { classes.add(name); }, remove(name) { classes.delete(name); },
            contains(name) { return classes.has(name); },
        };
        this.dataset = { min: "20", max: "20000" };
    }
    addEventListener() {}
    focus() { sandbox.document.activeElement = this; }
    setAttribute(name, value) { this[name] = String(value); }
    querySelector() { return { style: {} }; }
    getBoundingClientRect() { return { left: 0, top: 0, width: 200, height: 120 }; }
    setPointerCapture() {}
}

const els = {};
for (const id of SELECT_IDS) els[id] = new Select(id);
const documentListeners = {};

const calls = [];
const bridge = new Proxy({}, {
    get(_t, name) {
        if (name === "then") return undefined;
        return (...args) => calls.push([String(name), ...args]);
    },
    has() { return true; },
});

const sandbox = {
    console,
    document: {
        activeElement: null,
        getElementById(id) {
            if (id === "preview-canvas") return null;          // skip canvas painting
            if (!(id in els)) els[id] = new Generic(id);
            return els[id];
        },
        createElement() { return new Option(); },
        addEventListener(ev, cb) {
            (documentListeners[ev] = documentListeners[ev] || []).push(cb);
        },
    },
    window: null,
    bridge,
};
sandbox.window = sandbox;
sandbox.globalThis = sandbox;
vm.createContext(sandbox);

const src = fs.readFileSync(path.join(__dirname, "..", "dashboard_v2", "script.js"), "utf8");
vm.runInContext(src, sandbox);

// ── Assertions ─────────────────────────────────────────────────────────
let failed = 0;
function check(name, cond, extra) {
    if (cond) { console.log("  PASS  " + name); }
    else { failed++; console.log("  FAIL  " + name + (extra ? "  -> " + extra : "")); }
}
function section(t) { console.log("\n" + t); }

const preset = els["preset-select"];
const program = els["program-select"];
const mono = els["mono-output-select"];
const W = sandbox.window;

// ── 1. isSelectOpen ────────────────────────────────────────────────────
section("isSelectOpen");
check("false when nothing focused", W.isSelectOpen() === false);
sandbox.document.activeElement = program;
check("true for any select when one is focused", W.isSelectOpen() === true);
check("true for the focused select", W.isSelectOpen(program) === true);
check("false for a different select", W.isSelectOpen(preset) === false);
sandbox.document.activeElement = null;

// ── 2. Preset restore across two-signal arrival ────────────────────────
section("preset restore (saved id arrives before the lists)");
W.onSelectedPresetChanged(JSON.stringify({ id: "profile:Footsteps - CS2", dirty: false }));
check("nothing applied while both lists are empty", calls.length === 0, JSON.stringify(calls));
W.onPresetsChanged(JSON.stringify(
    [{ id: "builtin:all-sounds", name: "All Sounds" }]));
check("still nothing after builtins-only", calls.length === 0, JSON.stringify(calls));
W.onProfilesChanged(JSON.stringify({ "Footsteps - CS2": { freq_low: 150 } }));
check("dropdown shows the saved preset", preset.value === "profile:Footsteps - CS2", preset.value);
// The restore must be SILENT. Re-applying the entry is what let a saved profile
// overwrite the accent colour, thickness and sliders the user changed after
// choosing it - so zero bridge traffic is the assertion that guards that fix.
check("restore selects without applying anything", calls.length === 0, JSON.stringify(calls));

// A real user pick, by contrast, both applies and persists.
W.AR.applyPreset("builtin:all-sounds");
check("user pick applies", calls.some(c => c[0] === "apply_preset" && c[1] === "builtin:all-sounds"),
    JSON.stringify(calls));
check("user pick persists", calls.some(c => c[0] === "set_selected_preset" && c[1] === "builtin:all-sounds"),
    JSON.stringify(calls));

const before = calls.length;
W.onProfilesChanged(JSON.stringify({ "My CS2": { freq_low: 200 } }));
check("later rebuild does not re-apply", calls.length === before, JSON.stringify(calls.slice(before)));
check("selection survives the rebuild", preset.value === "builtin:all-sounds", preset.value);
check("profile is labelled for display", preset.labels().includes("My CS2"), preset.labels().join("|"));

// ── 3. Stale saved id must not eat the selection ───────────────────────
section("stale saved profile id");
W.onSelectedPresetChanged(JSON.stringify({ id: "profile:Deleted Profile", dirty: false }));
preset.value = "profile:My CS2";
W.onPresetsChanged(JSON.stringify([{ id: "builtin:all-sounds", name: "All Sounds" }]));
check("keeps the current pick", preset.value === "profile:My CS2", preset.value);
check("never lands on a blank selection", preset.selectedIndex !== -1, String(preset.selectedIndex));
W.onProfilesChanged(JSON.stringify({ "My CS2": { freq_low: 200 } }));
check("still keeps it after a second rebuild", preset.value === "profile:My CS2", preset.value);

// ── 4. Deferral while a popup is open ──────────────────────────────────
section("deferred fill (popup open)");
W._presetRestored = true;
program.value = "";
W._programsSig = null;
W.onProgramsChanged(JSON.stringify(["cs2.exe", "Discord.exe"]));
check("baseline list built", program.labels().join(",") === "All (system audio),cs2.exe,Discord.exe",
    program.labels().join(","));
program.value = "cs2.exe";

sandbox.document.activeElement = program;                 // user opens the popup
W.onProgramsChanged(JSON.stringify(["cs2.exe", "Discord.exe", "chrome.exe"]));
check("options untouched while open", program.options.length === 3, String(program.options.length));
check("selection untouched while open", program.value === "cs2.exe", program.value);

program.fire("blur");                                     // user clicks away
sandbox.document.activeElement = null;
check("queued rebuild applied on blur", program.options.length === 4, String(program.options.length));
check("selection preserved through the flush", program.value === "cs2.exe", program.value);

// ── 5. User picks during a deferred rebuild ────────────────────────────
section("user picks while a rebuild is queued");
sandbox.document.activeElement = program;
W._programsSig = null;
W.onProgramsChanged(JSON.stringify(["cs2.exe", "Discord.exe"]));   // chrome.exe disappears
program.value = "Discord.exe";                                     // user picks it in the popup
program.fire("change");
sandbox.document.activeElement = null;
check("user's pick wins over the queued rebuild", program.value === "Discord.exe", program.value);
check("queued options did land", program.options.length === 3, String(program.options.length));

// ── 6. Selected program stops playing ──────────────────────────────────
section("selected program drops off the list");
W._programsSig = null;
W.onProgramsChanged(JSON.stringify(["cs2.exe"]));
check("falls back to All (system audio)", program.value === "all", program.value);
check("not a blank selection", program.selectedIndex === 0, String(program.selectedIndex));

// ── 7. Mono select keeps "" as a real value ────────────────────────────
section("mono output select");
W.onMonoStateChanged(JSON.stringify({ devices: ["Headphones", "CABLE Input"], default: "Headphones",
    cable: "CABLE Input", enabled: false, selected: "" }));
check("System default selectable via empty value", mono.selectedIndex === 0, String(mono.selectedIndex));
W.onMonoStateChanged(JSON.stringify({ devices: ["Headphones", "CABLE Input"], default: "Headphones",
    cable: "CABLE Input", enabled: true, selected: "CABLE Input" }));
check("named device selected", mono.value === "CABLE Input", mono.value);

// ── 8. Dashboard text selection and context menu are disabled ─────────
section("text selection and context menu");
const selectEvent = {
    defaultPrevented: false,
    target: { closest() { return null; } },
    preventDefault() { this.defaultPrevented = true; },
};
documentListeners.selectstart[0](selectEvent);
check("text selection is prevented", selectEvent.defaultPrevented === true);

const contextMenuEvent = {
    defaultPrevented: false,
    target: { closest() { return null; } },
    preventDefault() { this.defaultPrevented = true; },
};
documentListeners.contextmenu[0](contextMenuEvent);
check("right click is prevented", contextMenuEvent.defaultPrevented === true);

const editableContextMenuEvent = {
    defaultPrevented: false,
    target: { closest() { return {}; } },
    preventDefault() { this.defaultPrevented = true; },
};
documentListeners.contextmenu[0](editableContextMenuEvent);
check("editable fields keep their context menu", editableContextMenuEvent.defaultPrevented === false);

const editableSelectEvent = {
    defaultPrevented: false,
    target: { closest() { return {}; } },
    preventDefault() { this.defaultPrevented = true; },
};
documentListeners.selectstart[0](editableSelectEvent);
check("editable fields keep text selection", editableSelectEvent.defaultPrevented === false);

// ── 9. Dashboard-native preset modals ─────────────────────────────────
section("preset modals");
const presetNameInput = sandbox.document.getElementById("preset-name-input");
const presetNewButton = sandbox.document.getElementById("preset-new-btn");
calls.splice(0);
presetNameInput.value = "stale";
presetNewButton.focus();
W.AR.addPreset();
check("new preset opens a modal", !els["preset-create-modal"].classList.contains("is-hidden"));
check("new preset focuses its input", sandbox.document.activeElement === presetNameInput);
check("new preset clears the previous value", presetNameInput.value === "", presetNameInput.value);

W.AR.createPreset({ preventDefault() {} });
check("blank name is rejected inline", /Enter a preset name/.test(els["preset-create-error"].textContent));
check("blank name does not call the bridge", calls.length === 0, JSON.stringify(calls));

W._profiles = { "Existing Name": { freq_low: 150 } };
presetNameInput.value = " existing name ";
W.AR.createPreset({ preventDefault() {} });
check("case-insensitive duplicate is rejected", /already exists/.test(els["preset-create-error"].textContent));
check("duplicate does not call the bridge", calls.length === 0, JSON.stringify(calls));

presetNameInput.value = "New Preset";
W.AR.createPreset({ preventDefault() {} });
check("valid name saves a profile", calls.some(c => c[0] === "save_profile" &&
    JSON.parse(c[1]).name === "New Preset"), JSON.stringify(calls));
check("valid name selects its profile", preset.value === "profile:New Preset", preset.value);
check("valid name closes the modal", els["preset-create-modal"].classList.contains("is-hidden"));

calls.splice(0);
preset.value = "profile:New Preset";
W.AR.deletePreset();
check("delete opens a confirmation modal", !els["preset-delete-modal"].classList.contains("is-hidden"));
check("delete captures the selected name", els["preset-delete-name"].textContent === "New Preset");
W.AR.closePresetDeleteModal();
check("delete cancel makes no bridge call", calls.length === 0, JSON.stringify(calls));

W.AR.deletePreset();
const deleteEnterEvent = { key: "Enter", defaultPrevented: false,
    preventDefault() { this.defaultPrevented = true; } };
documentListeners.keydown[0](deleteEnterEvent);
check("delete Enter confirms the captured profile", calls.some(c => c[0] === "delete_profile" && c[1] === "New Preset"),
    JSON.stringify(calls));
check("delete Enter is consumed", deleteEnterEvent.defaultPrevented === true);

// ── 10. Dashboard-native color modal ──────────────────────────────────
section("color modal");
calls.splice(0);
const accentColor = sandbox.document.getElementById("accent-color");
accentColor.dataset.color = "#9751F2";
accentColor.focus();
W.AR.openColorModal();
check("color modal opens with its HEX field focused", sandbox.document.activeElement === els["color-hex-input"]);
els["color-hex-input"].value = "#bad";
W.AR.applyColorModal({ preventDefault() {} });
check("invalid HEX remains inline", /six-digit HEX/.test(els["color-error"].textContent));
check("invalid HEX makes no appearance calls", calls.length === 0, JSON.stringify(calls));

W.AR.closeColorModal();
check("color cancel makes no appearance calls", calls.length === 0, JSON.stringify(calls));
W.AR.openColorModal();
els["color-hex-input"].value = "aabbcc";
W.AR.applyColorModal({ preventDefault() {} });
check("valid color sends normalized color", calls.some(c => c[0] === "set_accent_color" && c[1] === "#AABBCC"),
    JSON.stringify(calls));
check("valid color commits appearance", calls.some(c => c[0] === "commit_appearance"), JSON.stringify(calls));

calls.splice(0);
W.AR.openColorModal();
const escapeEvent = { key: "Escape", defaultPrevented: false,
    preventDefault() { this.defaultPrevented = true; } };
documentListeners.keydown[0](escapeEvent);
check("Escape closes the active modal", els["color-modal"].classList.contains("is-hidden"));
check("Escape discards color changes", calls.length === 0, JSON.stringify(calls));
check("Escape is consumed", escapeEvent.defaultPrevented === true);

const html = fs.readFileSync(path.join(__dirname, "..", "dashboard_v2", "index.html"), "utf8");
check("each new modal backdrop closes without submitting", /color-modal[\s\S]*?onclick="AR\.closeColorModal\(\)"/.test(html) &&
    /preset-create-modal[\s\S]*?onclick="AR\.closePresetCreateModal\(\)"/.test(html) &&
    /preset-delete-modal[\s\S]*?onclick="AR\.closePresetDeleteModal\(\)"/.test(html));

console.log(failed === 0 ? "\nALL PASS" : `\n${failed} FAILED`);
process.exit(failed === 0 ? 0 : 1);
