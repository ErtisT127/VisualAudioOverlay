// Dashboard interaction regression tests for dashboard_v2/script.js.
//
// Run:  node tests/test_dropdowns.js       (exit 0 = pass)
//
// Loads the real script.js against a minimal DOM stub; the bootstrap never
// fires because the stub swallows DOMContentLoaded.
//
// Covers the things that broke in this area:
//  - Every select (monitor / program / preset) is a live DOM menu - not a
//    native <select>. QtWebEngine draws native popups as
//    unstyled Qt widgets and cannot rebuild them while open, which is why a
//    list only appeared after the user had already picked an entry.
//  - Lists refresh WHILE open: a change arriving mid-open re-renders the
//    rows in place and keeps the selection.
//  - The saved preset stable id being restored across the two separate
//    signals that build the dropdown, silently.
const fs = require("fs");
const path = require("path");
const vm = require("vm");

function classListFor() {
  const classes = new Set();
  return {
    add(name) {
      classes.add(name);
    },
    remove(name) {
      classes.delete(name);
    },
    contains(name) {
      return classes.has(name);
    },
    toggle(name, on) {
      if (on === undefined) on = !classes.has(name);
      if (on) classes.add(name);
      else classes.delete(name);
    },
  };
}

class Option {
  constructor() {
    this.value = "";
    this.textContent = "";
    this.className = "";
    this.type = "";
    this.classList = classListFor();
  }
  addEventListener() {}
  setAttribute(name, value) {
    this[name] = String(value);
  }
  getAttribute(name) {
    return this[name] === undefined ? null : this[name];
  }
}

class Generic {
  constructor(id) {
    this.id = id;
    this.value = "";
    this.textContent = "";
    this.checked = false;
    this.children = [];
    this.style = {
      setProperty(name, value) {
        this[name] = value;
      },
    };
    const classes = new Set(id.includes("modal") ? ["is-hidden"] : []);
    this.classList = {
      toggle(name, on) {
        if (on) classes.add(name);
        else classes.delete(name);
      },
      add(name) {
        classes.add(name);
      },
      remove(name) {
        classes.delete(name);
      },
      contains(name) {
        return classes.has(name);
      },
    };
    this.dataset = { min: "20", max: "20000" };
  }
  addEventListener() {}
  focus() {
    sandbox.document.activeElement = this;
  }
  setAttribute(name, value) {
    this[name] = String(value);
  }
  querySelector() {
    return { style: {} };
  }
  getBoundingClientRect() {
    return { left: 0, top: 0, width: 200, height: 120 };
  }
  setPointerCapture() {}
  replaceChildren() {
    this.children = [];
  }
  appendChild(child) {
    this.children.push(child);
  }
}

const els = {};
const documentListeners = {};

const calls = [];
const bridge = new Proxy(
  {},
  {
    get(_t, name) {
      if (name === "then") return undefined;
      return (...args) => calls.push([String(name), ...args]);
    },
    has() {
      return true;
    },
  },
);

const sandbox = {
  console,
  document: {
    activeElement: null,
    getElementById(id) {
      if (id === "preview-canvas") return null; // skip canvas painting
      if (!(id in els)) els[id] = new Generic(id);
      return els[id];
    },
    createElement() {
      return new Option();
    },
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
  if (cond) {
    console.log("  PASS  " + name);
  } else {
    failed++;
    console.log("  FAIL  " + name + (extra ? "  -> " + extra : ""));
  }
}
function section(t) {
  console.log("\n" + t);
}
function hasCall(prefix, ...args) {
  return calls.some((c) => c[0] === prefix && args.every((a, i) => c[i + 1] === a));
}

const W = sandbox.window;
// Menus and labels auto-vivify on first render, so resolve them at use time.
const menuEl = (id) => els[id + "-menu"];
const labelEl = (id) => els[id + "-label"];

// ── 1. All selects are live DOM menus (no native <select> left) ────────
section("structure: every select is a live DOM menu");
const html = fs.readFileSync(path.join(__dirname, "..", "dashboard_v2", "index.html"), "utf8");
// The word "<select>" itself appears in design comments; strip those first.
const htmlNoComments = html.replace(/<!--[\s\S]*?-->/g, "");
check("no native <select> remains", !/<select/.test(htmlNoComments));
// Attributes sit one per line, so search with a bounded cross-line gap.
const close = '[\\s\\S]{0,120}?class="';
for (const id of ["monitor-select", "program-select", "preset-select"]) {
  check(
    id + " is a live trigger",
    new RegExp('id="' + id + '"' + close + 'select live-select"').test(htmlNoComments),
  );
  check(
    id + " has a label span",
    new RegExp('id="' + id + '-label" class="live-label"').test(htmlNoComments),
  );
  check(
    id + " has a live menu",
    new RegExp('id="' + id + '-menu"' + close + 'live-menu"').test(htmlNoComments),
  );
}
check("program menu opens downward (no --up variant)", !/live-menu--up/.test(html));
const css = fs.readFileSync(path.join(__dirname, "..", "dashboard_v2", "style.css"), "utf8");
check("no orphaned .select-wrap rules", !/select-wrap/.test(css));
check("no orphaned --up direction rules", !/live-menu--up/.test(css));
check("live menus style a themed scrollbar", /live-menu::-webkit-scrollbar/.test(css));
check("row cards let menus escape", /\.row \.card \{[\s\S]*?overflow: visible/.test(css));

// ── 2. Preset restore across two-signal arrival ────────────────────────
section("preset restore (saved id arrives before the lists)");
W.onSelectedPresetChanged(JSON.stringify({ id: "profile:Footsteps - CS2", dirty: false }));
check("nothing applied while both lists are empty", calls.length === 0, JSON.stringify(calls));
W.onPresetsChanged(JSON.stringify([{ id: "builtin:all-sounds", name: "All Sounds" }]));
check("still nothing after builtins-only", calls.length === 0, JSON.stringify(calls));
W.onProfilesChanged(JSON.stringify({ "Footsteps - CS2": { freq_low: 150 } }));
check(
  "dropdown shows the saved preset",
  W.liveSelected("preset-select") === "profile:Footsteps - CS2",
  W.liveSelected("preset-select"),
);
check(
  "trigger label shows the saved preset",
  labelEl("preset-select").textContent === "Footsteps - CS2",
  labelEl("preset-select").textContent,
);
// The restore must be SILENT. Re-applying the entry is what let a saved profile
// overwrite the accent colour, thickness and sliders the user changed after
// choosing it - so zero bridge traffic is the assertion that guards that fix.
check("restore selects without applying anything", calls.length === 0, JSON.stringify(calls));

// A real user pick, by contrast, both applies and persists.
W.AR.applyPreset("builtin:all-sounds");
check(
  "user pick applies",
  calls.some((c) => c[0] === "apply_preset" && c[1] === "builtin:all-sounds"),
  JSON.stringify(calls),
);
check(
  "user pick persists",
  calls.some((c) => c[0] === "set_selected_preset" && c[1] === "builtin:all-sounds"),
  JSON.stringify(calls),
);

const before = calls.length;
W.onProfilesChanged(JSON.stringify({ "My CS2": { freq_low: 200 } }));
check(
  "later rebuild does not re-apply",
  calls.length === before,
  JSON.stringify(calls.slice(before)),
);
check(
  "selection survives the rebuild",
  W.liveSelected("preset-select") === "builtin:all-sounds",
  W.liveSelected("preset-select"),
);
check(
  "profile is labelled for display",
  W.liveOptions("preset-select")
    .map((o) => o.label)
    .includes("My CS2"),
  W.liveOptions("preset-select")
    .map((o) => o.label)
    .join("|"),
);

// ── 3. Stale saved id must not eat the selection ───────────────────────
section("stale saved profile id");
W.onSelectedPresetChanged(JSON.stringify({ id: "profile:Deleted Profile", dirty: false }));
W.liveSelect("preset-select", "profile:My CS2");
W.onPresetsChanged(JSON.stringify([{ id: "builtin:all-sounds", name: "All Sounds" }]));
check(
  "keeps the current pick",
  W.liveSelected("preset-select") === "profile:My CS2",
  W.liveSelected("preset-select"),
);
check(
  "never lands on a blank selection",
  W.liveSelected("preset-select") !== null,
  String(W.liveSelected("preset-select")),
);
W.onProfilesChanged(JSON.stringify({ "My CS2": { freq_low: 200 } }));
check(
  "still keeps it after a second rebuild",
  W.liveSelected("preset-select") === "profile:My CS2",
  W.liveSelected("preset-select"),
);

// ── 4. Program live menu ───────────────────────────────────────────────
section("program live menu (list, open, hot refresh, pick)");
calls.splice(0);
W._programsSig = null;
W.onProgramsChanged(JSON.stringify(["cs2.exe", "Discord.exe"]));
check(
  "list lands with All (system audio) first",
  W.liveOptions("program-select")
    .map((o) => o.label)
    .join(",") === "All (system audio),cs2.exe,Discord.exe",
  W.liveOptions("program-select")
    .map((o) => o.label)
    .join(","),
);
check("defaults to All (system audio)", W.liveSelected("program-select") === "all");
check(
  "trigger label shows the current pick",
  labelEl("program-select").textContent === "All (system audio)",
  labelEl("program-select").textContent,
);
check("menu closed renders no rows", menuEl("program-select").children.length === 0);
check("list push itself is silent", calls.length === 0, JSON.stringify(calls));

// Identical re-push is deduplicated by the signature guard.
W.onProgramsChanged(JSON.stringify(["cs2.exe", "Discord.exe"]));
check("identical list does not re-render", menuEl("program-select").children.length === 0);

// Opening re-enumerates: this is the hot-refresh contract - a game that
// started after the app launched must be present when the user looks.
calls.splice(0);
W.AR.toggleLiveSelect("program-select");
check("open requests a fresh program list", hasCall("refresh_programs"), JSON.stringify(calls));
check("menu is open", W.liveIsOpen("program-select"));
check("menu is visible", menuEl("program-select").classList.contains("is-open"));
check(
  "menu rows built",
  menuEl("program-select").children.length === 3,
  String(menuEl("program-select").children.length),
);
check(
  "trigger marks the popup state",
  els["program-select"]["aria-expanded"] === "true",
  els["program-select"]["aria-expanded"],
);
const optLabels = menuEl("program-select").children.map((b) => b.textContent);
check(
  "rows carry role and labels",
  optLabels.join(",") === "All (system audio),cs2.exe,Discord.exe" &&
    menuEl("program-select").children.every((b) => b.getAttribute?.("role") === "option"),
  optLabels.join(","),
);

// ── 5. The regression: the list changes WHILE the menu is open ─────────
section("program list changes while open (the old Qt popup could not do this)");
calls.splice(0);
W._programsSig = null;
W.onProgramsChanged(JSON.stringify(["cs2.exe", "Discord.exe", "chrome.exe"]));
check("new program appears without closing the menu", W.liveIsOpen("program-select"));
check(
  "selection survives the live rebuild",
  W.liveSelected("program-select") === "all",
  W.liveSelected("program-select"),
);
check(
  "menu rows rebuilt in place",
  menuEl("program-select").children.length === 4,
  String(menuEl("program-select").children.length),
);
check("rebuild itself makes no bridge call", calls.length === 0, JSON.stringify(calls));

calls.splice(0);
W.AR.pickLiveOption("program-select", "cs2.exe");
check("picking closes the menu", !W.liveIsOpen("program-select"));
check("picking selects the entry", W.liveSelected("program-select") === "cs2.exe");
check(
  "picking updates the trigger label",
  labelEl("program-select").textContent === "cs2.exe",
  labelEl("program-select").textContent,
);
check(
  "picking returns focus to the trigger",
  sandbox.document.activeElement === els["program-select"],
);
check("picking tells the backend", hasCall("set_program", "cs2.exe"), JSON.stringify(calls));
check("closed trigger clears the popup state", els["program-select"]["aria-expanded"] === "false");

// The chosen program stops playing and drops off the list -> fall back to "all".
calls.splice(0);
W._programsSig = null;
W.onProgramsChanged(JSON.stringify(["Discord.exe"]));
check(
  "dropped program falls back to All (system audio)",
  W.liveSelected("program-select") === "all",
  W.liveSelected("program-select"),
);
check(
  "fallback label follows",
  labelEl("program-select").textContent === "All (system audio)",
  labelEl("program-select").textContent,
);
check("fallback is silent", calls.length === 0, JSON.stringify(calls));

// ── 6. Dismissal: Escape and outside clicks ────────────────────────────
section("live menu dismissal");
W.AR.toggleLiveSelect("program-select");
check("reopen works", W.liveIsOpen("program-select"));

const escapeEvent = {
  key: "Escape",
  defaultPrevented: false,
  preventDefault() {
    this.defaultPrevented = true;
  },
};
// keydown[0] is the modal handler; keydown[1] dismisses live dropdowns.
documentListeners.keydown[1](escapeEvent);
check("Escape closes the live menu", !W.liveIsOpen("program-select"));
check("Escape consumed", escapeEvent.defaultPrevented === true);

W.AR.toggleLiveSelect("program-select");
const insideEvent = {
  target: {
    closest() {
      return {};
    },
  },
};
documentListeners.pointerdown[0](insideEvent);
check("click inside the menu keeps it open", W.liveIsOpen("program-select"));
const outsideEvent = {
  target: {
    closest() {
      return null;
    },
  },
};
documentListeners.pointerdown[0](outsideEvent);
check("click outside closes it", !W.liveIsOpen("program-select"));

// ── 7. Monitor live menu ───────────────────────────────────────────────
section("monitor live menu");
calls.splice(0);
W.onMonitorsChanged(
  JSON.stringify([
    { name: "R27U91-JN", idx: 0 },
    { name: "DELL-U2723", idx: 1 },
  ]),
);
check(
  "list lands",
  W.liveOptions("monitor-select")
    .map((o) => o.label)
    .join(",") === "R27U91-JN,DELL-U2723",
  W.liveOptions("monitor-select")
    .map((o) => o.label)
    .join(","),
);
check("empty selection falls back to the first monitor", W.liveSelected("monitor-select") === "0");
W.onSelectedMonitorChanged(1);
check(
  "authoritative selection applies",
  W.liveSelected("monitor-select") === "1",
  W.liveSelected("monitor-select"),
);
check(
  "label follows",
  labelEl("monitor-select").textContent === "DELL-U2723",
  labelEl("monitor-select").textContent,
);
check("selection push is silent", calls.length === 0, JSON.stringify(calls));

calls.splice(0);
W.AR.toggleLiveSelect("monitor-select");
check(
  "opening monitor requests a fresh screen list",
  hasCall("refresh_monitors"),
  JSON.stringify(calls),
);
check(
  "menu rows built",
  menuEl("monitor-select").children.length === 2,
  String(menuEl("monitor-select").children.length),
);

calls.splice(0);
W.AR.pickLiveOption("monitor-select", "0");
check("picking a monitor selects it", W.liveSelected("monitor-select") === "0");
check("picking a monitor tells the backend", hasCall("set_monitor", 0), JSON.stringify(calls));
check("picking closes the menu", !W.liveIsOpen("monitor-select"));

// ── 8. Preset live menu: open without backend churn, hot rebuild ───────
section("preset live menu (open, hot refresh, pick)");
calls.splice(0);
W.AR.toggleLiveSelect("preset-select");
check("opening preset asks for no backend enumeration", calls.length === 0, JSON.stringify(calls));
check("preset menu is open", W.liveIsOpen("preset-select"));
check(
  "preset rows built from builtins + profiles",
  menuEl("preset-select").children.length === 2,
  String(menuEl("preset-select").children.length),
);
const rowsLabels = menuEl("preset-select").children.map((b) => b.textContent);
check(
  "rows show builtin and profile",
  rowsLabels.join(",") === "All Sounds,My CS2",
  rowsLabels.join(","),
);
check(
  "current pick is flagged in the menu",
  menuEl("preset-select").children[1].classList.contains("is-selected") &&
    menuEl("preset-select").children[1].getAttribute("aria-selected") === "true",
  menuEl("preset-select")
    .children.map((b) => b.getAttribute("aria-selected"))
    .join(","),
);

// A profile appears (created in another session / renamed) WHILE open.
calls.splice(0);
W.onProfilesChanged(
  JSON.stringify({
    "My CS2": { freq_low: 200 },
    "Footsteps - CS2": { freq_low: 150 },
  }),
);
check("profile list change does not close the menu", W.liveIsOpen("preset-select"));
check(
  "selection survives the live preset rebuild",
  W.liveSelected("preset-select") === "profile:My CS2",
  W.liveSelected("preset-select"),
);
check(
  "preset rows rebuilt in place",
  menuEl("preset-select").children.length === 3,
  String(menuEl("preset-select").children.length),
);
check("preset rebuild itself is silent", calls.length === 0, JSON.stringify(calls));

// The selected profile is removed externally while open -> first entry.
W.onProfilesChanged(JSON.stringify({ "Footsteps - CS2": { freq_low: 150 } }));
check(
  "removed profile falls back to the first entry",
  W.liveSelected("preset-select") === "builtin:all-sounds",
  W.liveSelected("preset-select"),
);
check(
  "fallback label follows",
  labelEl("preset-select").textContent === "All Sounds",
  labelEl("preset-select").textContent,
);

calls.splice(0);
W.AR.pickLiveOption("preset-select", "builtin:all-sounds");
check("picking a preset closes the menu", !W.liveIsOpen("preset-select"));
check(
  "picking a preset applies it",
  hasCall("apply_preset", "builtin:all-sounds"),
  JSON.stringify(calls),
);

// ── 9. Frozen dashboard refuses interaction ────────────────────────────
section("frozen dashboard");
calls.splice(0);
W.setDashboardFrozen(true);
W.AR.toggleLiveSelect("monitor-select");
check("frozen dashboard does not open a live menu", !W.liveIsOpen("monitor-select"));
W.AR.refreshDropdowns();
check("frozen dashboard does not refresh either list", calls.length === 0, JSON.stringify(calls));
W.AR.toggleLiveSelect("program-select");
check("frozen dashboard cannot reopen", !W.liveIsOpen("program-select"));
W.setDashboardFrozen(false);

// ── 11. Dashboard text selection and context menu are disabled ─────────
section("text selection and context menu");
const selectEvent = {
  defaultPrevented: false,
  target: {
    closest() {
      return null;
    },
  },
  preventDefault() {
    this.defaultPrevented = true;
  },
};
documentListeners.selectstart[0](selectEvent);
check("text selection is prevented", selectEvent.defaultPrevented === true);

const contextMenuEvent = {
  defaultPrevented: false,
  target: {
    closest() {
      return null;
    },
  },
  preventDefault() {
    this.defaultPrevented = true;
  },
};
documentListeners.contextmenu[0](contextMenuEvent);
check("right click is prevented", contextMenuEvent.defaultPrevented === true);

const editableContextMenuEvent = {
  defaultPrevented: false,
  target: {
    closest() {
      return {};
    },
  },
  preventDefault() {
    this.defaultPrevented = true;
  },
};
documentListeners.contextmenu[0](editableContextMenuEvent);
check(
  "editable fields keep their context menu",
  editableContextMenuEvent.defaultPrevented === false,
);

const editableSelectEvent = {
  defaultPrevented: false,
  target: {
    closest() {
      return {};
    },
  },
  preventDefault() {
    this.defaultPrevented = true;
  },
};
documentListeners.selectstart[0](editableSelectEvent);
check("editable fields keep text selection", editableSelectEvent.defaultPrevented === false);

// ── 12. Dashboard-native preset modals ─────────────────────────────────
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
check(
  "blank name is rejected inline",
  /Enter a preset name/.test(els["preset-create-error"].textContent),
);
check("blank name does not call the bridge", calls.length === 0, JSON.stringify(calls));

W._profiles = { "Existing Name": { freq_low: 150 } };
presetNameInput.value = " existing name ";
W.AR.createPreset({ preventDefault() {} });
check(
  "case-insensitive duplicate is rejected",
  /already exists/.test(els["preset-create-error"].textContent),
);
check("duplicate does not call the bridge", calls.length === 0, JSON.stringify(calls));

presetNameInput.value = "New Preset";
W.AR.createPreset({ preventDefault() {} });
check(
  "valid name saves a profile",
  calls.some((c) => c[0] === "save_profile" && JSON.parse(c[1]).name === "New Preset"),
  JSON.stringify(calls),
);
check(
  "valid name selects its profile",
  W.liveSelected("preset-select") === "profile:New Preset",
  W.liveSelected("preset-select"),
);
check("valid name labels the trigger", labelEl("preset-select").textContent === "New Preset");
check("valid name closes the modal", els["preset-create-modal"].classList.contains("is-hidden"));

calls.splice(0);
W.AR.deletePreset();
check(
  "delete opens a confirmation modal",
  !els["preset-delete-modal"].classList.contains("is-hidden"),
);
check("delete captures the selected name", els["preset-delete-name"].textContent === "New Preset");
W.AR.closePresetDeleteModal();
check("delete cancel makes no bridge call", calls.length === 0, JSON.stringify(calls));

W.AR.deletePreset();
const deleteEnterEvent = {
  key: "Enter",
  defaultPrevented: false,
  preventDefault() {
    this.defaultPrevented = true;
  },
};
documentListeners.keydown[0](deleteEnterEvent);
check(
  "delete Enter confirms the captured profile",
  calls.some((c) => c[0] === "delete_profile" && c[1] === "New Preset"),
  JSON.stringify(calls),
);
check("delete Enter is consumed", deleteEnterEvent.defaultPrevented === true);

// ── 13. Dashboard-native color modal ───────────────────────────────────
section("color modal");
calls.splice(0);
const accentColor = sandbox.document.getElementById("accent-color");
accentColor.dataset.color = "#9751F2";
accentColor.focus();
W.AR.openColorModal();
check(
  "color modal opens with its HEX field focused",
  sandbox.document.activeElement === els["color-hex-input"],
);
els["color-hex-input"].value = "#bad";
W.AR.applyColorModal({ preventDefault() {} });
check("invalid HEX remains inline", /six-digit HEX/.test(els["color-error"].textContent));
check("invalid HEX makes no appearance calls", calls.length === 0, JSON.stringify(calls));

W.AR.closeColorModal();
check("color cancel makes no appearance calls", calls.length === 0, JSON.stringify(calls));
W.AR.openColorModal();
els["color-hex-input"].value = "aabbcc";
W.AR.applyColorModal({ preventDefault() {} });
check(
  "valid color sends normalized color",
  calls.some((c) => c[0] === "set_accent_color" && c[1] === "#AABBCC"),
  JSON.stringify(calls),
);
check(
  "valid color commits appearance",
  calls.some((c) => c[0] === "commit_appearance"),
  JSON.stringify(calls),
);

calls.splice(0);
W.AR.openColorModal();
const modalEscapeEvent = {
  key: "Escape",
  defaultPrevented: false,
  preventDefault() {
    this.defaultPrevented = true;
  },
};
documentListeners.keydown[0](modalEscapeEvent);
check("Escape closes the active modal", els["color-modal"].classList.contains("is-hidden"));
check("Escape discards color changes", calls.length === 0, JSON.stringify(calls));
check("Escape is consumed", modalEscapeEvent.defaultPrevented === true);

check(
  "each modal backdrop closes without submitting",
  /color-modal[\s\S]*?onclick="AR\.closeColorModal\(\)"/.test(html) &&
    /preset-create-modal[\s\S]*?onclick="AR\.closePresetCreateModal\(\)"/.test(html) &&
    /preset-delete-modal[\s\S]*?onclick="AR\.closePresetDeleteModal\(\)"/.test(html),
);

// ── 14. Per-app gate: unsupported OS disables the program dropdown ──────
section("per-app gate (unsupported OS)");
calls.splice(0);
W.onProgramsSupportedChanged(false);
check("gate flags the field", W.liveDisabled("program-select"));
check(
  "trigger is aria-disabled",
  els["program-select"]["aria-disabled"] === "true",
  els["program-select"]["aria-disabled"],
);
check(
  "trigger carries the is-disabled class",
  els["program-select"].classList.contains("is-disabled"),
);
check(
  "trigger hover title explains the gate",
  /Windows 11/.test(els["program-select"].title || ""),
  els["program-select"].title,
);
check(
  "label hover title explains the gate too",
  /Windows 11/.test(labelEl("program-select").title || ""),
  labelEl("program-select").title,
);
check(
  "selection stays on All (system audio)",
  W.liveSelected("program-select") === "all",
  W.liveSelected("program-select"),
);

// Interacting with the gated field must be inert: no open, no re-enumeration.
W.AR.toggleLiveSelect("program-select");
check("gated toggle does not open", !W.liveIsOpen("program-select"));
check("gated toggle asks for no fresh list", !hasCall("refresh_programs"), JSON.stringify(calls));
calls.splice(0);
W.AR.pickLiveOption("program-select", "Discord.exe");
check("gated pick is ignored", W.liveSelected("program-select") === "all");
check("gated pick tells the backend nothing", calls.length === 0, JSON.stringify(calls));

// A stale list push while gated must not resurrect the field.
W._programsSig = null;
calls.splice(0);
W.onProgramsChanged(JSON.stringify(["Discord.exe"]));
check("list push keeps the field gated", W.liveDisabled("program-select"));
check(
  "list push keeps the aria-disabled state",
  els["program-select"]["aria-disabled"] === "true",
  els["program-select"]["aria-disabled"],
);

// Supported OS re-arms the field, clears the gate chrome and restores titles.
W.onProgramsSupportedChanged(true);
check("gate lifts the flag", !W.liveDisabled("program-select"));
check(
  "trigger clears aria-disabled",
  els["program-select"]["aria-disabled"] === "false",
  els["program-select"]["aria-disabled"],
);
check(
  "trigger drops the is-disabled class",
  !els["program-select"].classList.contains("is-disabled"),
);
check("trigger clears the gate title", (els["program-select"].title || "") === "");
check(
  "label title returns to the current pick",
  labelEl("program-select").title === "All (system audio)",
  labelEl("program-select").title,
);
calls.splice(0);
W._lastDropdownRefresh = {}; // reset the open-debounce; earlier toggles are sub-250ms old
W.AR.toggleLiveSelect("program-select");
check("supported toggle opens again", W.liveIsOpen("program-select"));
check("supported toggle asks for a fresh list", hasCall("refresh_programs"), JSON.stringify(calls));
W.AR.closeLiveSelect("program-select");

console.log(failed === 0 ? "\nALL PASS" : `\n${failed} FAILED`);
process.exit(failed === 0 ? 0 : 1);
