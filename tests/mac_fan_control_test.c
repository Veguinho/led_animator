/* Native SMC simulation: clang -O2 -Wall -Wextra -Werror -framework IOKit
 * -framework CoreFoundation tests/mac_fan_control_test.c -o /tmp/fan-tests */
#include <IOKit/IOKitLib.h>
#include <assert.h>
#include <limits.h>
static char test_config_path[PATH_MAX];
kern_return_t mock_smc_call(mach_port_t, uint32_t, const void *, size_t, void *, size_t *);
#define IOConnectCallStructMethod mock_smc_call
#define LED_PROJECT "/private/tmp/led-fan-tests"
#define LED_UID getuid()
#define LED_CONFIG test_config_path
#define main watcher_main
#include "../scripts/macos_led_fans.c"
#undef main
#undef IOConnectCallStructMethod

static float maxima[2] = {5927, 5489}, minima[2] = {2160, 2000}, targets[2] = {2160, 2000};
static unsigned char modes[2];
static int pending[2] = {-1, -1}, delay[2], writes, fail_fan = -1;

kern_return_t mock_smc_call(mach_port_t connection_id, uint32_t index, const void *input,
    size_t in_size, void *output, size_t *out_size) {
    (void)connection_id;
    assert(index == 2 && in_size == sizeof(Packet));
    const Packet *in = input; Packet *out = output;
    memset(out, 0, sizeof(*out)); *out_size = sizeof(*out);
    int fan = ((in->key >> 16) & 255) - '0'; assert(fan == 0 || fan == 1);
    uint16_t suffix = in->key & 65535;
    bool mode = suffix == ('M' << 8 | 'd');
    assert(mode || suffix == ('M' << 8 | 'x') || suffix == ('M' << 8 | 'n') || suffix == ('T' << 8 | 'g'));
    if (in->command == 9) {
        out->info.size = mode ? 1 : 4; out->info.type = code(mode ? "ui8 " : "flt ");
    } else if (in->command == 5) {
        if (mode) {
            if (pending[fan] >= 0 && --delay[fan] <= 0) { modes[fan] = pending[fan]; pending[fan] = -1; }
            out->bytes[0] = modes[fan];
        } else {
            float *rpm = suffix == ('M' << 8 | 'x') ? &maxima[fan]
                : suffix == ('M' << 8 | 'n') ? &minima[fan] : &targets[fan];
            memcpy(out->bytes, rpm, 4);
        }
    } else if (in->command == 6) {
        ++writes;
        assert(suffix != ('M' << 8 | 'x') && suffix != ('M' << 8 | 'n'));
        if (fan == fail_fan) { out->result = 0x84; return KERN_SUCCESS; }
        if (mode) { assert(in->bytes[0] <= 1); pending[fan] = in->bytes[0]; delay[fan] = 3; }
        else {
            assert(modes[fan] == 1);
            memcpy(&targets[fan], in->bytes, 4);
            assert(targets[fan] >= minima[fan] && targets[fan] <= maxima[fan]);
        }
    } else assert(false);
    return KERN_SUCCESS;
}

static void save_test_settings(double fan0, double fan1, bool automatic_mode, bool led_only) {
    CFStringRef keys[] = {CFSTR("fan0"), CFSTR("fan1"), CFSTR("automatic"), CFSTR("ledOnly")};
    CFNumberRef first = CFNumberCreate(NULL, kCFNumberDoubleType, &fan0);
    CFNumberRef second = CFNumberCreate(NULL, kCFNumberDoubleType, &fan1);
    const void *values[] = {first, second, automatic_mode ? kCFBooleanTrue : kCFBooleanFalse,
        led_only ? kCFBooleanTrue : kCFBooleanFalse};
    CFDictionaryRef dictionary = CFDictionaryCreate(NULL, (const void **)keys, values, 4,
        &kCFTypeDictionaryKeyCallBacks, &kCFTypeDictionaryValueCallBacks);
    CFDataRef data = CFPropertyListCreateData(NULL, dictionary, kCFPropertyListBinaryFormat_v1_0, 0, NULL);
    FILE *file = fopen(test_config_path, "wb"); assert(file);
    assert(fwrite(CFDataGetBytePtr(data), 1, CFDataGetLength(data), file) == (size_t)CFDataGetLength(data));
    fclose(file); chmod(test_config_path, 0600);
    CFRelease(data); CFRelease(dictionary); CFRelease(first); CFRelease(second);
}

int main(void) {
    Value sensor = {.info = {.size = 2, .type = code("sp78")}, .bytes = {0x43, 0x50}};
    double temperature;
    assert(decode_temperature(&sensor, &temperature) && temperature == 67.3125);
    sensor.bytes[0] = 0xff; sensor.bytes[1] = 0;
    assert(!decode_temperature(&sensor, &temperature));
    sensor.bytes[0] = 0; assert(!decode_temperature(&sensor, &temperature));
    sensor.info.type = code("flt "); sensor.info.size = 4;
    float floating = 64.125f; memcpy(sensor.bytes, &floating, 4);
    assert(decode_temperature(&sensor, &temperature) && temperature == 64.125);
    floating = NAN; memcpy(sensor.bytes, &floating, 4); assert(!decode_temperature(&sensor, &temperature));
    floating = 200; memcpy(sensor.bytes, &floating, 4); assert(!decode_temperature(&sensor, &temperature));
    sensor.info.size = 2; assert(!decode_temperature(&sensor, &temperature));
    char directory[] = "/private/tmp/fan-config-tests-XXXXXX";
    assert(mkdtemp(directory));
    snprintf(test_config_path, sizeof(test_config_path), "%s/settings.plist", directory);
    Settings settings = read_settings();
    assert(settings.percent[0] == 100 && settings.percent[1] == 100 && !settings.automatic && settings.led_only);
    save_test_settings(85, 90, true, false);
    settings = read_settings();
    assert(settings.percent[0] == 85 && settings.percent[1] == 90 && settings.automatic && !settings.led_only);
    chmod(test_config_path, 0666); settings = read_settings();
    assert(settings.percent[0] == 100 && !settings.automatic && settings.led_only);
    save_test_settings(0, 101, false, true); settings = read_settings();
    assert(settings.percent[0] == 100 && settings.percent[1] == 100);
    unlink(test_config_path);
    assert(symlink("/etc/hosts", test_config_path) == 0); settings = read_settings();
    assert(settings.percent[0] == 100 && settings.percent[1] == 100);
    unlink(test_config_path); rmdir(directory);

    assert(maximum(0) && maximum(1));
    assert(modes[0] == 1 && modes[1] == 1 && targets[0] == 5927 && targets[1] == 5489);
    int previous = writes;
    assert(maximum(0) && maximum(1) && writes == previous);
    assert(set_percent(0, 85) && set_percent(1, 90));
    assert(fabsf(targets[0] - 5927 * 0.85f) < 1 && fabsf(targets[1] - 5489 * 0.90f) < 1);
    minima[0] = 4000; assert(set_percent(0, 40) && targets[0] == 4000); minima[0] = 2160;
    previous = writes;
    assert(!set_percent(0, 0) && !set_percent(0, NAN) && !set_percent(0, 101) && writes == previous);
    maxima[0] = NAN; assert(!maximum(0) && writes == previous); maxima[0] = 5927;
    assert(automatic(0) && automatic(1));
    assert(modes[0] == 0 && modes[1] == 0 && !owned[0] && !owned[1]);
    fail_fan = 1; assert(maximum(0)); assert(!maximum(1));
    fail_fan = -1; assert(automatic(0) && automatic(1));
    assert(!owned[0] && !owned[1]);
    puts("Fan tests passed: temperature decoding, unavailable sensors, settings validation, independent speeds, factory limits, and automatic release.");
    return 0;
}
