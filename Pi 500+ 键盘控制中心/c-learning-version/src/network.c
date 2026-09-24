#define _GNU_SOURCE
#include "pi500.h"

#include <arpa/inet.h>
#include <ifaddrs.h>
#include <net/if.h>
#include <spawn.h>
#include <stdio.h>
#include <string.h>
#include <sys/wait.h>

extern char **environ;

static bool is_physical_lan_name(const char *name)
{
    return strcmp(name, "wlan0") == 0 || strcmp(name, "eth0") == 0 ||
           strcmp(name, "end0") == 0;
}

static bool physical_ipv4(char output[16])
{
    struct ifaddrs *interfaces = NULL;
    if (getifaddrs(&interfaces) != 0)
        return false;

    bool found = false;
    for (struct ifaddrs *it = interfaces; it != NULL; it = it->ifa_next) {
        if (it->ifa_addr == NULL || it->ifa_addr->sa_family != AF_INET)
            continue;
        if (!is_physical_lan_name(it->ifa_name))
            continue;
        if ((it->ifa_flags & IFF_UP) == 0 || (it->ifa_flags & IFF_LOOPBACK) != 0)
            continue;
        const struct sockaddr_in *address = (const struct sockaddr_in *)it->ifa_addr;
        if (inet_ntop(AF_INET, &address->sin_addr, output, 16) != NULL) {
            found = true;
            break;
        }
    }
    freeifaddrs(interfaces);
    return found;
}

static bool ping_baidu(void)
{
    pid_t pid = -1;
    char *const argv[] = {
        "/usr/bin/ping", "-4", "-c", "1", "-W", "1", "baidu.com", NULL,
    };
    if (posix_spawn(&pid, argv[0], NULL, NULL, argv, environ) != 0)
        return false;

    int status = 0;
    if (waitpid(pid, &status, 0) < 0)
        return false;
    return WIFEXITED(status) && WEXITSTATUS(status) == 0;
}

bool network_refresh(NetworkState *state)
{
    char address[16] = "--";
    if (!physical_ipv4(address)) {
        strcpy(state->ipv4, "--");
        state->online = false;
        state->consecutive_failures = 0;
        return false;
    }

    strcpy(state->ipv4, address);
    if (ping_baidu()) {
        state->online = true;
        state->consecutive_failures = 0;
    } else {
        state->consecutive_failures++;
        if (state->consecutive_failures >= 2)
            state->online = false;
    }
    return state->online;
}

