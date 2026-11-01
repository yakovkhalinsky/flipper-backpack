/* Pi Command Runner — Flipper side.
 *
 * Talks to flipper-runner on a Raspberry Pi backpack over USART (pins 13 TX / 14 RX),
 * 115200 8N1, line framed. The Flipper only ever sends tool ids and validated param
 * values; it never sends command strings. Wire format: see README / PROTOCOL.md.
 */
#include <furi.h>
#include <furi_hal.h>
#include <furi_hal_serial.h>
#include <furi_hal_serial_control.h>
#include <expansion/expansion.h>
#include <gui/gui.h>
#include <gui/view_dispatcher.h>
#include <gui/modules/submenu.h>
#include <gui/modules/text_box.h>
#include <gui/modules/dialog_ex.h>
#include <gui/modules/loading.h>
#include <gui/modules/popup.h>
#include <gui/modules/widget.h>
#include <gui/modules/number_input.h>
#include <gui/modules/variable_item_list.h>
#include <stdlib.h>
#include <string.h>

#define TAG "PiRunner"

#define BAUD             115200
#define FRAME_MAX        256
#define RX_QUEUE_DEPTH   48
#define MAX_TOOLS        32
#define MAX_PARAMS       2
#define MAX_ENUM         8
#define MAX_KV           16
#define MAX_LIST         20
#define MAX_FIELDS       8
#define TEXT_CAP         4096
#define HELLO_TIMEOUT_MS 1500
#define HELLO_TRIES      3
#define LIST_TIMEOUT_MS  3000

typedef enum {
    ViewMenu,
    ViewParam,
    ViewNumber,
    ViewText,
    ViewKv,
    ViewList,
    ViewDialog,
    ViewLoading,
    ViewPopup,
    ViewRunning,
} ViewId;

typedef enum {
    EvtLine = 100,
    EvtTimeout,
    EvtPopupDone,
    EvtRetry,
    EvtExit,
    EvtConfirmYes,
    EvtConfirmNo,
    EvtRefresh,
    EvtToolBase = 1000, /* + tool index */
    EvtParamBase = 2000, /* + enum value index */
    EvtListBase = 3000, /* + list item index */
} Evt;

typedef enum {
    StHandshake,
    StListing,
    StMenu,
    StParams,
    StConfirm,
    StRunning,
    StCancelling,
    StResult,
    StOffline,
} State;

typedef struct {
    char name[13];
    bool is_enum;
    uint8_t n_values;
    char values[MAX_ENUM][17];
    int32_t min, max, def;
} Param;

typedef struct {
    char id[25];
    char label[25];
    bool confirm;
    uint16_t timeout;
    uint8_t n_params;
    Param params[MAX_PARAMS];
} Tool;

typedef struct {
    char label[33];
    char tool[25];
    char pname[13];
    char pval[25];
} ListItem;

typedef struct {
    char line[FRAME_MAX];
} RxLine;

typedef struct {
    Gui* gui;
    ViewDispatcher* vd;
    Submenu* menu;
    Submenu* param_menu;
    NumberInput* number;
    TextBox* text;
    VariableItemList* kv;
    Submenu* list;
    DialogEx* dialog;
    Loading* loading;
    Popup* popup;
    Widget* running;

    FuriHalSerialHandle* serial;
    Expansion* expansion;
    FuriThread* rx_thread;
    FuriStreamBuffer* rx_stream;
    FuriMessageQueue* rx_queue;
    FuriTimer* timer;
    volatile bool rx_run;

    State state;
    ViewId view;
    uint8_t hello_tries;

    Tool tools[MAX_TOOLS];
    uint8_t n_tools;
    uint8_t n_tools_rx;

    /* pending run */
    int8_t cur_tool;
    uint8_t cur_param;
    char param_vals[MAX_PARAMS][25];
    bool chained;

    /* result */
    char res_type[10];
    char res_title[33];
    FuriString* res_text;
    uint8_t n_kv;
    char kv_label[MAX_KV][21];
    char kv_value[MAX_KV][41];
    uint8_t n_list;
    ListItem list_items[MAX_LIST];
    char status_level[8];
    char status_msg[100];
    char exit_text[40];
    char progress_text[48];
} App;

/* ------------------------------------------------------------------ serial */

static void rx_isr(FuriHalSerialHandle* handle, FuriHalSerialRxEvent event, void* ctx) {
    App* app = ctx;
    if(event & FuriHalSerialRxEventData) {
        uint8_t b = furi_hal_serial_async_rx(handle);
        furi_stream_buffer_send(app->rx_stream, &b, 1, 0);
    }
}

/* Assemble bytes into lines; enforce FRAME_MAX and printable ASCII. */
static int32_t rx_worker(void* ctx) {
    App* app = ctx;
    RxLine cur;
    size_t len = 0;
    bool discard = false;
    while(app->rx_run) {
        uint8_t buf[64];
        size_t n = furi_stream_buffer_receive(app->rx_stream, buf, sizeof(buf), 50);
        for(size_t i = 0; i < n; i++) {
            uint8_t b = buf[i];
            if(b == '\n') {
                if(!discard && len > 0) {
                    cur.line[len] = 0;
                    if(furi_message_queue_put(app->rx_queue, &cur, 0) == FuriStatusOk) {
                        view_dispatcher_send_custom_event(app->vd, EvtLine);
                    } else {
                        FURI_LOG_W(TAG, "rx queue full, dropping frame");
                    }
                }
                len = 0;
                discard = false;
            } else if(b == '\r') {
                continue;
            } else if(discard) {
                continue;
            } else if(len >= FRAME_MAX - 1 || b < 0x20 || b > 0x7e) {
                discard = true; /* oversize or garbage (e.g. Pi boot noise): drop line */
                len = 0;
            } else {
                cur.line[len++] = (char)b;
            }
        }
    }
    return 0;
}

static void tx_line(App* app, const char* line) {
    size_t n = strlen(line);
    if(n + 1 > FRAME_MAX) return;
    furi_hal_serial_tx(app->serial, (const uint8_t*)line, n);
    furi_hal_serial_tx(app->serial, (const uint8_t*)"\n", 1);
    furi_hal_serial_tx_wait_complete(app->serial);
}

/* ------------------------------------------------------------------ helpers */

static size_t split(char* s, char** f, size_t max) {
    size_t n = 0;
    f[n++] = s;
    for(char* p = s; *p && n < max; p++) {
        if(*p == '|') {
            *p = 0;
            f[n++] = p + 1;
        }
    }
    return n;
}

static void copy(char* dst, const char* src, size_t size) {
    strncpy(dst, src, size - 1);
    dst[size - 1] = 0;
}

static bool valid_id(const char* s) {
    size_t n = strlen(s);
    if(n < 1 || n > 24) return false;
    for(size_t i = 0; i < n; i++) {
        char c = s[i];
        if(!((c >= 'a' && c <= 'z') || (c >= '0' && c <= '9') || c == '-' || c == '_'))
            return false;
    }
    return true;
}

static bool valid_val(const char* s) {
    size_t n = strlen(s);
    if(n < 1 || n > 24) return false;
    for(size_t i = 0; i < n; i++) {
        char c = s[i];
        if(!((c >= 'a' && c <= 'z') || (c >= 'A' && c <= 'Z') || (c >= '0' && c <= '9') ||
             c == '-' || c == '_' || c == '.' || c == '@'))
            return false;
    }
    return true;
}

static void switch_view(App* app, ViewId v) {
    app->view = v;
    view_dispatcher_switch_to_view(app->vd, v);
}

static void timer_arm(App* app, uint32_t ms) {
    furi_timer_start(app->timer, furi_ms_to_ticks(ms));
}

static void timer_cb(void* ctx) {
    App* app = ctx;
    view_dispatcher_send_custom_event(app->vd, EvtTimeout);
}

static void popup_cb(void* ctx) {
    App* app = ctx;
    view_dispatcher_send_custom_event(app->vd, EvtPopupDone);
}

static void show_popup(App* app, const char* header, const char* text) {
    popup_reset(app->popup);
    popup_set_context(app->popup, app);
    popup_set_callback(app->popup, popup_cb);
    popup_set_header(app->popup, header, 64, 2, AlignCenter, AlignTop);
    popup_set_text(app->popup, text, 64, 16, AlignCenter, AlignTop);
    popup_disable_timeout(app->popup);
    switch_view(app, ViewPopup);
}

/* ------------------------------------------------------------------ handshake */

static void dialog_cb(DialogExResult result, void* ctx) {
    App* app = ctx;
    if(app->state == StOffline) {
        view_dispatcher_send_custom_event(
            app->vd, result == DialogExResultRight ? EvtRetry : EvtExit);
    } else {
        view_dispatcher_send_custom_event(
            app->vd, result == DialogExResultRight ? EvtConfirmYes : EvtConfirmNo);
    }
}

static void start_handshake(App* app) {
    app->state = StHandshake;
    app->hello_tries = 0;
    switch_view(app, ViewLoading);
    tx_line(app, "HELLO 1");
    timer_arm(app, HELLO_TIMEOUT_MS);
}

static void show_offline(App* app, const char* why) {
    furi_timer_stop(app->timer);
    app->state = StOffline;
    dialog_ex_reset(app->dialog);
    dialog_ex_set_context(app->dialog, app);
    dialog_ex_set_result_callback(app->dialog, dialog_cb);
    dialog_ex_set_header(app->dialog, "Pi not responding", 64, 2, AlignCenter, AlignTop);
    dialog_ex_set_text(app->dialog, why, 64, 20, AlignCenter, AlignTop);
    dialog_ex_set_left_button_text(app->dialog, "Exit");
    dialog_ex_set_right_button_text(app->dialog, "Retry");
    switch_view(app, ViewDialog);
}

/* ------------------------------------------------------------------ menu */

static void menu_cb(void* ctx, uint32_t index) {
    App* app = ctx;
    view_dispatcher_send_custom_event(app->vd, index);
}

static void build_menu(App* app) {
    submenu_reset(app->menu);
    submenu_set_header(app->menu, "Pi tools");
    for(uint8_t i = 0; i < app->n_tools; i++) {
        submenu_add_item(app->menu, app->tools[i].label, EvtToolBase + i, menu_cb, app);
    }
    submenu_add_item(app->menu, "[Refresh]", EvtRefresh, menu_cb, app);
    app->state = StMenu;
    switch_view(app, ViewMenu);
}

static int find_tool(App* app, const char* id) {
    for(uint8_t i = 0; i < app->n_tools; i++)
        if(!strcmp(app->tools[i].id, id)) return i;
    return -1;
}

/* ------------------------------------------------------------------ run flow */

static void next_step(App* app);

static void number_cb(void* ctx, int32_t number) {
    App* app = ctx;
    Param* p = &app->tools[app->cur_tool].params[app->cur_param];
    if(number < p->min) number = p->min;
    if(number > p->max) number = p->max;
    snprintf(app->param_vals[app->cur_param], 25, "%ld", (long)number);
    app->cur_param++;
    next_step(app);
}

static void send_run(App* app) {
    Tool* t = &app->tools[app->cur_tool];
    FuriString* cmd = furi_string_alloc_printf("RUN %s", t->id);
    for(uint8_t i = 0; i < t->n_params; i++) {
        if(!app->param_vals[i][0]) continue;
        furi_string_cat_printf(cmd, " %s=%s", t->params[i].name, app->param_vals[i]);
    }
    if(t->confirm) furi_string_cat_str(cmd, " @confirm");
    tx_line(app, furi_string_get_cstr(cmd));
    furi_string_free(cmd);

    app->state = StRunning;
    furi_string_reset(app->res_text);
    app->res_type[0] = 0;
    app->n_kv = app->n_list = 0;
    widget_reset(app->running);
    widget_add_string_element(app->running, 64, 4, AlignCenter, AlignTop, FontPrimary, t->label);
    widget_add_string_element(
        app->running, 64, 26, AlignCenter, AlignTop, FontSecondary, "running...");
    widget_add_string_element(
        app->running, 64, 52, AlignCenter, AlignTop, FontSecondary, "Back = cancel");
    switch_view(app, ViewRunning);
    timer_arm(app, (uint32_t)(t->timeout + 5) * 1000);
}

/* Walk through params (enum → submenu, int → number input), then confirm, then run. */
static void next_step(App* app) {
    Tool* t = &app->tools[app->cur_tool];
    if(app->cur_param < t->n_params) {
        Param* p = &t->params[app->cur_param];
        app->state = StParams;
        if(app->param_vals[app->cur_param][0]) {
            app->cur_param++; /* pre-filled by a list chain */
            next_step(app);
            return;
        }
        if(p->is_enum) {
            submenu_reset(app->param_menu);
            submenu_set_header(app->param_menu, p->name);
            for(uint8_t i = 0; i < p->n_values; i++)
                submenu_add_item(app->param_menu, p->values[i], EvtParamBase + i, menu_cb, app);
            switch_view(app, ViewParam);
        } else {
            number_input_set_header_text(app->number, p->name);
            number_input_set_result_callback(app->number, number_cb, app, p->def, p->min, p->max);
            switch_view(app, ViewNumber);
        }
        return;
    }
    if(t->confirm) {
        app->state = StConfirm;
        dialog_ex_reset(app->dialog);
        dialog_ex_set_context(app->dialog, app);
        dialog_ex_set_result_callback(app->dialog, dialog_cb);
        dialog_ex_set_header(app->dialog, t->label, 64, 2, AlignCenter, AlignTop);
        dialog_ex_set_text(app->dialog, "Are you sure?", 64, 24, AlignCenter, AlignTop);
        dialog_ex_set_left_button_text(app->dialog, "No");
        dialog_ex_set_right_button_text(app->dialog, "Yes");
        switch_view(app, ViewDialog);
        return;
    }
    send_run(app);
}

static void start_tool(App* app, int idx, const char* pname, const char* pval) {
    if(idx < 0) {
        show_popup(app, "Error", "Tool not available");
        return;
    }
    app->cur_tool = idx;
    app->cur_param = 0;
    memset(app->param_vals, 0, sizeof(app->param_vals));
    if(pname && pname[0]) {
        Tool* t = &app->tools[idx];
        for(uint8_t i = 0; i < t->n_params; i++)
            if(!strcmp(t->params[i].name, pname) && valid_val(pval))
                copy(app->param_vals[i], pval, 25);
    }
    next_step(app);
}

/* ------------------------------------------------------------------ results */

static void render_result(App* app) {
    const char* t = app->res_type;
    if(!strcmp(t, "kv")) {
        variable_item_list_reset(app->kv);
        for(uint8_t i = 0; i < app->n_kv; i++) {
            VariableItem* it = variable_item_list_add(app->kv, app->kv_label[i], 1, NULL, NULL);
            variable_item_set_current_value_text(it, app->kv_value[i]);
        }
        VariableItem* it = variable_item_list_add(app->kv, "Exit", 1, NULL, NULL);
        variable_item_set_current_value_text(it, app->exit_text);
        switch_view(app, ViewKv);
    } else if(!strcmp(t, "list")) {
        submenu_reset(app->list);
        submenu_set_header(app->list, app->res_title[0] ? app->res_title : "Select");
        for(uint8_t i = 0; i < app->n_list; i++)
            submenu_add_item(app->list, app->list_items[i].label, EvtListBase + i, menu_cb, app);
        switch_view(app, ViewList);
    } else if(!strcmp(t, "status") || !strcmp(t, "error") || !strcmp(t, "progress")) {
        snprintf(
            app->status_msg + strlen(app->status_msg),
            sizeof(app->status_msg) - strlen(app->status_msg),
            "\n%s",
            app->exit_text);
        const char* hdr = !strcmp(app->status_level, "ok")   ? "OK" :
                          !strcmp(app->status_level, "warn") ? "Warning" :
                                                               "Error";
        show_popup(app, hdr, app->status_msg);
    } else { /* text, table, or no view at all */
        if(app->res_title[0]) furi_string_cat_printf(app->res_text, "\n");
        furi_string_cat_printf(app->res_text, "\n[%s]", app->exit_text);
        text_box_reset(app->text);
        text_box_set_font(app->text, !strcmp(t, "table") ? TextBoxFontHex : TextBoxFontText);
        text_box_set_text(app->text, furi_string_get_cstr(app->res_text));
        text_box_set_focus(app->text, TextBoxFocusStart);
        switch_view(app, ViewText);
    }
    app->state = StResult;
}

static void append_text(App* app, const char* s) {
    if(furi_string_size(app->res_text) + strlen(s) + 2 < TEXT_CAP) {
        furi_string_cat_str(app->res_text, s);
        furi_string_cat_str(app->res_text, "\n");
    }
}

static void handle_run_frame(App* app, char** f, size_t n) {
    const char* tag = f[0];
    if(!strcmp(tag, "ACK")) return;
    if(!strcmp(tag, "PG") && n >= 3) {
        int pct = atoi(f[1]);
        if(pct < 0) pct = 0;
        if(pct > 100) pct = 100;
        char bar[24];
        int fill = pct / 5;
        for(int i = 0; i < 20; i++)
            bar[i] = i < fill ? '#' : '.';
        bar[20] = 0;
        snprintf(app->progress_text, sizeof(app->progress_text), "[%s] %d%%", bar, pct);
        widget_reset(app->running);
        widget_add_string_element(
            app->running,
            64,
            4,
            AlignCenter,
            AlignTop,
            FontPrimary,
            app->tools[app->cur_tool].label);
        widget_add_string_element(
            app->running, 64, 22, AlignCenter, AlignTop, FontSecondary, f[2]);
        widget_add_string_element(
            app->running, 64, 36, AlignCenter, AlignTop, FontKeyboard, app->progress_text);
        widget_add_string_element(
            app->running, 64, 52, AlignCenter, AlignTop, FontSecondary, "Back = cancel");
        return;
    }
    if(!strcmp(tag, "BEGIN") && n >= 3) {
        copy(app->res_type, f[1], sizeof(app->res_type));
        copy(app->res_title, f[2], sizeof(app->res_title));
        furi_string_reset(app->res_text);
        if(f[2][0]) append_text(app, f[2]);
        app->n_kv = app->n_list = 0;
        copy(app->status_level, "error", sizeof(app->status_level));
        app->status_msg[0] = 0;
        return;
    }
    if(!strcmp(tag, "L") && n >= 2) {
        append_text(app, f[1]);
    } else if(!strcmp(tag, "KV") && n >= 3 && app->n_kv < MAX_KV) {
        copy(app->kv_label[app->n_kv], f[1], 21);
        copy(app->kv_value[app->n_kv], f[2], 41);
        app->n_kv++;
    } else if((!strcmp(tag, "TH") || !strcmp(tag, "TR")) && n >= 2) {
        /* fixed-width columns for the monospace font: ~21 chars per screen line */
        char row[96] = {0};
        size_t w = (n - 1) > 0 ? 21 / (n - 1) : 21;
        if(w < 4) w = 4;
        for(size_t i = 1; i < n; i++) {
            char cell[24];
            snprintf(cell, sizeof(cell), "%-*.*s", (int)w, (int)(w - 1), f[i]);
            size_t rl = strlen(row);
            snprintf(row + rl, sizeof(row) - rl, "%s", cell);
        }
        append_text(app, row);
        if(!strcmp(tag, "TH")) append_text(app, "---------------------");
    } else if(!strcmp(tag, "ST") && n >= 3) {
        copy(app->status_level, f[1], sizeof(app->status_level));
        copy(app->status_msg, f[2], sizeof(app->status_msg));
        append_text(app, f[2]);
    } else if(!strcmp(tag, "LI") && n >= 5 && app->n_list < MAX_LIST) {
        ListItem* it = &app->list_items[app->n_list++];
        copy(it->label, f[1], sizeof(it->label));
        copy(it->tool, valid_id(f[2]) ? f[2] : "", sizeof(it->tool));
        copy(it->pname, f[3], sizeof(it->pname));
        copy(it->pval, valid_val(f[4]) ? f[4] : "", sizeof(it->pval));
    } else if(!strcmp(tag, "EXIT") && n >= 2) {
        furi_timer_stop(app->timer);
        snprintf(
            app->exit_text,
            sizeof(app->exit_text),
            "exit %s%s%s",
            f[1],
            n >= 3 && strcmp(f[2], "ok") ? " " : "",
            n >= 3 && strcmp(f[2], "ok") ? f[2] : "");
        if(!app->res_type[0]) {
            copy(app->res_type, "status", sizeof(app->res_type));
            copy(app->status_level, atoi(f[1]) == 0 ? "ok" : "error", sizeof(app->status_level));
            app->status_msg[0] = 0;
        }
        render_result(app);
    } else if(!strcmp(tag, "ERR")) {
        furi_timer_stop(app->timer);
        app->state = StResult;
        show_popup(app, "Pi refused", n >= 3 ? f[2] : f[1]);
    }
}

static void handle_line(App* app, char* line) {
    char* f[MAX_FIELDS];
    size_t n = split(line, f, MAX_FIELDS);
    switch(app->state) {
    case StHandshake:
        if(!strcmp(f[0], "HELLO") && n >= 2 && !strcmp(f[1], "1")) {
            app->state = StListing;
            app->n_tools_rx = 0;
            tx_line(app, "LIST");
            timer_arm(app, LIST_TIMEOUT_MS);
        } else if(!strcmp(f[0], "ERR") && n >= 2 && !strcmp(f[1], "bad_version")) {
            show_offline(app, "Protocol version mismatch.\nUpdate Pi or app.");
        }
        break;
    case StListing:
        if(!strcmp(f[0], "T") && n >= 7 && app->n_tools_rx < MAX_TOOLS && valid_id(f[1])) {
            Tool* t = &app->tools[app->n_tools_rx++];
            memset(t, 0, sizeof(*t));
            copy(t->id, f[1], sizeof(t->id));
            copy(t->label, f[2], sizeof(t->label));
            t->confirm = f[3][0] == '1';
            t->timeout = (uint16_t)atoi(f[4]);
            if(t->timeout < 1 || t->timeout > 300) t->timeout = 300;
        } else if(!strcmp(f[0], "P") && n >= 5 && app->n_tools_rx > 0) {
            Tool* t = &app->tools[app->n_tools_rx - 1];
            if(strcmp(t->id, f[1]) || t->n_params >= MAX_PARAMS) break;
            Param* p = &t->params[t->n_params];
            memset(p, 0, sizeof(*p));
            copy(p->name, f[2], sizeof(p->name));
            if(!strcmp(f[3], "enum")) {
                p->is_enum = true;
                char* s = f[4];
                while(s && *s && p->n_values < MAX_ENUM) {
                    char* c = strchr(s, ',');
                    if(c) *c = 0;
                    if(valid_val(s)) copy(p->values[p->n_values++], s, 17);
                    s = c ? c + 1 : NULL;
                }
                if(p->n_values == 0) break;
            } else if(!strcmp(f[3], "int") && n >= 7) {
                p->min = atoi(f[4]);
                p->max = atoi(f[5]);
                p->def = atoi(f[6]);
                if(p->min > p->max) break;
            } else {
                break; /* unknown param type: skip param, tool will be refused by Pi */
            }
            t->n_params++;
        } else if(!strcmp(f[0], "END")) {
            furi_timer_stop(app->timer);
            app->n_tools = app->n_tools_rx;
            build_menu(app);
        }
        break;
    case StRunning:
        handle_run_frame(app, f, n);
        break;
    case StCancelling:
        if(!strcmp(f[0], "EXIT") || !strcmp(f[0], "ERR")) {
            furi_timer_stop(app->timer);
            build_menu(app);
        }
        break;
    default:
        break; /* stray frames are ignored */
    }
}

/* ------------------------------------------------------------------ events */

static bool custom_cb(void* ctx, uint32_t event) {
    App* app = ctx;
    if(event == EvtLine) {
        RxLine l;
        while(furi_message_queue_get(app->rx_queue, &l, 0) == FuriStatusOk)
            handle_line(app, l.line);
    } else if(event == EvtTimeout) {
        if(app->state == StHandshake) {
            if(++app->hello_tries < HELLO_TRIES) {
                tx_line(app, "HELLO 1");
                timer_arm(app, HELLO_TIMEOUT_MS);
            } else {
                show_offline(app, "Check it is booted and\nflipper-runner is running.");
            }
        } else if(app->state == StListing) {
            show_offline(app, "Tool list timed out.");
        } else if(app->state == StRunning) {
            tx_line(app, "CANCEL");
            app->state = StResult;
            show_popup(app, "No reply", "Pi stopped answering.\nCancel sent.");
        } else if(app->state == StCancelling) {
            build_menu(app);
        }
    } else if(event == EvtRetry || event == EvtRefresh) {
        start_handshake(app);
    } else if(event == EvtExit) {
        view_dispatcher_stop(app->vd);
    } else if(event == EvtConfirmYes) {
        send_run(app);
    } else if(event == EvtConfirmNo) {
        build_menu(app);
    } else if(event == EvtPopupDone) {
        if(app->state == StOffline) return true;
        build_menu(app);
    } else if(event >= EvtListBase && event < EvtListBase + MAX_LIST) {
        ListItem* it = &app->list_items[event - EvtListBase];
        if(it->tool[0]) start_tool(app, find_tool(app, it->tool), it->pname, it->pval);
    } else if(event >= EvtParamBase && event < EvtParamBase + MAX_ENUM) {
        Param* p = &app->tools[app->cur_tool].params[app->cur_param];
        uint32_t i = event - EvtParamBase;
        if(i < p->n_values) {
            copy(app->param_vals[app->cur_param], p->values[i], 25);
            app->cur_param++;
            next_step(app);
        }
    } else if(event >= EvtToolBase && event < EvtToolBase + MAX_TOOLS) {
        start_tool(app, event - EvtToolBase, NULL, NULL);
    }
    return true;
}

static bool back_cb(void* ctx) {
    App* app = ctx;
    switch(app->view) {
    case ViewMenu:
    case ViewDialog:
        if(app->state == StConfirm) {
            build_menu(app);
            return true;
        }
        return false; /* exit app */
    case ViewRunning:
        tx_line(app, "CANCEL");
        app->state = StCancelling;
        timer_arm(app, 3000);
        switch_view(app, ViewLoading);
        return true;
    case ViewLoading:
        return false;
    default:
        build_menu(app);
        return true;
    }
}

/* ------------------------------------------------------------------ lifecycle */

int32_t pi_runner_app(void* p) {
    UNUSED(p);
    App* app = malloc(sizeof(App));
    memset(app, 0, sizeof(App));

    /* The expansion-module service listens on this same USART; stop it while we own it. */
    app->expansion = furi_record_open(RECORD_EXPANSION);
    expansion_disable(app->expansion);

    app->serial = furi_hal_serial_control_acquire(FuriHalSerialIdUsart);
    if(!app->serial) {
        FURI_LOG_E(TAG, "USART busy");
        expansion_enable(app->expansion);
        furi_record_close(RECORD_EXPANSION);
        free(app);
        return -1;
    }

    app->res_text = furi_string_alloc();
    app->rx_stream = furi_stream_buffer_alloc(1024, 1);
    app->rx_queue = furi_message_queue_alloc(RX_QUEUE_DEPTH, sizeof(RxLine));

    app->gui = furi_record_open(RECORD_GUI);
    app->vd = view_dispatcher_alloc();
    view_dispatcher_set_event_callback_context(app->vd, app);
    view_dispatcher_set_custom_event_callback(app->vd, custom_cb);
    view_dispatcher_set_navigation_event_callback(app->vd, back_cb);
    view_dispatcher_attach_to_gui(app->vd, app->gui, ViewDispatcherTypeFullscreen);

    app->menu = submenu_alloc();
    app->param_menu = submenu_alloc();
    app->number = number_input_alloc();
    app->text = text_box_alloc();
    app->kv = variable_item_list_alloc();
    app->list = submenu_alloc();
    app->dialog = dialog_ex_alloc();
    app->loading = loading_alloc();
    app->popup = popup_alloc();
    app->running = widget_alloc();
    view_dispatcher_add_view(app->vd, ViewMenu, submenu_get_view(app->menu));
    view_dispatcher_add_view(app->vd, ViewParam, submenu_get_view(app->param_menu));
    view_dispatcher_add_view(app->vd, ViewNumber, number_input_get_view(app->number));
    view_dispatcher_add_view(app->vd, ViewText, text_box_get_view(app->text));
    view_dispatcher_add_view(app->vd, ViewKv, variable_item_list_get_view(app->kv));
    view_dispatcher_add_view(app->vd, ViewList, submenu_get_view(app->list));
    view_dispatcher_add_view(app->vd, ViewDialog, dialog_ex_get_view(app->dialog));
    view_dispatcher_add_view(app->vd, ViewLoading, loading_get_view(app->loading));
    view_dispatcher_add_view(app->vd, ViewPopup, popup_get_view(app->popup));
    view_dispatcher_add_view(app->vd, ViewRunning, widget_get_view(app->running));

    app->timer = furi_timer_alloc(timer_cb, FuriTimerTypeOnce, app);

    furi_hal_serial_init(app->serial, BAUD); /* 8N1 default framing */
    app->rx_run = true;
    app->rx_thread = furi_thread_alloc_ex("PiRunnerRx", 1024, rx_worker, app);
    furi_thread_start(app->rx_thread);
    furi_hal_serial_async_rx_start(app->serial, rx_isr, app, false);

    start_handshake(app);
    view_dispatcher_run(app->vd);

    /* teardown: stop RX first, then release the port, then re-enable expansion */
    if(app->state == StRunning) tx_line(app, "CANCEL");
    furi_timer_stop(app->timer);
    furi_hal_serial_async_rx_stop(app->serial);
    app->rx_run = false;
    furi_thread_join(app->rx_thread);
    furi_thread_free(app->rx_thread);
    furi_hal_serial_deinit(app->serial);
    furi_hal_serial_control_release(app->serial);
    expansion_enable(app->expansion);
    furi_record_close(RECORD_EXPANSION);

    furi_timer_free(app->timer);
    for(ViewId v = ViewMenu; v <= ViewRunning; v++)
        view_dispatcher_remove_view(app->vd, v);
    submenu_free(app->menu);
    submenu_free(app->param_menu);
    number_input_free(app->number);
    text_box_free(app->text);
    variable_item_list_free(app->kv);
    submenu_free(app->list);
    dialog_ex_free(app->dialog);
    loading_free(app->loading);
    popup_free(app->popup);
    widget_free(app->running);
    view_dispatcher_free(app->vd);
    furi_record_close(RECORD_GUI);
    furi_message_queue_free(app->rx_queue);
    furi_stream_buffer_free(app->rx_stream);
    furi_string_free(app->res_text);
    free(app);
    return 0;
}
