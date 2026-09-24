#include "pi500.h"

#include <curl/curl.h>
#include <json-c/json.h>
#include <stdlib.h>
#include <string.h>

typedef struct {
    char *data;
    size_t length;
} Response;

static size_t receive(void *data, size_t size, size_t count, void *user)
{
    size_t bytes = size * count;
    Response *response = user;
    char *larger = realloc(response->data, response->length + bytes + 1);
    if (larger == NULL)
        return 0;
    response->data = larger;
    memcpy(response->data + response->length, data, bytes);
    response->length += bytes;
    response->data[response->length] = '\0';
    return bytes;
}

static int integer_member(struct json_object *object, const char *name)
{
    struct json_object *value = NULL;
    return json_object_object_get_ex(object, name, &value)
               ? json_object_get_int(value)
               : 0;
}

bool quota_fetch(const char *url, QuotaState *quota)
{
    CURL *curl = curl_easy_init();
    Response response = {0};
    if (curl == NULL)
        return false;
    curl_easy_setopt(curl, CURLOPT_URL, url);
    curl_easy_setopt(curl, CURLOPT_TIMEOUT_MS, 3000L);
    curl_easy_setopt(curl, CURLOPT_WRITEFUNCTION, receive);
    curl_easy_setopt(curl, CURLOPT_WRITEDATA, &response);
    CURLcode result = curl_easy_perform(curl);
    curl_easy_cleanup(curl);
    if (result != CURLE_OK || response.data == NULL) {
        free(response.data);
        return false;
    }

    struct json_object *root = json_tokener_parse(response.data);
    free(response.data);
    if (root == NULL)
        return false;
    struct json_object *ok = NULL, *primary = NULL, *secondary = NULL;
    bool valid = json_object_object_get_ex(root, "ok", &ok) &&
                 json_object_get_boolean(ok) &&
                 json_object_object_get_ex(root, "primary", &primary) &&
                 json_object_object_get_ex(root, "secondary", &secondary);
    if (valid) {
        QuotaState next = {
            .ok = true,
            .five_hour_remaining = integer_member(primary, "remaining_percent"),
            .seven_day_remaining = integer_member(secondary, "remaining_percent"),
            .fetched_at = (time_t)integer_member(root, "fetched_at"),
            .five_hour_reset = (time_t)integer_member(primary, "resets_at"),
            .seven_day_reset = (time_t)integer_member(secondary, "resets_at"),
        };
        *quota = next;
    }
    json_object_put(root);
    return valid;
}

