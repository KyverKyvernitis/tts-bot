/* Public Xlib ABI probe; no VOICEPEAK program, voice or license is included. */
#include <dlfcn.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

typedef struct _XDisplay Display;
typedef unsigned long Window;
typedef unsigned long Atom;
typedef struct _ProbeVisual Visual;
typedef struct _ProbeXImage XImage;
struct _ProbeXImage {
    int width, height, xoffset, format;
    char *data;
    int byte_order, bitmap_unit, bitmap_bit_order, bitmap_pad;
    int depth, bytes_per_line, bits_per_pixel;
    unsigned long red_mask, green_mask, blue_mask;
    void *obdata;
    struct {
        XImage *(*create_image)(Display *, Visual *, unsigned int, int, int,
                               char *, unsigned int, unsigned int, int, int);
        int (*destroy_image)(XImage *);
        unsigned long (*get_pixel)(XImage *, int, int);
        int (*put_pixel)(XImage *, int, int, unsigned long);
        XImage *(*sub_image)(XImage *, int, int, unsigned int, unsigned int);
        int (*add_pixel)(XImage *, long);
    } f;
};
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
       X_CARDINAL = 6, X_STRING = 31, X_ZPIXMAP = 2 };

/* Mandatory names from JUCE 7.0.12 X11Symbols::loadAllSymbols(). */
static const char *const mandatory_symbols[] = {
    "XAllocClassHint",
    "XAllocSizeHints",
    "XAllocWMHints",
    "XBitmapBitOrder",
    "XBitmapUnit",
    "XChangeActivePointerGrab",
    "XChangeProperty",
    "XCheckTypedWindowEvent",
    "XCheckWindowEvent",
    "XClearArea",
    "XCloseDisplay",
    "XConnectionNumber",
    "XConvertSelection",
    "XCreateColormap",
    "XCreateFontCursor",
    "XCreateGC",
    "XCreateImage",
    "XCreatePixmap",
    "XCreatePixmapCursor",
    "XCreatePixmapFromBitmapData",
    "XCreateWindow",
    "XDefaultRootWindow",
    "XDefaultScreen",
    "XDefaultScreenOfDisplay",
    "XDefaultVisual",
    "XDefineCursor",
    "XDeleteContext",
    "XDeleteProperty",
    "XDestroyImage",
    "XDestroyWindow",
    "XDisplayHeight",
    "XDisplayHeightMM",
    "XDisplayWidth",
    "XDisplayWidthMM",
    "XEventsQueued",
    "XFindContext",
    "XFlush",
    "XFree",
    "XFreeCursor",
    "XFreeColormap",
    "XFreeGC",
    "XFreeModifiermap",
    "XFreePixmap",
    "XGetAtomName",
    "XGetErrorDatabaseText",
    "XGetErrorText",
    "XGetGeometry",
    "XGetImage",
    "XGetInputFocus",
    "XGetModifierMapping",
    "XGetPointerMapping",
    "XGetSelectionOwner",
    "XGetVisualInfo",
    "XGetWMHints",
    "XGetWindowAttributes",
    "XGetWindowProperty",
    "XGrabPointer",
    "XGrabServer",
    "XImageByteOrder",
    "XInitImage",
    "XInitThreads",
    "XInstallColormap",
    "XInternAtom",
    "XkbKeycodeToKeysym",
    "XKeysymToKeycode",
    "XListProperties",
    "XLockDisplay",
    "XLookupString",
    "XMapRaised",
    "XMapWindow",
    "XMoveResizeWindow",
    "XNextEvent",
    "XOpenDisplay",
    "XPeekEvent",
    "XPending",
    "XPutImage",
    "XPutPixel",
    "XQueryBestCursor",
    "XQueryExtension",
    "XQueryPointer",
    "XQueryTree",
    "XRefreshKeyboardMapping",
    "XReparentWindow",
    "XResizeWindow",
    "XRestackWindows",
    "XRootWindow",
    "XSaveContext",
    "XScreenCount",
    "XScreenNumberOfScreen",
    "XSelectInput",
    "XSendEvent",
    "XSetClassHint",
    "XSetErrorHandler",
    "XSetIOErrorHandler",
    "XSetInputFocus",
    "XSetSelectionOwner",
    "XSetWMHints",
    "XSetWMIconName",
    "XSetWMName",
    "XSetWMNormalHints",
    "XStringListToTextProperty",
    "XSync",
    "XSynchronize",
    "XTranslateCoordinates",
    "XrmUniqueQuark",
    "XUngrabPointer",
    "XUngrabServer",
    "XUnlockDisplay",
    "XUnmapWindow",
    "Xutf8TextListToTextProperty",
    "XWarpPointer",
};

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
    int (*default_screen)(Display *);
    Visual *(*default_visual)(Display *, int);
    int (*default_depth)(Display *, int);
    XImage *(*create_image)(Display *, Visual *, unsigned int, int, int,
                           char *, unsigned int, unsigned int, int, int);
    int (*init_image)(XImage *);
    int (*put_pixel)(XImage *, int, int, unsigned long);
    int (*destroy_image)(XImage *);
};

static Display *expected_display;
static unsigned int error_count;
static unsigned int bad_atom_count;
static unsigned int callback_mismatch;

static int check_mandatory_symbols(void *library, void *extension)
{
    unsigned int missing = 0;
    size_t count = sizeof mandatory_symbols / sizeof mandatory_symbols[0];
    puts("stage: mandatory JUCE symbols");
    for (size_t i = 0; i < count; ++i) {
        const char *name = mandatory_symbols[i];
        void *address = dlsym(library, name);
        if (address == NULL)
            address = dlsym(extension, name);
        if (address == NULL) {
            ++missing;
            fprintf(stderr, "missing mandatory symbol: %s\n", name);
        } else {
            printf("required: %s=%p\n", name, address);
        }
    }
    printf("mandatory: checked=%zu missing=%u\n", count, missing);
    return missing == 0;
}

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

int main(int argc, char **argv)
{
    struct XApi api = {0};
    void *library = NULL;
    void *extension = NULL;
    Display *display = NULL;
    XImage *image = NULL;
    Window window = 0;
    XErrorHandler previous_handler = NULL;
    unsigned char *data = NULL;
    Atom property, absent, actual_type;
    unsigned long items, remaining;
    int format, result;
    int handler_installed = 0;
    int status = EXIT_FAILURE;
    int symbols_only = argc == 2 && strcmp(argv[1], "--symbols-only") == 0;
    const unsigned char payload[] = "voicepeak-x11-probe";
    const unsigned long cardinals[] = {0, 0, 1280, 720};
    const char *display_name = getenv("DISPLAY");

    setvbuf(stdout, NULL, _IONBF, 0);
    setvbuf(stderr, NULL, _IONBF, 0);
    if (argc != 1 && !symbols_only) {
        fputs("usage: x11-probe-x86_64 [--symbols-only]\n", stderr);
        return EXIT_FAILURE;
    }
    printf("stage: load DISPLAY=%s pointer_bits=%zu\n",
           display_name != NULL ? display_name : "<unset>", sizeof(void *) * 8);
    library = dlopen("libX11.so.6", RTLD_NOW | RTLD_GLOBAL);
    if (library == NULL) {
        fprintf(stderr, "dlopen failed: %s\n", dlerror());
        return EXIT_FAILURE;
    }
    extension = dlopen("libXext.so.6", RTLD_NOW | RTLD_GLOBAL);
    if (extension == NULL) {
        fprintf(stderr, "libXext dlopen failed: %s\n", dlerror());
        goto cleanup;
    }
    if (!check_mandatory_symbols(library, extension))
        goto cleanup;
    if (symbols_only) {
        status = EXIT_SUCCESS;
        goto cleanup;
    }
    if (display_name == NULL || display_name[0] == '\0') {
        fputs("DISPLAY is required\n", stderr);
        goto cleanup;
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
    LOAD(default_screen, "XDefaultScreen");
    LOAD(default_visual, "XDefaultVisual");
    LOAD(default_depth, "XDefaultDepth");
    LOAD(create_image, "XCreateImage");
    LOAD(init_image, "XInitImage");
    LOAD(put_pixel, "XPutPixel");
    LOAD(destroy_image, "XDestroyImage");

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

    puts("stage: XImage create and init");
    {
        int screen = api.default_screen(display);
        int depth = api.default_depth(display, screen);
        Visual *visual = api.default_visual(display, screen);
        unsigned long mask, first, second;
        size_t image_bytes;
        if (visual == NULL || depth < 1 || depth > 32)
            goto cleanup;
        image = api.create_image(display, visual, (unsigned int)depth,
                                  X_ZPIXMAP, 0, NULL, 2, 2, 32, 0);
        if (image == NULL)
            goto cleanup;
        printf("image: %p depth=%d bpp=%d stride=%d\n", (void *)image,
               image->depth, image->bits_per_pixel, image->bytes_per_line);
        if (image->width != 2 || image->height != 2 || image->data != NULL
            || image->bytes_per_line <= 0 || image->bytes_per_line > 1048576)
            goto cleanup;
        image_bytes = (size_t)image->bytes_per_line * (size_t)image->height;
        image->data = calloc(1, image_bytes);
        if (image->data == NULL || !api.init_image(image)
            || image->f.get_pixel == NULL || image->f.put_pixel == NULL)
            goto cleanup;
        mask = (1UL << depth) - 1;
        first = 0x55aaUL & mask;
        second = first ^ mask;

        puts("stage: native XPutPixel");
        result = api.put_pixel(image, 0, 0, first);
        printf("put-pixel: rc=%d get-callback=%p put-callback=%p\n", result,
               (void *)image->f.get_pixel, (void *)image->f.put_pixel);
        if (result == 0 || image->f.get_pixel == NULL || image->f.put_pixel == NULL)
            goto cleanup;

        puts("stage: restored XImage callbacks");
        if (image->f.get_pixel(image, 0, 0) != first
            || image->f.put_pixel(image, 1, 1, second) == 0
            || image->f.get_pixel(image, 1, 1) != second
            || image->f.get_pixel(image, 0, 0) != first) {
            fputs("XImage pixel or callback mismatch\n", stderr);
            goto cleanup;
        }
        puts("image callbacks: pixel data verified");
        api.destroy_image(image);
        image = NULL;
    }
    status = EXIT_SUCCESS;

cleanup:
    puts("stage: cleanup");
    if (data != NULL && api.free_data != NULL)
        api.free_data(data);
    if (image != NULL && api.destroy_image != NULL)
        api.destroy_image(image);
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
    if (extension != NULL)
        dlclose(extension);
    if (library != NULL)
        dlclose(library);
    if (status == EXIT_SUCCESS)
        puts(symbols_only ? "VOICEPEAK_X11_SYMBOLS_OK" : "VOICEPEAK_X11_PROBE_OK");
    else
        fputs("VOICEPEAK_X11_PROBE_FAILED\n", stderr);
    return status;
}
