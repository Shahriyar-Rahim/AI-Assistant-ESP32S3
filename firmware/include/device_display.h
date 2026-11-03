#pragma once

#include <cstddef>
#include <cstdint>

#include "esp_err.h"

namespace DeviceDisplay {

enum class GameLayout : uint8_t { Text = 0, TicTacToe = 1, Hangman = 2, Quiz = 3 };

struct GameView {
    GameLayout layout = GameLayout::Text;
    char title[20]{};
    char status[40]{};
    char line1[32]{};
    char line2[32]{};
    char line3[32]{};
    char rows[8][32]{};
    uint8_t row_count = 0;
    char board[9]{' ', ' ', ' ', ' ', ' ', ' ', ' ', ' ', ' '};
    char masked[16]{};
    uint8_t mistakes = 0;
    uint8_t max_mistakes = 6;
    uint16_t score_you = 0;
    uint16_t score_ai = 0;
    bool show_scores = false;
};

esp_err_t set_game_view(const GameView& view);
esp_err_t clear_game_view();

esp_err_t initialize();
esp_err_t set_state(const char* state);
esp_err_t set_speaking_for(uint32_t duration_ms);
esp_err_t set_dashboard(
    const char* page,
    const char* title,
    const char* detail,
    const char* const* items,
    size_t item_count,
    uint16_t todos,
    uint16_t reminders,
    uint16_t notes,
    uint16_t notifications,
    uint16_t alarms,
    const char* emoji = nullptr);

}
