#pragma once

#include <cstddef>

namespace DeviceGames {

struct Reply {
    char speech[256];
    char game[24];
    char status[24];
    const char* sound;
    bool handled;
    bool active;
    bool finished;
    char outcome[12];
    char difficulty[8];
    int score;
    int max_score;
    int rounds;
    int duration_seconds;
    unsigned record_id;
};

struct LogEntry {
    unsigned id;
    unsigned timestamp;
    char game[24];
    char outcome[12];
    char difficulty[8];
    int score;
    int max_score;
    int rounds;
    int duration_seconds;
};

// Every call runs on the ESP32-S3 itself: game state, rules, AI opponent and
// rendering never leave the device. The backend only relays recognized speech
// in and speaks the returned text.
void list(Reply& reply);
void start(const char* game, const char* difficulty, Reply& reply);
void input(const char* text, Reply& reply);
void stop(Reply& reply);
bool active();

// Finished games are kept in a 17 byte packed NVS log so results survive reboots and an offline backend.
size_t unsynced(LogEntry* out, size_t maximum);
void mark_synced(unsigned id);
unsigned pending_count();

}
