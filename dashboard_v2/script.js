/**
 * Visual Audio Overlay - dashboard_v2 frontend
 * ═══════════════════════════════════════════════════════════════════════
 * Talks to the Python `Bridge` (main.py) over QWebChannel.
 *   JS → Python : window.bridge.method(arg)
 *   Python → JS : window.bridge.signal.connect(cb)
 *
 * Bridge methods used (already in main.py):
 *   start_radar, stop_radar, set_sensitivity(float), set_gain(float),
 *   set_freq_range(int low, int high), set_max_amplitude(float),
 *   apply_preset(str), set_monitor(int), set_accent_color(hex),
 *   set_stroke_width(int), save_profile(jsonStr), delete_profile(str),
 *   commit_audio_settings(), commit_appearance(),
 *   request_initial_data()
 *
 *   set_program(str)            - per-app capture target
 *   programsChanged(jsonStr)    - list of running audio programs
 *   programsSupportedChanged(bool) - per-app capture available on this OS; gates the dropdown
 *
 *   set_selected_preset(str)      - persist the dropdown choice to settings.json
 *   selectedPresetChanged(str)    - selected option state as JSON
 *   audioSettingsChanged(jsonStr) - the live sensitivity/gain/freq/max-amp, so the
 *                                   sliders follow a preset and come back on load
 *
 * Mono output (single-sided listeners) - guarded so the UI works without them:
 *   set_mono_enabled(bool)      - turn the in-app mono down-mix on/off
 *   set_mono_output(str)        - which real device the mono mix plays to
 *   install_vbcable()           - launch the bundled VB-CABLE installer
 *   monoStateChanged(jsonStr)   - {devices, default, cable, enabled, selected}
 * ═══════════════════════════════════════════════════════════════════════
 */

let radarActive = false;
let operationBusy = false;
let operationState = "idle";
let hotkeyState = { combo: "F8", status: "active", editing: false, registered: false, error: "" };
let pendingHotkey = "";
let hotkeyCapturing = false;
let hotkeyRequestBusy = false;
const hotkeyPressed = new Set();
let moveModeActive = false;
let presetState = { id: "builtin:all-sounds", dirty: false };
let presetApplying = false;
let activeDashboardModal = null;
let modalReturnFocus = null;
let colorDraft = null;
let pendingDeletePresetName = "";
const EDITABLE_SELECTOR = [
  'input[type="text"]',
  'input[type="search"]',
  'input[type="password"]',
  'input[type="email"]',
  'input[type="url"]',
  'input[type="tel"]',
  'input[type="number"]',
  "textarea",
  '[contenteditable]:not([contenteditable="false"])',
].join(", ");

// Project links (opened in the real browser via bridge.open_url). The update
// banner overrides updateUrl when a specific release page is known.
const REPO_URL = "https://github.com/ErtisT127/VisualAudioOverlay";
let updateUrl = REPO_URL + "/releases/latest";

function openExternal(url) {
  if (bridge && bridge.open_url) bridge.open_url(url);
}

// ── Bridge init ────────────────────────────────────────────────────────
function initBridge() {
  return new Promise((resolve) => {
    new QWebChannel(qt.webChannelTransport, function (channel) {
      window.bridge = channel.objects.bridge;

      bridge.statusChanged.connect(onStatusChanged);
      bridge.deviceChanged.connect(onDeviceChanged);
      bridge.profilesChanged.connect(onProfilesChanged);
      bridge.monitorsChanged.connect(onMonitorsChanged);
      if (bridge.selectedMonitorChanged)
        bridge.selectedMonitorChanged.connect(onSelectedMonitorChanged);
      bridge.presetsChanged.connect(onPresetsChanged);
      bridge.overlayPositionChanged.connect(onOverlayPositionChanged);
      // Optional new signals - only connect if the backend provides them.
      if (bridge.programsChanged) bridge.programsChanged.connect(onProgramsChanged);
      if (bridge.programsSupportedChanged)
        bridge.programsSupportedChanged.connect(onProgramsSupportedChanged);
      if (bridge.monoStateChanged) bridge.monoStateChanged.connect(onMonoStateChanged);
      if (bridge.updateAvailable) bridge.updateAvailable.connect(onUpdateAvailable);
      if (bridge.appearanceChanged) bridge.appearanceChanged.connect(onAppearanceChanged);
      if (bridge.audioSettingsChanged) bridge.audioSettingsChanged.connect(onAudioSettingsChanged);
      if (bridge.selectedPresetChanged)
        bridge.selectedPresetChanged.connect(onSelectedPresetChanged);
      if (bridge.operationBusyChanged) bridge.operationBusyChanged.connect(onOperationBusyChanged);
      if (bridge.operationStateChanged)
        bridge.operationStateChanged.connect(onOperationStateChanged);
      if (bridge.hotkeyStateChanged) bridge.hotkeyStateChanged.connect(onHotkeyStateChanged);

      // Show the current version in the footer.
      if (bridge.get_app_version) {
        bridge.get_app_version(function (v) {
          setText("footer-version", "v" + v);
        });
      }

      bridge.request_initial_data();

      // The Program list only contains apps that are currently playing audio.
      // Re-enumerate whenever the user returns to this window (e.g. alt-tabs
      // back from the game), so a game launched after startup shows up without
      // having to Start the radar first. (All dropdowns are DOM menus now, so
      // a refresh can never hit an open native popup - see the history note
      // next to the deferred-fill machinery that used to guard this.)
      window.addEventListener("focus", () => {
        AR.refreshDropdowns();
      });

      resolve();
    });
  });
}

// Backend found a newer GitHub release. Show the dismissible banner.
function onUpdateAvailable(version, url) {
  if (url) updateUrl = url;
  setText("update-banner-text", "Version " + version + " is available.");
  toggleClass("update-banner", "is-hidden", false);
}

// ── Signal handlers (Python → JS) ──────────────────────────────────────
function onStatusChanged(message, isActive) {
  radarActive = isActive;
  setText("status-text", message);
  syncToggleUI();
}

function onOperationBusyChanged(busy) {
  operationBusy = !!busy;
  syncToggleUI();
}

function onOperationStateChanged(state) {
  operationState = state || "idle";
  syncToggleUI();
}

function onHotkeyStateChanged(jsonStr) {
  try {
    hotkeyState = JSON.parse(jsonStr) || hotkeyState;
  } catch (_) {
    return;
  }
  const error = hotkeyState.error || "";
  setText("hotkey-error", error);
  renderDashboardHotkey();
  if (!hotkeyState.editing && !error && !hotkeyRequestBusy && !modalIsHidden("hotkey-modal")) {
    AR.closeHotkeyModal(false);
  }
  syncHotkeyControls();
}

function displayHotkey(combo) {
  if (!combo) return "Set";
  return combo.replace(/CTRL/g, "Ctrl").replace(/ALT/g, "Alt").replace(/SHIFT/g, "Shift");
}

function displayCaptureHotkey(combo) {
  return combo ? displayHotkey(combo) : "Press keys";
}

function renderDashboardHotkey() {
  const trigger = document.getElementById("shortcut-trigger");
  const combo = hotkeyState.combo || "";
  setText("shortcut-key", displayHotkey(combo));
  if (trigger) {
    trigger.title =
      hotkeyState.status === "unavailable"
        ? "Shortcut unavailable. Click to rebind."
        : "Bind shortcut";
    trigger.classList.toggle("is-unavailable", hotkeyState.status === "unavailable");
  }
}

function modalIsHidden(id) {
  return document.getElementById(id)?.classList.contains("is-hidden") !== false;
}

function syncHotkeyControls() {
  const busy = hotkeyRequestBusy;
  const save = document.getElementById("hotkey-save");
  const clear = document.getElementById("hotkey-clear");
  const cancel = document.getElementById("hotkey-cancel");
  if (save) save.disabled = busy || !pendingHotkey;
  if (clear) clear.disabled = busy;
  if (cancel) cancel.disabled = busy;
}

function hotkeyKey(event) {
  const namedKeys = {
    " ": "SPACE",
    ArrowUp: "UP",
    ArrowDown: "DOWN",
    ArrowLeft: "LEFT",
    ArrowRight: "RIGHT",
    Insert: "INSERT",
    Delete: "DELETE",
    Home: "HOME",
    End: "END",
    PageUp: "PAGEUP",
    PageDown: "PAGEDOWN",
  };
  if (namedKeys[event.key]) return namedKeys[event.key];
  if (/^F(?:[1-9]|1[0-9]|2[0-4])$/.test(event.key)) return event.key.toUpperCase();
  if (/^[a-z0-9]$/i.test(event.key)) return event.key.toUpperCase();
  return "";
}

function renderHotkeyCapture(value, hint) {
  setText("hotkey-capture-value", value);
  setText("hotkey-capture-hint", hint || "");
}

function resetHotkeyCaptureUI(value) {
  pendingHotkey = "";
  hotkeyPressed.clear();
  const capture = document.getElementById("hotkey-capture");
  if (capture) {
    capture.classList.remove("is-capturing");
    capture.setAttribute("aria-pressed", "false");
  }
  renderHotkeyCapture(value ?? displayCaptureHotkey(hotkeyState.combo || ""), "");
}

function pressedHotkeyModifiers(event) {
  const modifiers = [];
  if (event.ctrlKey || hotkeyPressed.has("Control")) modifiers.push("CTRL");
  if (event.altKey || hotkeyPressed.has("Alt")) modifiers.push("ALT");
  if (event.shiftKey || hotkeyPressed.has("Shift")) modifiers.push("SHIFT");
  return modifiers;
}

function renderPressedModifiers() {
  const modifiers = [];
  if (hotkeyPressed.has("Control")) modifiers.push("CTRL");
  if (hotkeyPressed.has("Alt")) modifiers.push("ALT");
  if (hotkeyPressed.has("Shift")) modifiers.push("SHIFT");
  renderHotkeyCapture(
    modifiers.length ? displayHotkey(modifiers.join("+")) + " +" : "Press keys",
    "",
  );
}

function captureHotkey(event) {
  const modal = document.getElementById("hotkey-modal");
  if (!modal || modal.classList.contains("is-hidden")) return;
  if (event.key === "Escape") {
    event.preventDefault();
    AR.closeHotkeyModal();
    return;
  }
  if (!hotkeyCapturing) return;
  event.preventDefault();
  if (pendingHotkey) return;
  if (event.metaKey) {
    setText("hotkey-error", "Windows key combinations are not supported.");
    renderHotkeyCapture("Win", "Press a supported key");
    return;
  }
  if (event.repeat) return;
  hotkeyPressed.add(event.key);
  const modifiers = pressedHotkeyModifiers(event);
  const key = hotkeyKey(event);
  if (!key) {
    renderHotkeyCapture(
      modifiers.length ? displayHotkey(modifiers.join("+")) + " +" : "Press keys",
      "",
    );
    return;
  }

  pendingHotkey = [...modifiers, key].join("+");
  const needsPrimaryModifier = /^[A-Z0-9]$/.test(key) || key === "SPACE";
  if (
    needsPrimaryModifier &&
    !modifiers.some((modifier) => modifier === "CTRL" || modifier === "ALT")
  ) {
    pendingHotkey = "";
    renderHotkeyCapture("Press keys", "");
    setText("hotkey-error", "Letters, numbers, and Space need Ctrl or Alt.");
    return;
  }
  renderHotkeyCapture(displayHotkey(pendingHotkey), "");
  setText("hotkey-error", "");
  const save = document.getElementById("hotkey-save");
  if (save) save.disabled = false;
}

function releaseHotkey(event) {
  const modal = document.getElementById("hotkey-modal");
  if (!modal || modal.classList.contains("is-hidden") || !hotkeyCapturing) return;
  // Some Chromium layouts report left/right modifier names differently. Use
  // the modifier flags as the authoritative release state, then clear any
  // aliases left in the pressed set.
  if (!event.ctrlKey) hotkeyPressed.delete("Control");
  if (!event.altKey) hotkeyPressed.delete("Alt");
  if (!event.shiftKey) hotkeyPressed.delete("Shift");
  if (!pendingHotkey) renderPressedModifiers();
}

function clearPressedHotkeys() {
  hotkeyPressed.clear();
  if (hotkeyCapturing && !pendingHotkey) renderHotkeyCapture("Press keys", "");
}

function onDeviceChanged(label) {
  // label is "Name  (Nch)" - split the channel suffix onto its own line
  const m = label.match(/^(.*)\s+\(([^()]*)\)\s*$/);
  if (m) {
    setText("device-name", m[1].trim());
    setText("device-channels", m[2].trim());
  } else {
    setText("device-name", label);
    setText("device-channels", "");
  }
}

function onMonitorsChanged(jsonStr) {
  liveFill(
    "monitor-select",
    JSON.parse(jsonStr).map((m) => ({ value: m.idx, label: m.name })),
  );
}

function onSelectedMonitorChanged(idx) {
  liveSelect("monitor-select", String(idx));
}

function onPresetsChanged(jsonStr) {
  window._presets = JSON.parse(jsonStr); // builtin catalog
  rebuildPresetSelects();
}

function onProfilesChanged(jsonStr) {
  window._profiles = JSON.parse(jsonStr); // { name: {settings...} }
  rebuildPresetSelects();
}

// The preset/profile selected in the last session, restored from settings.json.
// Only records the name here - rebuildPresetSelects does the re-selecting, since
// the matching option may not exist in the dropdown yet.
function onSelectedPresetChanged(name) {
  let state = null;
  try {
    state = JSON.parse(name);
  } catch (_) {
    // Parsing is best effort; keep the previous preset state on failure.
  }
  if (state && state.id) {
    presetState = { id: state.id, dirty: !!state.dirty };
    window._savedPreset = state.id;
  } else {
    window._savedPreset = name || "builtin:all-sounds";
    presetState = { id: window._savedPreset, dirty: false };
  }
  rebuildPresetSelects();
  syncPresetButtons();
}

function onProgramsChanged(jsonStr) {
  const progs = JSON.parse(jsonStr);
  // Diff-guard: refresh fires on dropdown-open and on every window focus, so
  // identical lists arrive repeatedly. Skip the DOM rebuild when nothing
  // changed - the live menu could take a rebuild at any time, but avoiding
  // one spares the churn on a list the user may be pointing at.
  const sig = progs.join("\u0000");
  if (sig === window._programsSig) return;
  window._programsSig = sig;

  // No preferred value: liveFill keeps whatever the user has selected, and
  // falls back to the first option ("All (system audio)") if that program has
  // stopped playing and dropped off the list.
  liveFill("program-select", [
    { value: "all", label: "All (system audio)" },
    ...progs.map((p) => ({ value: p, label: p })),
  ]);
}

// Backend probed per-app capture support. On an unsupported OS the program
// dropdown is gated (aria-disabled + hover title shows why) - never silently
// re-targeted, never re-enabled. Lists only arrive while supported anyway.
function onProgramsSupportedChanged(supported) {
  const field = liveField("program-select");
  field.disabled = !supported;
  field.reason = supported ? "" : PER_APP_REQUIRES_WIN11;
  if (!supported) AR.closeLiveSelect("program-select");
  liveRender("program-select");
}

// ── Live dropdowns (every select on the panel) ───────────────────────
// All four lists (monitor / program / preset / mono output) are plain DOM
// menus instead of native <select>s. A native popup cannot be themed or
// rebuilt while open in QtWebEngine, which is exactly when a session list
// must NOT wait: monitors connect and games start playing audio while the
// panel is up. A DOM menu has no such restriction, so opening a list asks
// the backend for a fresh enumeration where one exists, and any signal reply
// re-renders the menu in place while it is still open.
//
// State lives in `liveFields`; all DOM access is best-effort so the model can
// be exercised without real elements (see tests/test_dropdowns.js).
const liveFields = {};

// Shown when the OS cannot capture a per-app target; mirrors the backend
// gate message in main.py so the hover title says exactly what Start reports.
const PER_APP_REQUIRES_WIN11 = "Per-app capture requires Windows 11 - switch to All (system audio)";

function liveField(id) {
  return liveFields[id] || (liveFields[id] = { options: [], selected: null, open: false });
}

function liveOptions(id) {
  return liveFields[id] ? liveFields[id].options.slice() : [];
}

function liveSelected(id) {
  const field = liveFields[id];
  return field ? field.selected : null;
}

function liveIsOpen(id) {
  const field = liveFields[id];
  return field ? field.open : false;
}

function liveDisabled(id) {
  const field = liveFields[id];
  return field ? !!field.disabled : false;
}

// Read-only views of the live-dropdown state, exported for the regression
// tests (which load this file without a real DOM).
window.liveOptions = liveOptions;
window.liveSelected = liveSelected;
window.liveIsOpen = liveIsOpen;
window.liveDisabled = liveDisabled;

// Backend pushed a fresh option list. `preferred` (when given and present in
// the list) outranks the current selection; otherwise the current pick is
// kept while it still exists, falling back to the first entry - the native
// <select> this replaces ended up on its first option the same way.
function liveFill(id, options, preferred) {
  const field = liveField(id);
  const keep = field.selected == null ? null : String(field.selected);
  const want =
    preferred != null && options.some((o) => String(o.value) === String(preferred))
      ? String(preferred)
      : keep != null && options.some((o) => String(o.value) === keep)
        ? keep
        : options.length
          ? String(options[0].value)
          : null;
  field.options = options.map((o) => ({ value: String(o.value), label: String(o.label) }));
  field.selected = want;
  liveRender(id);
}

// Backend pushed the authoritative selection. It can arrive before its list,
// so it is stored as-is; liveFill re-validates it once options land.
function liveSelect(id, value) {
  if (value == null) return;
  liveField(id).selected = String(value);
  liveRender(id);
}

function liveRender(id) {
  const field = liveFields[id];
  if (!field) return;
  if (field.disabled && field.open) field.open = false;
  const pick = field.options.find((o) => o.value === field.selected) || field.options[0];
  const labelEl = document.getElementById(id + "-label");
  if (labelEl) {
    labelEl.textContent = pick ? pick.label : "";
    // A gated field carries the reason as the hover title on both parts.
    labelEl.title = field.disabled
      ? field.reason || PER_APP_REQUIRES_WIN11
      : pick
        ? pick.label
        : "";
  }
  const menuEl = document.getElementById(id + "-menu");
  if (menuEl && typeof menuEl.replaceChildren === "function") {
    menuEl.replaceChildren();
    if (field.open) {
      field.options.forEach((o) => {
        const btn = document.createElement("button");
        btn.type = "button";
        btn.className = "live-option";
        btn.textContent = o.label;
        btn.title = o.label; // rows truncate past the menu cap; hover shows all
        btn.addEventListener("click", () => AR.pickLiveOption(id, o.value));
        btn.setAttribute("role", "option");
        const selected = o.value === field.selected;
        btn.setAttribute("aria-selected", selected ? "true" : "false");
        if (selected) btn.classList.add("is-selected");
        menuEl.appendChild(btn);
      });
    }
  }
  if (menuEl) menuEl.classList?.toggle?.("is-open", field.open);
  const triggerEl = document.getElementById(id);
  triggerEl?.setAttribute?.("aria-expanded", field.open ? "true" : "false");
  if (triggerEl) {
    triggerEl.setAttribute("aria-disabled", field.disabled ? "true" : "false");
    triggerEl.title = field.disabled ? field.reason || PER_APP_REQUIRES_WIN11 : "";
    triggerEl.classList.toggle("is-disabled", !!field.disabled);
  }
}

function liveIsOpenAny() {
  return Object.keys(liveFields).some((id) => liveFields[id].open);
}

function liveCloseAll() {
  Object.keys(liveFields).forEach((id) => {
    if (liveFields[id].open) {
      liveFields[id].open = false;
      liveRender(id);
    }
  });
}

// Saved overlay appearance (accent colour + thickness) restored from settings.json.
// Applying it locally never calls the bridge, so this does not loop into a re-save.
function onAppearanceChanged(jsonStr) {
  const a = JSON.parse(jsonStr);
  if (a.color) {
    setAccentSwatch(a.color);
    updateColorReadout(a.color);
  }
  if (a.thickness != null) {
    const th = document.getElementById("thickness");
    if (th) th.value = a.thickness;
    setText("thickness-val", a.thickness + " px");
    setFill("thickness", a.thickness, 1, 20);
  }
  drawPreview();
}

// Move the audio sliders and their readouts to match the backend. Sent on load
// (the values restored from settings.json) and after a built-in preset is applied
// - the backend is already authoritative in both cases, and setting an input's
// value in JS does not fire its oninput, so this never loops back into the bridge.
// Keys are optional, so a caller can push a subset.
function onAudioSettingsChanged(jsonStr) {
  const p = JSON.parse(jsonStr);
  if (p.sensitivity != null) {
    const slider = Math.round(p.sensitivity * 10000); // slider units = f * 10000
    setSliderValue("sensitivity", slider);
    setText("sensitivity-val", p.sensitivity.toFixed(4));
    setFill("sensitivity", slider);
  }
  if (p.gain != null) {
    const slider = Math.round(p.gain * 10); // slider units = f * 10
    setSliderValue("gain", slider);
    setText("gain-val", p.gain.toFixed(1) + "x");
    setFill("gain", slider);
  }
  if (p.freq_low != null && p.freq_high != null) {
    document.getElementById("freq-low").value = p.freq_low;
    document.getElementById("freq-high").value = p.freq_high;
    setText("freq-val", `${p.freq_low}-${p.freq_high} Hz`);
    updateDualFill();
  }
  if (p.max_amp != null) {
    const slider = Math.round(p.max_amp * 100); // slider units = max_amp * 100
    setSliderValue("max-amp", slider);
    setText("max-amp-val", p.max_amp.toFixed(2));
    setFill("max-amp", slider);
  }
}

// Mono-output state: device list + VB-CABLE detection + current selection.
function onMonoStateChanged(jsonStr) {
  const s = JSON.parse(jsonStr);

  const opts = [
    { value: "", label: "System default" + (s.default ? ` (${s.default})` : "") },
    ...(s.devices || []).map((d) => ({ value: d, label: d })),
  ];
  // "" is a real option value here (System default), so it is passed through
  // as the preferred pick rather than being treated as "no selection".
  liveFill("mono-output-select", opts, s.selected || "");

  const cb = document.getElementById("mono-enabled");
  if (cb) cb.checked = !!s.enabled;

  // Compact hint shown in the HARDWARE card (the full setup lives in the modal).
  const hint = document.getElementById("mono-hint");
  if (hint) {
    const where = s.selected || (s.default ? "default device" : "default");
    const state = s.enabled ? `On - ${where}` : "Off";
    hint.innerHTML =
      `${state} <a href="#" class="mono-setup-link" ` +
      `onclick="AR.openMonoSetup(); return false;">Setup</a>`;
  }

  // Cable status + install button live in the modal (which scrolls, so no clip).
  const status = document.getElementById("mono-status");
  const installBtn = document.getElementById("mono-install-btn");
  if (s.cable) {
    if (status) status.innerHTML = `<span class="ok">Virtual cable detected:</span> ${s.cable}`;
    if (installBtn) installBtn.classList.add("is-hidden");
  } else {
    if (status)
      status.innerHTML =
        `<span class="warn">No virtual cable found.</span> Install VB-CABLE to ` +
        `route your game's audio without hearing it twice.`;
    if (installBtn) installBtn.classList.remove("is-hidden");
  }
}

function onOverlayPositionChanged(jsonStr) {
  const state = JSON.parse(jsonStr);
  moveModeActive = !!state.drag_enabled;
  setText("position-val", `${state.x}, ${state.y}`);
  syncMoveUI();
}

// ── Preset / profile dropdowns ─────────────────────────────────────────
// The dropdown combines the permanent built-in catalog with user profiles.
function rebuildPresetSelects() {
  const presets = window._presets || [];
  const profiles = Object.keys(window._profiles || {});
  const opts = [
    ...presets.map((p) => ({
      value: p.id,
      label: p.name + (presetState.id === p.id && presetState.dirty ? " *" : ""),
    })),
    ...profiles.map((n) => ({
      value: `profile:${n}`,
      label: n + (presetState.id === `profile:${n}` && presetState.dirty ? " *" : ""),
    })),
  ];
  // Until the selection saved in settings.json has been restored, it outranks
  // whatever the dropdown currently shows: presets and profiles arrive as two
  // separate signals, so the first rebuild can land on an arbitrary entry
  // simply because the list holding the saved one has not arrived yet. A saved
  // name that no longer exists (a profile deleted since) falls back to the
  // current pick rather than dropping the dropdown to its first entry.
  const saved = window._presetRestored ? null : window._savedPreset;
  const current = liveSelected("preset-select");
  const stateId = presetState.id;
  const has = (v) => v != null && opts.some((o) => o.value === v);
  const want = has(saved)
    ? saved
    : has(stateId)
      ? stateId
      : has(current)
        ? current
        : opts[0]?.value;
  liveFill("preset-select", opts, want);
  maybeRestorePreset();
}

function markPresetDirty() {
  if (presetApplying) return;
  const baseline = presetAudioValues(presetState.id);
  presetState.dirty = baseline ? !audioValuesEqual(currentAudioValues(), baseline) : true;
  rebuildPresetSelects();
  syncPresetButtons();
  persistPresetState();
}

function persistPresetState() {
  if (bridge.set_preset_state) {
    bridge.set_preset_state(JSON.stringify(presetState));
  }
}

function currentAudioValues() {
  return {
    sensitivity: intVal("sensitivity", 50),
    gain: intVal("gain", 10),
    freq_low: intVal("freq-low", 150),
    freq_high: intVal("freq-high", 4000),
    max_amp: intVal("max-amp", 100),
  };
}

function presetAudioValues(id) {
  if (id === "builtin:all-sounds") {
    return { sensitivity: 50, gain: 10, freq_low: 20, freq_high: 20000, max_amp: 100 };
  }
  if (id?.startsWith("profile:")) {
    const profile = (window._profiles || {})[id.slice(8)];
    if (!profile) return null;
    return {
      sensitivity: profile.sensitivity ?? 50,
      gain: profile.gain ?? 10,
      freq_low: profile.freq_low ?? 150,
      freq_high: profile.freq_high ?? 4000,
      max_amp: profile.max_amp ?? 100,
    };
  }
  return null;
}

function audioValuesEqual(a, b) {
  return ["sensitivity", "gain", "freq_low", "freq_high", "max_amp"].every(
    (key) => Number(a[key]) === Number(b[key]),
  );
}

function syncPresetButtons() {
  const reset = document.getElementById("preset-reset-btn");
  const save = document.getElementById("preset-save-btn");
  const del = document.getElementById("preset-delete-btn");
  if (reset) reset.disabled = !presetState.dirty;
  if (save) {
    save.disabled = !presetState.dirty || !presetState.id.startsWith("profile:");
    save.title = "Save preset";
    save.setAttribute("aria-label", save.title);
  }
  if (del) del.disabled = !presetState.id.startsWith("profile:");
}

// Latch the restore once the saved name is actually showing in the dropdown,
// which hands control of the selection back to the user (see rebuildPresetSelects).
//
// It deliberately does NOT re-apply the entry. Live settings are restored one
// value at a time, so merely restoring the selection cannot overwrite them.
function maybeRestorePreset() {
  if (window._presetRestored) return;
  const name = window._savedPreset;
  if (!name) return;
  if (liveSelected("preset-select") !== name) return; // not in the list (yet)
  window._presetRestored = true;
}

function profileData(name) {
  return {
    name,
    sensitivity: intVal("sensitivity", 50),
    gain: intVal("gain", 10),
    freq_low: intVal("freq-low", 150),
    freq_high: intVal("freq-high", 4000),
    max_amp: intVal("max-amp", 100),
  };
}

function openDashboardModal(id, focusId) {
  const modal = document.getElementById(id);
  if (!modal) return;
  modalReturnFocus = document.activeElement?.focus ? document.activeElement : null;
  activeDashboardModal = id;
  toggleClass(id, "is-hidden", false);
  document.getElementById(focusId)?.focus();
}

function closeDashboardModal(id) {
  toggleClass(id, "is-hidden", true);
  if (activeDashboardModal !== id) return;
  activeDashboardModal = null;
  const returnFocus = modalReturnFocus;
  modalReturnFocus = null;
  returnFocus?.focus();
}

function normalizeHex(value) {
  const hex = String(value || "").trim();
  const normalized = hex.startsWith("#") ? hex : `#${hex}`;
  return /^#[0-9a-fA-F]{6}$/.test(normalized) ? normalized.toUpperCase() : null;
}

function hexToHsv(hex) {
  const value = normalizeHex(hex) || "#9751F2";
  const rgb = [1, 3, 5].map((offset) => parseInt(value.slice(offset, offset + 2), 16) / 255);
  const max = Math.max(...rgb);
  const min = Math.min(...rgb);
  const delta = max - min;
  let hue = 0;
  if (delta) {
    if (max === rgb[0]) hue = 60 * (((rgb[1] - rgb[2]) / delta) % 6);
    else if (max === rgb[1]) hue = 60 * ((rgb[2] - rgb[0]) / delta + 2);
    else hue = 60 * ((rgb[0] - rgb[1]) / delta + 4);
  }
  return { h: (hue + 360) % 360, s: max ? delta / max : 0, v: max };
}

function hsvToHex(h, s, v) {
  const chroma = v * s;
  const second = chroma * (1 - Math.abs(((h / 60) % 2) - 1));
  const match = v - chroma;
  const channels =
    h < 60
      ? [chroma, second, 0]
      : h < 120
        ? [second, chroma, 0]
        : h < 180
          ? [0, chroma, second]
          : h < 240
            ? [0, second, chroma]
            : h < 300
              ? [second, 0, chroma]
              : [chroma, 0, second];
  return (
    "#" +
    channels
      .map((channel) =>
        Math.round((channel + match) * 255)
          .toString(16)
          .padStart(2, "0"),
      )
      .join("")
      .toUpperCase()
  );
}

function renderColorDraft() {
  if (!colorDraft) return;
  const hex = hsvToHex(colorDraft.h, colorDraft.s, colorDraft.v);
  const plane = document.getElementById("color-sv-plane");
  if (plane) plane.style.setProperty("--hue-color", hsvToHex(colorDraft.h, 1, 1));
  const marker = document.getElementById("color-sv-marker");
  if (marker) {
    marker.style.left = `${colorDraft.s * 100}%`;
    marker.style.top = `${(1 - colorDraft.v) * 100}%`;
  }
  const hue = document.getElementById("color-hue");
  if (hue) hue.value = String(Math.round(colorDraft.h));
  const input = document.getElementById("color-hex-input");
  if (input) input.value = hex;
  const draft = document.getElementById("color-draft-swatch");
  if (draft) draft.style.background = hex;
}

function setAccentSwatch(hex) {
  const swatch = document.getElementById("accent-color");
  const normalized = normalizeHex(hex);
  if (!swatch || !normalized) return;
  swatch.dataset.color = normalized;
  swatch.style.background = normalized;
}

function presetNameExists(name) {
  const normalized = name.trim().toLowerCase();
  return Object.keys(window._profiles || {}).some(
    (existing) => existing.trim().toLowerCase() === normalized,
  );
}

// ── AR namespace (JS → Python) ─────────────────────────────────────────
window.AR = {
  toggleRadar() {
    if (operationBusy) return;
    if (radarActive) bridge.stop_radar();
    else bridge.start_radar();
    // UI syncs authoritatively via statusChanged
  },

  setSensitivity(val) {
    const f = val / 10000;
    setText("sensitivity-val", f.toFixed(4));
    setFill("sensitivity", val);
    bridge.set_sensitivity(f);
    markPresetDirty();
  },

  setGain(val) {
    const f = val / 10;
    setText("gain-val", f.toFixed(1) + "x");
    setFill("gain", val);
    bridge.set_gain(f);
    markPresetDirty();
  },

  setMaxAmp(val) {
    const f = val / 100;
    setText("max-amp-val", f.toFixed(2));
    setFill("max-amp", val);
    bridge.set_max_amplitude(f);
    markPresetDirty();
  },

  openHotkeyModal() {
    if (hotkeyRequestBusy || !bridge?.begin_hotkey_edit) return;
    resetHotkeyCaptureUI(displayCaptureHotkey(hotkeyState.combo || ""));
    hotkeyCapturing = false;
    setText("hotkey-error", "");
    hotkeyRequestBusy = true;
    syncHotkeyControls();
    bridge.begin_hotkey_edit((json) => {
      hotkeyRequestBusy = false;
      onHotkeyStateChanged(json);
      if (hotkeyState.editing) {
        renderHotkeyCapture(displayCaptureHotkey(hotkeyState.combo), "");
        toggleClass("hotkey-modal", "is-hidden", false);
        const capture = document.getElementById("hotkey-capture");
        if (capture) {
          capture.classList.remove("is-capturing");
          capture.setAttribute("aria-pressed", "false");
        }
      }
      syncHotkeyControls();
    });
  },

  closeHotkeyModal(cancel = true) {
    hotkeyCapturing = false;
    resetHotkeyCaptureUI(displayCaptureHotkey(hotkeyState.combo || ""));
    setText("hotkey-error", "");
    if (!cancel || !hotkeyState.editing || !bridge?.cancel_hotkey_edit) {
      toggleClass("hotkey-modal", "is-hidden", true);
      return;
    }
    if (hotkeyRequestBusy) return;
    hotkeyRequestBusy = true;
    syncHotkeyControls();
    bridge.cancel_hotkey_edit((json) => {
      hotkeyRequestBusy = false;
      onHotkeyStateChanged(json);
      toggleClass("hotkey-modal", "is-hidden", true);
      syncHotkeyControls();
    });
  },

  beginHotkeyCapture() {
    hotkeyCapturing = true;
    resetHotkeyCaptureUI("Press keys");
    setText("hotkey-error", "");
    const save = document.getElementById("hotkey-save");
    if (save) save.disabled = true;
    const capture = document.getElementById("hotkey-capture");
    if (capture) {
      capture.classList.add("is-capturing");
      capture.setAttribute("aria-pressed", "true");
      capture.focus();
    }
  },

  saveHotkey() {
    if (!pendingHotkey || hotkeyRequestBusy || !bridge?.commit_hotkey) return;
    hotkeyRequestBusy = true;
    syncHotkeyControls();
    bridge.commit_hotkey(pendingHotkey, (json) => {
      hotkeyRequestBusy = false;
      onHotkeyStateChanged(json);
      if (!hotkeyState.editing && !hotkeyState.error) {
        hotkeyCapturing = false;
        resetHotkeyCaptureUI(displayCaptureHotkey(hotkeyState.combo || ""));
        toggleClass("hotkey-modal", "is-hidden", true);
      }
      syncHotkeyControls();
    });
  },

  clearHotkey() {
    if (hotkeyRequestBusy || !bridge?.clear_hotkey) return;
    hotkeyRequestBusy = true;
    syncHotkeyControls();
    bridge.clear_hotkey((json) => {
      hotkeyRequestBusy = false;
      onHotkeyStateChanged(json);
      hotkeyCapturing = false;
      resetHotkeyCaptureUI(displayCaptureHotkey(hotkeyState.combo || ""));
      toggleClass("hotkey-modal", "is-hidden", true);
      syncHotkeyControls();
    });
  },

  commitAudioSettings() {
    if (bridge.commit_audio_settings) bridge.commit_audio_settings();
  },

  // Frequency is in real Hz (locked decision). Dual handles.
  setFreqRange() {
    const lowEl = document.getElementById("freq-low");
    const highEl = document.getElementById("freq-high");
    let low = parseInt(lowEl.value);
    let high = parseInt(highEl.value);
    if (low > high) {
      [low, high] = [high, low];
    } // keep ordered
    setText("freq-val", `${low}-${high} Hz`);
    updateDualFill();
    bridge.set_freq_range(low, high);
    markPresetDirty();
  },

  applyPreset(name) {
    if (!name) return;
    // Remember the choice so it survives a restart. Also stops a stale saved
    // name (e.g. a profile deleted since) from overriding this pick on the
    // next dropdown rebuild.
    window._presetRestored = true;
    const profiles = window._profiles || {};
    if (name.startsWith("profile:")) {
      const profileName = name.slice(8);
      if (!profiles[profileName]) return;
      presetApplying = true;
      applyProfileValues(profiles[profileName]);
      presetApplying = false;
      // Persist the complete profile once its live values are applied.
      AR.commitAudioSettings();
    } else {
      presetApplying = true;
      bridge.apply_preset(name);
      presetApplying = false;
    }
    presetState = { id: name, dirty: false };
    persistPresetState();
    if (bridge.set_selected_preset) bridge.set_selected_preset(name);
    rebuildPresetSelects();
    syncPresetButtons();
  },

  addPreset() {
    const input = document.getElementById("preset-name-input");
    if (input) input.value = "";
    setText("preset-create-error", "");
    openDashboardModal("preset-create-modal", "preset-name-input");
  },

  closePresetCreateModal() {
    closeDashboardModal("preset-create-modal");
  },

  createPreset(event) {
    event?.preventDefault();
    const input = document.getElementById("preset-name-input");
    const name = input?.value.trim() || "";
    if (!name) {
      setText("preset-create-error", "Enter a preset name.");
      input?.focus();
      return;
    }
    if (presetNameExists(name)) {
      setText("preset-create-error", "A preset with this name already exists.");
      input?.focus();
      return;
    }
    const profile = profileData(name);
    bridge.save_profile(JSON.stringify(profile));
    window._profiles = { ...(window._profiles || {}), [name]: profile };
    presetState = { id: `profile:${name}`, dirty: false };
    persistPresetState();
    if (bridge.set_selected_preset) bridge.set_selected_preset(presetState.id);
    rebuildPresetSelects();
    syncPresetButtons();
    closeDashboardModal("preset-create-modal");
  },

  resetPreset() {
    if (!presetState.dirty || !presetState.id) return;
    AR.applyPreset(presetState.id);
  },

  savePreset() {
    if (!presetState.dirty) return;
    if (!presetState.id.startsWith("profile:")) return;
    const name = presetState.id.slice(8);
    bridge.save_profile(JSON.stringify(profileData(name)));
    presetState = { id: `profile:${name}`, dirty: false };
    persistPresetState();
    if (bridge.set_selected_preset) bridge.set_selected_preset(presetState.id);
    rebuildPresetSelects();
    syncPresetButtons();
  },

  deletePreset() {
    const id = liveSelected("preset-select");
    if (!id || !id.startsWith("profile:")) return;
    const name = id.slice(8);
    if (!name) return;
    if (!(window._profiles || {})[name]) return;
    pendingDeletePresetName = name;
    setText("preset-delete-name", name);
    openDashboardModal("preset-delete-modal", "preset-delete-confirm");
  },

  closePresetDeleteModal() {
    pendingDeletePresetName = "";
    closeDashboardModal("preset-delete-modal");
  },

  confirmPresetDelete() {
    if (!pendingDeletePresetName) return;
    bridge.delete_profile(pendingDeletePresetName);
    AR.closePresetDeleteModal();
  },

  setMonitor(idx) {
    bridge.set_monitor(parseInt(idx));
  },

  setProgram(value) {
    // Backend method may not exist yet - guard it.
    if (bridge.set_program) bridge.set_program(value);
    else console.log("set_program not wired yet; selected:", value);
  },

  refreshPrograms() {
    // Re-enumerate live audio programs when the dropdown is opened, so the
    // game shows up even if it started playing after the app launched.
    const channel = window.bridge;
    if (channel && channel.refresh_programs) channel.refresh_programs();
  },

  refreshMonitors() {
    const channel = window.bridge;
    if (channel && channel.refresh_monitors) channel.refresh_monitors();
  },

  refreshDropdown(id) {
    if (window._dashboardFrozen || document.hidden) return;
    // Only monitor/program lists come from the backend's live enumeration;
    // preset and mono lists arrive on their own signals, so opening those
    // menus needs no round-trip.
    if (id !== "monitor-select" && id !== "program-select") {
      resetDropdownRefreshTimer();
      return;
    }
    const now = Date.now();
    if (window._lastDropdownRefresh[id] && now - window._lastDropdownRefresh[id] < 250) {
      resetDropdownRefreshTimer();
      return;
    }
    window._lastDropdownRefresh[id] = now;
    if (id === "monitor-select") AR.refreshMonitors();
    else if (id === "program-select") AR.refreshPrograms();
    resetDropdownRefreshTimer();
  },

  refreshDropdowns() {
    if (window._dashboardFrozen || document.hidden) {
      resetDropdownRefreshTimer();
      return;
    }
    // Live menus tolerate an in-place rebuild, so the idle poll and the
    // window-focus refresh no longer need to dodge an open popup.
    AR.refreshMonitors();
    AR.refreshPrograms();
    resetDropdownRefreshTimer();
  },

  // ── Live dropdowns (Monitor / Program) ───────────────────────────
  toggleLiveSelect(id) {
    if (window._dashboardFrozen || document.hidden) return;
    const field = liveField(id);
    if (field.disabled) return;
    field.open = !field.open;
    // Opening re-enumerates (250ms-debounced), so a monitor connected or a
    // game started since the last lookup shows up in the list right away.
    if (field.open) AR.refreshDropdown(id);
    liveRender(id);
  },

  closeLiveSelect(id) {
    const field = liveFields[id];
    if (!field || !field.open) return;
    field.open = false;
    liveRender(id);
  },

  pickLiveOption(id, value) {
    if (liveField(id).disabled) return;
    liveSelect(id, value);
    AR.closeLiveSelect(id);
    const trigger = document.getElementById(id);
    if (trigger && typeof trigger.focus === "function") trigger.focus();
    if (id === "monitor-select") AR.setMonitor(parseInt(value));
    else if (id === "program-select") AR.setProgram(value);
    else if (id === "mono-output-select") AR.setMonoOutput(value);
    else if (id === "preset-select") AR.applyPreset(value);
  },

  // ── Mono output ────────────────────────────────────────────────
  setMonoEnabled(on) {
    if (bridge.set_mono_enabled) bridge.set_mono_enabled(!!on);
    if (on) AR.openMonoSetup(); // first enable: walk them through setup
  },

  setMonoOutput(value) {
    if (bridge.set_mono_output) bridge.set_mono_output(value);
  },

  openMonoSetup() {
    toggleClass("mono-modal", "is-hidden", false);
  },
  closeMonoSetup() {
    toggleClass("mono-modal", "is-hidden", true);
  },

  installCable() {
    if (bridge.install_vbcable) bridge.install_vbcable();
  },

  toggleMoveMode() {
    bridge.set_overlay_drag_enabled(!moveModeActive);
  },

  nudgeOverlay(dx, dy) {
    bridge.nudge_overlay(parseInt(dx), parseInt(dy));
  },

  resetOverlay() {
    bridge.reset_overlay_position();
  },

  setAccentColor(hex) {
    bridge.set_accent_color(hex);
    setAccentSwatch(hex);
    updateColorReadout(hex);
    drawPreview();
  },

  openColorModal() {
    const current =
      normalizeHex(document.getElementById("accent-color")?.dataset.color) || "#9751F2";
    colorDraft = { ...hexToHsv(current), original: current };
    setText("color-error", "");
    const original = document.getElementById("color-original-swatch");
    if (original) original.style.background = current;
    renderColorDraft();
    openDashboardModal("color-modal", "color-hex-input");
  },

  closeColorModal() {
    colorDraft = null;
    closeDashboardModal("color-modal");
  },

  pickColor(event) {
    if (!colorDraft || (event.type === "pointermove" && event.buttons === 0)) return;
    const plane = document.getElementById("color-sv-plane");
    if (!plane) return;
    if (event.type === "pointerdown") plane.setPointerCapture?.(event.pointerId);
    const rect = plane.getBoundingClientRect();
    colorDraft.s = Math.max(0, Math.min(1, (event.clientX - rect.left) / rect.width));
    colorDraft.v = Math.max(0, Math.min(1, 1 - (event.clientY - rect.top) / rect.height));
    setText("color-error", "");
    renderColorDraft();
  },

  nudgeColor(event) {
    if (!colorDraft || !["ArrowLeft", "ArrowRight", "ArrowUp", "ArrowDown"].includes(event.key))
      return;
    event.preventDefault();
    const step = 0.02;
    if (event.key === "ArrowLeft") colorDraft.s = Math.max(0, colorDraft.s - step);
    if (event.key === "ArrowRight") colorDraft.s = Math.min(1, colorDraft.s + step);
    if (event.key === "ArrowDown") colorDraft.v = Math.max(0, colorDraft.v - step);
    if (event.key === "ArrowUp") colorDraft.v = Math.min(1, colorDraft.v + step);
    setText("color-error", "");
    renderColorDraft();
  },

  setColorHue(value) {
    if (!colorDraft) return;
    colorDraft.h = Math.max(0, Math.min(360, Number(value))) % 360;
    setText("color-error", "");
    renderColorDraft();
  },

  setColorHex(value) {
    const hex = normalizeHex(value);
    if (!hex || !colorDraft) return;
    Object.assign(colorDraft, hexToHsv(hex));
    setText("color-error", "");
    renderColorDraft();
  },

  applyColorModal(event) {
    event?.preventDefault();
    const hex = normalizeHex(document.getElementById("color-hex-input")?.value);
    if (!hex) {
      setText("color-error", "Enter a six-digit HEX color, for example #9751F2.");
      document.getElementById("color-hex-input")?.focus();
      return;
    }
    AR.setAccentColor(hex);
    AR.commitAppearance();
    AR.closeColorModal();
  },

  setThickness(val) {
    setText("thickness-val", val + " px");
    setFill("thickness", val, 1, 20);
    bridge.set_stroke_width(parseInt(val));
    drawPreview();
  },

  commitAppearance() {
    if (bridge.commit_appearance) bridge.commit_appearance();
  },

  // ── Update banner ─────────────────────────────────────────────────
  openUpdate() {
    openExternal(updateUrl);
  },
  openRepo() {
    openExternal(REPO_URL);
  },
  dismissUpdate() {
    toggleClass("update-banner", "is-hidden", true);
  },
};

// Apply only the audio settings stored in a profile. Other dashboard settings
// are intentionally independent from preset selection.
function applyProfileValues(p) {
  setSliderValue("sensitivity", p.sensitivity ?? 50);
  setSliderValue("gain", p.gain ?? 10);
  setSliderValue("max-amp", p.max_amp ?? 100);
  document.getElementById("freq-low").value = p.freq_low ?? 150;
  document.getElementById("freq-high").value = p.freq_high ?? 4000;
  AR.setSensitivity(intVal("sensitivity", 50));
  AR.setGain(intVal("gain", 10));
  AR.setMaxAmp(intVal("max-amp", 100));
  AR.setFreqRange();
}

// ── Preview canvas - mirrors native overlay rendering ──────────────────────
// native overlay: faint white base circle + accent-coloured arc "blips",
// 35° span, round cap, stroke width = thickness. Here we draw one static
// sample blip so the user sees the chosen colour + thickness style.
function drawPreview() {
  const canvas = document.getElementById("preview-canvas");
  if (!canvas) return;
  // A display-mode/DPI switch can discard Chromium's canvas backing store
  // while leaving the DOM node alive. Keep the bitmap at the current CSS
  // size × DPR so it is redrawn sharply after the compositor recreates its
  // device (and do not rely on a stale 150x150 backing surface).
  let cssWidth = canvas.clientWidth || canvas.width || 1;
  let cssHeight = canvas.clientHeight || canvas.height || 1;
  if (typeof canvas.getBoundingClientRect === "function") {
    const rect = canvas.getBoundingClientRect();
    if (rect.width > 0) cssWidth = rect.width;
    if (rect.height > 0) cssHeight = rect.height;
  }
  const dpr = Math.max(1, Number(window.devicePixelRatio) || 1);
  const backingWidth = Math.max(1, Math.round(cssWidth * dpr));
  const backingHeight = Math.max(1, Math.round(cssHeight * dpr));
  if (canvas.width !== backingWidth || canvas.height !== backingHeight) {
    canvas.width = backingWidth;
    canvas.height = backingHeight;
  }
  const ctx = canvas.getContext("2d");
  // QtWebEngine may recreate the canvas backing surface after a frozen page
  // is resumed.  There is nothing useful to draw until a 2D context exists;
  // the visibility/pageshow hooks below will retry on the next frame.
  if (!ctx) return;
  // Draw in CSS pixels; the transform maps those coordinates to the DPR
  // backing bitmap and keeps line widths consistent across monitors.
  if (typeof ctx.setTransform === "function") {
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  }
  const W = cssWidth,
    H = cssHeight;
  const cx = W / 2,
    cy = H / 2;
  const radius = (Math.min(W, H) / 2) * 0.8;

  ctx.clearRect(0, 0, W, H);

  // Base circle (matches overlay: white @ ~12% alpha, 2px)
  ctx.beginPath();
  ctx.arc(cx, cy, radius, 0, Math.PI * 2);
  ctx.strokeStyle = "rgba(255,255,255,0.18)";
  ctx.lineWidth = 2;
  ctx.stroke();

  // Sample blip arc
  const accent = document.getElementById("accent-color")?.dataset.color || "#9751F2";
  const thickness = parseInt(document.getElementById("thickness")?.value || 6);
  const sampleAngleDeg = -35; // up-and-to-the-right, like the mockup
  const spanDeg = 35;
  // canvas 0° = +x axis, clockwise; overlay angle 0 = up. Convert:
  const centerDeg = -90 + sampleAngleDeg;
  const start = ((centerDeg - spanDeg / 2) * Math.PI) / 180;
  const end = ((centerDeg + spanDeg / 2) * Math.PI) / 180;

  ctx.beginPath();
  ctx.arc(cx, cy, radius, start, end);
  ctx.strokeStyle = accent;
  ctx.lineWidth = thickness;
  ctx.lineCap = "round";
  ctx.stroke();
}

// ── Dual-range fill geometry ───────────────────────────────────────────
function updateDualFill() {
  const wrap = document.getElementById("freq-range");
  const fill = wrap.querySelector(".dualrange-fill");
  const min = +wrap.dataset.min,
    max = +wrap.dataset.max;
  let lo = +document.getElementById("freq-low").value;
  let hi = +document.getElementById("freq-high").value;
  if (lo > hi) [lo, hi] = [hi, lo];
  const pct = (v) => ((v - min) / (max - min)) * 100;
  fill.style.left = pct(lo) + "%";
  fill.style.right = 100 - pct(hi) + "%";
}

// ── Small helpers ──────────────────────────────────────────────────────
function setText(id, txt) {
  const el = document.getElementById(id);
  if (el) el.textContent = txt;
}
function toggleClass(id, cls, on) {
  const el = document.getElementById(id);
  if (el) el.classList.toggle(cls, on);
}
function intVal(id, def) {
  const el = document.getElementById(id);
  return el ? parseInt(el.value) : def;
}

// Dropdowns are refreshed on demand when opened, with a low-rate visible-page
// poll as a backstop for monitor/program changes that happen while the panel is
// idle.  The timer is deliberately stopped while the WebEngine page is hidden
// or Frozen; opening a select then restarts the same idle interval.
const DROPDOWN_REFRESH_IDLE_MS = 10000;
let _dropdownRefreshTimer = null;
// Hosted on window like the other cross-signal latches so tests can clear the
// 250ms open-dedupe window between scenarios.
window._lastDropdownRefresh = window._lastDropdownRefresh || {};

function stopDropdownRefreshTimer() {
  if (_dropdownRefreshTimer !== null && typeof window.clearTimeout === "function") {
    window.clearTimeout(_dropdownRefreshTimer);
  }
  _dropdownRefreshTimer = null;
}

function scheduleDropdownRefresh(delay = DROPDOWN_REFRESH_IDLE_MS) {
  stopDropdownRefreshTimer();
  if (window._dashboardFrozen || document.hidden || typeof window.setTimeout !== "function") return;
  _dropdownRefreshTimer = window.setTimeout(() => {
    _dropdownRefreshTimer = null;
    if (window._dashboardFrozen || document.hidden) return;
    AR.refreshDropdowns();
    scheduleDropdownRefresh();
  }, delay);
}

function resetDropdownRefreshTimer() {
  scheduleDropdownRefresh();
}

// Called by the Qt lifecycle bridge when QWebEnginePage enters/leaves Frozen.
// Keeping this explicit avoids relying solely on document.hidden, which is not
// guaranteed to change for every tray/minimize path.
window.setDashboardFrozen = function (frozen) {
  window._dashboardFrozen = !!frozen;
  if (window._dashboardFrozen) {
    liveCloseAll();
    stopDropdownRefreshTimer();
  } else {
    scheduleDropdownRefresh();
  }
};

// History: QtWebEngine draws native <select> popups as unstyled Qt widgets,
// and swapping options while a popup was open made Qt GROW the rendered list
// (the runaway-dropdown bug) while the popup focus-churn retriggered a
// blocking COM enumeration on every window focus. 49b828f diff-guarded the
// program list; monitor and program moved to in-place-rebuildable DOM menus;
// and now every select on the panel (preset and mono-output included) is a
// DOM menu (see the Live dropdowns section), so no deferral machinery
// remains - fills always land immediately, open or not.
//
// `_pendingFills` (deferred native-select rebuilds) was removed along with
// the last native <select>.

// Paint the filled portion of a single slider via the --fill CSS var.
function setFill(id, val, min, max) {
  const el = document.getElementById(id);
  if (!el) return;
  const lo = min ?? +el.min,
    hi = max ?? +el.max;
  const pct = ((val - lo) / (hi - lo)) * 100;
  el.style.setProperty("--fill", pct + "%");
}

function setSliderValue(id, value) {
  const el = document.getElementById(id);
  if (el) el.value = value;
}

function updateColorReadout(hex) {
  setText("color-hex", hex.toUpperCase());
  const r = parseInt(hex.slice(1, 3), 16);
  const g = parseInt(hex.slice(3, 5), 16);
  const b = parseInt(hex.slice(5, 7), 16);
  setText("color-rgb", `${r}, ${g}, ${b}`);
}

function syncToggleUI() {
  const btn = document.getElementById("btn-toggle");
  const dot = document.getElementById("status-dot");
  if (btn) {
    if (operationState === "starting") btn.textContent = "Starting...";
    else if (operationState === "restarting") btn.textContent = "Restarting...";
    else if (operationState === "stopping" || operationState === "closing")
      btn.textContent = "Stopping...";
    else btn.textContent = radarActive ? "End" : "Start";
    btn.classList.toggle("is-active", radarActive && operationState === "running");
    btn.disabled = operationBusy;
  }
  if (dot) {
    dot.classList.toggle("status-dot--on", radarActive);
    dot.classList.toggle("status-dot--off", !radarActive);
  }
}

function syncMoveUI() {
  const btn = document.getElementById("btn-move");
  if (!btn) return;
  btn.textContent = moveModeActive ? "Done" : "Move";
  btn.classList.toggle("is-active", moveModeActive);
}

// Frozen WebEngine pages can resume without replaying the initial script.  A
// cheap redraw keeps the static customization preview visible after restoring
// the dashboard from the tray or taskbar.
function redrawPreviewAfterResume() {
  const redraw = () => drawPreview();
  if (typeof window.requestAnimationFrame === "function") {
    window.requestAnimationFrame(redraw);
  } else {
    redraw();
  }
}

if (document.addEventListener) {
  document.addEventListener("visibilitychange", () => {
    if (!document.hidden) redrawPreviewAfterResume();
  });
}
if (window.addEventListener) {
  window.addEventListener("pageshow", redrawPreviewAfterResume);
}

function isEditableTarget(target) {
  return Boolean(target?.closest?.(EDITABLE_SELECTOR));
}

document.addEventListener("keydown", (event) => {
  if (!activeDashboardModal) return;
  if (event.key === "Escape") {
    event.preventDefault();
    if (activeDashboardModal === "color-modal") AR.closeColorModal();
    if (activeDashboardModal === "preset-create-modal") AR.closePresetCreateModal();
    if (activeDashboardModal === "preset-delete-modal") AR.closePresetDeleteModal();
  } else if (event.key === "Enter" && activeDashboardModal === "preset-delete-modal") {
    event.preventDefault();
    AR.confirmPresetDelete();
  }
});

// Close any open live dropdown on Escape (a no-op while a modal is up - the
// handler above consumed the event first).
document.addEventListener("keydown", (event) => {
  if (event.key !== "Escape" || !liveIsOpenAny()) return;
  event.preventDefault();
  liveCloseAll();
});

// Clicking anywhere outside an open live dropdown dismisses it.
document.addEventListener("pointerdown", (event) => {
  const openIds = Object.keys(liveFields).filter((id) => liveFields[id].open);
  if (!openIds.length) return;
  const target = event.target;
  const inside =
    target &&
    typeof target.closest === "function" &&
    openIds.some((id) => target.closest(`#${id}, #${id}-menu`));
  if (!inside) liveCloseAll();
});

document.addEventListener("selectstart", (event) => {
  if (!isEditableTarget(event.target)) event.preventDefault();
});

document.addEventListener("contextmenu", (event) => {
  if (!isEditableTarget(event.target)) event.preventDefault();
});

// ── Bootstrap ──────────────────────────────────────────────────────────
document.addEventListener("DOMContentLoaded", function () {
  // Default program option until/unless backend sends a list
  liveFill("program-select", [{ value: "all", label: "All (system audio)" }]);

  // Live menus: click opens (and always re-enumerates while opening). Arrow
  // keys on the closed trigger open it like a native popup would.
  ["monitor-select", "program-select", "preset-select", "mono-output-select"].forEach((id) => {
    const trigger = document.getElementById(id);
    if (!trigger) return;
    trigger.addEventListener("click", () => AR.toggleLiveSelect(id));
    trigger.addEventListener("keydown", (event) => {
      if (event.key !== "ArrowDown" && event.key !== "ArrowUp") return;
      if (liveIsOpen(id)) return; // arrows belong to the open menu
      event.preventDefault();
      AR.toggleLiveSelect(id);
    });
    const menu = document.getElementById(id + "-menu");
    if (!menu || typeof menu.addEventListener !== "function") return;
    // In-menu navigation: the options are real focusable buttons, but mirror
    // the arrow-key feel of a native popup while one is focused.
    menu.addEventListener("keydown", (event) => {
      if (!menu.querySelectorAll) return;
      const opts = Array.from(menu.querySelectorAll(".live-option"));
      if (!opts.length) return;
      let idx = opts.indexOf(document.activeElement);
      if (event.key === "ArrowDown" || event.key === "ArrowUp") {
        event.preventDefault();
        idx = idx === -1 ? (event.key === "ArrowDown" ? -1 : opts.length) : idx;
        idx = event.key === "ArrowDown" ? Math.min(idx + 1, opts.length - 1) : Math.max(idx - 1, 0);
      } else if (event.key === "Home" || event.key === "End") {
        event.preventDefault();
        idx = event.key === "Home" ? 0 : opts.length - 1;
      } else {
        return;
      }
      if (idx >= 0 && opts[idx].focus) opts[idx].focus();
    });
  });
  scheduleDropdownRefresh();

  // Wire dual-range inputs
  ["freq-low", "freq-high"].forEach((id) =>
    document.getElementById(id).addEventListener("input", AR.setFreqRange),
  );
  document.addEventListener("keydown", captureHotkey, true);
  document.addEventListener("keyup", releaseHotkey, true);
  window.addEventListener("blur", clearPressedHotkeys);
  document.addEventListener("visibilitychange", () => {
    if (document.hidden) {
      clearPressedHotkeys();
      liveCloseAll();
      stopDropdownRefreshTimer();
    } else {
      AR.refreshDropdowns();
      scheduleDropdownRefresh();
    }
  });

  // Initial paint of values/fills/preview
  AR_initLocal();

  initBridge().then(() => console.log("Visual Audio Overlay bridge ready."));
});

// Local (no-bridge) initial UI state so the panel looks right immediately.
function AR_initLocal() {
  setFill("sensitivity", 50);
  setFill("gain", 10);
  setFill("max-amp", 100);
  setFill("thickness", 6, 1, 20);
  setText("sensitivity-val", (50 / 10000).toFixed(4));
  setText("gain-val", "1.0x");
  setText("max-amp-val", "1.00");
  setText("thickness-val", "6 px");
  setText("position-val", "0, 0");
  syncMoveUI();
  updateDualFill();
  setText("freq-val", "150-4000 Hz");
  setAccentSwatch("#9751F2");
  updateColorReadout("#9751F2");
  renderDashboardHotkey();
  drawPreview();
  syncPresetButtons();
}
