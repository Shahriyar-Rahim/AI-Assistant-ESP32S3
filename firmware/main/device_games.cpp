#include "device_games.h"

#include <algorithm>
#include <cctype>
#include <cstdarg>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <initializer_list>

#include "device_display.h"
#include "esp_log.h"
#include "esp_random.h"
#include "esp_timer.h"
#include "nvs.h"
#include <ctime>

namespace {

constexpr char TAG[] = "device_games";

enum class Game : uint8_t { None, TicTacToe, Rps, Number, Trivia, Hangman, Word };

struct GameInfo {
    Game id;
    const char* key;
    const char* title;
    const char* spoken;
};

constexpr GameInfo GAMES[] = {
    {Game::TicTacToe, "tic_tac_toe", "TIC TAC TOE", "tic tac toe"},
    {Game::Rps, "rock_paper_scissors", "ROCK PAPER SCISSORS", "rock paper scissors"},
    {Game::Number, "number_guessing", "NUMBER GUESSING", "number guessing"},
    {Game::Trivia, "trivia", "TRIVIA", "trivia"},
    {Game::Hangman, "hangman", "HANGMAN", "hangman"},
    {Game::Word, "word_association", "WORD ASSOCIATION", "word association"},
};
constexpr size_t GAME_COUNT = sizeof(GAMES) / sizeof(GAMES[0]);

const GameInfo* info_for(Game game) {
    for (const auto& entry : GAMES) {
        if (entry.id == game) return &entry;
    }
    return nullptr;
}

struct Question {
    const char* text;
    const char* choices[4];
    char answer;
};

constexpr Question QUESTIONS[] = {
    {"Which planet is known as the Red Planet?", {"Mars", "Venus", "Jupiter", "Mercury"}, 'A'},
    {"How many sides does a hexagon have?", {"Five", "Six", "Seven", "Eight"}, 'B'},
    {"Which data structure uses FIFO order?", {"Stack", "Queue", "Tree", "Set"}, 'B'},
    {"Which HTTP method retrieves a resource?", {"Get", "Patch", "Delete", "Trace"}, 'A'},
    {"Which language styles web pages?", {"SQL", "CSS", "Bash", "C"}, 'B'},
    {"What does RAM mainly provide?", {"Working memory", "Long term storage", "Network routing", "Power"}, 'A'},
    {"What is the capital of France?", {"Berlin", "Madrid", "Paris", "Rome"}, 'C'},
    {"How many legs does a spider have?", {"Six", "Eight", "Ten", "Four"}, 'B'},
    {"Which gas do plants absorb?", {"Oxygen", "Helium", "Nitrogen", "Carbon dioxide"}, 'D'},
    {"What is the largest ocean on Earth?", {"Atlantic", "Indian", "Pacific", "Arctic"}, 'C'},
    {"Which chip family is the ESP32 from?", {"Espressif", "Intel", "Nvidia", "Qualcomm"}, 'A'},
    {"What is two times nine?", {"Sixteen", "Eighteen", "Twenty", "Twenty two"}, 'B'},
    {"Which animal is the fastest on land?", {"Lion", "Horse", "Cheetah", "Dog"}, 'C'},
    {"How many days are in a leap year?", {"365", "364", "366", "360"}, 'C'},
};
constexpr size_t QUESTION_COUNT = sizeof(QUESTIONS) / sizeof(QUESTIONS[0]);
constexpr int TRIVIA_ROUNDS = 5;

constexpr const char* HANGMAN_WORDS[] = {
    "planet", "garden", "signal", "puzzle", "window", "lantern", "rocket", "button",
    "camera", "pocket", "orange", "bridge", "thunder", "compass", "battery", "silver",
};
constexpr size_t HANGMAN_WORD_COUNT = sizeof(HANGMAN_WORDS) / sizeof(HANGMAN_WORDS[0]);

struct Topic {
    const char* word;
    const char* related[12];
};

constexpr Topic TOPICS[] = {
    {"sun", {"star", "light", "day", "warm", "hot", "sky", "bright", "summer", "sunny", "yellow", "heat", "ray"}},
    {"ocean", {"water", "wave", "blue", "sea", "fish", "salt", "beach", "deep", "whale", "ship", "tide", "shark"}},
    {"book", {"read", "story", "page", "words", "library", "author", "novel", "write", "paper", "chapter", "school", "pen"}},
    {"music", {"song", "sound", "rhythm", "melody", "band", "guitar", "piano", "dance", "singer", "note", "radio", "drum"}},
    {"night", {"dark", "moon", "stars", "sleep", "dream", "bed", "evening", "owl", "black", "quiet", "midnight", "late"}},
    {"winter", {"cold", "snow", "ice", "coat", "frost", "december", "freeze", "chill", "white", "scarf", "sled", "wind"}},
};
constexpr size_t TOPIC_COUNT = sizeof(TOPICS) / sizeof(TOPICS[0]);
constexpr int WORD_TURNS = 5;
constexpr int WORD_TARGET = 3;

Game current = Game::None;
bool finished = false;
int64_t game_started_us = 0;
int ai_random_percent = 25;
char current_difficulty[8] = "normal";

struct PendingResult {
    bool ready = false;
    char game[24]{};
    char outcome[12]{};
    int score = 0;
    int max_score = 0;
    int rounds = 0;
    int duration_seconds = 0;
};
PendingResult pending;

// Tic tac toe
char board[9];
uint16_t ttt_you = 0;
uint16_t ttt_ai = 0;
char ttt_result[16];

// Rock paper scissors
uint16_t rps_you = 0;
uint16_t rps_ai = 0;
uint16_t rps_draw = 0;
char rps_last[32];

// Number guessing
int secret = 0;
int guesses = 0;
char number_hint[16];

// Trivia
size_t trivia_order[TRIVIA_ROUNDS];
int trivia_index = 0;
int trivia_score = 0;

// Hangman
const char* hangman_word = "";
bool hangman_guessed[26];
int hangman_mistakes = 0;
constexpr int HANGMAN_MAX = 6;

// Word association
size_t topic_index = 0;
int word_turns = 0;
int word_hits = 0;
bool word_used[12];
char word_last[32];

// Compact persistent result log: 17 bytes per record, 64 records, synced records are evicted first.
constexpr char LOG_NAMESPACE[] = "gamelog";
constexpr char LOG_KEY[] = "records";
constexpr size_t LOG_CAPACITY = 64;
constexpr const char* OUTCOME_NAMES[] = {"win", "loss", "draw", "abandoned"};
constexpr const char* DIFFICULTY_NAMES[] = {"easy", "normal", "hard"};
constexpr uint8_t FLAG_SYNCED = 0x10;

struct __attribute__((packed)) LogRecord {
    uint32_t id;
    uint32_t timestamp;
    uint16_t score;
    uint16_t max_score;
    uint16_t duration;
    uint8_t game;
    uint8_t flags;
    uint8_t rounds;
};

struct __attribute__((packed)) GameLog {
    uint32_t next_id = 1;
    uint8_t count = 0;
    LogRecord records[LOG_CAPACITY];
};
constexpr size_t LOG_HEADER = 5;
GameLog game_log;
bool game_log_loaded = false;

size_t index_of(const char* const* names, size_t count, const char* value, size_t fallback) {
    for (size_t index = 0; index < count; ++index) {
        if (std::strcmp(names[index], value) == 0) return index;
    }
    return fallback;
}

void log_load() {
    if (game_log_loaded) return;
    game_log_loaded = true;
    nvs_handle_t handle;
    if (nvs_open(LOG_NAMESPACE, NVS_READONLY, &handle) != ESP_OK) return;
    static GameLog loaded;
    size_t size = sizeof(loaded);
    if (nvs_get_blob(handle, LOG_KEY, &loaded, &size) == ESP_OK && size >= LOG_HEADER && loaded.count <= LOG_CAPACITY &&
        size == LOG_HEADER + loaded.count * sizeof(LogRecord)) {
        game_log = loaded;
    }
    nvs_close(handle);
}

void log_save() {
    nvs_handle_t handle;
    if (nvs_open(LOG_NAMESPACE, NVS_READWRITE, &handle) != ESP_OK) return;
    const size_t size = LOG_HEADER + game_log.count * sizeof(LogRecord);
    if (nvs_set_blob(handle, LOG_KEY, &game_log, size) == ESP_OK) nvs_commit(handle);
    nvs_close(handle);
}

uint32_t log_append(const char* game_key, const char* outcome, const char* difficulty, int score, int max_score,
                    int rounds, int duration) {
    log_load();
    uint8_t game_index = 0;
    for (size_t index = 0; index < GAME_COUNT; ++index) {
        if (std::strcmp(GAMES[index].key, game_key) == 0) game_index = static_cast<uint8_t>(index);
    }
    if (game_log.count >= LOG_CAPACITY) {
        size_t victim = 0;
        for (size_t index = 0; index < game_log.count; ++index) {
            if (game_log.records[index].flags & FLAG_SYNCED) { victim = index; break; }
        }
        std::memmove(&game_log.records[victim], &game_log.records[victim + 1],
                     (game_log.count - victim - 1) * sizeof(LogRecord));
        --game_log.count;
    }
    const time_t now = time(nullptr);
    LogRecord& record = game_log.records[game_log.count++];
    record.id = game_log.next_id++;
    record.timestamp = now > 1700000000 ? static_cast<uint32_t>(now) : 0;
    record.score = static_cast<uint16_t>(std::clamp(score, 0, 65535));
    record.max_score = static_cast<uint16_t>(std::clamp(max_score, 0, 65535));
    record.duration = static_cast<uint16_t>(std::clamp(duration, 0, 65535));
    record.game = game_index;
    record.rounds = static_cast<uint8_t>(std::clamp(rounds, 0, 255));
    record.flags = static_cast<uint8_t>(
        index_of(OUTCOME_NAMES, 4, outcome, 3) | (index_of(DIFFICULTY_NAMES, 3, difficulty, 1) << 2));
    log_save();
    return record.id;
}

void say(DeviceGames::Reply& reply, const char* pattern, ...) {
    va_list arguments;
    va_start(arguments, pattern);
    std::vsnprintf(reply.speech, sizeof(reply.speech), pattern, arguments);
    va_end(arguments);
}

void reset_reply(DeviceGames::Reply& reply) {
    std::memset(&reply, 0, sizeof(reply));
    reply.sound = nullptr;
}

void conclude(const char* outcome, int score, int max_score, int rounds) {
    const GameInfo* info = info_for(current);
    if (info == nullptr || pending.ready) return;
    pending.ready = true;
    std::snprintf(pending.game, sizeof(pending.game), "%s", info->key);
    std::snprintf(pending.outcome, sizeof(pending.outcome), "%s", outcome);
    pending.score = score;
    pending.max_score = max_score;
    pending.rounds = rounds;
    pending.duration_seconds = static_cast<int>((esp_timer_get_time() - game_started_us) / 1000000);
}

void finish_reply(DeviceGames::Reply& reply) {
    const GameInfo* info = info_for(current);
    if (info != nullptr) std::snprintf(reply.game, sizeof(reply.game), "%s", info->key);
    if (pending.ready) {
        std::snprintf(reply.game, sizeof(reply.game), "%s", pending.game);
        std::snprintf(reply.outcome, sizeof(reply.outcome), "%s", pending.outcome);
        std::snprintf(reply.difficulty, sizeof(reply.difficulty), "%s", current_difficulty);
        reply.score = pending.score;
        reply.max_score = pending.max_score;
        reply.rounds = pending.rounds;
        reply.duration_seconds = pending.duration_seconds;
        reply.record_id = log_append(pending.game, pending.outcome, current_difficulty, pending.score,
                                     pending.max_score, pending.rounds, pending.duration_seconds);
        pending = PendingResult{};
    }
    reply.active = current != Game::None;
    reply.finished = current != Game::None && finished;
    std::snprintf(reply.status, sizeof(reply.status), "%s",
        current == Game::None ? "idle" : (finished ? "finished" : "playing"));
}

uint32_t random_below(uint32_t limit) {
    return limit == 0 ? 0 : esp_random() % limit;
}

// Lowercases, strips punctuation and wraps the text in single spaces so whole words and
// phrases can be matched with strstr.
void normalize(const char* text, char* out, size_t capacity) {
    size_t length = 0;
    if (capacity < 3) return;
    out[length++] = ' ';
    bool last_space = true;
    for (const char* cursor = text; cursor != nullptr && *cursor != '\0' && length + 2 < capacity; ++cursor) {
        const unsigned char value = static_cast<unsigned char>(*cursor);
        if (value == '\'') continue;
        if (std::isalnum(value)) {
            out[length++] = static_cast<char>(std::tolower(value));
            last_space = false;
        } else if (!last_space) {
            out[length++] = ' ';
            last_space = true;
        }
    }
    if (!last_space) out[length++] = ' ';
    out[length] = '\0';
}

bool has(const char* normalized, const char* phrase) {
    char needle[48];
    std::snprintf(needle, sizeof(needle), " %s ", phrase);
    return std::strstr(normalized, needle) != nullptr;
}

bool has_any(const char* normalized, std::initializer_list<const char*> phrases) {
    for (const char* phrase : phrases) {
        if (has(normalized, phrase)) return true;
    }
    return false;
}

size_t tokenize(const char* normalized, char tokens[][20], size_t max_tokens) {
    size_t count = 0;
    const char* cursor = normalized;
    while (*cursor != '\0' && count < max_tokens) {
        while (*cursor == ' ') ++cursor;
        if (*cursor == '\0') break;
        size_t length = 0;
        while (cursor[length] != '\0' && cursor[length] != ' ') ++length;
        const size_t copied = std::min<size_t>(length, 19);
        std::memcpy(tokens[count], cursor, copied);
        tokens[count][copied] = '\0';
        ++count;
        cursor += length;
    }
    return count;
}

int word_value(const char* word) {
    static constexpr struct { const char* word; int value; } TABLE[] = {
        {"zero", 0}, {"one", 1}, {"two", 2}, {"three", 3}, {"four", 4}, {"five", 5}, {"six", 6},
        {"seven", 7}, {"eight", 8}, {"nine", 9}, {"ten", 10}, {"eleven", 11}, {"twelve", 12},
        {"thirteen", 13}, {"fourteen", 14}, {"fifteen", 15}, {"sixteen", 16}, {"seventeen", 17},
        {"eighteen", 18}, {"nineteen", 19}, {"twenty", 20}, {"thirty", 30}, {"forty", 40},
        {"fifty", 50}, {"sixty", 60}, {"seventy", 70}, {"eighty", 80}, {"ninety", 90},
        {"hundred", 100},
    };
    for (const auto& entry : TABLE) {
        if (std::strcmp(entry.word, word) == 0) return entry.value;
    }
    return -1;
}

bool parse_number(const char* normalized, int& value) {
    char tokens[24][20];
    const size_t count = tokenize(normalized, tokens, 24);
    bool found = false;
    int total = 0;
    for (size_t index = 0; index < count; ++index) {
        const char* token = tokens[index];
        if (std::isdigit(static_cast<unsigned char>(token[0]))) {
            value = std::atoi(token);
            return true;
        }
        const int spoken = word_value(token);
        if (spoken < 0) {
            if (found) break;
            continue;
        }
        if (spoken == 100) {
            total = 100;
        } else if (found && total >= 20 && total % 10 == 0 && spoken < 10) {
            total += spoken;
        } else {
            total = spoken;
        }
        found = true;
    }
    if (found) value = total;
    return found;
}

bool wants_stop(const char* normalized) {
    return has_any(normalized, {"stop", "quit", "exit", "cancel", "enough", "end game", "end the game",
        "stop playing", "im done", "i am done", "main menu", "go home", "no more"});
}

bool wants_restart(const char* normalized) {
    return has_any(normalized, {"again", "restart", "new game", "another", "replay", "rematch", "yes",
        "sure", "yeah", "okay", "ok", "play"});
}

void show(DeviceDisplay::GameView& view) {
    const esp_err_t result = DeviceDisplay::set_game_view(view);
    if (result != ESP_OK) ESP_LOGW(TAG, "Cannot render game view: %s", esp_err_to_name(result));
}

void wrap_rows(DeviceDisplay::GameView& view, const char* text, size_t width) {
    const char* cursor = text;
    while (*cursor != '\0' && view.row_count < 3) {
        while (*cursor == ' ') ++cursor;
        size_t length = std::strlen(cursor);
        if (length > width) {
            length = width;
            while (length > 0 && cursor[length] != ' ') --length;
            if (length == 0) length = width;
        }
        std::snprintf(view.rows[view.row_count], sizeof(view.rows[0]), "%.*s", static_cast<int>(length), cursor);
        ++view.row_count;
        cursor += length;
    }
}

// ---------- Tic tac toe ----------

constexpr int LINES[8][3] = {
    {0, 1, 2}, {3, 4, 5}, {6, 7, 8}, {0, 3, 6}, {1, 4, 7}, {2, 5, 8}, {0, 4, 8}, {2, 4, 6},
};

char ttt_winner(const char* cells) {
    for (const auto& line : LINES) {
        if (cells[line[0]] != ' ' && cells[line[0]] == cells[line[1]] && cells[line[1]] == cells[line[2]]) {
            return cells[line[0]];
        }
    }
    return ' ';
}

bool ttt_full(const char* cells) {
    for (int index = 0; index < 9; ++index) {
        if (cells[index] == ' ') return false;
    }
    return true;
}

int minimax(char* cells, bool ai_turn, int depth) {
    const char winner = ttt_winner(cells);
    if (winner == 'O') return 10 - depth;
    if (winner == 'X') return depth - 10;
    if (ttt_full(cells)) return 0;
    int best = ai_turn ? -100 : 100;
    for (int index = 0; index < 9; ++index) {
        if (cells[index] != ' ') continue;
        cells[index] = ai_turn ? 'O' : 'X';
        const int score = minimax(cells, !ai_turn, depth + 1);
        cells[index] = ' ';
        best = ai_turn ? std::max(best, score) : std::min(best, score);
    }
    return best;
}

int ttt_ai_move() {
    int empty[9];
    int count = 0;
    for (int index = 0; index < 9; ++index) {
        if (board[index] == ' ') empty[count++] = index;
    }
    if (count == 0) return -1;
    // A share of moves is random so the opponent can be beaten; the share adapts to the player.
    if (static_cast<int>(random_below(100)) < ai_random_percent) return empty[random_below(static_cast<uint32_t>(count))];
    int best_move = empty[0];
    int best_score = -100;
    for (int index = 0; index < count; ++index) {
        board[empty[index]] = 'O';
        const int score = minimax(board, false, 1);
        board[empty[index]] = ' ';
        if (score > best_score) {
            best_score = score;
            best_move = empty[index];
        }
    }
    return best_move;
}

int ttt_moves() {
    int moves = 0;
    for (char cell : board) moves += cell == 'X' ? 1 : 0;
    return moves;
}

void ttt_render() {
    DeviceDisplay::GameView view;
    view.layout = DeviceDisplay::GameLayout::TicTacToe;
    std::snprintf(view.title, sizeof(view.title), "TIC TAC TOE");
    std::memcpy(view.board, board, sizeof(view.board));
    view.show_scores = true;
    view.score_you = ttt_you;
    view.score_ai = ttt_ai;
    if (finished) {
        std::snprintf(view.status, sizeof(view.status), "%s", ttt_result);
        std::snprintf(view.line1, sizeof(view.line1), "SAY AGAIN OR STOP");
    } else {
        std::snprintf(view.status, sizeof(view.status), "YOUR TURN X");
        std::snprintf(view.line1, sizeof(view.line1), "SAY SQUARE 1 TO 9");
        std::snprintf(view.line2, sizeof(view.line2), "OR TOP LEFT, CENTER");
    }
    show(view);
}

void ttt_start(DeviceGames::Reply& reply) {
    std::memset(board, ' ', sizeof(board));
    finished = false;
    ttt_result[0] = '\0';
    ttt_render();
    say(reply, "Tic tac toe. You are X and I am O. Say a square from one to nine, or say top left, center, bottom right and so on.");
    reply.sound = "success";
}

bool ttt_parse_square(const char* normalized, int& square) {
    struct Entry { const char* phrase; int square; };
    static constexpr Entry PHRASES[] = {
        {"top left", 0}, {"upper left", 0}, {"top middle", 1}, {"top center", 1}, {"top centre", 1},
        {"upper middle", 1}, {"top right", 2}, {"upper right", 2}, {"middle left", 3}, {"center left", 3},
        {"centre left", 3}, {"middle right", 5}, {"center right", 5}, {"centre right", 5},
        {"bottom left", 6}, {"lower left", 6}, {"bottom middle", 7}, {"bottom center", 7},
        {"bottom centre", 7}, {"lower middle", 7}, {"bottom right", 8}, {"lower right", 8},
        {"center", 4}, {"centre", 4}, {"middle", 4},
    };
    for (const auto& entry : PHRASES) {
        if (has(normalized, entry.phrase)) {
            square = entry.square;
            return true;
        }
    }
    int number = 0;
    if (parse_number(normalized, number) && number >= 1 && number <= 9) {
        square = number - 1;
        return true;
    }
    return false;
}

void ttt_input(const char* normalized, DeviceGames::Reply& reply) {
    int square = -1;
    if (finished) {
        if (wants_restart(normalized)) ttt_start(reply);
        else reply.handled = false;
        return;
    }
    if (!ttt_parse_square(normalized, square)) {
        reply.handled = false;
        return;
    }
    if (board[square] != ' ') {
        say(reply, "Square %d is already taken. Pick another square.", square + 1);
        reply.sound = "error";
        ttt_render();
        return;
    }
    board[square] = 'X';
    if (ttt_winner(board) == 'X') {
        ++ttt_you;
        finished = true;
        std::snprintf(ttt_result, sizeof(ttt_result), "YOU WIN");
        conclude("win", 1, 1, ttt_moves());
        ttt_render();
        say(reply, "You took square %d and won! Nicely played. Say play again, or stop.", square + 1);
        reply.sound = "success";
        return;
    }
    if (ttt_full(board)) {
        finished = true;
        std::snprintf(ttt_result, sizeof(ttt_result), "DRAW");
        conclude("draw", 0, 1, ttt_moves());
        ttt_render();
        say(reply, "It is a draw. Say play again, or stop.");
        reply.sound = "move";
        return;
    }
    const int ai_square = ttt_ai_move();
    board[ai_square] = 'O';
    if (ttt_winner(board) == 'O') {
        ++ttt_ai;
        finished = true;
        std::snprintf(ttt_result, sizeof(ttt_result), "BUDDY WINS");
        conclude("loss", 0, 1, ttt_moves());
        ttt_render();
        say(reply, "I took square %d and I win this round. Say play again, or stop.", ai_square + 1);
        reply.sound = "error";
        return;
    }
    if (ttt_full(board)) {
        finished = true;
        std::snprintf(ttt_result, sizeof(ttt_result), "DRAW");
        conclude("draw", 0, 1, ttt_moves());
        ttt_render();
        say(reply, "I took square %d. It is a draw. Say play again, or stop.", ai_square + 1);
        reply.sound = "move";
        return;
    }
    ttt_render();
    say(reply, "You took square %d. I took square %d. Your turn.", square + 1, ai_square + 1);
    reply.sound = "move";
}

// ---------- Rock paper scissors ----------

void rps_render() {
    DeviceDisplay::GameView view;
    view.layout = DeviceDisplay::GameLayout::Text;
    std::snprintf(view.title, sizeof(view.title), "ROCK PAPER SCISSORS");
    std::snprintf(view.line1, sizeof(view.line1), "YOU %u", rps_you);
    std::snprintf(view.line2, sizeof(view.line2), "BUDDY %u", rps_ai);
    std::snprintf(view.status, sizeof(view.status), "SAY ROCK PAPER SCISSORS");
    std::snprintf(view.line3, sizeof(view.line3), "%s", rps_last[0] == '\0' ? "DRAWS 0" : rps_last);
    show(view);
}

void rps_start(DeviceGames::Reply& reply) {
    rps_you = rps_ai = rps_draw = 0;
    rps_last[0] = '\0';
    finished = false;
    rps_render();
    say(reply, "Rock paper scissors. Say rock, paper, or scissors.");
    reply.sound = "success";
}

void rps_input(const char* normalized, DeviceGames::Reply& reply) {
    const char* choices[] = {"rock", "paper", "scissors"};
    int mine = -1;
    if (has(normalized, "rock")) mine = 0;
    else if (has(normalized, "paper")) mine = 1;
    else if (has_any(normalized, {"scissors", "scissor"})) mine = 2;
    if (mine < 0) {
        reply.handled = false;
        return;
    }
    const int theirs = static_cast<int>(random_below(3));
    const char* outcome;
    if (mine == theirs) {
        ++rps_draw;
        outcome = "That round is a draw.";
        reply.sound = "move";
    } else if ((mine + 1) % 3 == theirs) {
        ++rps_ai;
        outcome = "I win this round.";
        reply.sound = "error";
    } else {
        ++rps_you;
        outcome = "You win this round.";
        reply.sound = "success";
    }
    std::snprintf(rps_last, sizeof(rps_last), "%s V %s", choices[mine], choices[theirs]);
    for (char* cursor = rps_last; *cursor != '\0'; ++cursor) *cursor = static_cast<char>(std::toupper(static_cast<unsigned char>(*cursor)));
    rps_render();
    say(reply, "You chose %s. I chose %s. %s Score is you %u, me %u. Go again.", choices[mine], choices[theirs],
        outcome, rps_you, rps_ai);
}

// ---------- Number guessing ----------

void number_render() {
    DeviceDisplay::GameView view;
    view.layout = DeviceDisplay::GameLayout::Text;
    std::snprintf(view.title, sizeof(view.title), "NUMBER GUESSING");
    std::snprintf(view.line1, sizeof(view.line1), "TRIES %d", guesses);
    std::snprintf(view.line2, sizeof(view.line2), "%s", number_hint[0] == '\0' ? "1 TO 100" : number_hint);
    std::snprintf(view.status, sizeof(view.status), "%s", finished ? "YOU GOT IT" : "SAY A NUMBER 1 TO 100");
    if (finished) std::snprintf(view.line3, sizeof(view.line3), "SAY AGAIN OR STOP");
    show(view);
}

void number_start(DeviceGames::Reply& reply) {
    secret = 1 + static_cast<int>(random_below(100));
    guesses = 0;
    number_hint[0] = '\0';
    finished = false;
    number_render();
    say(reply, "I am thinking of a number from one to one hundred. What is your guess?");
    reply.sound = "success";
}

void number_input(const char* normalized, DeviceGames::Reply& reply) {
    if (finished) {
        if (wants_restart(normalized)) number_start(reply);
        else reply.handled = false;
        return;
    }
    int guess = 0;
    if (!parse_number(normalized, guess)) {
        reply.handled = false;
        return;
    }
    if (guess < 1 || guess > 100) {
        say(reply, "Pick a number between one and one hundred.");
        reply.sound = "error";
        return;
    }
    ++guesses;
    if (guess == secret) {
        finished = true;
        std::snprintf(number_hint, sizeof(number_hint), "IT WAS %d", secret);
        conclude("win", guesses, 0, guesses);
        number_render();
        say(reply, "Correct! The number was %d. You got it in %d tries. Say play again, or stop.", secret, guesses);
        reply.sound = "success";
        return;
    }
    const bool higher = guess < secret;
    std::snprintf(number_hint, sizeof(number_hint), "%s", higher ? "GO HIGHER" : "GO LOWER");
    number_render();
    say(reply, "%d is too %s. Try %s.", guess, higher ? "low" : "high", higher ? "higher" : "lower");
    reply.sound = "move";
}

// ---------- Trivia ----------

void trivia_render(const char* status) {
    DeviceDisplay::GameView view;
    view.layout = DeviceDisplay::GameLayout::Quiz;
    std::snprintf(view.title, sizeof(view.title), "TRIVIA %c/%c", static_cast<char>('0' + std::min(trivia_index + 1, TRIVIA_ROUNDS)), '0' + TRIVIA_ROUNDS);
    if (finished) {
        std::snprintf(view.rows[0], sizeof(view.rows[0]), "SCORE %d OF %d", trivia_score, TRIVIA_ROUNDS);
        std::snprintf(view.rows[1], sizeof(view.rows[1]), "SAY AGAIN OR STOP");
        view.row_count = 2;
    } else {
        const Question& question = QUESTIONS[trivia_order[trivia_index]];
        wrap_rows(view, question.text, 28);
        for (int choice = 0; choice < 4; ++choice) {
            std::snprintf(view.rows[view.row_count], sizeof(view.rows[0]), "%c %s", 'A' + choice, question.choices[choice]);
            ++view.row_count;
        }
        std::snprintf(view.line1, sizeof(view.line1), "%s", question.text);
    }
    std::snprintf(view.status, sizeof(view.status), "%s", status);
    view.show_scores = false;
    show(view);
}

void trivia_speak_question(DeviceGames::Reply& reply, const char* prefix) {
    const Question& question = QUESTIONS[trivia_order[trivia_index]];
    say(reply, "%s Question %d. %s A, %s. B, %s. C, %s. D, %s.", prefix, trivia_index + 1, question.text,
        question.choices[0], question.choices[1], question.choices[2], question.choices[3]);
}

void trivia_start(DeviceGames::Reply& reply) {
    size_t pool[QUESTION_COUNT];
    for (size_t index = 0; index < QUESTION_COUNT; ++index) pool[index] = index;
    for (size_t index = QUESTION_COUNT - 1; index > 0; --index) {
        std::swap(pool[index], pool[random_below(static_cast<uint32_t>(index + 1))]);
    }
    for (int index = 0; index < TRIVIA_ROUNDS; ++index) trivia_order[index] = pool[index];
    trivia_index = 0;
    trivia_score = 0;
    finished = false;
    trivia_render("SAY A B C OR D");
    trivia_speak_question(reply, "Trivia, five questions.");
    reply.sound = "success";
}

int trivia_parse_choice(const char* normalized, const Question& question) {
    char tokens[24][20];
    const size_t count = tokenize(normalized, tokens, 24);
    auto letter_index = [](const char* token) -> int {
        if (std::strcmp(token, "a") == 0) return 0;
        if (std::strcmp(token, "b") == 0 || std::strcmp(token, "bee") == 0) return 1;
        if (std::strcmp(token, "c") == 0 || std::strcmp(token, "see") == 0 || std::strcmp(token, "sea") == 0) return 2;
        if (std::strcmp(token, "d") == 0 || std::strcmp(token, "dee") == 0) return 3;
        return -1;
    };
    for (size_t index = 0; index + 1 < count; ++index) {
        if (std::strcmp(tokens[index], "option") == 0 || std::strcmp(tokens[index], "answer") == 0 ||
            std::strcmp(tokens[index], "letter") == 0 || std::strcmp(tokens[index], "choice") == 0) {
            const int choice = letter_index(tokens[index + 1]);
            if (choice >= 0) return choice;
        }
    }
    if (count == 1) {
        const int choice = letter_index(tokens[0]);
        if (choice >= 0) return choice;
    }
    static constexpr const char* ORDINALS[] = {"first", "second", "third", "fourth"};
    for (int choice = 0; choice < 4; ++choice) {
        if (has(normalized, ORDINALS[choice])) return choice;
    }
    for (int choice = 0; choice < 4; ++choice) {
        char spoken[40];
        normalize(question.choices[choice], spoken, sizeof(spoken));
        // normalize() pads with spaces; has() adds its own, so trim before matching.
        char* begin = spoken + 1;
        const size_t length = std::strlen(begin);
        if (length > 0 && begin[length - 1] == ' ') begin[length - 1] = '\0';
        if (has(normalized, begin)) return choice;
    }
    return -1;
}

void trivia_input(const char* normalized, DeviceGames::Reply& reply) {
    if (finished) {
        if (wants_restart(normalized)) trivia_start(reply);
        else reply.handled = false;
        return;
    }
    const Question& question = QUESTIONS[trivia_order[trivia_index]];
    const int choice = trivia_parse_choice(normalized, question);
    if (choice < 0) {
        reply.handled = false;
        return;
    }
    const bool correct = ('A' + choice) == question.answer;
    if (correct) ++trivia_score;
    ++trivia_index;
    const char* verdict = correct ? "Correct!" : "Not quite.";
    reply.sound = correct ? "success" : "error";
    if (trivia_index >= TRIVIA_ROUNDS) {
        finished = true;
        conclude(trivia_score >= 3 ? "win" : "loss", trivia_score, TRIVIA_ROUNDS, TRIVIA_ROUNDS);
        trivia_render("GAME COMPLETE");
        say(reply, "%s Trivia complete. You scored %d out of %d. Say play again, or stop.", verdict, trivia_score,
            TRIVIA_ROUNDS);
        return;
    }
    trivia_render(correct ? "CORRECT" : "NOT QUITE");
    char prefix[32];
    std::snprintf(prefix, sizeof(prefix), "%s", verdict);
    trivia_speak_question(reply, prefix);
}

// ---------- Hangman ----------

void hangman_render() {
    DeviceDisplay::GameView view;
    view.layout = DeviceDisplay::GameLayout::Hangman;
    std::snprintf(view.title, sizeof(view.title), "HANGMAN");
    size_t cursor = 0;
    for (const char* letter = hangman_word; *letter != '\0' && cursor + 3 < sizeof(view.masked); ++letter) {
        view.masked[cursor++] = (finished || hangman_guessed[*letter - 'a']) ? static_cast<char>(std::toupper(*letter)) : '_';
        view.masked[cursor++] = ' ';
    }
    if (cursor > 0) view.masked[cursor - 1] = '\0';
    view.mistakes = static_cast<uint8_t>(hangman_mistakes);
    view.max_mistakes = HANGMAN_MAX;
    char wrong[20] = "";
    size_t wrong_length = 0;
    for (int letter = 0; letter < 26 && wrong_length + 2 < sizeof(wrong); ++letter) {
        if (hangman_guessed[letter] && std::strchr(hangman_word, 'a' + letter) == nullptr) {
            wrong[wrong_length++] = static_cast<char>('A' + letter);
            wrong[wrong_length] = '\0';
        }
    }
    if (finished) {
        std::snprintf(view.status, sizeof(view.status), "%s", hangman_mistakes >= HANGMAN_MAX ? "BUDDY WINS" : "YOU WIN");
        std::snprintf(view.line1, sizeof(view.line1), "SAY AGAIN OR STOP");
    } else {
        std::snprintf(view.status, sizeof(view.status), "GUESS A LETTER");
        std::snprintf(view.line1, sizeof(view.line1), "MISSED %s", wrong);
    }
    std::snprintf(view.line2, sizeof(view.line2), "MISTAKES %d/%d", hangman_mistakes, HANGMAN_MAX);
    show(view);
}

bool hangman_complete() {
    for (const char* letter = hangman_word; *letter != '\0'; ++letter) {
        if (!hangman_guessed[*letter - 'a']) return false;
    }
    return true;
}

void hangman_start(DeviceGames::Reply& reply) {
    hangman_word = HANGMAN_WORDS[random_below(HANGMAN_WORD_COUNT)];
    std::memset(hangman_guessed, 0, sizeof(hangman_guessed));
    hangman_mistakes = 0;
    finished = false;
    hangman_render();
    say(reply, "Hangman. I picked a word with %u letters. Guess a letter.", static_cast<unsigned>(std::strlen(hangman_word)));
    reply.sound = "success";
}

void hangman_input(const char* normalized, DeviceGames::Reply& reply) {
    if (finished) {
        if (wants_restart(normalized)) hangman_start(reply);
        else reply.handled = false;
        return;
    }
    char tokens[24][20];
    const size_t count = tokenize(normalized, tokens, 24);
    char letter = '\0';
    for (size_t index = 0; index + 1 < count; ++index) {
        if (std::strcmp(tokens[index], "letter") == 0 || std::strcmp(tokens[index], "guess") == 0 ||
            std::strcmp(tokens[index], "try") == 0) {
            if (std::strlen(tokens[index + 1]) == 1 && std::isalpha(static_cast<unsigned char>(tokens[index + 1][0]))) {
                letter = tokens[index + 1][0];
                break;
            }
        }
    }
    if (letter == '\0' && count == 1 && std::strlen(tokens[0]) == 1 &&
        std::isalpha(static_cast<unsigned char>(tokens[0][0]))) {
        letter = tokens[0][0];
    }
    if (letter == '\0' && count == 1 && std::strlen(tokens[0]) >= 3) {
        if (std::strcmp(tokens[0], hangman_word) == 0) {
            for (const char* cursor = hangman_word; *cursor != '\0'; ++cursor) hangman_guessed[*cursor - 'a'] = true;
            finished = true;
            conclude("win", HANGMAN_MAX - hangman_mistakes, HANGMAN_MAX, hangman_mistakes);
            hangman_render();
            say(reply, "Yes! The word was %s. You win! Say play again, or stop.", hangman_word);
            reply.sound = "success";
            return;
        }
        if (std::strlen(tokens[0]) == std::strlen(hangman_word)) {
            ++hangman_mistakes;
            reply.sound = "error";
            if (hangman_mistakes >= HANGMAN_MAX) {
                finished = true;
                conclude("loss", 0, HANGMAN_MAX, hangman_mistakes);
                hangman_render();
                say(reply, "Not %s. Game over, the word was %s. Say play again, or stop.", tokens[0], hangman_word);
            } else {
                hangman_render();
                say(reply, "The word is not %s. %d of %d misses.", tokens[0], hangman_mistakes, HANGMAN_MAX);
            }
            return;
        }
    }
    if (letter == '\0') {
        reply.handled = false;
        return;
    }
    letter = static_cast<char>(std::tolower(static_cast<unsigned char>(letter)));
    if (hangman_guessed[letter - 'a']) {
        say(reply, "You already guessed %c. Try another letter.", letter);
        reply.sound = "error";
        return;
    }
    hangman_guessed[letter - 'a'] = true;
    if (std::strchr(hangman_word, letter) != nullptr) {
        if (hangman_complete()) {
            finished = true;
            conclude("win", HANGMAN_MAX - hangman_mistakes, HANGMAN_MAX, hangman_mistakes);
            hangman_render();
            say(reply, "Yes, %c! The word was %s. You win! Say play again, or stop.", letter, hangman_word);
            reply.sound = "success";
            return;
        }
        hangman_render();
        say(reply, "Yes, %c is in the word. Guess another letter.", letter);
        reply.sound = "success";
        return;
    }
    ++hangman_mistakes;
    reply.sound = "error";
    if (hangman_mistakes >= HANGMAN_MAX) {
        finished = true;
        conclude("loss", 0, HANGMAN_MAX, hangman_mistakes);
        hangman_render();
        say(reply, "No %c. That is %d misses. Game over, the word was %s. Say play again, or stop.", letter,
            hangman_mistakes, hangman_word);
        return;
    }
    hangman_render();
    say(reply, "No, %c is not in the word. %d of %d misses.", letter, hangman_mistakes, HANGMAN_MAX);
}

// ---------- Word association ----------

void word_render() {
    DeviceDisplay::GameView view;
    view.layout = DeviceDisplay::GameLayout::Text;
    std::snprintf(view.title, sizeof(view.title), "WORD ASSOCIATION");
    std::snprintf(view.line1, sizeof(view.line1), "%s", TOPICS[topic_index].word);
    for (char* cursor = view.line1; *cursor != '\0'; ++cursor) *cursor = static_cast<char>(std::toupper(static_cast<unsigned char>(*cursor)));
    std::snprintf(view.line2, sizeof(view.line2), "HITS %d/%d", word_hits, WORD_TARGET);
    if (finished) {
        std::snprintf(view.status, sizeof(view.status), "%s", word_hits >= WORD_TARGET ? "YOU WIN" : "ROUND OVER");
        std::snprintf(view.line3, sizeof(view.line3), "SAY AGAIN OR STOP");
    } else {
        std::snprintf(view.status, sizeof(view.status), "TURN %d OF %d", word_turns + 1, WORD_TURNS);
        std::snprintf(view.line3, sizeof(view.line3), "%s", word_last[0] == '\0' ? "SAY A RELATED WORD" : word_last);
    }
    show(view);
}

void word_start(DeviceGames::Reply& reply) {
    topic_index = random_below(TOPIC_COUNT);
    word_turns = 0;
    word_hits = 0;
    std::memset(word_used, 0, sizeof(word_used));
    word_last[0] = '\0';
    finished = false;
    word_render();
    say(reply, "Word association. My word is %s. Say a word related to it. You need %d out of %d.",
        TOPICS[topic_index].word, WORD_TARGET, WORD_TURNS);
    reply.sound = "success";
}

void word_input(const char* normalized, DeviceGames::Reply& reply) {
    if (finished) {
        if (wants_restart(normalized)) word_start(reply);
        else reply.handled = false;
        return;
    }
    char tokens[24][20];
    const size_t count = tokenize(normalized, tokens, 24);
    if (count == 0 || count > 5) {
        reply.handled = false;
        return;
    }
    const Topic& topic = TOPICS[topic_index];
    int matched = -1;
    for (size_t token = 0; token < count && matched < 0; ++token) {
        for (int related = 0; related < 12; ++related) {
            if (std::strcmp(tokens[token], topic.related[related]) == 0) {
                matched = related;
                break;
            }
        }
    }
    ++word_turns;
    std::snprintf(word_last, sizeof(word_last), "%s", tokens[count - 1]);
    for (char* cursor = word_last; *cursor != '\0'; ++cursor) *cursor = static_cast<char>(std::toupper(static_cast<unsigned char>(*cursor)));
    bool counted = false;
    if (matched >= 0 && !word_used[matched]) {
        word_used[matched] = true;
        ++word_hits;
        counted = true;
    }
    if (word_hits >= WORD_TARGET || word_turns >= WORD_TURNS) {
        finished = true;
        conclude(word_hits >= WORD_TARGET ? "win" : "loss", word_hits, WORD_TARGET, word_turns);
        word_render();
        if (word_hits >= WORD_TARGET) {
            say(reply, "%s fits! You got %d related words. You win! Say play again, or stop.", tokens[count - 1], word_hits);
            reply.sound = "success";
        } else {
            say(reply, "Round over. You found %d of %d related words for %s. Say play again, or stop.", word_hits,
                WORD_TARGET, topic.word);
            reply.sound = "error";
        }
        return;
    }
    word_render();
    if (counted) {
        say(reply, "Yes, %s goes with %s. %d of %d. Another word?", tokens[count - 1], topic.word, word_hits, WORD_TARGET);
        reply.sound = "success";
    } else if (matched >= 0) {
        say(reply, "You already used that one. Try a different word for %s.", topic.word);
        reply.sound = "error";
    } else {
        say(reply, "Hmm, %s does not fit %s. Try another word.", tokens[count - 1], topic.word);
        reply.sound = "error";
    }
}

void conclude_abandoned() {
    switch (current) {
        case Game::Rps: {
            const int rounds = rps_you + rps_ai + rps_draw;
            if (rounds == 0) conclude("abandoned", 0, 0, 0);
            else conclude(rps_you > rps_ai ? "win" : (rps_you < rps_ai ? "loss" : "draw"), rps_you, rounds, rounds);
            break;
        }
        case Game::TicTacToe: conclude("abandoned", 0, 1, ttt_moves()); break;
        case Game::Number: conclude("abandoned", 0, 0, guesses); break;
        case Game::Trivia: conclude("abandoned", trivia_score, TRIVIA_ROUNDS, trivia_index); break;
        case Game::Hangman: conclude("abandoned", 0, HANGMAN_MAX, hangman_mistakes); break;
        case Game::Word: conclude("abandoned", word_hits, WORD_TARGET, word_turns); break;
        case Game::None: break;
    }
}

Game game_from_name(const char* name) {
    if (name == nullptr || name[0] == '\0' || std::strcmp(name, "random") == 0) {
        return GAMES[random_below(GAME_COUNT)].id;
    }
    char normalized[48];
    normalize(name, normalized, sizeof(normalized));
    for (const auto& entry : GAMES) {
        char key[32];
        std::snprintf(key, sizeof(key), "%s", entry.key);
        for (char* cursor = key; *cursor != '\0'; ++cursor) {
            if (*cursor == '_') *cursor = ' ';
        }
        if (has(normalized, key) || has(normalized, entry.spoken)) return entry.id;
    }
    return Game::None;
}

}  // namespace

namespace DeviceGames {

bool active() {
    return current != Game::None;
}

void list(Reply& reply) {
    reset_reply(reply);
    reply.handled = true;
    DeviceDisplay::clear_game_view();
    const char* items[] = {"TIC TAC TOE  HANGMAN", "ROCK PAPER SCISSORS", "NUMBER GUESSING TRIVIA", "WORD ASSOCIATION"};
    DeviceDisplay::set_dashboard("games", "GAMES", "SAY PLAY AND A NAME", items, 4, 0, 0, 0, 0, 0);
    say(reply,
        "I have six games on my own processor: tic tac toe, rock paper scissors, number guessing, trivia, hangman, "
        "and word association. Say play and a game name, or say pick a game and I will choose one.");
    reply.sound = "notification";
    finish_reply(reply);
}

void start(const char* game, const char* difficulty, Reply& reply) {
    reset_reply(reply);
    reply.handled = true;
    const Game selected = game_from_name(game);
    if (selected == Game::None) {
        say(reply, "I do not have that game. Say which games do we have to hear the list.");
        reply.sound = "error";
        finish_reply(reply);
        return;
    }
    if (current != Game::None && !finished) conclude_abandoned();
    current = selected;
    game_started_us = esp_timer_get_time();
    std::snprintf(current_difficulty, sizeof(current_difficulty), "%s",
        (difficulty != nullptr && (std::strcmp(difficulty, "easy") == 0 || std::strcmp(difficulty, "hard") == 0))
            ? difficulty : "normal");
    ai_random_percent = std::strcmp(current_difficulty, "easy") == 0 ? 50 : (std::strcmp(current_difficulty, "hard") == 0 ? 0 : 25);
    switch (selected) {
        case Game::TicTacToe: ttt_you = ttt_ai = 0; ttt_start(reply); break;
        case Game::Rps: rps_start(reply); break;
        case Game::Number: number_start(reply); break;
        case Game::Trivia: trivia_start(reply); break;
        case Game::Hangman: hangman_start(reply); break;
        case Game::Word: word_start(reply); break;
        case Game::None: break;
    }
    ESP_LOGI(TAG, "Started %s on device", info_for(selected)->key);
    finish_reply(reply);
}

void stop(Reply& reply) {
    reset_reply(reply);
    reply.handled = true;
    if (current == Game::None) {
        say(reply, "No game is running right now.");
    } else {
        say(reply, "Okay, game over. Say play a game whenever you want another.");
        reply.sound = "notification";
        if (!finished) conclude_abandoned();
    }
    finish_reply(reply);
    current = Game::None;
    finished = false;
    DeviceDisplay::clear_game_view();
    DeviceDisplay::set_state("idle");
    reply.active = false;
    reply.finished = false;
    std::snprintf(reply.status, sizeof(reply.status), "idle");
}

void input(const char* text, Reply& reply) {
    reset_reply(reply);
    reply.handled = true;
    if (current == Game::None) {
        reply.handled = false;
        finish_reply(reply);
        return;
    }
    char normalized[160];
    normalize(text, normalized, sizeof(normalized));
    if (wants_stop(normalized)) {
        stop(reply);
        return;
    }
    switch (current) {
        case Game::TicTacToe: ttt_input(normalized, reply); break;
        case Game::Rps: rps_input(normalized, reply); break;
        case Game::Number: number_input(normalized, reply); break;
        case Game::Trivia: trivia_input(normalized, reply); break;
        case Game::Hangman: hangman_input(normalized, reply); break;
        case Game::Word: word_input(normalized, reply); break;
        case Game::None: reply.handled = false; break;
    }
    finish_reply(reply);
}

size_t unsynced(LogEntry* out, size_t maximum) {
    log_load();
    size_t written = 0;
    for (size_t index = 0; index < game_log.count && written < maximum; ++index) {
        const LogRecord& record = game_log.records[index];
        if (record.flags & FLAG_SYNCED) continue;
        LogEntry& entry = out[written++];
        entry.id = record.id;
        entry.timestamp = record.timestamp;
        std::snprintf(entry.game, sizeof(entry.game), "%s", GAMES[record.game < GAME_COUNT ? record.game : 0].key);
        std::snprintf(entry.outcome, sizeof(entry.outcome), "%s", OUTCOME_NAMES[record.flags & 0x3]);
        std::snprintf(entry.difficulty, sizeof(entry.difficulty), "%s", DIFFICULTY_NAMES[((record.flags >> 2) & 0x3) % 3]);
        entry.score = record.score;
        entry.max_score = record.max_score;
        entry.rounds = record.rounds;
        entry.duration_seconds = record.duration;
    }
    return written;
}

void mark_synced(unsigned id) {
    log_load();
    for (size_t index = 0; index < game_log.count; ++index) {
        if (game_log.records[index].id == id) {
            game_log.records[index].flags |= FLAG_SYNCED;
            log_save();
            return;
        }
    }
}

unsigned pending_count() {
    log_load();
    unsigned total = 0;
    for (size_t index = 0; index < game_log.count; ++index) total += (game_log.records[index].flags & FLAG_SYNCED) ? 0 : 1;
    return total;
}

}  // namespace DeviceGames
