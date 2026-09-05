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
#include <array>
#include <atomic>
#include <cmath>
#include <condition_variable>
#include <cstdint>
#include <deque>
#include <functional>
#include <mutex>
#include <optional>
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

struct Blip {
    float angle = 0.0f;
    float life = 0.0f;
    ComPtr<ID2D1PathGeometry> geometry;
};

inline float angle_diff(float a, float b) {
    float d = std::fmod(std::fabs(a - b), 360.0f);
    return d > 180.0f ? 360.0f - d : d;
}

class Overlay {
  public:
    Overlay() = default;
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
            dirty_ = true;
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
            LONG_PTR ex_style = GetWindowLongPtrW(hwnd_, GWL_EXSTYLE);
            if (enabled) {
                ex_style &= ~static_cast<LONG_PTR>(WS_EX_TRANSPARENT);
            } else {
                ex_style |= static_cast<LONG_PTR>(WS_EX_TRANSPARENT);
            }
            SetWindowLongPtrW(hwnd_, GWL_EXSTYLE, ex_style);
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
                // Recreate the device/swapchain on the render thread so the
                // existing HWND and latest blips survive the transition.
                display_change_pending_ = false;
                release_graphics();
                if (!init_graphics()) {
                    // A projection switch can leave the adapter unavailable
                    // briefly. Keep the HWND alive and retry instead of
                    // permanently blanking the overlay after one transient
                    // initialization failure.
                    graphics_retry_at_ = now + 200;
                } else {
                    graphics_retry_at_ = 0;
                }
                dirty_ = true;
            }
            if (!blips_.empty() && now >= next_decay_) {
                decay_blips();
                next_decay_ = blips_.empty() ? 0 : now + 30;
            }
            if (dirty_ || (!blips_.empty() && now >= next_present_)) {
                render();
                next_present_ = now + 30;
                dirty_ = false;
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
        if (audio->generation > generation_) {
            generation_ = audio->generation;
            blips_.clear();
            next_decay_ = 0;
        }
        float life = std::min(1.0f, audio->intensity * kVisualGain);
        bool merged = false;
        for (auto &blip : blips_) {
            if (angle_diff(blip.angle, audio->angle) < 20.0f) {
                blip.life = std::max(blip.life, life);
                merged = true;
                break;
            }
        }
        if (!merged) {
            blips_.push_back(Blip{audio->angle, life});
            if (blips_.size() > kMaxBlips)
                blips_.erase(blips_.begin(), blips_.begin() + (blips_.size() - kMaxBlips));
        }
        dirty_ = true;
        // Decay is an independent 30 ms clock. Do not postpone it on every
        // audio packet: changing directions must let older blips fade while
        // new packets continue to arrive.
        if (next_decay_ == 0)
            next_decay_ = GetTickCount64() + 30;
    }

    void decay_blips() {
        for (auto &blip : blips_)
            blip.life -= kDecay;
        blips_.erase(std::remove_if(blips_.begin(), blips_.end(),
                                    [](const Blip &b) { return b.life <= 0.0f; }),
                     blips_.end());
        // Even when the final blip expires, a transparent frame must be
        // submitted to clear the previous composition surface.  Leaving
        // dirty_ false here makes the last arc remain visible indefinitely.
        dirty_ = true;
        if (blips_.empty())
            next_decay_ = 0;
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
        ComPtr<IDXGIAdapter> adapter;
        if (FAILED(dxgi_device_->GetAdapter(&adapter)))
            return false;
        if (FAILED(adapter->GetParent(IID_PPV_ARGS(&dxgi_factory_))))
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
        return recreate_swapchain();
    }

    bool recreate_swapchain() {
        if (!dxgi_factory_)
            return false;
        d2d_context_->SetTarget(nullptr);
        target_bitmap_.Reset();
        swapchain_.Reset();
        DXGI_SWAP_CHAIN_DESC1 desc{};
        desc.Width = static_cast<UINT>(width_);
        desc.Height = static_cast<UINT>(height_);
        desc.Format = DXGI_FORMAT_B8G8R8A8_UNORM;
        desc.SampleDesc.Count = 1;
        desc.BufferUsage = DXGI_USAGE_RENDER_TARGET_OUTPUT;
        desc.BufferCount = 2;
        desc.SwapEffect = DXGI_SWAP_EFFECT_FLIP_SEQUENTIAL;
        desc.AlphaMode = DXGI_ALPHA_MODE_PREMULTIPLIED;
        HRESULT hr = dxgi_factory_->CreateSwapChainForComposition(d3d_device_.Get(), &desc, nullptr,
                                                                  &swapchain_);
        if (FAILED(hr))
            return false;
        // Keep the overlay in the same SDR/P709 space as the legacy QWidget
        // surface. Without an explicit color space, an HDR desktop can apply a
        // different SDR white level and make otherwise identical alpha strokes
        // appear noticeably dimmer.
        ComPtr<IDXGISwapChain3> swapchain3;
        if (SUCCEEDED(swapchain_.As(&swapchain3))) {
            swapchain3->SetColorSpace1(DXGI_COLOR_SPACE_RGB_FULL_G22_NONE_P709);
        }
        if (FAILED(dcomp_visual_->SetContent(swapchain_.Get())))
            return false;
        if (FAILED(dcomp_device_->Commit()))
            return false;
        ComPtr<IDXGISurface> surface;
        if (FAILED(swapchain_->GetBuffer(0, IID_PPV_ARGS(&surface))))
            return false;
        D2D1_BITMAP_PROPERTIES1 props = D2D1::BitmapProperties1(
            D2D1_BITMAP_OPTIONS_TARGET | D2D1_BITMAP_OPTIONS_CANNOT_DRAW,
            D2D1::PixelFormat(DXGI_FORMAT_B8G8R8A8_UNORM, D2D1_ALPHA_MODE_PREMULTIPLIED));
        if (FAILED(
                d2d_context_->CreateBitmapFromDxgiSurface(surface.Get(), &props, &target_bitmap_)))
            return false;
        d2d_context_->SetTarget(target_bitmap_.Get());
        recreate_target_ = false;
        return true;
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
        const float cx = width_ * 0.5f;
        const float cy = height_ * 0.5f;
        const float radius = std::min(width_, height_) * 0.4f;
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

    void render() {
        if (recreate_target_ && !recreate_swapchain())
            return;
        if (!d2d_context_)
            return;
        d2d_context_->BeginDraw();
        // Match QPainter's default SourceOver composition explicitly. This is
        // important for a premultiplied target: replacing it with COPY would
        // make translucent strokes look either washed out or fully opaque.
        d2d_context_->SetPrimitiveBlend(D2D1_PRIMITIVE_BLEND_SOURCE_OVER);
        d2d_context_->Clear(D2D1::ColorF(0, 0.0f));
        const float cx = width_ * 0.5f;
        const float cy = height_ * 0.5f;
        const float radius = std::min(width_, height_) * 0.4f;
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
        auto color = D2D1::ColorF((color_ >> 24 & 0xff) / 255.0f, (color_ >> 16 & 0xff) / 255.0f,
                                  (color_ >> 8 & 0xff) / 255.0f, 1.0f);
        d2d_context_->CreateSolidColorBrush(color, &brush);
        ComPtr<ID2D1SolidColorBrush> base;
        d2d_context_->CreateSolidColorBrush(D2D1::ColorF(1, 1, 1, 30.0f / 255.0f), &base);
        d2d_context_->DrawEllipse(D2D1::Ellipse(D2D1::Point2F(cx, cy), radius, radius), base.Get(),
                                  2.0f);
        for (auto &blip : blips_) {
            // QWidget used QColor(alpha=int(life * 255)); quantize the D2D
            // opacity the same way so the fade curve and short-blip brightness
            // remain visually identical at each 30 ms tick.
            float opacity = std::floor(std::max(0.0f, std::min(1.0f, blip.life)) * 255.0f) / 255.0f;
            brush->SetOpacity(opacity);
            if (!ensure_blip_geometry(blip))
                continue;
            d2d_context_->DrawGeometry(blip.geometry.Get(), brush.Get(), stroke_width_,
                                       round_stroke_.Get());
        }
        if (drag_enabled_) {
            ComPtr<ID2D1SolidColorBrush> setup;
            d2d_context_->CreateSolidColorBrush(D2D1::ColorF(1, 1, 1, 120.0f / 255.0f), &setup);
            d2d_context_->DrawEllipse(
                D2D1::Ellipse(D2D1::Point2F(cx, cy), radius + 8.0f, radius + 8.0f), setup.Get(),
                1.0f, dash_stroke_.Get());
        }
        const HRESULT draw_hr = d2d_context_->EndDraw();
        if (draw_hr == D2DERR_RECREATE_TARGET) {
            // The compositor can invalidate the target during a display/DPI
            // change or a graphics-driver reset.  Recreate it on the next
            // render tick instead of leaving the overlay stuck on its last
            // frame.
            target_bitmap_.Reset();
            recreate_target_ = true;
            dirty_ = true;
            return;
        }
        if (FAILED(draw_hr)) {
            dirty_ = true;
            return;
        }
        // The render loop already limits submissions to dirty changes and a
        // 30 ms cadence. Avoid waiting for the monitor vblank here: a sync
        // interval of 1 makes the native thread block behind DWM when the GPU
        // is busy, which is exactly the contention this backend is intended to
        // avoid.
        const HRESULT present_hr = swapchain_->Present(0, 0);
        if (FAILED(present_hr)) {
            recover_from_graphics_error(present_hr);
            dirty_ = true;
            return;
        }
        const HRESULT commit_hr = dcomp_device_->Commit();
        if (FAILED(commit_hr)) {
            recover_from_graphics_error(commit_hr);
            dirty_ = true;
        }
    }

    void recover_from_graphics_error(HRESULT hr) {
        if (hr == DXGI_ERROR_DEVICE_REMOVED || hr == DXGI_ERROR_DEVICE_RESET) {
            release_graphics();
            if (!init_graphics()) {
                graphics_retry_at_ = GetTickCount64() + 200;
                return;
            }
            graphics_retry_at_ = 0;
            return;
        }
        target_bitmap_.Reset();
        recreate_target_ = true;
    }

    void release_graphics() {
        if (d2d_context_)
            d2d_context_->SetTarget(nullptr);
        for (auto &blip : blips_)
            blip.geometry.Reset();
        target_bitmap_.Reset();
        swapchain_.Reset();
        round_stroke_.Reset();
        dash_stroke_.Reset();
        dcomp_visual_.Reset();
        dcomp_target_.Reset();
        dcomp_device_.Reset();
        d2d_context_.Reset();
        d2d_device_.Reset();
        d2d_factory_.Reset();
        dxgi_factory_.Reset();
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
    uint64_t graphics_retry_at_ = 0;
    uint64_t generation_ = 0;
    uint32_t color_ = 0x9751F2FF;
    float stroke_width_ = 6.0f;
    uint64_t next_decay_ = 0;
    uint64_t next_present_ = 0;
    std::vector<Blip> blips_;

    ComPtr<ID3D11Device> d3d_device_;
    ComPtr<ID3D11DeviceContext> d3d_context_;
    ComPtr<IDXGIDevice> dxgi_device_;
    ComPtr<IDXGIFactory2> dxgi_factory_;
    ComPtr<IDXGISwapChain1> swapchain_;
    ComPtr<ID2D1Factory1> d2d_factory_;
    ComPtr<ID2D1Device> d2d_device_;
    ComPtr<ID2D1DeviceContext> d2d_context_;
    ComPtr<ID2D1Bitmap1> target_bitmap_;
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
