/* Public Xlib ABI probe; no VOICEPEAK program, voice or license is included. */
#include <dlfcn.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

typedef struct _XDisplay Display;
typedef unsigned long Window;
typedef unsigned long Atom;
typedef struct {
    int type;
    Display *display;
    unsigned long resourceid;
    unsigned long serial;
    unsigned char error_code;
    unsigned char request_code;
    unsigned char minor_code;
} XErrorEvent;
typedef int (*XErrorHandler)(Display *, XErrorEvent *);

enum { X_SUCCESS = 0, X_BAD_ATOM = 5, X_PROP_REPLACE = 0,
       X_CARDINAL = 6, X_STRING = 31 };

struct XApi {
    int (*init_threads)(void);
    Display *(*open_display)(const char *);
    Window (*default_root)(Display *);
    Window (*create_window)(Display *, Window, int, int, unsigned int,
                            unsigned int, unsigned int, unsigned long, unsigned long);
    Atom (*intern_atom)(Display *, const char *, int);
    int (*change_property)(Display *, Window, Atom, Atom, int, int,
                           const unsigned char *, int);
    int (*get_property)(Display *, Window, Atom, long, long, int, Atom,
                        Atom *, int *, unsigned long *, unsigned long *,
                        unsigned char **);
    int (*free_data)(void *);
    int (*sync)(Display *, int);
    XErrorHandler (*set_error_handler)(XErrorHandler);
    int (*destroy_window)(Display *, Window);
    int (*close_display)(Display *);
};

static Display *expected_display;
static unsigned int error_count;
static unsigned int bad_atom_count;
static unsigned int callback_mismatch;

static int on_x_error(Display *display, XErrorEvent *event)
{
    ++error_count;
    if (event == NULL || display != expected_display
        || event->display != expected_display) {
        ++callback_mismatch;
        puts("callback: invalid display or event");
        return 0;
    }
    if (event->error_code == X_BAD_ATOM)
        ++bad_atom_count;
    printf("callback: display=%p error=%u request=%u minor=%u resource=%lu\n",
           (void *)display, (unsigned int)event->error_code,
           (unsigned int)event->request_code, (unsigned int)event->minor_code,
           event->resourceid);
    return 0;
}

#define LOAD(field, symbol_name) do { \
    void *address; \
    const char *problem; \
    dlerror(); \
    address = dlsym(library, symbol_name); \
    problem = dlerror(); \
    if (problem != NULL || address == NULL || sizeof api.field != sizeof address) { \
        fprintf(stderr, "load failed: %s: %s\n", symbol_name, \
                problem != NULL ? problem : "invalid symbol pointer"); \
        goto cleanup; \
    } \
    memcpy(&api.field, &address, sizeof address); \
    printf("symbol: %s=%p\n", symbol_name, address); \
} while (0)

int main(void)
{
    struct XApi api = {0};
    void *library = NULL;
    Display *display = NULL;
    Window window = 0;
    XErrorHandler previous_handler = NULL;
    unsigned char *data = NULL;
    Atom property, absent, actual_type;
    unsigned long items, remaining;
    int format, result;
    int handler_installed = 0;
    int status = EXIT_FAILURE;
    const unsigned char payload[] = "voicepeak-x11-probe";
    const unsigned long cardinals[] = {0, 0, 1280, 720};
    const char *display_name = getenv("DISPLAY");

    setvbuf(stdout, NULL, _IONBF, 0);
    setvbuf(stderr, NULL, _IONBF, 0);
    printf("stage: load DISPLAY=%s pointer_bits=%zu\n",
           display_name != NULL ? display_name : "<unset>", sizeof(void *) * 8);
    if (display_name == NULL || display_name[0] == '\0') {
        fputs("DISPLAY is required\n", stderr);
        return EXIT_FAILURE;
    }
    library = dlopen("libX11.so.6", RTLD_NOW | RTLD_GLOBAL);
    if (library == NULL) {
        fprintf(stderr, "dlopen failed: %s\n", dlerror());
        return EXIT_FAILURE;
    }
    LOAD(init_threads, "XInitThreads");
    LOAD(open_display, "XOpenDisplay");
    LOAD(default_root, "XDefaultRootWindow");
    LOAD(create_window, "XCreateSimpleWindow");
    LOAD(intern_atom, "XInternAtom");
    LOAD(change_property, "XChangeProperty");
    LOAD(get_property, "XGetWindowProperty");
    LOAD(free_data, "XFree");
    LOAD(sync, "XSync");
    LOAD(set_error_handler, "XSetErrorHandler");
    LOAD(destroy_window, "XDestroyWindow");
    LOAD(close_display, "XCloseDisplay");

    puts("stage: XInitThreads");
    if (!api.init_threads()) {
        fputs("XInitThreads failed\n", stderr);
        goto cleanup;
    }
    puts("stage: XOpenDisplay");
    display = api.open_display(display_name);
    printf("display: %p\n", (void *)display);
    if (display == NULL) {
        fputs("XOpenDisplay returned NULL\n", stderr);
        goto cleanup;
    }
    expected_display = display;
    previous_handler = api.set_error_handler(on_x_error);
    handler_installed = 1;

    puts("stage: own hidden window");
    {
        Window root = api.default_root(display);
        printf("root: %lu\n", root);
        if (root == 0)
            goto cleanup;
        window = api.create_window(display, root, 0, 0, 1, 1, 0, 0, 0);
    }
    printf("window: %lu\n", window);
    if (window == 0)
        goto cleanup;
    property = api.intern_atom(display, "_VOICEPEAK_X11_PROBE_VALUE", 0);
    absent = api.intern_atom(display, "_VOICEPEAK_X11_PROBE_ABSENT", 0);
    if (property == 0 || absent == 0)
        goto cleanup;

    puts("stage: valid 8-bit property");
    api.change_property(display, window, property, X_STRING, 8, X_PROP_REPLACE,
                        payload, (int)(sizeof payload - 1));
    api.sync(display, 0);
    actual_type = 0;
    format = 0;
    items = remaining = 0;
    result = api.get_property(display, window, property, 0, 1024, 0, X_STRING,
                              &actual_type, &format, &items, &remaining, &data);
    printf("valid: rc=%d type=%lu format=%d items=%lu remaining=%lu data=%p\n",
           result, actual_type, format, items, remaining, (void *)data);
    if (result != X_SUCCESS || actual_type != X_STRING || format != 8
        || items != sizeof payload - 1 || remaining != 0 || data == NULL
        || memcmp(data, payload, sizeof payload - 1) != 0 || error_count != 0)
        goto cleanup;
    api.free_data(data);
    data = NULL;

    puts("stage: valid 32-bit CARDINAL property");
    api.change_property(display, window, property, X_CARDINAL, 32, X_PROP_REPLACE,
                        (const unsigned char *)cardinals, 4);
    api.sync(display, 0);
    actual_type = 0;
    format = 0;
    items = remaining = 0;
    result = api.get_property(display, window, property, 0, 4, 0, X_CARDINAL,
                              &actual_type, &format, &items, &remaining, &data);
    printf("cardinal: rc=%d type=%lu format=%d items=%lu remaining=%lu data=%p\n",
           result, actual_type, format, items, remaining, (void *)data);
    if (result != X_SUCCESS || actual_type != X_CARDINAL || format != 32
        || items != 4 || remaining != 0 || data == NULL
        || memcmp(data, cardinals, sizeof cardinals) != 0 || error_count != 0)
        goto cleanup;
    api.free_data(data);
    data = NULL;

    puts("stage: missing property");
    actual_type = 0;
    format = 0;
    items = remaining = 0;
    result = api.get_property(display, window, absent, 0, 1024, 0, 0,
                              &actual_type, &format, &items, &remaining, &data);
    printf("missing: rc=%d type=%lu format=%d items=%lu remaining=%lu data=%p\n",
           result, actual_type, format, items, remaining, (void *)data);
    if (result != X_SUCCESS || actual_type != 0 || format != 0 || items != 0
        || remaining != 0 || error_count != 0)
        goto cleanup;
    if (data != NULL) {
        api.free_data(data);
        data = NULL;
    }

    puts("stage: None atom error callback");
    actual_type = 0;
    format = 0;
    items = remaining = 0;
    result = api.get_property(display, window, 0, 0, 1024, 0, 0,
                              &actual_type, &format, &items, &remaining, &data);
    api.sync(display, 0);
    printf("invalid: rc=%d errors=%u bad_atom=%u mismatch=%u\n",
           result, error_count, bad_atom_count, callback_mismatch);
    if (result == X_SUCCESS || error_count != 1 || bad_atom_count != 1
        || callback_mismatch != 0)
        goto cleanup;
    status = EXIT_SUCCESS;

cleanup:
    puts("stage: cleanup");
    if (data != NULL && api.free_data != NULL)
        api.free_data(data);
    if (display != NULL) {
        if (window != 0)
            api.destroy_window(display, window);
        api.sync(display, 0);
        if (status == EXIT_SUCCESS
            && (error_count != 1 || bad_atom_count != 1 || callback_mismatch != 0))
            status = EXIT_FAILURE;
        if (handler_installed)
            api.set_error_handler(previous_handler);
        api.close_display(display);
    }
    if (library != NULL)
        dlclose(library);
    if (status == EXIT_SUCCESS)
        puts("VOICEPEAK_X11_PROBE_OK");
    else
        fputs("VOICEPEAK_X11_PROBE_FAILED\n", stderr);
    return status;
}
