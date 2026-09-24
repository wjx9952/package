#define _GNU_SOURCE
#include "pi500.h"

#include <glob.h>
#include <json-c/json.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/stat.h>

#define MAX_SESSIONS 12
#define MAX_PENDING 32
#define MAX_CALL_ID 96
#define SESSION_MAX_AGE (6 * 60 * 60)
#define PROCESSING_STALE (15 * 60)
#define COMPLETED_HOLD 60

typedef struct {
    char path[4096];
    time_t modified;
} SessionFile;

typedef struct {
    bool active;
    time_t latest_event;
    time_t completed_at;
    char pending[MAX_PENDING][MAX_CALL_ID];
    size_t pending_count;
} SessionState;

static int newest_first(const void *left, const void *right)
{
    const SessionFile *a = left;
    const SessionFile *b = right;
    return a->modified < b->modified ? 1 : a->modified > b->modified ? -1 : 0;
}

static const char *json_string_member(struct json_object *object, const char *name)
{
    struct json_object *value = NULL;
    if (!json_object_object_get_ex(object, name, &value) ||
        !json_object_is_type(value, json_type_string))
        return "";
    return json_object_get_string(value);
}

static time_t parse_iso8601(const char *text)
{
    struct tm parsed = {0};
    if (text == NULL || strptime(text, "%Y-%m-%dT%H:%M:%S", &parsed) == NULL)
        return 0;
    return timegm(&parsed);
}

static bool needs_confirmation(struct json_object *payload)
{
    const char *name = json_string_member(payload, "name");
    if (strcmp(name, "request_permissions") == 0 ||
        strcmp(name, "request_user_input") == 0)
        return true;
    const char *input = json_string_member(payload, "input");
    return strstr(input, "tools.request_permissions(") != NULL ||
           strstr(input, "tools.request_user_input(") != NULL ||
           (strstr(input, "exec_command(") != NULL &&
            strstr(input, "require_escalated") != NULL);
}

static void pending_add(SessionState *state, const char *call_id)
{
    if (call_id[0] == '\0' || state->pending_count == MAX_PENDING)
        return;
    for (size_t i = 0; i < state->pending_count; ++i) {
        if (strcmp(state->pending[i], call_id) == 0)
            return;
    }
    snprintf(state->pending[state->pending_count++], MAX_CALL_ID, "%s", call_id);
}

static void pending_remove(SessionState *state, const char *call_id)
{
    for (size_t i = 0; i < state->pending_count; ++i) {
        if (strcmp(state->pending[i], call_id) != 0)
            continue;
        state->pending_count--;
        if (i != state->pending_count)
            memcpy(state->pending[i], state->pending[state->pending_count], MAX_CALL_ID);
        return;
    }
}

static SessionState scan_session(const char *path)
{
    SessionState state = {0};
    FILE *file = fopen(path, "r");
    if (file == NULL)
        return state;

    char *line = NULL;
    size_t capacity = 0;
    while (getline(&line, &capacity, file) >= 0) {
        struct json_object *event = json_tokener_parse(line);
        if (event == NULL)
            continue;
        const char *timestamp = json_string_member(event, "timestamp");
        time_t event_time = parse_iso8601(timestamp);
        if (event_time != 0)
            state.latest_event = event_time;

        struct json_object *payload = NULL;
        if (!json_object_object_get_ex(event, "payload", &payload)) {
            json_object_put(event);
            continue;
        }
        const char *event_type = json_string_member(event, "type");
        const char *payload_type = json_string_member(payload, "type");
        if (strcmp(event_type, "event_msg") == 0) {
            if (strcmp(payload_type, "task_started") == 0 ||
                strcmp(payload_type, "turn_started") == 0) {
                state.active = true;
                state.completed_at = 0;
                state.pending_count = 0;
            } else if (strcmp(payload_type, "task_complete") == 0 ||
                       strcmp(payload_type, "turn_complete") == 0) {
                state.active = false;
                state.completed_at = event_time;
                state.pending_count = 0;
            } else if (strcmp(payload_type, "turn_aborted") == 0) {
                state.active = false;
                state.completed_at = 0;
                state.pending_count = 0;
            }
        } else if (strcmp(event_type, "response_item") == 0) {
            const char *call_id = json_string_member(payload, "call_id");
            if ((strcmp(payload_type, "function_call") == 0 ||
                 strcmp(payload_type, "custom_tool_call") == 0) &&
                needs_confirmation(payload)) {
                pending_add(&state, call_id);
            } else if (strcmp(payload_type, "function_call_output") == 0 ||
                       strcmp(payload_type, "custom_tool_call_output") == 0) {
                pending_remove(&state, call_id);
            }
        }
        json_object_put(event);
    }
    free(line);
    fclose(file);
    return state;
}

CodexState codex_read_combined_status(const char *codex_home)
{
    char pattern[4096];
    snprintf(pattern, sizeof(pattern), "%s/sessions/*/*/*/rollout-*.jsonl",
             codex_home);
    glob_t matches = {0};
    if (glob(pattern, 0, NULL, &matches) != 0)
        return CODEX_IDLE;

    SessionFile *files = calloc(matches.gl_pathc, sizeof(*files));
    if (files == NULL) {
        globfree(&matches);
        return CODEX_IDLE;
    }
    size_t count = 0;
    for (size_t i = 0; i < matches.gl_pathc; ++i) {
        struct stat info;
        if (stat(matches.gl_pathv[i], &info) != 0)
            continue;
        snprintf(files[count].path, sizeof(files[count].path), "%s",
                 matches.gl_pathv[i]);
        files[count++].modified = info.st_mtime;
    }
    qsort(files, count, sizeof(*files), newest_first);

    bool processing = false;
    bool completed = false;
    time_t now = time(NULL);
    size_t limit = count < MAX_SESSIONS ? count : MAX_SESSIONS;
    for (size_t i = 0; i < limit; ++i) {
        if (now - files[i].modified > SESSION_MAX_AGE)
            continue;
        SessionState state = scan_session(files[i].path);
        if (state.pending_count > 0) {
            free(files);
            globfree(&matches);
            return CODEX_CONFIRMATION;
        }
        if (state.active && now - state.latest_event <= PROCESSING_STALE)
            processing = true;
        if (state.completed_at != 0 && now - state.completed_at <= COMPLETED_HOLD)
            completed = true;
    }
    free(files);
    globfree(&matches);
    if (processing)
        return CODEX_PROCESSING;
    if (completed)
        return CODEX_COMPLETED;
    return CODEX_IDLE;
}

