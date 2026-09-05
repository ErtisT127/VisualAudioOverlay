#pragma once

#include <stdint.h>

#if defined(_WIN32)
#if defined(OVERLAY_NATIVE_BUILD)
#define VAO_API __declspec(dllexport)
#else
#define VAO_API __declspec(dllimport)
#endif
#else
#define VAO_API
#endif

#ifdef __cplusplus
extern "C" {
#endif

typedef struct VaoEvent {
    uint32_t type;
    int32_t x;
    int32_t y;
    uint64_t sequence;
} VaoEvent;

enum {
    VAO_EVENT_NONE = 0,
    VAO_EVENT_POSITION_PREVIEW = 1,
    VAO_EVENT_POSITION_COMMITTED = 2,
    VAO_EVENT_ERROR = 3,
};

VAO_API void *vao_create(void);
VAO_API void vao_destroy(void *handle);
VAO_API int vao_show(void *handle);
VAO_API int vao_hide(void *handle);
VAO_API int vao_set_geometry(void *handle, int32_t x, int32_t y, int32_t width, int32_t height);
VAO_API int vao_set_drag_enabled(void *handle, int enabled);
VAO_API int vao_set_generation(void *handle, uint64_t generation);
VAO_API int vao_set_style(void *handle, uint32_t color_rgba, float stroke_width);
VAO_API int vao_submit_audio(void *handle, uint64_t generation, float angle, float intensity,
                             int64_t timestamp_ns);
VAO_API int vao_poll_event(void *handle, VaoEvent *event_out);

/* Short aliases keep the C ABI convenient for ctypes users. */
VAO_API void *create(void);
VAO_API void destroy(void *handle);
VAO_API int show(void *handle);
VAO_API int hide(void *handle);
VAO_API int set_geometry(void *handle, int32_t x, int32_t y, int32_t width, int32_t height);
VAO_API int set_drag_enabled(void *handle, int enabled);
VAO_API int set_generation(void *handle, uint64_t generation);
VAO_API int set_style(void *handle, uint32_t color_rgba, float stroke_width);
VAO_API int submit_audio(void *handle, uint64_t generation, float angle, float intensity,
                         int64_t timestamp_ns);
VAO_API int poll_event(void *handle, VaoEvent *event_out);

#ifdef __cplusplus
}
#endif
