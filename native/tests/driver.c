/* Test-only executable. HTTP fixtures are linked with --wrap=http; production
 * has no endpoint override, mock transport or credential-testing backdoor. */
#include "inbox.h"
#include <curl/curl.h>
#include <locale.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

static void signal_handler(int signo)
{
    interrupted = signo;
}
int __wrap_http(App *a, const char *url, const char *token, const char *body, size_t limit,
                Buffer *out, long *status)
{
    (void)token;
    (void)limit;
    const char *path = getenv("YTI_TEST_HTTP");
    if (!path)
        return fail(a, "Unexpected network request in test");
    Json *fixture = json_file(a, path);
    if (!fixture)
        return -1;
    Json *responses = jget(fixture, "responses"), *entry = jfirst(responses),
         *calls = jget(fixture, "calls");
    if (!entry) {
        json_object_put(fixture);
        return fail(a, "HTTP fixture exhausted");
    }
    const char *suffix = jstr(entry, "path");
    size_t u = strlen(url), s = strlen(suffix);
    if (u < s || strcmp(url + u - s, suffix)) {
        json_object_put(fixture);
        return fail(a, "Unexpected fixture URL");
    }
    Json *call = json_object_new_object();
    jsetstr(call, "url", url);
    json_object_object_add(call, "body", body ? json_parse(a, body) : NULL);
    json_object_array_add(calls, call);
    if (*jstr(entry, "sql") && db_exec(a, "%s", jstr(entry, "sql"))) {
        json_object_put(fixture);
        return -1;
    }
    *status = json_object_get_int(jget(entry, "status"));
    const char *payload = jget(entry, "raw") ? jstr(entry, "raw") : jtext(jget(entry, "body"));
    buffer_add(out, payload, strlen(payload));
    json_object_array_del_idx(responses, 0, 1);
    int rc = atomic_write(a, path, jtext(fixture), strlen(jtext(fixture)), 0600);
    json_object_put(fixture);
    return rc;
}
int main(int argc, char **argv)
{
    setlocale(LC_ALL, "");
    setlocale(LC_NUMERIC, "C");
    struct sigaction action = {0};
    action.sa_handler = signal_handler;
    sigaction(SIGINT, &action, NULL);
    sigaction(SIGTERM, &action, NULL);
    curl_global_init(CURL_GLOBAL_DEFAULT);
    App a;
    int rc = app_open(&a);
    Json *out = NULL, *input = NULL;
    if (!rc && argc > 1) {
        if (!strcmp(argv[1], "_snapshot"))
            out = sync_snapshot(&a);
        else if (!strcmp(argv[1], "_apply") && argc == 3) {
            input = json_file(&a, argv[2]);
            if (input)
                out = sync_apply(&a, input);
        } else if (!strcmp(argv[1], "_merge") && argc == 4) {
            input = json_file(&a, argv[2]);
            Json *right = json_file(&a, argv[3]);
            if (input && right && !sync_validate(&a, input) && !sync_validate(&a, right))
                out = sync_merge(input, right);
            json_object_put(right);
        } else if (!strcmp(argv[1], "_feed") && argc == 4) {
            char *xml = read_file(&a, argv[3], 5000000);
            if (xml)
                out = parse_feed(&a, xml, argv[2]);
            free(xml);
        } else if (!strcmp(argv[1], "_canonical") && argc == 3) {
            char *url = canonical_channel(&a, argv[2]);
            if (url)
                out = json_object_new_string(url);
            free(url);
        } else if (!strcmp(argv[1], "_backup") && argc == 3)
            rc = db_backup(&a, argv[2]);
        else if (!strcmp(argv[1], "_timeout")) {
            char *args[] = {"yes", NULL};
            Buffer b = {0};
            rc = process(&a, args, 1, &b, false);
            buffer_free(&b);
        } else
            rc = run_command(&a, argc - 1, argv + 1);
        if (out)
            puts(jtext(out));
        else if (*a.error)
            rc = -1;
    }
    if (rc)
        fprintf(stderr, "%s\n", a.error);
    json_object_put(out);
    json_object_put(input);
    app_close(&a);
    curl_global_cleanup();
    return interrupted ? 128 + interrupted : rc ? 1 : 0;
}
