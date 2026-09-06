#include "overlay_native.h"

#define WIN32_LEAN_AND_MEAN
#include <d2d1_1.h>
#include <d3d11.h>
#include <dcomp.h>
#include <dwmapi.h>
#include <dxgi1_2.h>
#include <dxgi1_4.h>
#include <windows.h>
#include <windowsx.h>
#include <wrl/client.h>

#ifndef WS_EX_NOREDIRECTIONBITMAP
#define WS_EX_NOREDIRECTIONBITMAP 0x00200000L
#endif

#include <algorithm>
#include <atomic>
#include <cctype>
#include <cmath>
#include <condition_variable>
#include <cstddef>
#include <cstdint>
#include <cstdio>
#include <cstdlib>
#include <deque>
#include <functional>
#include <mutex>
#include <optional>
#include <string>
#include <thread>
#include <utility>
#include <vector>

using Microsoft::WRL::ComPtr;

namespace {

constexpr int kDefaultWidth = 300;
constexpr int kDefaultHeight = 300;
constexpr float kDecay = 0.04f;
constexpr float kArcSpan = 35.0f;
constexpr float kVisualGain = 5.0f;
constexpr size_t kMaxBlips = 64;
constexpr size_t kMaxEvents = 32;
constexpr size_t kMaxCommands = 128;
constexpr UINT_PTR kDecayTimer = 1;
// After this many consecutive render() failures the render loop rebuilds the
// whole graphics stack, and never more often than this debounce, so a
// persistent per-frame failure cannot busy-loop on a dead device.
constexpr uint32_t kRenderFailuresBeforeReinit = 10;
constexpr uint64_t kRenderReinitDebounceMs = 500;
// A blip that keeps receiving hits at its angle holds its level instead of
// decaying, so sustained audio renders one steady frame instead of fighting a
// 30 ms decay clock that never catches up.
constexpr uint64_t kDecayHoldMs = 150;

struct Blip {
    float angle = 0.0f;
    float life = 0.0f;
    uint64_t last_hit_ms = 0;
    // The 8-bit alpha level last submitted to the composition surface for this
    // blip. Dirty detection compares against it so pixel-identical frames
    // (same angle and level) skip the redraw entirely.
    uint8_t shown_opacity = 0;
    ComPtr<ID2D1PathGeometry> geometry;
};

inline float angle_diff(float a, float b) {
    float d = std::fmod(std::fabs(a - b), 360.0f);
    return d > 180.0f ? 360.0f - d : d;
}

// QWidget painted with QColor(alpha=int(life * 255)). Keep the fade curve in
// the same 255 discrete levels and compare levels, not raw life, when deciding
// whether a frame would actually change pixels.
inline uint8_t quantize_opacity(float life) {
    return static_cast<uint8_t>(std::floor(std::max(0.0f, std::min(1.0f, life)) * 255.0f));
}

// Commit-pacing knobs are read from the environment so the update stream can
// be varied (A/B) without rebuilding the DLL. Overlay diagnostics (init /
// show / hide / recover / slow frames plus a 5 s stats summary) are appended
// to a file only while a log path is set; with no path the render loop never
// opens one.
//
// Read the real Windows environment block (GetEnvironmentVariableA) instead
// of the CRT's cached copy: values set in-process by the Python side
// (os.environ before vao_create, see native_overlay.py) are only visible
// through the Windows block, while the CRT copy never updates after startup.
std::string env_value(const char *name) {
    const DWORD needed = GetEnvironmentVariableA(name, nullptr, 0);
    if (needed == 0)
        return {};
    std::string value(needed, '\0');
    const DWORD got = GetEnvironmentVariableA(name, value.data(), needed);
    // needed counts the terminating NUL as well; drop it.
    if (got == 0 || got >= needed)
        return {};
    value.resize(got);
    return value;
}

bool env_flag(const char *name, bool fallback) {
    const std::string v = env_value(name);
    if (v.empty())
        return fallback;
    const char c = v.front();
    return c == '1' || c == 'y' || c == 'Y' || c == 't' || c == 'T';
}

uint64_t env_u64(const char *name, uint64_t fallback) {
    const std::string v = env_value(name);
    if (v.empty())
        return fallback;
    char *end = nullptr;
    const unsigned long long n = std::strtoull(v.c_str(), &end, 10);
    return end && *end == '\0' ? n : fallback;
}

// Diag path parsing mirrors the sibling knobs' off convention: "0", "no" or
// "false" (any case) turns diagnostics off instead of being mistaken for a
// log path, "1" is shorthand for the default file name in the process
// working directory, and any other value is a literal log path.  The Python
// wrapper normally sets the full overlay.log path when app file logging is
// on, so this env var only matters for manual shell usage.
bool is_false_value(const std::string &value) {
    std::string lower;
    lower.reserve(value.size());
    for (const char c : value)
        lower.push_back(static_cast<char>(std::tolower(static_cast<unsigned char>(c))));
    return lower == "0" || lower == "no" || lower == "false";
}

std::string diag_env_path() {
    const std::string p = env_value("VAO_NATIVE_DIAG");
    if (p.empty() || is_false_value(p))
        return {};
    return p == "1" ? std::string("overlay_native_diag.log") : p;
}

std::string hex_u32(uint32_t v) {
    std::string s;
    s.reserve(8);
    for (int shift = 28; shift >= 0; shift -= 4) {
        const uint32_t nibble = (v >> shift) & 0xF;
        s.push_back(nibble < 10 ? static_cast<char>('0' + nibble)
                                : static_cast<char>('A' + nibble - 10));
    }
    return s;
}

// Wall-clock prefix for the diagnostics log, matching the ISO-style
// timestamps the Python side writes to lifecycle.log. The render thread is a
// single writer of debug lines, so lifecycle's pid/tid/level chrome adds no
// information here.
std::string padded(unsigned value, unsigned width) {
    std::string s = std::to_string(value);
    if (width > s.size())
        s.insert(0, width - s.size(), '0');
    return s;
}

std::string log_timestamp() {
    SYSTEMTIME st{};
    GetLocalTime(&st);
    return padded(st.wYear, 4) + "-" + padded(st.wMonth, 2) + "-" + padded(st.wDay, 2) + "T" +
           padded(st.wHour, 2) + ":" + padded(st.wMinute, 2) + ":" + padded(st.wSecond, 2) + "." +
           padded(st.wMilliseconds, 3);
}

// Diagnostics cap mirrors the Python lifecycle log: 2 MiB, then the file is
// truncated in place (no backups kept).
constexpr long kMaxLogBytes = 2 * 1024 * 1024;

class Overlay {
  public:
    Overlay()
        : dwmflush_(env_flag("VAO_RENDER_DWMFLUSH", true)),
          render_interval_ms_(env_u64("VAO_RENDER_INTERVAL_MS", 0)), diag_path_(diag_env_path()) {}
    ~Overlay() { destroy(); }

    bool create() {
        std::unique_lock<std::mutex> lock(mutex_);
        if (thread_.joinable()) {
            return ready_ && !failed_;
        }
        stop_ = false;
        ready_ = false;
        failed_ = false;
        thread_ = std::thread(&Overlay::thread_main, this);
        ready_cv_.wait(lock, [this] { return ready_; });
        return !failed_;
    }

    void destroy() {
        {
            std::lock_guard<std::mutex> lock(mutex_);
            if (!thread_.joinable()) {
                return;
            }
            stop_ = true;
            wake_ = true;
        }
        if (thread_id_ != 0) {
            PostThreadMessageW(thread_id_, WM_QUIT, 0, 0);
        }
        ready_cv_.notify_all();
        if (thread_.joinable()) {
            thread_.join();
        }
        std::lock_guard<std::mutex> lock(mutex_);
        hwnd_ = nullptr;
        thread_id_ = 0;
    }

    int show() {
        return command([this] {
            ShowWindow(hwnd_, SW_SHOWNOACTIVATE);
            SetWindowPos(hwnd_, HWND_TOPMOST, 0, 0, 0, 0, SWP_NOMOVE | SWP_NOSIZE | SWP_NOACTIVATE);
            // Repaint immediately: hide() may have left the DComp surface with
            // stale content and DWM only re-samples it after a Commit.
            dirty_ = true;
            diag_write("show");
        });
    }
    int hide() {
        return command([this] {
            // A drag can be cancelled by a stop/restart while the left button is
            // still held.  Always release capture before hiding; otherwise USER32
            // keeps routing subsequent mouse messages to this HWND and the game
            // appears to have lost input even though the overlay is invisible.
            if (dragging_ && GetCapture() == hwnd_)
                ReleaseCapture();
            dragging_ = false;
            EnableWindow(hwnd_, FALSE);
            ShowWindow(hwnd_, SW_HIDE);
            blips_.clear();
            KillTimer(hwnd_, kDecayTimer);
            dirty_ = false;
            diag_write("hide");
        });
    }

    int set_geometry(int x, int y, int width, int height) {
        width = std::max(1, std::min(width, 4096));
        height = std::max(1, std::min(height, 4096));
        return command([this, x, y, width, height] {
            width_ = width;
            height_ = height;
            for (auto &blip : blips_)
                blip.geometry.Reset();
            SetWindowPos(hwnd_, HWND_TOPMOST, x, y, width, height,
                         SWP_NOACTIVATE | SWP_NOOWNERZORDER);
            recreate_target_ = true;
            dirty_ = true;
        });
    }

    int set_drag_enabled(bool enabled) {
        return command([this, enabled] {
            // HTTRANSPARENT only asks USER32 to continue hit testing within
            // this GUI thread.  The layered/transparent styles installed at
            // creation provide the cross-process pass-through; disabling the
            // HWND in normal mode is an additional guard against activation or
            // capture leftovers.
            if (!enabled && dragging_) {
                ReleaseCapture();
            }
            drag_enabled_ = enabled;
            dragging_ = false;
            ULONG_PTR ex_style = static_cast<ULONG_PTR>(GetWindowLongPtrW(hwnd_, GWL_EXSTYLE));
            if (enabled) {
                ex_style &= ~static_cast<ULONG_PTR>(WS_EX_TRANSPARENT);
            } else {
                ex_style |= static_cast<ULONG_PTR>(WS_EX_TRANSPARENT);
            }
            SetWindowLongPtrW(hwnd_, GWL_EXSTYLE, static_cast<LONG_PTR>(ex_style));
            EnableWindow(hwnd_, enabled ? TRUE : FALSE);
            SetWindowPos(hwnd_, nullptr, 0, 0, 0, 0,
                         SWP_NOMOVE | SWP_NOSIZE | SWP_NOZORDER | SWP_NOACTIVATE |
                             SWP_FRAMECHANGED);
            SetCursor(LoadCursorW(nullptr, enabled ? IDC_HAND : IDC_ARROW));
            dirty_ = true;
        });
    }

    int set_generation(uint64_t generation) {
        return command([this, generation] {
            // A new capture session invalidates every pending/visible result
            // from the previous worker.  Apply this on the render thread before
            // consuming the mailbox so an old sample cannot reappear after a
            // stop/restart race.
            generation_ = generation;
            blips_.clear();
            dirty_ = true;
        });
    }

    int set_style(uint32_t color_rgba, float stroke_width) {
        if (!std::isfinite(stroke_width)) {
            return 0;
        }
        stroke_width = std::max(1.0f, std::min(stroke_width, 64.0f));
        return command([this, color_rgba, stroke_width] {
            color_ = color_rgba;
            stroke_width_ = stroke_width;
            dirty_ = true;
        });
    }

    int submit_audio(uint64_t generation, float angle, float intensity, int64_t /*timestamp_ns*/) {
        if (!std::isfinite(angle) || !std::isfinite(intensity)) {
            return 0;
        }
        angle = std::fmod(angle, 360.0f);
        if (angle < -180.0f)
            angle += 360.0f;
        if (angle > 180.0f)
            angle -= 360.0f;
        intensity = std::max(0.0f, std::min(intensity, 1.0f));
        std::lock_guard<std::mutex> lock(mutex_);
        if (!thread_.joinable() || failed_ || stop_.load())
            return 0;
        latest_audio_ = Audio{generation, angle, intensity};
        wake_ = true;
        wake_cv_.notify_one();
        return 1;
    }

    int poll_event(VaoEvent *out) {
        if (!out)
            return 0;
        std::lock_guard<std::mutex> lock(mutex_);
        if (events_.empty())
            return 0;
        *out = events_.front();
        events_.pop_front();
        return 1;
    }

  private:
    struct Audio {
        uint64_t generation;
        float angle;
        float intensity;
    };
    using Command = std::function<void()>;

    template <typename Fn> int command(Fn &&fn) {
        std::lock_guard<std::mutex> lock(mutex_);
        if (!thread_.joinable() || failed_ || stop_.load() || hwnd_ == nullptr)
            return 0;
        if (commands_.size() >= kMaxCommands) {
            // Exported setters are asynchronous. Keep the queue bounded so a
            // burst of stale geometry/style updates cannot grow without limit
            // while the render thread is handling a device reset.
            commands_.pop_front();
        }
        commands_.emplace_back(std::forward<Fn>(fn));
        wake_ = true;
        wake_cv_.notify_one();
        return 1;
    }

    void thread_main() {
        thread_id_ = GetCurrentThreadId();
        HRESULT hr = CoInitializeEx(nullptr, COINIT_MULTITHREADED);
        (void)hr;
        const wchar_t *class_name = L"VisualAudioOverlay.Native";
        WNDCLASSEXW wc{sizeof(wc)};
        wc.hInstance = GetModuleHandleW(nullptr);
        wc.lpfnWndProc = &Overlay::window_proc;
        wc.lpszClassName = class_name;
        wc.hCursor = LoadCursorW(nullptr, IDC_ARROW);
        RegisterClassExW(&wc);
        hwnd_ = CreateWindowExW(WS_EX_NOACTIVATE | WS_EX_TOOLWINDOW |
                                    // HTTRANSPARENT is scoped to a GUI thread.
                                    // A layered + transparent top-level window
                                    // is required for reliable cross-process
                                    // click-through (e.g. a game HWND).
                                    WS_EX_LAYERED | WS_EX_TRANSPARENT | WS_EX_NOREDIRECTIONBITMAP,
                                class_name, L"Visual Audio Overlay", WS_POPUP, 0, 0, width_,
                                height_, nullptr, nullptr, wc.hInstance, this);
        if (!hwnd_) {
            fail_locked();
            CoUninitialize();
            return;
        }
        SetWindowLongPtrW(hwnd_, GWLP_USERDATA, reinterpret_cast<LONG_PTR>(this));
        // LWA_ALPHA=255 preserves the swap chain's premultiplied per-pixel
        // alpha; it does not dim the DirectComposition content.
        SetLayeredWindowAttributes(hwnd_, 0, 255, LWA_ALPHA);
        // Keep the rendered surface visible while excluding this HWND from
        // system hit testing.  Do not rely on WS_EX_TRANSPARENT or
        // HTTRANSPARENT: both are insufficient for a game owned by another
        // process.  Drag mode re-enables the window below.
        EnableWindow(hwnd_, FALSE);
        SetWindowPos(hwnd_, HWND_TOPMOST, 0, 0, width_, height_, SWP_NOACTIVATE | SWP_HIDEWINDOW);
        apply_dwm_attributes();
        if (!init_graphics()) {
            fail_locked();
            DestroyWindow(hwnd_);
            hwnd_ = nullptr;
            CoUninitialize();
            return;
        }
        diag_write("init ok flush=" + std::string(dwmflush_ ? "1" : "0") +
                   " interval_ms=" + std::to_string(render_interval_ms_));
        {
            std::lock_guard<std::mutex> lock(mutex_);
            ready_ = true;
        }
        ready_cv_.notify_all();

        MSG msg{};
        while (!stop_) {
            while (PeekMessageW(&msg, nullptr, 0, 0, PM_REMOVE)) {
                if (msg.message == WM_QUIT) {
                    stop_ = true;
                    break;
                }
                TranslateMessage(&msg);
                DispatchMessageW(&msg);
            }
            execute_commands();
            consume_audio();
            auto now = GetTickCount64();
            if (display_change_pending_ || (!d2d_context_ && now >= graphics_retry_at_)) {
                // A mode switch or monitor hotplug can invalidate the DComp
                // target without immediately returning DXGI_ERROR_DEVICE_*.
                // Recreate the device/surface on the render thread so the
                // existing HWND and latest blips survive the transition.
                display_change_pending_ = false;
                reinit_graphics(now);
            }
            if (!blips_.empty() && now >= next_decay_) {
                decay_blips(now);
            }
            // Content reaches DWM through an IDCompositionSurface that DWM
            // samples at composition time: there is no swap chain and no flip
            // queue to pace, so a frame is committed only when the 255-level
            // opacity/arc set actually changed (dirty_). render() keeps dirty_
            // set when it bailed so the missed frame stays pending.
            //
            // Pacing (VAO_RENDER_DWMFLUSH / VAO_RENDER_INTERVAL_MS) stops the
            // stream from bursting at audio pace: an unsettled cadence of
            // commits over a flip-model game is what the overlay degrades
            // (game-only flicker after minutes that a ~45 s commit pause
            // clears), so by default every Commit is followed by DwmFlush(),
            // phase-locking commits to DWM's composition clock.
            bool due = render_interval_ms_ == 0 || now >= next_render_at_;
            if (due && dirty_ && d2d_context_) {
                const uint64_t r_start = GetTickCount64();
                const bool rendered = render();
                const uint64_t r_ms = GetTickCount64() - r_start;
                if (rendered) {
                    dirty_ = false;
                    ++renders_ok_;
                    consecutive_render_failures_ = 0;
                    if (render_interval_ms_ != 0)
                        next_render_at_ = GetTickCount64() + render_interval_ms_;
                } else {
                    ++renders_failed_;
                    const uint64_t failed_at = GetTickCount64();
                    // A persistent per-frame failure that no recover path
                    // turns into a device rebuild (e.g. CreateSurface failing
                    // with a non-device error) must not spin at tick rate
                    // forever: after a run of failures, rebuild the whole
                    // stack, but not more often than the debounce allows.
                    if (++consecutive_render_failures_ >= kRenderFailuresBeforeReinit &&
                        failed_at - last_reinit_at_ >= kRenderReinitDebounceMs) {
                        consecutive_render_failures_ = 0;
                        last_reinit_at_ = failed_at;
                        reinit_graphics(failed_at);
                    }
                }
                if (r_ms >= 50)
                    diag_write("slow_render ms=" + std::to_string(r_ms) +
                               " rendered=" + std::string(rendered ? "1" : "0"));
            }
            // Stats tick only while the radar window is visible: the thread
            // lives for the whole process (stop merely hides), and a heartbeat
            // of frozen counters after "hide" is noise.  Events (show/hide/
            // recover/slow_render) still record the transitions themselves.
            if (IsWindowVisible(hwnd_) && now - diag_last_ >= 5000) {
                diag_last_ = now;
                diag_write("stats renders_ok=" + std::to_string(renders_ok_) +
                           " renders_fail=" + std::to_string(renders_failed_) + " audio_pkts=" +
                           std::to_string(audio_pkts_) + " recovers=" + std::to_string(recovers_) +
                           " blips=" + std::to_string(blips_.size()) +
                           " dirty=" + std::string(dirty_ ? "1" : "0"));
            }
            std::unique_lock<std::mutex> lock(mutex_);
            wake_cv_.wait_for(lock, std::chrono::milliseconds(5),
                              [this] { return wake_ || stop_; });
            wake_ = false;
        }
        KillTimer(hwnd_, kDecayTimer);
        release_graphics();
        DestroyWindow(hwnd_);
        UnregisterClassW(class_name, wc.hInstance);
        {
            std::lock_guard<std::mutex> lock(mutex_);
            hwnd_ = nullptr;
            ready_ = true;
        }
        CoUninitialize();
    }

    void fail_locked() {
        std::lock_guard<std::mutex> lock(mutex_);
        failed_ = true;
        ready_ = true;
        push_event_locked(VAO_EVENT_ERROR, 0, 0);
        ready_cv_.notify_all();
    }

    static LRESULT CALLBACK window_proc(HWND hwnd, UINT msg, WPARAM wp, LPARAM lp) {
        auto *self = reinterpret_cast<Overlay *>(GetWindowLongPtrW(hwnd, GWLP_USERDATA));
        if (msg == WM_NCCREATE) {
            auto *cs = reinterpret_cast<CREATESTRUCTW *>(lp);
            self = static_cast<Overlay *>(cs->lpCreateParams);
            SetWindowLongPtrW(hwnd, GWLP_USERDATA, reinterpret_cast<LONG_PTR>(self));
        }
        if (!self)
            return DefWindowProcW(hwnd, msg, wp, lp);
        switch (msg) {
        case WM_NCHITTEST:
            return self->drag_enabled_ ? HTCLIENT : HTTRANSPARENT;
        case WM_MOUSEACTIVATE:
            // Never activate/focus the overlay when it is clicked through.
            return self->drag_enabled_ ? MA_NOACTIVATE : MA_NOACTIVATEANDEAT;
        case WM_LBUTTONDOWN:
            if (self->drag_enabled_) {
                self->dragging_ = true;
                self->drag_origin_ = POINT{GET_X_LPARAM(lp), GET_Y_LPARAM(lp)};
                GetWindowRect(hwnd, &self->drag_window_origin_);
                self->drag_screen_origin_ = POINT{GET_X_LPARAM(lp), GET_Y_LPARAM(lp)};
                ClientToScreen(hwnd, &self->drag_screen_origin_);
                SetCapture(hwnd);
                return 0;
            }
            break;
        case WM_MOUSEMOVE:
            if (self->dragging_) {
                POINT pt{GET_X_LPARAM(lp), GET_Y_LPARAM(lp)};
                ClientToScreen(hwnd, &pt);
                int dx = pt.x - self->drag_screen_origin_.x;
                int dy = pt.y - self->drag_screen_origin_.y;
                SetWindowPos(hwnd, HWND_TOPMOST, self->drag_window_origin_.left + dx,
                             self->drag_window_origin_.top + dy, 0, 0, SWP_NOSIZE | SWP_NOACTIVATE);
                RECT r{};
                GetWindowRect(hwnd, &r);
                self->push_event(VAO_EVENT_POSITION_PREVIEW, r.left, r.top);
                return 0;
            }
            break;
        case WM_LBUTTONUP:
            if (self->dragging_) {
                self->dragging_ = false;
                ReleaseCapture();
                RECT r{};
                GetWindowRect(hwnd, &r);
                self->push_event(VAO_EVENT_POSITION_COMMITTED, r.left, r.top);
                return 0;
            }
            break;
        case WM_SETCURSOR:
            if (self->drag_enabled_) {
                SetCursor(LoadCursorW(nullptr, self->dragging_ ? IDC_SIZEALL : IDC_HAND));
                return TRUE;
            }
            break;
        case WM_DISPLAYCHANGE:
        case WM_DPICHANGED:
            // WM_DISPLAYCHANGE is broadcast on topology/mode changes, while
            // WM_DPICHANGED is delivered when this HWND crosses a mixed-DPI
            // boundary. Both can leave a DirectComposition target stale.
            self->display_change_pending_ = true;
            self->recreate_target_ = true;
            self->dirty_ = true;
            break;
        default:
            break; // Unhandled messages fall through to DefWindowProcW below.
        }
        return DefWindowProcW(hwnd, msg, wp, lp);
    }

    void push_event(uint32_t type, int x, int y) {
        std::lock_guard<std::mutex> lock(mutex_);
        push_event_locked(type, x, y);
    }

    void push_event_locked(uint32_t type, int x, int y) {
        if (events_.size() >= kMaxEvents)
            events_.pop_front();
        events_.push_back(VaoEvent{type, x, y, ++event_sequence_});
    }

    void execute_commands() {
        std::deque<Command> pending;
        {
            std::lock_guard<std::mutex> lock(mutex_);
            pending.swap(commands_);
        }
        for (auto &fn : pending)
            fn();
    }

    void consume_audio() {
        std::optional<Audio> audio;
        {
            std::lock_guard<std::mutex> lock(mutex_);
            audio = latest_audio_;
            latest_audio_.reset();
        }
        if (!audio || !IsWindowVisible(hwnd_))
            return;
        if (audio->generation < generation_)
            return;
        ++audio_pkts_;
        const uint64_t now = GetTickCount64();
        if (audio->generation > generation_) {
            generation_ = audio->generation;
            // A new capture session invalidates every visible result from the
            // previous worker, so drop them and clear the surface.
            if (!blips_.empty()) {
                blips_.clear();
                next_decay_ = 0;
                dirty_ = true;
            }
        }
        const float life = std::min(1.0f, audio->intensity * kVisualGain);
        if (quantize_opacity(life) == 0)
            return; // Below the first visible alpha level; nothing to draw.
        bool changed = false;
        bool merged = false;
        for (auto &blip : blips_) {
            if (angle_diff(blip.angle, audio->angle) < 20.0f) {
                // A hit refreshes the decay hold even when it does not move
                // the level, so steady audio stays on one rendered frame.
                blip.last_hit_ms = now;
                if (life > blip.life) {
                    blip.life = life;
                    if (quantize_opacity(blip.life) != blip.shown_opacity)
                        changed = true;
                }
                merged = true;
                break;
            }
        }
        if (!merged) {
            if (blips_.size() >= kMaxBlips)
                blips_.erase(blips_.begin()); // The push below repaints anyway.
            Blip blip;
            blip.angle = audio->angle;
            blip.life = life;
            blip.last_hit_ms = now;
            blips_.push_back(blip);
            changed = true;
        }
        // Mark dirty only when the frame would differ from the last present
        // (quantized alpha moved, or an arc appeared/disappeared).
        if (changed)
            dirty_ = true;
        // Decay is an independent 30 ms clock. Do not postpone it on every
        // audio packet: changing directions must let older blips fade while
        // new packets continue to arrive.
        if (next_decay_ == 0 && !blips_.empty())
            next_decay_ = now + 30;
    }

    void decay_blips(uint64_t now) {
        bool changed = false;
        for (auto &blip : blips_) {
            if (blip.last_hit_ms != 0 && now - blip.last_hit_ms < kDecayHoldMs)
                continue; // Still fed by audio; keep the level steady.
            blip.last_hit_ms = 0;
            blip.life -= kDecay;
            if (quantize_opacity(blip.life) != blip.shown_opacity)
                changed = true;
        }
        const size_t before = blips_.size();
        blips_.erase(std::remove_if(blips_.begin(), blips_.end(),
                                    [](const Blip &b) { return b.life <= 0.0f; }),
                     blips_.end());
        if (blips_.size() != before)
            changed = true;
        // Even when the final blip expires, a transparent frame must be
        // submitted to clear the previous composition surface.  Leaving
        // dirty_ false here makes the last arc remain visible indefinitely.
        if (changed)
            dirty_ = true;
        if (blips_.empty())
            next_decay_ = 0;
        else
            next_decay_ = now + 30;
    }

    bool init_graphics() {
        UINT flags = D3D11_CREATE_DEVICE_BGRA_SUPPORT;
        // Sane default; overwritten by D3D11CreateDevice with the highest
        // feature level the device actually supports.
        D3D_FEATURE_LEVEL level = D3D_FEATURE_LEVEL_11_0;
        HRESULT hr = D3D11CreateDevice(nullptr, D3D_DRIVER_TYPE_HARDWARE, nullptr, flags, nullptr,
                                       0, D3D11_SDK_VERSION, &d3d_device_, &level, &d3d_context_);
        if (FAILED(hr)) {
            // Some remote-desktop and software-only sessions expose no hardware
            // adapter. WARP keeps the dashboard usable without affecting the
            // normal hardware path.
            hr = D3D11CreateDevice(nullptr, D3D_DRIVER_TYPE_WARP, nullptr, flags, nullptr, 0,
                                   D3D11_SDK_VERSION, &d3d_device_, &level, &d3d_context_);
        }
        if (FAILED(hr))
            return false;
        if (FAILED(d3d_device_.As(&dxgi_device_)))
            return false;
        if (FAILED(D2D1CreateFactory(
                D2D1_FACTORY_TYPE_SINGLE_THREADED, __uuidof(ID2D1Factory1),
                reinterpret_cast<void **>(d2d_factory_.ReleaseAndGetAddressOf()))))
            return false;
        if (FAILED(d2d_factory_->CreateDevice(dxgi_device_.Get(), &d2d_device_)))
            return false;
        if (FAILED(
                d2d_device_->CreateDeviceContext(D2D1_DEVICE_CONTEXT_OPTIONS_NONE, &d2d_context_)))
            return false;
        // QPainter's audio pen uses round caps.  Keep an explicit stroke style
        // instead of relying on the D2D default (flat caps), which changes the
        // perceived arc length and makes short blips visibly thinner.
        D2D1_STROKE_STYLE_PROPERTIES round_props = D2D1::StrokeStyleProperties(
            D2D1_CAP_STYLE_ROUND, D2D1_CAP_STYLE_ROUND, D2D1_CAP_STYLE_ROUND, D2D1_LINE_JOIN_ROUND,
            10.0f, D2D1_DASH_STYLE_SOLID, 0.0f);
        if (FAILED(d2d_factory_->CreateStrokeStyle(round_props, nullptr, 0, &round_stroke_)))
            return false;
        // The setup ring in the QWidget implementation is Qt::DashLine.  A
        // D2D dash style gives the same lightweight editing affordance while
        // keeping the native surface independent of Qt.
        D2D1_STROKE_STYLE_PROPERTIES dash_props = D2D1::StrokeStyleProperties(
            D2D1_CAP_STYLE_FLAT, D2D1_CAP_STYLE_FLAT, D2D1_CAP_STYLE_FLAT, D2D1_LINE_JOIN_ROUND,
            10.0f, D2D1_DASH_STYLE_DASH, 0.0f);
        if (FAILED(d2d_factory_->CreateStrokeStyle(dash_props, nullptr, 0, &dash_stroke_)))
            return false;
        if (FAILED(DCompositionCreateDevice(dxgi_device_.Get(), IID_PPV_ARGS(&dcomp_device_))))
            return false;
        if (FAILED(dcomp_device_->CreateTargetForHwnd(hwnd_, TRUE, &dcomp_target_)))
            return false;
        if (FAILED(dcomp_device_->CreateVisual(&dcomp_visual_)))
            return false;
        if (FAILED(dcomp_target_->SetRoot(dcomp_visual_.Get())))
            return false;
        return SUCCEEDED(create_content_surface());
    }

    // DWM samples this surface at composition time instead of us flipping a
    // swap chain to it. A flip-model chain floating over a composed fullscreen
    // game contends with the game's own planes (MPO / independent-flip
    // fallback churn), which surfaces as whole-screen jitter after minutes; a
    // plain surface never claims a plane or queues frames, so redrawing is
    // just a Commit DWM picks up on its next frame.
    HRESULT create_content_surface() {
        if (!dcomp_device_)
            return E_FAIL;
        HRESULT hr = dcomp_device_->CreateSurface(
            static_cast<UINT>(width_), static_cast<UINT>(height_), DXGI_FORMAT_B8G8R8A8_UNORM,
            DXGI_ALPHA_MODE_PREMULTIPLIED, &dcomp_surface_);
        if (FAILED(hr))
            return hr;
        hr = dcomp_visual_->SetContent(dcomp_surface_.Get());
        if (SUCCEEDED(hr))
            hr = dcomp_device_->Commit();
        if (FAILED(hr)) {
            dcomp_surface_.Reset();
            recreate_target_ = true;
            return hr;
        }
        recreate_target_ = false;
        return S_OK;
    }

    bool ensure_blip_geometry(Blip &blip) {
        if (blip.geometry)
            return true;
        ComPtr<ID2D1PathGeometry> geometry;
        if (FAILED(d2d_factory_->CreatePathGeometry(&geometry)))
            return false;
        ComPtr<ID2D1GeometrySink> sink;
        if (FAILED(geometry->Open(&sink)))
            return false;
        const float cx = static_cast<float>(width_) * 0.5f;
        const float cy = static_cast<float>(height_) * 0.5f;
        const float radius = static_cast<float>(std::min(width_, height_)) * 0.4f;
        const float start = (90.0f - blip.angle - kArcSpan * 0.5f) * 3.1415926535f / 180.0f;
        const float end = (90.0f - blip.angle + kArcSpan * 0.5f) * 3.1415926535f / 180.0f;
        auto point = [cx, cy, radius](float angle) {
            return D2D1::Point2F(cx + radius * std::cos(angle), cy - radius * std::sin(angle));
        };
        sink->BeginFigure(point(start), D2D1_FIGURE_BEGIN_HOLLOW);
        sink->AddArc(D2D1::ArcSegment(point(end), D2D1::SizeF(radius, radius), 0.0f,
                                      D2D1_SWEEP_DIRECTION_COUNTER_CLOCKWISE,
                                      kArcSpan > 180 ? D2D1_ARC_SIZE_LARGE : D2D1_ARC_SIZE_SMALL));
        sink->EndFigure(D2D1_FIGURE_END_OPEN);
        if (FAILED(sink->Close()))
            return false;
        blip.geometry = std::move(geometry);
        return true;
    }

    // Returns true when the new content reached the composition tree. On
    // failure the caller keeps dirty_ set so the state that was not drawn
    // stays pending.
    bool render() {
        if (!d2d_context_)
            return false;
        if (recreate_target_) {
            // The surface is size-immutable and can be invalidated by a
            // display/DPI change or a graphics-driver reset. Drop the old one
            // so the next step builds it fresh.
            d2d_context_->SetTarget(nullptr);
            dcomp_surface_.Reset();
            recreate_target_ = false;
        }
        if (!dcomp_surface_) {
            const HRESULT surface_hr = create_content_surface();
            if (FAILED(surface_hr)) {
                // A failed (re)creation is a graphics-stack error like any
                // other: device-removal classes must rebuild the whole stack,
                // and without this the overlay would silently retry every tick
                // on a dead DComp device and stay blank forever.
                recover_from_graphics_error(surface_hr, GetTickCount64());
                return false;
            }
        }
        ComPtr<IDXGISurface> surface;
        POINT surface_offset{};
        HRESULT hr = dcomp_surface_->BeginDraw(nullptr, IID_PPV_ARGS(&surface), &surface_offset);
        if (FAILED(hr)) {
            recover_from_graphics_error(hr, GetTickCount64());
            return false;
        }
        // DirectComposition discards each update's backing memory after
        // EndDraw and may hand out a different buffer on every BeginDraw, so
        // the D2D target must wrap this update's surface only.  Reusing a
        // bitmap from an earlier frame would draw into a buffer DWM no longer
        // samples, and the overlay would never appear.  The buffer handed out
        // is a large shared texture (an arena); CANNOT_DRAW is required there
        // - D2D rejects wrapping it with a plain TARGET bitmap.
        D2D1_BITMAP_PROPERTIES1 props = D2D1::BitmapProperties1(
            D2D1_BITMAP_OPTIONS_TARGET | D2D1_BITMAP_OPTIONS_CANNOT_DRAW,
            D2D1::PixelFormat(DXGI_FORMAT_B8G8R8A8_UNORM, D2D1_ALPHA_MODE_PREMULTIPLIED), 96.0f,
            96.0f, nullptr);
        ComPtr<ID2D1Bitmap1> target;
        hr = d2d_context_->CreateBitmapFromDxgiSurface(surface.Get(), &props, &target);
        if (FAILED(hr)) {
            dcomp_surface_->EndDraw();
            recover_from_graphics_error(hr, GetTickCount64());
            return false;
        }
        d2d_context_->SetTarget(target.Get());
        d2d_context_->BeginDraw();
        // The update object is a slice of the backing arena; BeginDraw reports
        // where that slice starts, and the whole frame must be drawn there.
        // Every coordinate below stays surface-relative under this transform.
        d2d_context_->SetTransform(D2D1::Matrix3x2F::Translation(
            static_cast<float>(surface_offset.x), static_cast<float>(surface_offset.y)));
        // Match QPainter's default SourceOver composition explicitly. This is
        // important for a premultiplied target: replacing it with COPY would
        // make translucent strokes look either washed out or fully opaque.
        d2d_context_->SetPrimitiveBlend(D2D1_PRIMITIVE_BLEND_SOURCE_OVER);
        d2d_context_->Clear(D2D1::ColorF(0, 0.0f));
        const float cx = static_cast<float>(width_) * 0.5f;
        const float cy = static_cast<float>(height_) * 0.5f;
        const float radius = static_cast<float>(std::min(width_, height_)) * 0.4f;
        if (drag_enabled_) {
            // Layered/composited windows need a non-zero alpha surface to be
            // reliably hit-testable.  This mirrors OverlayRadar.paintEvent's
            // QColor(255, 255, 255, 8) fill exactly.
            ComPtr<ID2D1SolidColorBrush> drag_background;
            d2d_context_->CreateSolidColorBrush(D2D1::ColorF(1, 1, 1, 8.0f / 255.0f),
                                                &drag_background);
            d2d_context_->FillRectangle(
                D2D1::RectF(0.0f, 0.0f, static_cast<float>(width_), static_cast<float>(height_)),
                drag_background.Get());
        }
        ComPtr<ID2D1SolidColorBrush> brush;
        auto color = D2D1::ColorF(static_cast<float>(color_ >> 24u & 0xffu) / 255.0f,
                                  static_cast<float>(color_ >> 16u & 0xffu) / 255.0f,
                                  static_cast<float>(color_ >> 8u & 0xffu) / 255.0f, 1.0f);
        d2d_context_->CreateSolidColorBrush(color, &brush);
        ComPtr<ID2D1SolidColorBrush> base;
        d2d_context_->CreateSolidColorBrush(D2D1::ColorF(1, 1, 1, 30.0f / 255.0f), &base);
        d2d_context_->DrawEllipse(D2D1::Ellipse(D2D1::Point2F(cx, cy), radius, radius), base.Get(),
                                  2.0f);
        for (auto &blip : blips_) {
            // QWidget used QColor(alpha=int(life * 255)). Draw at the same
            // 255-level opacity and record the level actually submitted, so
            // the dirty detection upstream skips pixel-identical frames.
            const uint8_t level = quantize_opacity(blip.life);
            brush->SetOpacity(static_cast<float>(level) / 255.0f);
            if (!ensure_blip_geometry(blip))
                continue;
            d2d_context_->DrawGeometry(blip.geometry.Get(), brush.Get(), stroke_width_,
                                       round_stroke_.Get());
            blip.shown_opacity = level;
        }
        if (drag_enabled_) {
            ComPtr<ID2D1SolidColorBrush> setup;
            d2d_context_->CreateSolidColorBrush(D2D1::ColorF(1, 1, 1, 120.0f / 255.0f), &setup);
            d2d_context_->DrawEllipse(
                D2D1::Ellipse(D2D1::Point2F(cx, cy), radius + 8.0f, radius + 8.0f), setup.Get(),
                1.0f, dash_stroke_.Get());
        }
        const HRESULT draw_hr = d2d_context_->EndDraw();
        d2d_context_->SetTarget(nullptr);
        // Release the surface lock even when D2D failed; only the result below
        // decides whether the content reached DWM.
        const HRESULT end_hr = dcomp_surface_->EndDraw();
        if (FAILED(draw_hr)) {
            // A lost device surfaces here as D2DERR_RECREATE_TARGET or a
            // generic EndDraw failure (device died mid-batch).  Route both
            // through the recover path: device-removal classes rebuild the
            // stack, anything else just forces a fresh target next tick.
            recover_from_graphics_error(draw_hr, GetTickCount64());
            return false;
        }
        if (FAILED(end_hr)) {
            recover_from_graphics_error(end_hr, GetTickCount64());
            return false;
        }
        // Publish the surface update; DWM samples it on its next composition
        // frame. There is no vsync wait and no flip queue here, so the commit
        // can never stall this thread behind the game's presentation.
        const HRESULT commit_hr = dcomp_device_->Commit();
        if (FAILED(commit_hr)) {
            recover_from_graphics_error(commit_hr, GetTickCount64());
            return false;
        }
        if (dwmflush_) {
            // Wait until DWM composes this update before the next one starts,
            // so commits arrive one per composition frame, evenly spaced and
            // phase-locked to the display clock.  Skip the wait when DWM is
            // not composing (session switch/remote disconnect): the wait has
            // no timeout and must not wedge the render thread behind a dead
            // compositor, which would also hang vao_destroy's join at exit.
            BOOL composing = FALSE;
            if (SUCCEEDED(DwmIsCompositionEnabled(&composing)) && composing) {
                const HRESULT flush_hr = DwmFlush();
                if (FAILED(flush_hr)) {
                    const uint64_t now = GetTickCount64();
                    if (now - last_dwm_diag_at_ >= 5000) {
                        last_dwm_diag_at_ = now;
                        diag_write("dwm_flush hr=0x" + hex_u32(static_cast<uint32_t>(flush_hr)));
                    }
                }
            }
        }
        return true;
    }

    // Recreate the whole graphics stack after a display change, a device
    // removal/reset (TDR) or a failed init. The first failure retries after
    // 200 ms; every further failure doubles the wait up to 2 s, so a driver
    // that stays down cannot busy-loop this thread against it while the game
    // fights to recover its own device.
    bool reinit_graphics(uint64_t now) {
        release_graphics();
        if (!init_graphics()) {
            graphics_retry_at_ = now + recovery_delay_ms_;
            recovery_delay_ms_ = std::min(recovery_delay_ms_ * 2, 2000ULL);
            return false;
        }
        recovery_delay_ms_ = 200;
        graphics_retry_at_ = 0;
        // The old surfaces are gone: force every blip to repaint from zero on
        // the next tick instead of comparing against stale shown_opacity.
        for (auto &blip : blips_)
            blip.shown_opacity = 0;
        dirty_ = true;
        return true;
    }

    void recover_from_graphics_error(HRESULT hr, uint64_t now) {
        ++recovers_;
        diag_write("recover hr=0x" + hex_u32(static_cast<uint32_t>(hr)));
        if (hr == DXGI_ERROR_DEVICE_REMOVED || hr == DXGI_ERROR_DEVICE_RESET) {
            reinit_graphics(now);
            return;
        }
        recreate_target_ = true;
        dirty_ = true;
    }

    // Overlay diagnostics (VAO_NATIVE_DIAG / overlay.log under app debug):
    // append one wall-clock-prefixed line per event or stats tick. Every
    // writer runs on the render thread, so no locking is needed.
    void diag_write(const std::string &line) {
        if (diag_path_.empty())
            return;
        FILE *f = fopen(diag_path_.c_str(), "ab");
        if (!f)
            return;
        if (std::fseek(f, 0, SEEK_END) == 0 && std::ftell(f) >= kMaxLogBytes) {
            // Cap mirrors the Python lifecycle log (2 MiB).  Delete the file
            // instead of truncating it in place: deleting frees the old space
            // before the next write, so a full disk cannot erase the log and
            // then fail to record the line that crossed the cap.
            std::fclose(f);
            std::remove(diag_path_.c_str());
            f = fopen(diag_path_.c_str(), "ab");
            if (!f)
                return;
        }
        const std::string out = log_timestamp() + " " + line + "\n";
        std::fwrite(out.data(), 1, out.size(), f);
        std::fclose(f);
    }

    void release_graphics() {
        if (d2d_context_)
            d2d_context_->SetTarget(nullptr);
        for (auto &blip : blips_)
            blip.geometry.Reset();
        dcomp_surface_.Reset();
        round_stroke_.Reset();
        dash_stroke_.Reset();
        dcomp_visual_.Reset();
        dcomp_target_.Reset();
        dcomp_device_.Reset();
        d2d_context_.Reset();
        d2d_device_.Reset();
        d2d_factory_.Reset();
        dxgi_device_.Reset();
        d3d_context_.Reset();
        d3d_device_.Reset();
    }

    void apply_dwm_attributes() {
        const int preference = 1;
        DwmSetWindowAttribute(hwnd_, 33, &preference, sizeof(preference));
        MARGINS margins{0, 0, 0, 0};
        DwmExtendFrameIntoClientArea(hwnd_, &margins);
    }

    std::mutex mutex_;
    std::condition_variable ready_cv_;
    std::condition_variable wake_cv_;
    std::thread thread_;
    std::deque<Command> commands_;
    std::deque<VaoEvent> events_;
    std::optional<Audio> latest_audio_;
    uint64_t event_sequence_ = 0;
    std::atomic<bool> stop_{false};
    bool ready_ = false;
    bool failed_ = false;
    bool wake_ = false;
    DWORD thread_id_ = 0;
    HWND hwnd_ = nullptr;
    int width_ = kDefaultWidth;
    int height_ = kDefaultHeight;
    bool drag_enabled_ = false;
    bool dragging_ = false;
    POINT drag_origin_{};
    POINT drag_screen_origin_{};
    RECT drag_window_origin_{};
    bool dirty_ = true;
    bool recreate_target_ = false;
    bool display_change_pending_ = false;
    bool dwmflush_ = true;
    uint64_t render_interval_ms_ = 0;
    uint64_t next_render_at_ = 0;
    uint64_t graphics_retry_at_ = 0;
    uint64_t recovery_delay_ms_ = 200;
    uint32_t consecutive_render_failures_ = 0;
    uint64_t last_reinit_at_ = 0;
    uint64_t last_dwm_diag_at_ = 0;
    uint64_t generation_ = 0;
    uint32_t color_ = 0x9751F2FF;
    float stroke_width_ = 6.0f;
    uint64_t next_decay_ = 0;
    std::vector<Blip> blips_;
    std::string diag_path_;
    uint64_t diag_last_ = 0;
    uint64_t renders_ok_ = 0;
    uint64_t renders_failed_ = 0;
    uint64_t audio_pkts_ = 0;
    uint64_t recovers_ = 0;

    ComPtr<ID3D11Device> d3d_device_;
    ComPtr<ID3D11DeviceContext> d3d_context_;
    ComPtr<IDXGIDevice> dxgi_device_;
    ComPtr<IDCompositionSurface> dcomp_surface_;
    ComPtr<ID2D1Factory1> d2d_factory_;
    ComPtr<ID2D1Device> d2d_device_;
    ComPtr<ID2D1DeviceContext> d2d_context_;
    ComPtr<ID2D1StrokeStyle> round_stroke_;
    ComPtr<ID2D1StrokeStyle> dash_stroke_;
    ComPtr<IDCompositionDevice> dcomp_device_;
    ComPtr<IDCompositionTarget> dcomp_target_;
    ComPtr<IDCompositionVisual> dcomp_visual_;
};

template <typename Fn>
auto with_handle(void *handle, Fn &&fn) -> decltype(fn(static_cast<Overlay *>(nullptr))) {
    if (!handle)
        return decltype(fn(static_cast<Overlay *>(nullptr))){};
    return std::forward<Fn>(fn)(static_cast<Overlay *>(handle));
}

} // namespace

extern "C" {

VAO_API void *vao_create(void) {
    auto *overlay = new (std::nothrow) Overlay();
    if (!overlay || !overlay->create()) {
        delete overlay;
        return nullptr;
    }
    return overlay;
}

VAO_API void vao_destroy(void *handle) {
    with_handle(handle, [](Overlay *o) {
        o->destroy();
        return 0;
    });
    delete static_cast<Overlay *>(handle);
}
VAO_API int vao_show(void *handle) {
    return with_handle(handle, [](Overlay *o) { return o->show(); });
}
VAO_API int vao_hide(void *handle) {
    return with_handle(handle, [](Overlay *o) { return o->hide(); });
}
VAO_API int vao_set_geometry(void *handle, int32_t x, int32_t y, int32_t w, int32_t h) {
    return with_handle(handle, [=](Overlay *o) { return o->set_geometry(x, y, w, h); });
}
VAO_API int vao_set_drag_enabled(void *handle, int enabled) {
    return with_handle(handle, [=](Overlay *o) { return o->set_drag_enabled(enabled != 0); });
}
VAO_API int vao_set_generation(void *handle, uint64_t generation) {
    return with_handle(handle, [=](Overlay *o) { return o->set_generation(generation); });
}
VAO_API int vao_set_style(void *handle, uint32_t c, float w) {
    return with_handle(handle, [=](Overlay *o) { return o->set_style(c, w); });
}
VAO_API int vao_submit_audio(void *handle, uint64_t g, float a, float i, int64_t t) {
    return with_handle(handle, [=](Overlay *o) { return o->submit_audio(g, a, i, t); });
}
VAO_API int vao_poll_event(void *handle, VaoEvent *out) {
    return with_handle(handle, [=](Overlay *o) { return o->poll_event(out); });
}

VAO_API void *create(void) { return vao_create(); }
VAO_API void destroy(void *h) { vao_destroy(h); }
VAO_API int show(void *h) { return vao_show(h); }
VAO_API int hide(void *h) { return vao_hide(h); }
VAO_API int set_geometry(void *h, int32_t x, int32_t y, int32_t w, int32_t z) {
    return vao_set_geometry(h, x, y, w, z);
}
VAO_API int set_drag_enabled(void *h, int e) { return vao_set_drag_enabled(h, e); }
VAO_API int set_generation(void *h, uint64_t g) { return vao_set_generation(h, g); }
VAO_API int set_style(void *h, uint32_t c, float w) { return vao_set_style(h, c, w); }
VAO_API int submit_audio(void *h, uint64_t g, float a, float i, int64_t t) {
    return vao_submit_audio(h, g, a, i, t);
}
VAO_API int poll_event(void *h, VaoEvent *e) { return vao_poll_event(h, e); }
}
