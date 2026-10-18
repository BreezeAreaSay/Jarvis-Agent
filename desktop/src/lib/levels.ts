// Громкость микрофона и речи Jarvis — часто меняются, поэтому живут вне React: сфера читает их каждый кадр.
import { onEvent } from "@/api/events";
import type { VoiceState } from "@/api/types";

export const levels = {
  mic: 0,
  tts: 0,
  state: "off" as VoiceState,
  wakeAt: 0, // время последнего «Джарвис» — вспышка сферы
  speaking: false,
};

let bound = false;

export function bindLevels() {
  if (bound) return;
  bound = true;
  onEvent<{ level: number }>("voice.level", (e) => {
    levels.mic = Math.max(0, Math.min(1, e.data.level ?? 0));
  });
  onEvent<{ level: number }>("tts.level", (e) => {
    levels.tts = Math.max(0, Math.min(1, e.data.level ?? 0));
  });
  onEvent<{ state: VoiceState }>("voice.state", (e) => {
    levels.state = e.data.state;
  });
  onEvent("voice.wake", () => {
    levels.wakeAt = performance.now();
  });
  onEvent<{ speaking: boolean }>("tts.state", (e) => {
    levels.speaking = !!e.data.speaking;
    if (!levels.speaking) levels.tts = 0;
  });
}
