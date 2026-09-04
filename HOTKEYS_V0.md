# Scalpr Manual Hotkeys v0

These shortcuts operate the existing paper-trading dashboard controls. They add no order route and do not change the payload produced by `submitTrade()`.

## Single-key actions

Single-key shortcuts are disabled while focus is in an input, select, textarea, or editable element.

| Key | Existing action |
| --- | --- |
| `/` | Focus and select the underlying/symbol field. |
| `C` | Toggle Call/Put through the existing direction control. |
| `[` / `]` | Decrement/increment contract quantity. |
| `L` | Refresh the staged contract through `quoteContract()`. |
| `P` | Run `runPrecheck()`. |
| `E` | Toggle the existing exit-rules details. |
| `X` | Toggle the existing experimental-settings details. |
| `?` | Toggle the on-ticket hotkey legend. |

## Deliberate destructive chords

| Chord | Existing action |
| --- | --- |
| `Ctrl/Command + Enter` | Call the existing `submitTrade()` action. |
| `Alt + Shift + X` | Call the existing `liquidateAll()` action. |

A plain `Enter`, `X`, or any other unmodified single key cannot engage or flatten. Repeated keydown events are ignored. The visible quick-flatten button uses a separate arm-then-confirm interaction: first click arms it for five seconds; a second click within that window invokes the existing `liquidateAll()` function.

Quick contract presets set SPY plus Call/Put and invoke the existing chain loader, which selects the nearest available expiry and closest strike. Quantity presets set 1, 2, or 5 contracts through the existing quantity input event.
