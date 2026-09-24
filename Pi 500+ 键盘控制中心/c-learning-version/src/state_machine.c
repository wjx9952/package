#include "pi500.h"

LightingState effective_lighting_state(CodexState codex, bool keyboard_mode)
{
    switch (codex) {
    case CODEX_CONFIRMATION:
        return LIGHT_CONFIRMATION;
    case CODEX_PROCESSING:
        return LIGHT_PROCESSING;
    case CODEX_COMPLETED:
        return LIGHT_COMPLETED;
    case CODEX_IDLE:
    default:
        return keyboard_mode ? LIGHT_KEYBOARD_MODE : LIGHT_IDLE;
    }
}

const char *codex_state_name(CodexState state)
{
    switch (state) {
    case CODEX_PROCESSING:
        return "processing";
    case CODEX_COMPLETED:
        return "completed";
    case CODEX_CONFIRMATION:
        return "confirmation";
    case CODEX_IDLE:
    default:
        return "idle";
    }
}

