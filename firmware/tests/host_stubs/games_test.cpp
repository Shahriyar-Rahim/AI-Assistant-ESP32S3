#include "device_games.h"
#include "device_display.h"
#include "nvs.h"
#include <cassert>
#include <cstdio>
#include <cstring>

static unsigned next_random = 0;
uint32_t esp_random() { return next_random; }
int64_t esp_timer_get_time() { return 1000000; }
const char *esp_err_to_name(esp_err_t) { return "error"; }
esp_err_t nvs_open(const char *, int, nvs_handle_t *) { return 1; }
esp_err_t nvs_get_blob(nvs_handle_t, const char *, void *, size_t *) { return 1; }
esp_err_t nvs_set_blob(nvs_handle_t, const char *, const void *, size_t) { return 1; }
esp_err_t nvs_commit(nvs_handle_t) { return 1; }
void nvs_close(nvs_handle_t) {}

namespace DeviceDisplay {
esp_err_t set_game_view(const GameView &) { return ESP_OK; }
esp_err_t clear_game_view() { return ESP_OK; }
esp_err_t set_state(const char *) { return ESP_OK; }
esp_err_t set_dashboard(const char *, const char *, const char *, const char *const *, size_t,
                        uint16_t, uint16_t, uint16_t, uint16_t, uint16_t, const char *) { return ESP_OK; }
}

int main()
{
    DeviceGames::Reply reply{};
    next_random = 0;
    DeviceGames::start("dice", "normal", reply);
    assert(reply.active && !reply.finished && std::strstr(reply.speech, "rolled a 1"));
    next_random = 5;
    DeviceGames::input("roll again", reply);
    assert(reply.active && std::strstr(reply.speech, "rolled a 6"));
    DeviceGames::input("what time is it", reply);
    assert(!reply.handled && reply.active);
    DeviceGames::stop(reply);
    assert(!reply.active && reply.outcome[0] == '\0');

    DeviceGames::start("roulette", "normal", reply);
    for (unsigned number = 0; number <= 36; ++number)
    {
        next_random = number;
        DeviceGames::input("spin", reply);
        const bool red = number != 0 && ((number <= 10 || (number >= 19 && number <= 28))
                                            ? number % 2 == 1 : number % 2 == 0);
        char expected[32];
        std::snprintf(expected, sizeof(expected), "on %u, %s", number,
                      number == 0 ? "green" : red ? "red" : "black");
        assert(reply.active && std::strstr(reply.speech, expected));
    }
    DeviceGames::start("symbols", "normal", reply);
    next_random = 5;
    DeviceGames::input("choose again", reply);
    assert(reply.active && std::strstr(reply.speech, "triangle"));
    DeviceGames::input("stop", reply);
    assert(!reply.active);
}
