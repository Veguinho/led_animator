/* SPDX-License-Identifier: GPL-2.0-or-later
 * SMC protocol structures adapted from hholtmann/smcFanControl, smc-command:
 * Copyright (C) 2006 devnull; portions (C) 2013 Michael Wilber.
 * Local, fixed-purpose fan watcher for this Intel Mac. No shell commands run.
 */
#include <IOKit/IOKitLib.h>
#include <libproc.h>
#include <sys/sysctl.h>
#include <signal.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>
#include <math.h>
#include <limits.h>
#include <CoreFoundation/CoreFoundation.h>
#include <fcntl.h>
#include <sys/stat.h>

#ifndef LED_PROJECT
#error LED_PROJECT must be the absolute project directory
#endif
#ifndef LED_UID
#error LED_UID must be the project user's numeric uid
#endif

typedef struct { uint8_t major, minor, build, reserved; uint16_t release; } Version;
typedef struct { uint16_t version, length; uint32_t cpu, gpu, mem; } Limits;
typedef struct { uint32_t size, type; uint8_t attributes; } KeyInfo;
typedef struct {
    uint32_t key; Version version; Limits limits; KeyInfo info;
    uint8_t result, status, command; uint32_t data; uint8_t bytes[32];
} Packet;
typedef struct { KeyInfo info; uint8_t bytes[32]; } Value;
static io_connect_t connection;
static volatile sig_atomic_t stopping;
static bool owned[2];
static const char *scripts[] = { "main.py", "stream_arduino.py", "system_audio_visualizer.py", "led_animator.py" };
typedef struct { double percent[2]; bool automatic, led_only; } Settings;

static Settings read_settings(void) {
    Settings settings = { {100, 100}, false, true };
#ifdef LED_CONFIG
    int fd = open(LED_CONFIG, O_RDONLY | O_NOFOLLOW | O_NONBLOCK);
    if (fd < 0) return settings;
    struct stat info;
    unsigned char bytes[16384];
    if (fstat(fd, &info) != 0 || !S_ISREG(info.st_mode) || info.st_uid != LED_UID
        || (info.st_mode & 0022) || info.st_size < 1 || info.st_size > (off_t)sizeof(bytes)) {
        close(fd); return settings;
    }
    ssize_t size = read(fd, bytes, (size_t)info.st_size); close(fd);
    if (size != info.st_size) return settings;
    CFDataRef data = CFDataCreate(NULL, bytes, size);
    if (!data) return settings;
    CFPropertyListRef plist = CFPropertyListCreateWithData(NULL, data, kCFPropertyListImmutable, NULL, NULL);
    CFRelease(data);
    if (!plist) return settings;
    if (CFGetTypeID(plist) == CFDictionaryGetTypeID()) {
        CFDictionaryRef dictionary = (CFDictionaryRef)plist;
        CFStringRef keys[] = {CFSTR("fan0"), CFSTR("fan1")};
        for (int fan = 0; fan < 2; ++fan) {
            CFTypeRef value = CFDictionaryGetValue(dictionary, keys[fan]); double percent;
            if (value && CFGetTypeID(value) == CFNumberGetTypeID()
                && CFNumberGetValue(value, kCFNumberDoubleType, &percent)
                && isfinite(percent) && percent >= 40 && percent <= 100) settings.percent[fan] = percent;
        }
        CFTypeRef automatic = CFDictionaryGetValue(dictionary, CFSTR("automatic"));
        if (automatic && CFGetTypeID(automatic) == CFBooleanGetTypeID()) settings.automatic = CFBooleanGetValue(automatic);
        CFTypeRef led_only = CFDictionaryGetValue(dictionary, CFSTR("ledOnly"));
        if (led_only && CFGetTypeID(led_only) == CFBooleanGetTypeID()) settings.led_only = CFBooleanGetValue(led_only);
    }
    CFRelease(plist);
#endif
    return settings;
}

static uint32_t code(const char *s) {
    return ((uint32_t)(uint8_t)s[0] << 24) | ((uint32_t)(uint8_t)s[1] << 16)
         | ((uint32_t)(uint8_t)s[2] << 8) | (uint8_t)s[3];
}
static bool call(Packet *in, Packet *out) {
    size_t size = sizeof(*out);
    kern_return_t result = IOConnectCallStructMethod(connection, 2, in, sizeof(*in), out, &size);
    if (in->command == 6 && (result != KERN_SUCCESS || out->result != 0))
        fprintf(stderr, "SMC write %c%c%c%c: IOKit=%08x SMC=%02x\n", (in->key >> 24)&255, (in->key >> 16)&255, (in->key >> 8)&255, in->key&255, result, out->result);
    return result == KERN_SUCCESS && size == sizeof(*out) && out->result == 0;
}
static bool read_key(const char *key, Value *value) {
    Packet in = {0}, out = {0};
    in.key = code(key); in.command = 9;
    if (!call(&in, &out) || !out.info.size || out.info.size > 32) return false;
    value->info = out.info;
    in.info.size = out.info.size; in.command = 5;
    memset(&out, 0, sizeof(out));
    if (!call(&in, &out)) return false;
    memcpy(value->bytes, out.bytes, value->info.size);
    return true;
}
static bool write_key(const char *key, const Value *value) {
    Packet in = {0}, out = {0};
    in.key = code(key); in.command = 6; in.info.size = value->info.size;
    memcpy(in.bytes, value->bytes, value->info.size);
    if (!call(&in, &out)) return false;
    Value check;
    /* Intel T2 writes can be acknowledged before the new value is visible. */
    for (int attempt = 0; attempt < 20; ++attempt) {
        if (read_key(key, &check) && check.info.size == value->info.size
            && memcmp(check.bytes, value->bytes, value->info.size) == 0) return true;
        usleep(25000);
    }
    fprintf(stderr, "SMC write %s did not take effect.\n", key);
    return false;
}
static bool open_smc(void) {
    io_service_t service = IOServiceGetMatchingService(kIOMainPortDefault, IOServiceMatching("AppleSMC"));
    if (!service) return false;
    kern_return_t result = IOServiceOpen(service, mach_task_self(), 0, &connection);
    IOObjectRelease(service);
    return result == KERN_SUCCESS;
}
static bool mode_value(int fan, Value *mode) {
    char key[5]; snprintf(key, sizeof(key), "F%dMd", fan);
    return read_key(key, mode) && mode->info.size == 1 && mode->info.type == code("ui8 ");
}
static bool automatic(int fan) {
    Value mode;
    char key[5]; snprintf(key, sizeof(key), "F%dMd", fan);
    if (!mode_value(fan, &mode)) return false;
    if (mode.bytes[0] == 0) { owned[fan] = false; return true; }
    mode.bytes[0] = 0;
    if (!write_key(key, &mode)) return false;
    owned[fan] = false;
    return true;
}
static bool set_percent(int fan, double percent) {
    char key[5]; Value max, target, mode;
    if (!isfinite(percent) || percent < 40 || percent > 100) return false;
    snprintf(key, sizeof(key), "F%dMx", fan);
    if (!read_key(key, &max) || max.info.size != 4 || max.info.type != code("flt ")) return false;
    float rpm; memcpy(&rpm, max.bytes, 4);
    if (!isfinite(rpm) || rpm < 1000 || rpm > 10000 || !mode_value(fan, &mode)) return false;
    Value minimum;
    snprintf(key, sizeof(key), "F%dMn", fan);
    if (!read_key(key, &minimum) || minimum.info.size != 4 || minimum.info.type != max.info.type) return false;
    float min_rpm; memcpy(&min_rpm, minimum.bytes, 4);
    if (!isfinite(min_rpm) || min_rpm < 0 || min_rpm > rpm) return false;
    float requested = fmaxf(min_rpm, (float)(rpm * percent / 100.0));
    memcpy(max.bytes, &requested, 4);
    snprintf(key, sizeof(key), "F%dTg", fan);
    if (!read_key(key, &target) || target.info.size != max.info.size || target.info.type != max.info.type) return false;
    /* Change mode before writing target: the Intel T2 SMC ignores automatic-mode targets. */
    if (mode.bytes[0] != 1) {
        snprintf(key, sizeof(key), "F%dMd", fan); mode.bytes[0] = 1;
        owned[fan] = true; /* Ensure cleanup even if a write succeeds but verification fails. */
        if (!write_key(key, &mode)) return false;
    }
    owned[fan] = true;
    snprintf(key, sizeof(key), "F%dTg", fan);
    /* Recheck on every tick, including after waking, without redundant SMC writes. */
    return read_key(key, &target) && (memcmp(target.bytes, max.bytes, 4) == 0 || write_key(key, &max));
}
static bool maximum(int fan) { return set_percent(fan, 100); }
static bool recognized_script(const char *argument, const char *cwd) {
    char full[PATH_MAX], resolved[PATH_MAX];
    if (argument[0] == '/') snprintf(full, sizeof(full), "%s", argument);
    else snprintf(full, sizeof(full), "%s/%s", cwd, argument);
    /* The root helper needs no access to Documents: ordinary launch paths can
     * be compared directly with the configured path. Resolve aliases only as
     * a fallback, when macOS grants access to the containing directory. */
    for (size_t i = 0; i < sizeof(scripts)/sizeof(*scripts); ++i) {
        snprintf(resolved, sizeof(resolved), "%s/%s", LED_PROJECT, scripts[i]);
        if (strcmp(full, resolved) == 0) return true;
    }
    if (!realpath(full, resolved)) return false;
    for (size_t i = 0; i < sizeof(scripts)/sizeof(*scripts); ++i) {
        snprintf(full, sizeof(full), "%s/%s", LED_PROJECT, scripts[i]);
        if (strcmp(full, resolved) == 0) return true;
    }
    return false;
}
static bool led_process(pid_t pid) {
    struct proc_bsdinfo info;
    if (proc_pidinfo(pid, PROC_PIDTBSDINFO, 0, &info, sizeof(info)) != sizeof(info)
        || info.pbi_uid != LED_UID || info.pbi_status == SZOMB) return false;
    if (strncasecmp(info.pbi_name, "python", 6) != 0 && strncasecmp(info.pbi_comm, "python", 6) != 0) return false;
    char executable[PROC_PIDPATHINFO_MAXSIZE];
    if (proc_pidpath(pid, executable, sizeof(executable)) <= 0) return false;
    const char *base = strrchr(executable, '/'); base = base ? base + 1 : executable;
    if (strncasecmp(base, "python", 6) != 0) return false;
    struct proc_vnodepathinfo paths;
    if (proc_pidinfo(pid, PROC_PIDVNODEPATHINFO, 0, &paths, sizeof(paths)) != sizeof(paths)) return false;
    int query[] = { CTL_KERN, KERN_PROCARGS2, pid };
    char args[65536]; size_t size = sizeof(args);
    if (sysctl(query, 3, args, &size, NULL, 0) != 0 || size < sizeof(int) + 2) return false;
    int argc; memcpy(&argc, args, sizeof(argc));
    if (argc < 2 || argc > 4096) return false;
    char *cursor = args + sizeof(int), *end = args + size;
    char *zero = memchr(cursor, 0, end - cursor);
    if (!zero) return false;
    cursor = zero + 1;
    while (cursor < end && !*cursor) ++cursor;
    for (int i = 0; i < argc && cursor < end; ++i) {
        zero = memchr(cursor, 0, end - cursor); if (!zero) return false;
        if (i > 0) {
            if (strcmp(cursor, "-c") == 0 || strcmp(cursor, "-m") == 0) return false;
            if (strcmp(cursor, "-X") == 0 || strcmp(cursor, "-W") == 0) {
                cursor = zero + 1; zero = memchr(cursor, 0, end - cursor);
                if (!zero) return false;
                ++i;
            } else if (cursor[0] != '-') return recognized_script(cursor, paths.pvi_cdir.vip_path);
        }
        cursor = zero + 1;
    }
    return false;
}
static int led_running(bool print) {
    pid_t pids[16384];
    int bytes = proc_listpids(PROC_ALL_PIDS, 0, pids, sizeof(pids));
    if (bytes <= 0 || bytes >= (int)sizeof(pids)) return -1;
    int count = 0;
    for (int i = 0; i < bytes/(int)sizeof(pid_t); ++i) {
        if (pids[i] > 0 && led_process(pids[i])) {
            ++count; if (print) printf("LED process: %d\n", pids[i]);
        }
    }
    return count;
}
static void stop(int signal_number) { (void)signal_number; stopping = 1; }
static bool validate_hardware(void) {
    char model[128]; size_t size = sizeof(model); Value count, mode;
    return sysctlbyname("hw.model", model, &size, NULL, 0) == 0
        && strcmp(model, "MacBookPro15,1") == 0
        && read_key("FNum", &count) && count.info.size == 1 && count.bytes[0] == 2
        && mode_value(0, &mode) && mode_value(1, &mode);
}
static void status(void) {
    printf("LED processes running: %d\n", led_running(true));
    for (int fan = 0; fan < 2; ++fan) {
        const char *suffix[] = { "Ac", "Mx", "Tg", "Md" };
        printf("Fan %d:", fan);
        for (int k = 0; k < 4; ++k) {
            char key[5]; Value v; snprintf(key, sizeof(key), "F%d%s", fan, suffix[k]);
            if (!read_key(key, &v)) { printf(" %s=unavailable", suffix[k]); continue; }
            if (k == 3) printf(" mode=%s", v.bytes[0] == 0 ? "automatic" : "manual");
            else if (v.info.size == 4 && v.info.type == code("flt ")) {
                float rpm; memcpy(&rpm, v.bytes, 4); printf(" %s=%.0f", suffix[k], rpm);
            }
        }
        puts("");
    }
}
static void json_rpm(int fan, const char *suffix, const char *label) {
    char key[5]; Value value; snprintf(key, sizeof(key), "F%d%s", fan, suffix);
    printf("\"%s\":", label);
    if (read_key(key, &value) && value.info.size == 4 && value.info.type == code("flt ")) {
        float rpm; memcpy(&rpm, value.bytes, 4);
        if (isfinite(rpm) && rpm >= 0 && rpm <= 10000) { printf("%.2f", rpm); return; }
    }
    printf("null");
}
static bool decode_temperature(const Value *value, double *temperature) {
    if (value->info.type == code("sp78") && value->info.size == 2) {
        int16_t raw = (int16_t)(((uint16_t)value->bytes[0] << 8) | value->bytes[1]);
        *temperature = raw / 256.0;
    } else if (value->info.type == code("flt ") && value->info.size == 4) {
        float number; memcpy(&number, value->bytes, sizeof(number)); *temperature = number;
    } else return false;
    /* Missing or powered-down sensors can return zero rather than an error. */
    return isfinite(*temperature) && *temperature > 0 && *temperature <= 150;
}
static void json_temperature(const char *key, const char *label) {
    Value value; double temperature;
    printf("\"%s\":", label);
    if (read_key(key, &value) && decode_temperature(&value, &temperature)) printf("%.2f", temperature);
    else printf("null");
}
static void json_status(void) {
    Settings settings = read_settings();
    printf("{\"ledProcesses\":%d,\"settings\":{\"fan0\":%.1f,\"fan1\":%.1f,\"automatic\":%s,\"ledOnly\":%s},\"fans\":[",
        led_running(false), settings.percent[0], settings.percent[1], settings.automatic ? "true" : "false", settings.led_only ? "true" : "false");
    for (int fan = 0; fan < 2; ++fan) {
        if (fan) printf(",");
        printf("{\"id\":%d,", fan);
        json_rpm(fan, "Ac", "rpm"); printf(",");
        json_rpm(fan, "Mn", "min"); printf(",");
        json_rpm(fan, "Mx", "max"); printf(",");
        json_rpm(fan, "Tg", "target"); printf(",\"manual\":");
        Value mode;
        if (mode_value(fan, &mode)) printf("%s", mode.bytes[0] == 1 ? "true" : "false");
        else printf("null");
        printf("}");
    }
    printf("],\"temperatures\":{");
    json_temperature("TC0F", "cpu"); printf(",");
    json_temperature("TG0P", "gpu"); printf(",");
    json_temperature("TB0T", "battery");
    puts("}}");
}
int main(int argc, char **argv) {
    if (argc != 2 || (strcmp(argv[1], "--status") && strcmp(argv[1], "--json") && strcmp(argv[1], "--watch") && strcmp(argv[1], "--restore"))) {
        fprintf(stderr, "Usage: %s --status|--json|--watch|--restore\n", argv[0]); return 2;
    }
    if (!open_smc() || !validate_hardware()) { fputs("Unsupported or inaccessible SMC hardware.\n", stderr); return 1; }
    if (!strcmp(argv[1], "--status")) { status(); IOServiceClose(connection); return 0; }
    if (!strcmp(argv[1], "--json")) { json_status(); IOServiceClose(connection); return 0; }
    if (geteuid() != 0) { fputs("Fan control requires macOS administrator authorization.\n", stderr); IOServiceClose(connection); return 1; }
    if (!strcmp(argv[1], "--restore")) {
        bool first = automatic(0), second = automatic(1); status(); IOServiceClose(connection);
        return first && second ? 0 : 1;
    }
    setvbuf(stdout, NULL, _IOLBF, 0);
    signal(SIGTERM, stop); signal(SIGINT, stop); signal(SIGHUP, stop);
    /* Recover automatic control if a previous watcher was killed mid-session. */
    bool first_auto = automatic(0), second_auto = automatic(1);
    if (!first_auto || !second_auto) { fputs("Cannot initialize automatic fan control.\n", stderr); IOServiceClose(connection); return 1; }
    int previous = -1;
    Settings previous_settings = {{-1, -1}, false, true};
    while (!stopping) {
        int count = led_running(false);
        Settings settings = read_settings();
        bool active = !settings.automatic && (!settings.led_only || count > 0), ok = count >= 0;
        for (int fan = 0; fan < 2; ++fan) {
            if (active) { if (!(settings.percent[fan] == 100 ? maximum(fan) : set_percent(fan, settings.percent[fan]))) ok = false; }
            else if (owned[fan] && !automatic(fan)) ok = false;
        }
        if (!ok) {
            for (int fan = 0; fan < 2; ++fan) if (owned[fan]) automatic(fan);
            fputs("Fan watcher failed; released manual control and will restart.\n", stderr);
            IOServiceClose(connection); return 1;
        }
        if ((int)active != previous || previous_settings.percent[0] != settings.percent[0]
            || previous_settings.percent[1] != settings.percent[1]
            || previous_settings.automatic != settings.automatic || previous_settings.led_only != settings.led_only) {
            printf("LED processes=%d; fans %s; targets %.0f%% / %.0f%%; LED only=%s.\n", count,
                active ? "manual" : "automatic", settings.percent[0], settings.percent[1], settings.led_only ? "yes" : "no");
            previous = active;
            previous_settings = settings;
        }
        sleep(2);
    }
    bool first = !owned[0] || automatic(0), second = !owned[1] || automatic(1);
    IOServiceClose(connection);
    return first && second ? 0 : 1;
}
