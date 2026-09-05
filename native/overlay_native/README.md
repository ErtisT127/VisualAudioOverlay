# Native DirectComposition overlay

This DLL owns a transparent top-level Win32 window and renders the radar with
D3D11/DirectComposition and Direct2D. It has no Qt dependency. The exported
functions use a C ABI and accept an opaque handle returned by `vao_create`.

```bash
cmake -S native/overlay_native -B native/overlay_native/build -G "MinGW Makefiles"
cmake --build native/overlay_native/build --config Release
```

The resulting `overlay_native.dll` is suitable for loading with `ctypes.WinDLL`.
The style colour is packed as `0xRRGGBBAA`; `vao_submit_audio` keeps only the
latest sample and rejects samples from an older generation. Call
`vao_set_generation` when starting a new capture session so pending samples
from the previous worker are invalidated before the mailbox is consumed.
Position drag notifications are read with `vao_poll_event`; the queue is
bounded and cannot grow with pointer or audio event rate.

In normal radar mode the HWND uses `WS_EX_LAYERED | WS_EX_TRANSPARENT` with a
constant layered alpha of 255, so its premultiplied DirectComposition pixels
remain unchanged while mouse hit testing passes through to another process.
The HWND is also disabled in normal mode as a guard against accidental focus
or capture; drag mode temporarily enables it and removes `WS_EX_TRANSPARENT`.
